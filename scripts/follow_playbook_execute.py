#!/usr/bin/env python3
"""Low-volume X follow executor for the active growth accounts.

The script is intentionally conservative:
- rank candidates from existing scout artifacts,
- enforce a daily JSON ledger before opening X,
- verify the live profile and Follow button over CDP,
- click at most a small number of follows.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
STATE_DIR = os.path.join(ROOT_DIR, "state")
sys.path.insert(0, SCRIPTS_DIR)

import chrome  # noqa: E402
import login  # noqa: E402
from lock import chrome_lock  # noqa: E402


LEDGER_PATH = os.path.join(STATE_DIR, "follow_ledger.json")
SHORTLIST_PATH = os.path.join(STATE_DIR, "follow_shortlist_{date}.json")

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")

ACCOUNTS: dict[str, dict[str, Any]] = {
    "flatkey": {
        "label": "Flatkey",
        "config": "accounts/flatkey/config.json",
        "public_handle": "mguozhen03",
        "self_handles": {"flatkey", "mguozhen03"},
        "min_followers": 80,
        "max_followers": 120_000,
        "topic_terms": [
            "agent", "agents", "ai", "llm", "model", "models", "claude",
            "cursor", "codex", "coding", "router", "routing", "context",
            "mcp", "workflow", "automation", "infra", "api", "token",
            "tokens", "developer", "devtool", "build",
        ],
        "strong_terms": [
            "ai agent", "coding agent", "claude code", "cursor", "codex",
            "model routing", "llm", "mcp", "developer tool", "devtool",
            "build in public",
        ],
    },
    "btcmind": {
        "label": "BTCMind",
        "config": "accounts/hunter_solvea/config.json",
        "public_handle": "btcmind101",
        "self_handles": {"hunter_solvea", "btcmind", "btcmind101"},
        "min_followers": 80,
        "max_followers": 150_000,
        "topic_terms": [
            "bitcoin", "btc", "crypto", "onchain", "on-chain", "wallet",
            "stablecoin", "stablecoins", "defi", "liquidity", "exchange",
            "custody", "security", "bridge", "tokenized", "treasury",
            "solana", "ethereum", "base", "analytics", "market structure",
        ],
        "strong_terms": [
            "bitcoin", "btc", "onchain", "on-chain", "stablecoin",
            "wallet", "crypto ux", "market structure", "defi", "custody",
            "tokenized",
        ],
    },
    "hunterguo101": {
        "label": "Hunter founder",
        "config": "accounts/hunterguo101/config.json",
        "public_handle": "hunterguo101",
        "self_handles": {"hunterguo101", "guohunter95258"},
        "min_followers": 80,
        "max_followers": 120_000,
        "topic_terms": [
            "founder", "builder", "startup", "saas", "ai", "agent",
            "agents", "automation", "gtm", "sales", "growth", "operator",
            "ecommerce", "customer", "support", "coding", "mcp", "ship",
            "build in public", "workflow",
        ],
        "strong_terms": [
            "founder", "building", "build in public", "ai agent",
            "automation", "gtm", "saas", "operator", "coding agent",
            "workflow",
        ],
    },
}

ACCOUNT_ORDER = ["flatkey", "btcmind", "hunterguo101"]

NEGATIVE_TERMS = [
    "airdrop", "giveaway", "whitelist", "presale", "100x", "gem call",
    "signals", "pump", "paid promo", "dm for promo", "onlyfans", "nsfw",
    "follow back", "i follow back", "engagement group", "ambassador",
    "raider", "raiding", "casino", "betting", "parlay",
]
NEGATIVE_RE = re.compile(r"\bamb\b|\bkol\b|\bshill\b", re.I)


@dataclass
class Candidate:
    account: str
    handle: str
    source: str
    profile_url: str = ""
    name: str = ""
    bio: str = ""
    text: str = ""
    query: str = ""
    followers: int = 0
    following: int = 0
    posts: int = 0
    joined: str = ""
    avg_likes: float = 0.0
    avg_replies: float = 0.0
    home_max_likes: int = 0
    sample_likes: int = 0
    source_score: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    reject_reason: str = ""


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def state_path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_int(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return 0
    mult = 1
    if s[-1:].lower() == "k":
        mult, s = 1_000, s[:-1]
    elif s[-1:].lower() == "m":
        mult, s = 1_000_000, s[:-1]
    elif s[-1:].lower() == "b":
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def compact(text: str, limit: int = 260) -> str:
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    return out if len(out) <= limit else out[: limit - 1] + "..."


def normalize_handle(raw: str) -> str:
    h = (raw or "").strip().lstrip("@")
    if "/" in h:
        h = h.split("/", 1)[0]
    return h


def combined_text(c: Candidate) -> str:
    return " ".join([c.name, c.bio, c.text, c.query]).lower()


def topic_hits(account: str, text: str) -> tuple[int, int, list[str]]:
    cfg = ACCOUNTS[account]
    hay = text.lower()
    weak = [t for t in cfg["topic_terms"] if t in hay]
    strong = [t for t in cfg["strong_terms"] if t in hay]
    hits = []
    for term in strong + weak:
        if term not in hits:
            hits.append(term)
    return len(weak), len(strong), hits[:8]


def negative_hits(text: str) -> list[str]:
    hay = text.lower()
    hits = [t for t in NEGATIVE_TERMS if t in hay]
    if NEGATIVE_RE.search(text):
        hits.append("amb/kol/shill")
    return hits


def follower_band_points(account: str, followers: int) -> tuple[float, str]:
    cfg = ACCOUNTS[account]
    if followers <= 0:
        return 0.0, "unknown followers"
    if followers < cfg["min_followers"]:
        return -35.0, f"below follower floor {followers}"
    if followers > cfg["max_followers"]:
        return -45.0, f"above follower cap {followers}"
    if followers <= 2_500:
        return 28.0, "small account likely to notice"
    if followers <= 15_000:
        return 22.0, "mid-small account"
    if followers <= 50_000:
        return 12.0, "larger but still reachable"
    return 4.0, "large account"


def score_candidate(c: Candidate) -> Candidate:
    if not HANDLE_RE.match(c.handle):
        c.reject_reason = "invalid handle"
        c.score = -999
        return c
    if c.handle.lower() in {h.lower() for h in ACCOUNTS[c.account]["self_handles"]}:
        c.reject_reason = "self handle"
        c.score = -999
        return c

    text = combined_text(c)
    weak, strong, hits = topic_hits(c.account, text)
    bad = negative_hits(text)
    if bad:
        c.reasons.append("negative:" + ",".join(bad[:4]))
    if c.followers and c.avg_likes and c.avg_likes > max(2_500, c.followers * 2):
        c.reject_reason = "suspicious engagement ratio"
        c.score = -999
        return c

    band, band_reason = follower_band_points(c.account, c.followers)
    c.reasons.append(band_reason)
    if hits:
        c.reasons.append("topic:" + ",".join(hits))

    engagement = min(24.0, (c.avg_likes * 0.08) + (c.avg_replies * 0.5))
    engagement += min(16.0, c.home_max_likes * 0.015)
    engagement += min(12.0, c.sample_likes * 0.03)

    recent = 0.0
    joined_l = c.joined.lower()
    if any(y in joined_l for y in ("2026", "2025", "2024")):
        recent = 10.0
        c.reasons.append("recent-ish account")

    source = min(25.0, c.source_score * 0.35) if c.source_score else 0.0
    topic = weak * 4.0 + strong * 9.0
    spam_penalty = 35.0 if bad else 0.0
    too_many_posts_penalty = 12.0 if c.posts and c.posts > 30_000 else 0.0

    c.score = round(band + topic + engagement + recent + source - spam_penalty - too_many_posts_penalty, 2)

    if weak + strong <= 0:
        c.reject_reason = "off-topic"
    elif c.followers and c.followers < ACCOUNTS[c.account]["min_followers"]:
        c.reject_reason = "too small to verify quality"
    elif c.followers and c.followers > ACCOUNTS[c.account]["max_followers"]:
        c.reject_reason = "too large for follow-back strategy"
    elif bad:
        c.reject_reason = "spam/growth-farm terms"
    elif c.posts and c.posts > 60_000:
        c.reject_reason = "extreme post count"
    elif c.score < 32:
        c.reject_reason = "score below threshold"
    return c


def add_candidate(bucket: dict[tuple[str, str], Candidate], c: Candidate) -> None:
    c.handle = normalize_handle(c.handle)
    if not c.profile_url:
        c.profile_url = f"https://x.com/{c.handle}" if c.handle else ""
    c = score_candidate(c)
    if not c.handle:
        return
    key = (c.account, c.handle.lower())
    prev = bucket.get(key)
    if prev is None or c.score > prev.score:
        bucket[key] = c


def latest_files(pattern: str) -> list[str]:
    paths = glob.glob(os.path.join(ROOT_DIR, pattern))
    return sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)


def load_fast_growers(bucket: dict[tuple[str, str], Candidate]) -> None:
    path = os.path.join(ROOT_DIR, "state", "fast_grower_accounts.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        account_fit = (r.get("account_fit") or "").strip()
        targets: list[str] = []
        if account_fit == "btcmind":
            targets = ["btcmind"]
        elif account_fit == "Hunter's X":
            targets = ["flatkey", "hunterguo101"]
        elif account_fit == "vocai":
            targets = ["hunterguo101"]
        for account in targets:
            add_candidate(
                bucket,
                Candidate(
                    account=account,
                    handle=r.get("handle", ""),
                    source="fast_grower_accounts.csv",
                    profile_url=r.get("profile_url", ""),
                    bio=r.get("bio", ""),
                    text=" ".join([r.get("clusters", ""), r.get("sample_post", "")]),
                    followers=parse_int(r.get("followers")),
                    joined=r.get("joined", ""),
                    avg_likes=float(r.get("avg_likes") or 0),
                    avg_replies=float(r.get("avg_replies") or 0),
                    source_score=float(r.get("score") or 0),
                ),
            )


def load_ai_founder_recent(bucket: dict[tuple[str, str], Candidate]) -> None:
    for path in latest_files("state/ai_founder_recent_qualified_candidates_*.json")[:3]:
        payload = read_json(path, {})
        rows = payload.get("qualified") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            continue
        for r in rows:
            base = {
                "handle": r.get("handle", ""),
                "profile_url": r.get("profile_url", ""),
                "name": r.get("name", ""),
                "bio": r.get("bio", ""),
                "text": " ".join([r.get("sample_text", ""), r.get("query", "")]),
                "query": r.get("query", ""),
                "followers": parse_int(r.get("followers")),
                "posts": parse_int(r.get("posts")),
                "joined": r.get("joined", ""),
                "sample_likes": parse_int(r.get("sample_likes")),
                "source_score": 40 if r.get("qualified") else 18,
            }
            body = " ".join([base["bio"], base["text"]]).lower()
            if topic_hits("hunterguo101", body)[0] + topic_hits("hunterguo101", body)[1]:
                add_candidate(bucket, Candidate(account="hunterguo101", source=os.path.basename(path), **base))
            if topic_hits("flatkey", body)[0] + topic_hits("flatkey", body)[1]:
                add_candidate(bucket, Candidate(account="flatkey", source=os.path.basename(path), **base))
            if topic_hits("btcmind", body)[0] + topic_hits("btcmind", body)[1]:
                add_candidate(bucket, Candidate(account="btcmind", source=os.path.basename(path), **base))


def load_flatkey_coldstart(bucket: dict[tuple[str, str], Candidate]) -> None:
    for path in latest_files("state/flatkey_home_coldstart_candidates_*.json")[:8]:
        rows = read_json(path, [])
        if not isinstance(rows, list):
            continue
        for r in rows:
            base = {
                "handle": r.get("handle", ""),
                "profile_url": r.get("profile_url", ""),
                "name": r.get("name", ""),
                "bio": r.get("bio", ""),
                "text": " ".join(r.get("home_texts") or []),
                "followers": parse_int(r.get("followers")),
                "following": parse_int(r.get("following")),
                "posts": parse_int(r.get("posts_count") or r.get("posts_raw")),
                "joined": r.get("joined", ""),
                "home_max_likes": parse_int(r.get("home_max_likes")),
                "source_score": 45 if r.get("qualifies_recent_fast") else 18,
            }
            body = " ".join([base["bio"], base["text"]]).lower()
            if topic_hits("flatkey", body)[0] + topic_hits("flatkey", body)[1]:
                add_candidate(bucket, Candidate(account="flatkey", source=os.path.basename(path), **base))
            if topic_hits("hunterguo101", body)[0] + topic_hits("hunterguo101", body)[1]:
                add_candidate(bucket, Candidate(account="hunterguo101", source=os.path.basename(path), **base))


def load_flatkey_reply_targets(bucket: dict[tuple[str, str], Candidate]) -> None:
    for name in ("flatkey_target_candidates_refined.json", "flatkey_target_candidates.json"):
        payload = read_json(os.path.join(ROOT_DIR, "state", name), {})
        rows = payload.get("candidates") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            continue
        for r in rows:
            add_candidate(
                bucket,
                Candidate(
                    account="flatkey",
                    handle=r.get("author", ""),
                    source=name,
                    profile_url=f"https://x.com/{r.get('author', '')}",
                    name=r.get("display_name", ""),
                    text=" ".join([r.get("text", ""), " ".join(r.get("labels") or []), r.get("source_query", "")]),
                    query=r.get("source_query", ""),
                    sample_likes=parse_int(r.get("likes")),
                    source_score=float(r.get("score") or 0),
                ),
            )


def load_candidates(accounts: list[str]) -> list[Candidate]:
    bucket: dict[tuple[str, str], Candidate] = {}
    load_fast_growers(bucket)
    load_ai_founder_recent(bucket)
    load_flatkey_coldstart(bucket)
    load_flatkey_reply_targets(bucket)

    rows = [c for c in bucket.values() if c.account in accounts and not c.reject_reason]
    rows.sort(key=lambda c: (c.score, c.followers, c.sample_likes, c.home_max_likes), reverse=True)
    return rows


def account_config(account: str) -> dict[str, Any]:
    cfg_path = os.path.join(ROOT_DIR, ACCOUNTS[account]["config"])
    cfg = read_json(cfg_path, {})
    if not cfg:
        raise RuntimeError(f"missing account config: {cfg_path}")
    return cfg


def ensure_chrome(account: str) -> int:
    cfg = account_config(account)
    port = int(cfg["chrome_port"])
    if chrome.ping(port):
        login.ensure_page_tab(port)
        return port
    profile = cfg.get("chrome_profile_dir") or os.path.join(ROOT_DIR, "chrome-profiles", cfg.get("handle") or account)
    login.launch_chrome(port, profile)
    login.ensure_page_tab(port)
    return port


PROFILE_CHECK_JS = r"""
(function() {
    var target = arguments[0] || '';
    target = String(target).replace(/^@/, '').toLowerCase();
    function text(el) {
        return (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
    }
    function visible(el) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        var s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    }
    function parseCount(s) {
        if (!s) return 0;
        s = String(s).replace(/,/g, '').trim();
        var mult = 1;
        if (/[Kk]$/.test(s)) { mult = 1000; s = s.slice(0, -1); }
        else if (/[Mm]$/.test(s)) { mult = 1000000; s = s.slice(0, -1); }
        else if (/[Bb]$/.test(s)) { mult = 1000000000; s = s.slice(0, -1); }
        var n = parseFloat(s);
        return isNaN(n) ? 0 : Math.round(n * mult);
    }
    function metric(pathPart) {
        var links = Array.from(document.querySelectorAll('a[href]'));
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            if (href.toLowerCase().indexOf('/' + target + '/' + pathPart) < 0) continue;
            var m = text(links[i]).match(/([0-9][0-9,.]*[KMBkmb]?)\s*(Followers?|Following)/i);
            if (m) return parseCount(m[1]);
            var spans = Array.from(links[i].querySelectorAll('span')).map(text);
            for (var j = 0; j < spans.length; j++) {
                if (/^[0-9][0-9,.]*[KMBkmb]?$/.test(spans[j])) return parseCount(spans[j]);
            }
        }
        return 0;
    }
    var body = text(document.body).slice(0, 6000);
    var nameEl = document.querySelector('[data-testid="UserName"]');
    var bioEl = document.querySelector('[data-testid="UserDescription"]');
    var name = text(nameEl);
    var bio = text(bioEl);
    var posts = 0;
    var pm = body.match(/([0-9][0-9,.]*[KMBkmb]?)\s+posts/i);
    if (pm) posts = parseCount(pm[1]);

    var buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
    var state = 'unknown';
    var label = '';
    var idx = -1;
    for (var i = 0; i < buttons.length; i++) {
        var b = buttons[i];
        var aria = b.getAttribute('aria-label') || '';
        var dt = b.getAttribute('data-testid') || '';
        var txt = text(b);
        var all = (aria + ' ' + dt + ' ' + txt).toLowerCase();
        var r = b.getBoundingClientRect();
        var inArticle = !!b.closest('article[data-testid="tweet"]');
        if (inArticle || r.top > 900) continue;
        if (all.indexOf(target) >= 0 || txt === 'Follow' || dt.toLowerCase().indexOf('-follow') >= 0) {
            if (/following|unfollow/.test(all) || txt === 'Following') {
                state = 'already_following'; label = aria || txt || dt; idx = i; break;
            }
            if (/pending|requested/.test(all) || txt === 'Pending') {
                state = 'requested'; label = aria || txt || dt; idx = i; break;
            }
            if (/follow/.test(all) && !/followers/.test(all)) {
                state = 'can_follow'; label = aria || txt || dt; idx = i; break;
            }
        }
    }
    if (/this account doesn't exist|account suspended|temporarily restricted/i.test(body)) {
        state = 'unavailable';
    }
    if (/protected posts|these posts are protected/i.test(body)) {
        state = state === 'can_follow' ? 'protected_can_follow' : 'protected';
    }
    window.__x_follow_button_index = idx;
    return JSON.stringify({
        state: state,
        label: label,
        name: name,
        bio: bio,
        followers: metric('followers') || metric('verified_followers'),
        following: metric('following'),
        posts: posts,
        body: body
    });
})()
"""


CLICK_FOLLOW_JS = r"""
(function() {
    var idx = window.__x_follow_button_index;
    function visible(el) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        var s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    }
    var buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
    if (idx === undefined || idx < 0 || idx >= buttons.length) return 'no button';
    buttons[idx].click();
    return 'clicked';
})()
"""


def inspect_profile(ws, port: int, candidate: Candidate, wait: float) -> dict[str, Any]:
    with chrome_lock(port, timeout=240):
        chrome.navigate(ws, candidate.profile_url or f"https://x.com/{candidate.handle}", wait=wait)
        chrome.set_viewport(ws, 1320, 1600)
        time.sleep(0.8)
        expr = PROFILE_CHECK_JS.replace("arguments[0]", json.dumps(candidate.handle))
        raw = chrome.eval_js(ws, expr)
    try:
        return json.loads(raw) if raw else {"state": "unknown"}
    except Exception:
        return {"state": "unknown", "raw": raw}


def live_reject_reason(candidate: Candidate, profile: dict[str, Any]) -> str:
    state = profile.get("state") or "unknown"
    if state in {"already_following", "requested"}:
        return state
    if state not in {"can_follow"}:
        return f"button_state:{state}"

    live_followers = parse_int(profile.get("followers"))
    live_posts = parse_int(profile.get("posts"))
    live_text = " ".join([
        candidate.name,
        candidate.bio,
        candidate.text,
        profile.get("name", ""),
        profile.get("bio", ""),
        profile.get("body", "")[:1200],
    ])
    weak, strong, _hits = topic_hits(candidate.account, live_text)
    bad = negative_hits(live_text)
    if bad:
        return "negative:" + ",".join(bad[:4])
    if live_followers and live_followers < ACCOUNTS[candidate.account]["min_followers"]:
        return f"below follower floor:{live_followers}"
    if live_followers and live_followers > ACCOUNTS[candidate.account]["max_followers"]:
        return f"above follower cap:{live_followers}"
    if live_posts and live_posts > 60_000:
        return f"extreme post count:{live_posts}"
    if weak + strong <= 0:
        return "live profile off-topic"
    return ""


def click_follow(ws, port: int, wait: float) -> str:
    with chrome_lock(port, timeout=240):
        res = chrome.eval_js(ws, CLICK_FOLLOW_JS)
        time.sleep(wait)
    return res or ""


def load_ledger() -> dict[str, Any]:
    ledger = read_json(LEDGER_PATH, {})
    if not isinstance(ledger, dict):
        ledger = {}
    ledger.setdefault("days", {})
    ledger.setdefault("follows", [])
    return ledger


def day_bucket(ledger: dict[str, Any], day: str) -> dict[str, Any]:
    days = ledger.setdefault("days", {})
    bucket = days.setdefault(day, {"total": 0, "accounts": {}})
    bucket.setdefault("total", 0)
    bucket.setdefault("accounts", {})
    return bucket


def account_day_count(ledger: dict[str, Any], day: str, account: str) -> int:
    bucket = day_bucket(ledger, day)
    acct = bucket["accounts"].get(account) or {}
    return int(acct.get("count") or 0)


def total_day_count(ledger: dict[str, Any], day: str) -> int:
    return int(day_bucket(ledger, day).get("total") or 0)


def already_followed(ledger: dict[str, Any], account: str, handle: str) -> bool:
    h = handle.lower()
    for row in ledger.get("follows") or []:
        if row.get("account") == account and str(row.get("handle", "")).lower() == h:
            return True
    return False


def record_follow(
    ledger: dict[str, Any],
    day: str,
    account: str,
    candidate: Candidate,
    profile: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    bucket = day_bucket(ledger, day)
    acct = bucket["accounts"].setdefault(account, {"count": 0, "handles": []})
    acct["count"] = int(acct.get("count") or 0) + 1
    acct.setdefault("handles", []).append(candidate.handle)
    bucket["total"] = int(bucket.get("total") or 0) + 1
    ledger.setdefault("follows", []).append(
        {
            "date": day,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "account": account,
            "account_label": ACCOUNTS[account]["label"],
            "handle": candidate.handle,
            "profile_url": candidate.profile_url,
            "score": candidate.score,
            "source": candidate.source,
            "reasons": candidate.reasons,
            "live_profile": {
                "name": profile.get("name", ""),
                "bio": profile.get("bio", ""),
                "followers": parse_int(profile.get("followers")),
                "following": parse_int(profile.get("following")),
                "posts": parse_int(profile.get("posts")),
            },
        }
    )
    write_json(LEDGER_PATH, ledger)


def group_by_account(rows: list[Candidate]) -> dict[str, list[Candidate]]:
    out = {a: [] for a in ACCOUNT_ORDER}
    for c in rows:
        out.setdefault(c.account, []).append(c)
    for account in out:
        out[account].sort(key=lambda c: (c.score, c.followers, c.sample_likes, c.home_max_likes), reverse=True)
    return out


def execution_account_order(selected: list[str], day: str) -> list[str]:
    if len(selected) <= 1:
        return selected
    try:
        offset = date.fromisoformat(day).toordinal() % len(selected)
    except ValueError:
        offset = 0
    return selected[offset:] + selected[:offset]


def write_shortlist(day: str, rows: list[Candidate], limit_per_account: int = 25) -> str:
    grouped = group_by_account(rows)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date_key": day,
        "accounts": {
            account: [asdict(c) for c in grouped.get(account, [])[:limit_per_account]]
            for account in ACCOUNT_ORDER
        },
    }
    path = SHORTLIST_PATH.format(date=day)
    write_json(path, payload)
    return path


def print_shortlist(rows: list[Candidate], limit: int) -> None:
    grouped = group_by_account(rows)
    for account in ACCOUNT_ORDER:
        cands = grouped.get(account, [])[:limit]
        log(f"{ACCOUNTS[account]['label']} shortlist: {len(cands)} shown")
        for c in cands:
            log(
                "  @{:<20} score={:<6} followers={:<7} source={} reason={}".format(
                    c.handle,
                    c.score,
                    c.followers or "?",
                    c.source,
                    "; ".join(c.reasons[:3]),
                )
            )


def execute(args: argparse.Namespace) -> int:
    accounts = ACCOUNT_ORDER if args.account == "all" else [args.account]
    ledger = load_ledger()
    day = args.date or datetime.now().strftime("%Y-%m-%d")
    rows = load_candidates(accounts)
    rows = [c for c in rows if not already_followed(ledger, c.account, c.handle)]
    short_path = write_shortlist(day, rows)
    log(f"ranked {len(rows)} candidates -> {short_path}")

    if args.dry_run and not args.live_check:
        print_shortlist(rows, args.show)
        log("dry run only; no profiles opened and no follows clicked")
        return 0

    grouped = group_by_account(rows)
    total_left = max(0, args.max_total - total_day_count(ledger, day))
    if total_left <= 0:
        log(f"daily global cap already reached for {day}: {args.max_total}")
        return 0

    followed: list[tuple[str, Candidate]] = []
    skipped: list[tuple[str, str, str]] = []
    ports: dict[str, int] = {}
    sockets: dict[str, Any] = {}
    ordered_accounts = execution_account_order(accounts, day)
    log("execution account order: " + ", ".join(ACCOUNTS[a]["label"] for a in ordered_accounts))
    try:
        cursor = {account: 0 for account in ordered_accounts}
        inspected = {account: 0 for account in ordered_accounts}
        progressed = True
        while total_left > 0 and progressed:
            progressed = False
            for account in ordered_accounts:
                if total_left <= 0:
                    break
                per_left = max(0, args.max_per_account - account_day_count(ledger, day, account))
                per_left -= len([x for x in followed if x[0] == account])
                if per_left <= 0:
                    if inspected[account] == 0:
                        log(f"{ACCOUNTS[account]['label']}: daily account cap already reached")
                    continue
                candidates = grouped.get(account, [])[: args.live_candidates]
                if not candidates:
                    if inspected[account] == 0:
                        log(f"{ACCOUNTS[account]['label']}: no eligible candidates")
                    continue

                port = ports.get(account)
                if port is None:
                    port = ensure_chrome(account)
                    ports[account] = port
                    sockets[account] = chrome.connect(port, timeout=60)
                ws = sockets[account]

                account_success = False
                while cursor[account] < len(candidates) and not account_success:
                    c = candidates[cursor[account]]
                    cursor[account] += 1
                    inspected[account] += 1
                    progressed = True
                    jitter = random.uniform(args.min_wait, args.max_wait)
                    log(f"{ACCOUNTS[account]['label']}: inspect @{c.handle} score={c.score}")
                    profile = inspect_profile(ws, port, c, args.page_wait)
                    reason = live_reject_reason(c, profile)
                    if reason:
                        skipped.append((account, c.handle, reason))
                        log(f"  skip @{c.handle}: {reason}")
                        time.sleep(jitter)
                        continue
                    if args.dry_run:
                        followed.append((account, c))
                        account_success = True
                        log(f"  dry-run would follow @{c.handle}")
                        total_left -= 1
                        time.sleep(jitter)
                        continue
                    res = click_follow(ws, port, args.click_wait)
                    after = inspect_profile(ws, port, c, max(1.0, args.page_wait / 2))
                    after_state = after.get("state") or "unknown"
                    if res == "clicked" and after_state in {"already_following", "requested"}:
                        record_follow(ledger, day, account, c, after, dry_run=False)
                        followed.append((account, c))
                        account_success = True
                        total_left -= 1
                        log(f"  followed @{c.handle} ({after_state})")
                    else:
                        skipped.append((account, c.handle, f"click_result:{res}; after:{after_state}"))
                        log(f"  follow did not verify @{c.handle}: click_result={res} after={after_state}")
                    time.sleep(jitter)
        for account in ordered_accounts:
            log(f"{ACCOUNTS[account]['label']}: inspected {inspected[account]}, followed {len([x for x in followed if x[0] == account])}")
    finally:
        for ws in sockets.values():
            try:
                ws.close()
            except Exception:
                pass

    log("summary")
    for account, c in followed:
        prefix = "would follow" if args.dry_run else "followed"
        log(f"  {prefix} {ACCOUNTS[account]['label']} -> @{c.handle} ({c.profile_url})")
    if not followed:
        log("  no follows executed")
    if skipped:
        log(f"  skipped {len(skipped)} live candidates")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the low-volume X follow playbook.")
    parser.add_argument("--account", choices=["all", *ACCOUNT_ORDER], default="all")
    parser.add_argument("--date", default="", help="ledger date key; defaults to local machine date")
    parser.add_argument("--max-total", type=int, default=2, help="global daily cap across selected accounts")
    parser.add_argument("--max-per-account", type=int, default=2, help="daily cap per account")
    parser.add_argument("--live-candidates", type=int, default=8, help="max live profiles to inspect per account")
    parser.add_argument("--show", type=int, default=8, help="offline shortlist rows to print per account")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live-check", action="store_true", help="with --dry-run, still open profiles but do not click")
    parser.add_argument("--page-wait", type=float, default=3.5)
    parser.add_argument("--click-wait", type=float, default=2.0)
    parser.add_argument("--min-wait", type=float, default=2.0)
    parser.add_argument("--max-wait", type=float, default=4.0)
    args = parser.parse_args()
    if args.max_total >= 3:
        log("warning: --max-total is 3 or more; user asked for less than 3 per day")
    if args.max_per_account >= 3:
        log("warning: --max-per-account is 3 or more; user asked for less than 3 per day")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
