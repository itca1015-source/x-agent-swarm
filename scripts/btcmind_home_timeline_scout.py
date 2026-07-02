#!/usr/bin/env python3
"""BTCMind Home / For You timeline scout.

Scrapes the logged-in X Home / For You timeline, captures media metadata, and
routes BTCMind-fit posts into either:
- quote candidates queued to Telegram for approval, or
- autonomous plain reposts after the deterministic BTCMind safety gate.

It never posts originals and never auto-quotes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
STATE_DIR = os.path.join(ROOT_DIR, "state")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")
HOME_SEEN_PATH = os.path.join(STATE_DIR, "btcmind_home_timeline_seen.json")
REPOST_SEEN_PATH = os.path.join(STATE_DIR, "btcmind_repost_seen.json")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "btcmind_home_timeline_scout")
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "engage_config.json")
PLAYBOOK_PATH = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "playbook.md")
ANALYTICS_PATH = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "analytics_judgement.md")

sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
import engage as _engage
import generate as _generate
import telegram as _tg
import btcmind_autonomy as _btcmind
from lock import chrome_lock, file_lock


HOME_EXTRACT_JS = r"""
(function() {
    function compact(el) {
        return (el && (el.innerText || el.textContent) || '')
            .replace(/\u00a0/g, ' ')
            .replace(/[ \t]+/g, ' ')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }
    function visibleText(el) {
        return (el && (el.innerText || el.textContent) || '')
            .replace(/\s+/g, ' ')
            .trim();
    }
    function metric(el, testid) {
        var node = el.querySelector('[data-testid="' + testid + '"]');
        if (!node) return '';
        var label = node.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+(Replies|Reply|reposts?|likes?|bookmarks?|views?)/i);
        if (m) return m[1] + (m[2] || '');
        return (node.innerText || '').replace(/[^0-9KMBkmb.,]/g, '');
    }
    function analyticsMetric(el) {
        var node = el.querySelector('a[href*="/analytics"], [aria-label*="views" i], [aria-label*="Views" i]');
        if (!node) return '';
        var label = node.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+views?/i);
        if (m) return m[1] + (m[2] || '');
        return (node.innerText || '').replace(/[^0-9KMBkmb.,]/g, '');
    }
    function statusFromAnchor(a) {
        if (!a || !a.href) return null;
        var m = a.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
        if (!m) return null;
        return {author: m[1], id: m[2], url: 'https://x.com/' + m[1] + '/status/' + m[2]};
    }
    function unique(arr) {
        var seen = {};
        var out = [];
        arr.forEach(function(x) {
            if (!x || seen[x]) return;
            seen[x] = true;
            out.push(x);
        });
        return out;
    }
    function normalizedHref(a) {
        try {
            var u = new URL(a.href);
            if (u.hostname === 'x.com' || u.hostname === 'twitter.com') {
                return u.pathname + u.search;
            }
            return a.href;
        } catch(e) {
            return a.getAttribute('href') || '';
        }
    }
    var out = [];
    Array.from(document.querySelectorAll('article[data-testid="tweet"]')).forEach(function(el, idx) {
        try {
            var all = compact(el);
            if (!all || /\bPromoted\b/i.test(all)) return;
            var timeEl = el.querySelector('time');
            var main = statusFromAnchor(timeEl ? timeEl.closest('a[href*="/status/"]') : null);
            var statusLinks = Array.from(el.querySelectorAll('a[href*="/status/"]'))
                .map(statusFromAnchor)
                .filter(Boolean);
            if (!main && statusLinks.length) main = statusLinks[0];
            if (!main || !main.id) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var tweetText = textEl ? compact(textEl) : '';

            var imageNodes = Array.from(el.querySelectorAll('[data-testid="tweetPhoto"] img, a[href*="/photo/"] img, img[src*="pbs.twimg.com/media"]'));
            var images = unique(imageNodes.map(function(img) {
                var src = img.currentSrc || img.src || '';
                if (!src || /profile_images/.test(src)) return '';
                return JSON.stringify({
                    src: src,
                    alt: img.getAttribute('alt') || '',
                    aria: img.getAttribute('aria-label') || '',
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0
                });
            }).filter(Boolean)).map(function(x) {
                try { return JSON.parse(x); } catch(e) { return {}; }
            });

            var videoNodes = Array.from(el.querySelectorAll('[data-testid="videoPlayer"], video'));
            var videos = unique(videoNodes.map(function(v) {
                var node = v.tagName && v.tagName.toLowerCase() === 'video' ? v : v.querySelector('video');
                return JSON.stringify({
                    src: node ? (node.currentSrc || node.src || '') : '',
                    poster: node ? (node.getAttribute('poster') || '') : '',
                    aria: v.getAttribute('aria-label') || '',
                    text: visibleText(v).slice(0, 500)
                });
            })).map(function(x) {
                try { return JSON.parse(x); } catch(e) { return {}; }
            });

            var cardTexts = unique(Array.from(el.querySelectorAll('[data-testid="card.wrapper"], a[data-testid="card.wrapper"]'))
                .map(function(c) { return visibleText(c).slice(0, 700); })
                .filter(Boolean));
            var hrefs = unique(Array.from(el.querySelectorAll('a[href]')).map(normalizedHref));
            var statusIds = unique(statusLinks.map(function(x) { return x.id; }));
            var otherStatus = statusLinks.some(function(x) {
                return x.id !== main.id || x.author.toLowerCase() !== main.author.toLowerCase();
            });

            var rect = el.getBoundingClientRect();
            out.push({
                dom_index: idx,
                id: main.id,
                author: main.author,
                url: main.url,
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                text: tweetText,
                all_text: all.slice(0, 5000),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                likes_raw: metric(el, 'like'),
                bookmarks_raw: metric(el, 'bookmark'),
                views_raw: analyticsMetric(el),
                has_image: images.length > 0,
                has_video: videos.length > 0,
                has_card: cardTexts.length > 0,
                has_gif: !!el.querySelector('[data-testid="gif"], [aria-label*="GIF" i]'),
                images: images.slice(0, 8),
                videos: videos.slice(0, 4),
                card_texts: cardTexts.slice(0, 4),
                hrefs: hrefs.slice(0, 80),
                status_ids_in_card: statusIds.slice(0, 8),
                is_quote_context: otherStatus,
                is_repost_context: /\breposted\b/i.test(all),
                viewport_top: Math.round(rect.top),
                viewport_height: Math.round(rect.height)
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


CRYPTO_TERMS = [
    "btc", "bitcoin", "stablecoin", "usdc", "usdt", "onchain", "on-chain",
    "wallet", "custody", "seed phrase", "phishing", "wallet drain", "scam",
    "bridge", "exchange", "liquidity", "perp", "funding", "liquidation",
    "treasury", "tokenized", "rwa", "defi", "smart contract", "crypto ai",
    "ai agent", "agents", "compliance", "risk", "settlement", "etf",
]

QUOTE_BLOCK_PATTERNS = [
    ("trader_or_whale_pnl", r"\b(whale|wallet|trader|og)\b.{0,120}\b(bought|sold|buying|selling|profit|loss|long|short|entry|exit|liquidat)\b"),
    ("buy_sell_reversal", r"\b(sold|sell|bought|buy|buying back|spent)\b.{0,120}\$[A-Za-z][A-Za-z0-9_]*"),
    ("self_promotional_product_update", r"\b(our new|we have integrated|we integrated|we['’]?re launching|we launched|we are building|is building it|have you tried|already live|now live|one click away|one-stop shop)\b"),
    ("dashboard_portal_promo", r"\b(portal|dashboard|developer tools|connect with|try it|launch a branded|integrated .{0,40} skills)\b"),
    ("political_regulatory_bait", r"\b(senator|president|election|warren|trump|biden|congress|bill|act)\b"),
]

WEAK_QUOTE_PHRASES = [
    "worth watching",
    "worth tracking",
    "watch the",
    "the real signal is",
    "the hidden constraint is",
    "market structure matters",
    "operators should watch",
    "operator lens",
    "btc mind",
    "not financial advice",
    "generic bullish",
    "this is huge",
    "must read",
]

JARGON_TERMS = [
    "wallet", "wallets", "flow", "flows", "onchain", "on-chain", "liquidity",
    "exchange", "exchanges", "stablecoin", "stablecoins", "market", "structure",
    "custody", "settlement", "treasury", "security", "risk", "risks", "protocol",
    "protocols", "token", "tokens", "operator", "operators", "signal", "signals",
    "rails", "bridge", "bridging", "agent", "agents", "compliance",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug_ts() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _compact(text: str, n: int = 260) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def _parse_count(raw: str) -> int:
    s = (raw or "").strip().replace(",", "").upper()
    if not s:
        return 0
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _parse_dt(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _daily_key(name: str) -> str:
    return f"_daily:{name}:{datetime.now():%Y-%m-%d}"


def _daily_count(state: dict, name: str) -> int:
    try:
        return int(state.get(_daily_key(name), 0) or 0)
    except Exception:
        return 0


def _increment_daily(state: dict, name: str):
    state[_daily_key(name)] = _daily_count(state, name) + 1


def _recent_action(state: dict, name: str, min_hours: float) -> bool:
    if min_hours <= 0:
        return False
    when = state.get(f"last_{name}_at")
    if not when:
        return False
    try:
        dt = datetime.fromisoformat(str(when))
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < min_hours * 3600


def _mark_action(state: dict, name: str):
    state[f"last_{name}_at"] = datetime.now().isoformat()


def _media_text(row: dict) -> str:
    parts: list[str] = []
    for img in row.get("images") or []:
        alt = _compact(str(img.get("alt") or img.get("aria") or ""), 240)
        if alt:
            parts.append(f"image alt: {alt}")
    for vid in row.get("videos") or []:
        vtxt = _compact(" ".join(str(vid.get(k) or "") for k in ("aria", "text")), 240)
        if vtxt:
            parts.append(f"video metadata: {vtxt}")
        if vid.get("poster"):
            parts.append("video has poster frame")
    for card in row.get("card_texts") or []:
        if card:
            parts.append(f"card: {_compact(card, 300)}")
    return "\n".join(parts)


def _analysis_text(row: dict) -> str:
    parts = [
        str(row.get("text") or ""),
        _media_text(row),
        str(row.get("all_text") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()[:4000]


def _display_source_text(row: dict) -> str:
    text = str(row.get("text") or "").strip()
    media = _media_text(row)
    if media:
        text = (text + "\n\n" if text else "") + media
    if not text:
        text = str(row.get("all_text") or "").strip()
    return text[:1100]


def _crypto_score(row: dict) -> int:
    hay = _analysis_text(row).lower()
    score = sum(1 for term in CRYPTO_TERMS if term in hay)
    if row.get("has_video"):
        score += 2
    if row.get("has_image"):
        score += 1
    engagement = int(row.get("likes") or 0) + 2 * int(row.get("reposts") or 0) + 3 * int(row.get("replies") or 0)
    if engagement:
        score += min(5, int(math.log10(max(1, engagement))) + 1)
    return score


def _media_dependent(row: dict) -> bool:
    text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
    return bool(row.get("has_video") or (row.get("has_image") and len(text) < 80))


def _home_post_filter_reason(row: dict, action: str, cfg: dict) -> str:
    if action != "quote_candidate":
        return ""
    hcfg = cfg.get("home_timeline_scout") or {}
    min_likes = int(hcfg.get("min_quote_likes") or 80)
    min_reposts = int(hcfg.get("min_quote_reposts") or 5)
    min_replies = int(hcfg.get("min_quote_replies") or 8)
    likes = int(row.get("likes") or 0)
    reposts = int(row.get("reposts") or 0)
    replies = int(row.get("replies") or 0)
    if likes < min_likes and reposts < min_reposts and replies < min_replies:
        return f"low_public_pull:{likes}likes/{reposts}reposts/{replies}replies"
    hay = _analysis_text(row)
    for name, pattern in QUOTE_BLOCK_PATTERNS:
        if re.search(pattern, hay, flags=re.I | re.S):
            return name
    return ""


def _quote_quality_issues(text: str) -> list[str]:
    raw = re.sub(r"\s+", " ", text or "").strip()
    hay = raw.lower()
    issues: list[str] = []
    if not raw:
        return ["empty"]
    if len(raw) < 70:
        issues.append("too_short_to_land_a_take")
    if len(raw) > 240:
        issues.append("too_long_for_quote_hook")
    if len([s for s in re.split(r"[.!?]+", raw) if s.strip()]) > 2:
        issues.append("too_many_sentences")
    if raw.count("?") > 1:
        issues.append("too_many_questions")
    if raw.endswith("?"):
        issues.append("ends_as_question_instead_of_point_of_view")
    if sum(raw.count(ch) for ch in [",", ";", ":"]) >= 5:
        issues.append("too_listy_or_clause_heavy")
    for phrase in WEAK_QUOTE_PHRASES:
        if phrase in hay:
            issues.append(f"weak_phrase:{phrase}")
    if re.match(r"^(?:crypto|onchain|on-chain|wallet|stablecoin|bitcoin|btc)\s+(needs|is|will|can)\b", hay):
        issues.append("generic_crypto_opening")
    if re.match(r"^market structure\s+(matters|is|will|needs)\b", hay):
        issues.append("generic_market_structure_opening")
    jargon_hits = sum(1 for term in JARGON_TERMS if re.search(rf"\b{re.escape(term)}\b", hay))
    if jargon_hits >= 8:
        issues.append("jargon_dense")
    if not any(marker in hay for marker in [
        "not ", "isn't", "is not", "but ", "until ", "once ", "when ",
        "without ", "instead", "breaks", "leaks", "hides", "moves", "moved",
        "turns", "cheap ", "expensive", "real ", "only ", "fails", "misses",
    ]):
        issues.append("no_tension_or_contrast")
    if re.search(r"\b(need|needs|should|must)\b.{0,60}\b(before|when|if|without)\b", hay) and jargon_hits >= 5:
        issues.append("sounds_like_internal_policy_note")
    return issues


def _enrich_row(row: dict, policy: dict) -> dict:
    row["likes"] = _parse_count(row.get("likes_raw", ""))
    row["reposts"] = _parse_count(row.get("reposts_raw", ""))
    row["replies"] = _parse_count(row.get("replies_raw", ""))
    row["bookmarks"] = _parse_count(row.get("bookmarks_raw", ""))
    row["views"] = _parse_count(row.get("views_raw", ""))
    dt = _parse_dt(row.get("iso", ""))
    row["age_minutes"] = int((datetime.now(timezone.utc) - dt).total_seconds() / 60) if dt else 999999
    row["media_text"] = _media_text(row)
    row["analysis_text"] = _analysis_text(row)
    row["crypto_score"] = _crypto_score(row)
    c = dict(row)
    c["text"] = row["analysis_text"]
    row["deterministic_classification"] = _btcmind.classify_candidate(c, policy)
    if _media_dependent(row) and row["deterministic_classification"].get("action") == "plain_repost_candidate":
        cls = dict(row["deterministic_classification"])
        cls["action"] = "quote_candidate"
        cls["reason"] = "media-dependent source needs human quote/repost review"
        row["deterministic_classification"] = cls
    return row


def _extract_home(ws, policy: dict) -> list[dict]:
    raw = _chrome.eval_js(ws, HOME_EXTRACT_JS)
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    return [_enrich_row(r, policy) for r in rows]


def _prepare_home(ws, url: str):
    _chrome.navigate(ws, url, wait=5.0)
    _chrome.set_viewport(ws, 1400, 2400)
    time.sleep(1.0)
    _chrome.eval_js(ws, """
        (function(){
            var tabs = Array.from(document.querySelectorAll('[role="tab"], a[role="tab"]'));
            var t = tabs.find(function(x){ return /For you/i.test(x.innerText || x.textContent || ''); });
            if (t) t.click();
            var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
            for (var b of btns) {
                var label = (b.innerText || b.textContent || b.getAttribute('aria-label') || '').trim();
                if (/See new posts/i.test(label)) { b.click(); break; }
            }
            window.scrollTo(0, 0);
            return 'ok';
        })()
    """)
    time.sleep(1.5)


def scrape_home(port: int, policy: dict, target_posts: int, max_scrolls: int,
                max_refreshes: int, wait: float, self_handle: str) -> list[dict]:
    records: dict[str, dict] = {}
    with chrome_lock(port, timeout=7200, on_wait=lambda s: _log(str(s))):
        ws = _chrome.connect(port, timeout=60)
        try:
            for refresh in range(max(1, max_refreshes)):
                url = "https://x.com/home" if refresh == 0 else f"https://x.com/home?btcmind_home_refresh={int(time.time())}_{refresh}"
                _prepare_home(ws, url)
                stagnant = 0
                for i in range(max_scrolls + 1):
                    before = len(records)
                    for row in _extract_home(ws, policy):
                        handle = str(row.get("author") or "").lstrip("@")
                        if not handle or handle.lower() == self_handle.lower():
                            continue
                        key = str(row.get("id") or "")
                        if not key:
                            continue
                        existing = records.get(key)
                        if existing:
                            existing["last_seen_refresh"] = refresh
                            existing["last_seen_scroll"] = i
                            continue
                        row["first_seen_refresh"] = refresh
                        row["last_seen_refresh"] = refresh
                        row["first_seen_scroll"] = i
                        row["last_seen_scroll"] = i
                        records[key] = row
                    new = len(records) - before
                    _log(f"home refresh={refresh} scroll={i} posts={len(records)} new={new}")
                    if len(records) >= target_posts:
                        break
                    stagnant = stagnant + 1 if new == 0 else 0
                    if stagnant >= 6:
                        _log(f"home refresh={refresh} stalled after 6 stagnant scrolls")
                        break
                    _chrome.eval_js(ws, """
                        (function(){
                            window.scrollBy(0, Math.floor(window.innerHeight * 0.86));
                            if (document.scrollingElement) {
                                document.scrollingElement.scrollBy(0, Math.floor(window.innerHeight * 0.86));
                            }
                            return JSON.stringify({y: window.scrollY, h: document.body.scrollHeight});
                        })()
                    """)
                    time.sleep(wait)
                if len(records) >= target_posts:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
    rows = list(records.values())
    rows.sort(key=lambda r: (int(r.get("first_seen_scroll") or 0), int(r.get("dom_index") or 0)))
    return rows[:target_posts]


def _json_from_model(raw: str, default: Any) -> Any:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return default
    return default


def _message_text(msg) -> str:
    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _shortlist_for_ai(rows: list[dict], max_items: int) -> list[dict]:
    def pri(row: dict) -> tuple:
        action = (row.get("deterministic_classification") or {}).get("action")
        action_score = {"plain_repost_candidate": 4, "quote_candidate": 3, "skip": 0}.get(action, 1)
        engagement = int(row.get("likes") or 0) + 2 * int(row.get("reposts") or 0) + 3 * int(row.get("replies") or 0)
        return (
            action_score,
            int(row.get("crypto_score") or 0),
            1 if row.get("has_video") else 0,
            1 if row.get("has_image") else 0,
            engagement,
        )
    candidates = [
        r for r in rows
        if (r.get("deterministic_classification") or {}).get("action") != "skip"
        or int(r.get("crypto_score") or 0) >= 4
    ]
    candidates.sort(key=pri, reverse=True)
    return candidates[:max_items]


def ai_review(rows: list[dict], max_items: int) -> dict[str, dict]:
    shortlist = _shortlist_for_ai(rows, max_items)
    if not shortlist:
        return {}
    items = []
    for r in shortlist:
        cls = r.get("deterministic_classification") or {}
        items.append({
            "id": r.get("id"),
            "author": r.get("author"),
            "url": r.get("url"),
            "metrics": {
                "likes": r.get("likes", 0),
                "reposts": r.get("reposts", 0),
                "replies": r.get("replies", 0),
                "views": r.get("views", 0),
                "age_minutes": r.get("age_minutes", 0),
            },
            "media": {
                "has_image": bool(r.get("has_image")),
                "has_video": bool(r.get("has_video")),
                "image_count": len(r.get("images") or []),
                "video_count": len(r.get("videos") or []),
                "media_dependent": _media_dependent(r),
                "media_text": _compact(r.get("media_text") or "", 500),
            },
            "deterministic": {
                "action": cls.get("action"),
                "reason": cls.get("reason"),
                "signals": cls.get("signals") or [],
            },
            "text": _compact(r.get("analysis_text") or r.get("all_text") or "", 900),
        })

    playbook = _load_text(PLAYBOOK_PATH)
    rubric = _load_text(ANALYTICS_PATH)
    prompt = f"""You are reviewing X Home / For You posts for the BTCMind account.

BTCMind identity and rules:
{playbook[:2200]}

Analytics rubric:
{rubric[:1400]}

Task:
- Decide which posts are appropriate for BTCMind to QUOTE or PLAIN-REPOST.
- Quote candidates require human approval. They need a real BTCMind lens:
  market structure, wallet/security, stablecoin/payments, on-chain data, or crypto-AI operator implication.
- Plain repost is only for source posts that are valuable without extra context and safe as-is.
- If a post depends on untranscribed video/audio or unreadable image details, do NOT plain-repost it. Use quote_candidate only if the visible text/media metadata is enough for a defensible angle.
- Skip price calls, whale/trader PnL, buy/sell reversal posts, token shills, product promos, integration launch announcements, broad news recaps, political/regulatory bait, vague AI crypto hype, memes, and posts that would make BTCMind look like a trading account.
- Prefer public security, wallet UX, stablecoin/payment mechanics, on-chain data caveats, exchange/liquidity structure, and crypto-AI operator constraints.
- Never infer BTCMind product capabilities.

Return STRICT JSON only:
{{
  "decisions": [
    {{
      "id": "post id",
      "action": "skip | quote_candidate | plain_repost_candidate",
      "reason": "short reason",
      "quote_angle": "short BTCMind angle if quote_candidate, else empty",
      "risk_flags": ["optional short flags"]
    }}
  ]
}}

Posts:
{json.dumps(items, ensure_ascii=False, indent=2)}
"""
    raw = _generate._call_claude([{"role": "user", "content": prompt}], max_tokens=3500)
    data = _json_from_model(raw, {"decisions": []})
    decisions = {}
    for d in data.get("decisions") or []:
        pid = str(d.get("id") or "")
        action = str(d.get("action") or "skip")
        if pid and action in {"skip", "quote_candidate", "plain_repost_candidate"}:
            decisions[pid] = {
                "action": action,
                "reason": str(d.get("reason") or "")[:500],
                "quote_angle": str(d.get("quote_angle") or "")[:300],
                "risk_flags": [str(x)[:120] for x in (d.get("risk_flags") or [])[:6]],
            }
    return decisions


def generate_btcmind_quote(row: dict, ai_decision: dict, max_chars: int) -> dict:
    playbook = _load_text(PLAYBOOK_PATH)
    source = _display_source_text(row)
    quote_limit = min(max_chars, 240)
    prompt = f"""You operate the BTCMind X account.

BTCMind playbook:
{playbook[:2600]}

Write three candidate quote-tweet comments for the source post below, then choose the strongest one.

Required angle from review:
{ai_decision.get('quote_angle') or ai_decision.get('reason') or 'Add a BTCMind operator lens.'}

Growth-quality rules:
- The quote must be follow-worthy for crypto builders/operators, not merely safe.
- Lead with a tension, mechanism, or reversal: "not X, Y", "the bottleneck moved from X to Y", "once X happens, Y breaks", or a similarly sharp point.
- Make one point only. No shopping lists. No internal research memo tone.
- Prefer concrete words from the source post over generic words like "market structure", "operator lens", or "worth watching".
- Best range: 100-210 characters. Absolute max: {quote_limit}.
- No hashtags, emojis, URLs, or @-mentions.
- Do not mention BTCMind.
- Do not claim product capabilities, users, launch, integrations, signals, predictions, or trading execution.
- Do not give financial advice or price targets.
- Do not summarize the source. Add a non-obvious mechanism, caveat, measurement problem, security/payment/wallet implication, or market-structure implication.
- Do not end with a question.
- Avoid weak openings/phrases: "Worth watching...", "The real signal is...", "The hidden constraint is...", "Market structure matters...".
- If the visible source text is not enough to support a quote, return an empty reply.

Source post by @{row.get('author')}:
{source[:1300]}

Return STRICT JSON only:
{{
  "op_summary": "one short source summary",
  "reply_angle": "one short description of your angle",
  "variants": [
    {{"reply": "candidate quote", "why_it_can_pull_attention": "short reason"}}
  ],
  "reply": "the strongest candidate quote"
}}
"""
    msg = _generate._client_get().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1100,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _json_from_model(_message_text(msg), {})
    candidates = []
    chosen = str(data.get("reply") or "").strip().strip('"')
    if chosen:
        candidates.append(chosen)
    for item in data.get("variants") or []:
        txt = str((item or {}).get("reply") or "").strip().strip('"')
        if txt and txt not in candidates:
            candidates.append(txt)
    reply = ""
    block_reason = ""
    for cand in candidates:
        cand = re.sub(r"\s+", " ", cand).strip()
        if len(cand) > quote_limit:
            cand = cand[:quote_limit].rsplit(" ", 1)[0].rstrip() + "..."
        issues = _quote_quality_issues(cand)
        if not issues and not _chrome.text_repeats_itself(cand):
            reply = cand
            break
        block_reason = ",".join(issues[:3]) or "repeating_draft"
    if not reply:
        data["reply_angle"] = f"blocked_quality:{block_reason or 'no_passing_variant'}"
    return {
        "op_summary": str(data.get("op_summary") or "")[:240],
        "reply_angle": str(data.get("reply_angle") or ai_decision.get("quote_angle") or "")[:240],
        "reply": reply,
    }


def _entry_base(cfg: dict, row: dict, kind: str) -> dict:
    return {
        "id": f"btcmind_home_{kind}_{int(time.time())}_{str(row.get('id') or 'x')[-8:]}",
        "account": cfg.get("hunter_handle", "btcmind101"),
        "telegram_label": "BTCMind",
        "kind": kind,
        "source": "btcmind_home_timeline_scout",
        "source_keyword": "home_for_you",
        "target": str(row.get("author") or "").lstrip("@"),
        "target_url": row.get("url", ""),
        "target_text": _display_source_text(row),
        "source_text": _display_source_text(row),
        "post_likes": row.get("likes", 0),
        "post_replies": row.get("replies", 0),
        "post_reposts": row.get("reposts", 0),
        "post_age_min": row.get("age_minutes", 0),
        "media": {
            "has_image": bool(row.get("has_image")),
            "has_video": bool(row.get("has_video")),
            "media_dependent": _media_dependent(row),
            "images": row.get("images") or [],
            "videos": row.get("videos") or [],
            "card_texts": row.get("card_texts") or [],
        },
        "deterministic_classification": row.get("deterministic_classification") or {},
        "ai_decision": row.get("ai_decision") or {},
        "queued_at": _ts(),
        "telegram_message_id": 0,
    }


def _queue_or_update(entry: dict):
    with file_lock("reply_queue", timeout=60, on_wait=lambda s: _log(str(s))):
        cur = _load_json(QUEUE_PATH, [])
        for i, existing in enumerate(cur):
            if existing.get("id") == entry.get("id"):
                cur[i] = entry
                _save_json(QUEUE_PATH, cur)
                return
        cur.append(entry)
        _save_json(QUEUE_PATH, cur)


def _telegram_credentials(cfg: dict) -> tuple[str, str]:
    tg_cfg = cfg.get("telegram") or {}
    token = os.environ.get(str(tg_cfg.get("bot_token_env") or ""), "")
    chat_id = os.environ.get(str(tg_cfg.get("chat_id_env") or ""), "")
    return token, chat_id


def _send_quote_card(cfg: dict, entry: dict) -> int:
    token, chat_id = _telegram_credentials(cfg)
    if not token or not chat_id:
        _log("  Telegram quote card skipped: configured env vars are not set")
        return 0
    msg_id = _tg.send_reply_card(entry, bot_token=token, chat_id=chat_id)
    if msg_id:
        _log(f"  Telegram quote card sent: message_id={msg_id}")
    else:
        _log("  Telegram quote card returned no message_id")
    return msg_id


def _format_repost_notice(entry: dict) -> str:
    status = entry.get("status")
    action = "Autonomous plain repost posted." if status == "posted" else "Autonomous plain repost failed."
    log_path = os.environ.get("BTCMIND_HOME_SCOUT_RUN_LOG", "")
    lines = [
        "BTCMind Home / For You scout",
        "",
        action,
        f"Author: @{entry.get('target', '')}",
        f"URL: {entry.get('target_url', '')}",
        f"Likes/replies/reposts/age: {entry.get('post_likes', 0)} likes, {entry.get('post_replies', 0)} replies, {entry.get('post_reposts', 0)} reposts, {entry.get('post_age_min', '?')}m",
        f"Safety: {entry.get('safety_reason') or 'passed BTCMind autonomy safety gate'}",
    ]
    if entry.get("error"):
        lines.append(f"Error: {entry['error']}")
    if log_path:
        lines.append(f"Log: {log_path}")
    return "\n".join(lines)


def _send_repost_notice(cfg: dict, entry: dict) -> int:
    token, chat_id = _telegram_credentials(cfg)
    if not token or not chat_id:
        _log("  Telegram repost notice skipped: configured env vars are not set")
        return 0
    msg_id = _tg.send_plain_text(_format_repost_notice(entry), bot_token=token, chat_id=chat_id)
    if msg_id:
        _log(f"  Telegram repost notice sent: message_id={msg_id}")
    else:
        _log("  Telegram repost notice returned no message_id")
    return msg_id


def _append_seen_action(seen: dict, row_id: str, action: str):
    seen[row_id] = {"action": action, "at": datetime.now().isoformat()}


def _recent_seen(seen: dict, row_id: str, dedup_hours: int) -> bool:
    val = seen.get(row_id)
    if not val:
        return False
    when = val.get("at") if isinstance(val, dict) else val
    try:
        dt = datetime.fromisoformat(str(when))
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < dedup_hours * 3600


def route_candidates(cfg: dict, rows: list[dict], args, report: dict):
    hcfg = cfg.get("home_timeline_scout") or {}
    qcfg = cfg.get("quote_scout") or {}
    rcfg = cfg.get("repost_scout") or {}
    handle = cfg.get("hunter_handle", "btcmind101")
    max_chars = int((cfg.get("generation") or {}).get("max_post_chars") or 280)
    send_to_tg = bool(hcfg.get("send_to_telegram", True))
    dedup_h = int(hcfg.get("dedup_window_hours") or rcfg.get("dedup_window_hours") or 72)

    home_seen = _load_json(HOME_SEEN_PATH, {})
    repost_seen = _load_json(REPOST_SEEN_PATH, {})

    quote_daily_cap = int(hcfg.get("daily_quote_draft_cap") or qcfg.get("daily_draft_cap") or 3)
    max_quote_run = int(args.max_quote_drafts or hcfg.get("max_quote_drafts_per_run") or qcfg.get("max_drafts_per_run") or 2)
    if quote_daily_cap:
        max_quote_run = min(max_quote_run, max(0, quote_daily_cap - _daily_count(home_seen, "quote_drafts")))

    repost_daily_cap = int(hcfg.get("daily_repost_cap") or rcfg.get("daily_cap") or 3)
    max_reposts = int(args.max_reposts or hcfg.get("max_reposts_per_run") or rcfg.get("max_reposts_per_run") or 1)
    if repost_daily_cap:
        max_reposts = min(max_reposts, max(0, repost_daily_cap - _daily_count(repost_seen, "reposts")))
    min_spacing_h = float(hcfg.get("minimum_hours_between_reposts") or rcfg.get("minimum_hours_between_reposts") or 2)
    spacing_active = _recent_action(repost_seen, "repost", min_spacing_h)

    quoted = 0
    reposted = 0
    quote_candidates = []
    repost_candidates = []

    ordered = sorted(
        rows,
        key=lambda r: (
            {"plain_repost_candidate": 3, "quote_candidate": 2, "skip": 0}.get((r.get("ai_decision") or {}).get("action"), 0),
            int(r.get("crypto_score") or 0),
            int(r.get("likes") or 0) + 2 * int(r.get("reposts") or 0) + 3 * int(r.get("replies") or 0),
        ),
        reverse=True,
    )

    for row in ordered:
        row_id = str(row.get("id") or "")
        if not row_id or _recent_seen(home_seen, row_id, dedup_h):
            continue
        ai_action = (row.get("ai_decision") or {}).get("action") or "skip"
        det_action = (row.get("deterministic_classification") or {}).get("action") or "skip"

        if ai_action == "plain_repost_candidate" and det_action == "plain_repost_candidate" and not _media_dependent(row):
            repost_candidates.append(row)
        elif ai_action == "quote_candidate" or det_action == "quote_candidate":
            block_reason = _home_post_filter_reason(row, "quote_candidate", cfg)
            if block_reason:
                _append_seen_action(home_seen, row_id, f"skip_quote_filter:{block_reason}")
                continue
            quote_candidates.append(row)

    for row in repost_candidates:
        if reposted >= max_reposts or not args.post_reposts or spacing_active or args.dry_run:
            break
        author = str(row.get("author") or "").lstrip("@")
        if _btcmind.author_repost_seen_recently(repost_seen, author, days=7):
            _append_seen_action(home_seen, str(row.get("id")), "skip_author_repost_recent")
            continue
        entry = _entry_base(cfg, row, "repost")
        entry["status"] = "posting"
        safety = _btcmind.check_entry("repost", entry, cfg)
        entry["safety_verdict"] = "approve" if safety.get("ok") else "block"
        entry["safety_reason"] = safety.get("reason", "")
        entry["safety_issues"] = safety.get("issues", [])
        entry["safety_policy"] = "btcmind_autonomy"
        if not safety.get("ok"):
            entry["status"] = "blocked_safety"
            _queue_or_update(entry)
            _append_seen_action(home_seen, str(row.get("id")), "blocked_repost_safety")
            continue
        _queue_or_update(entry)
        try:
            res = _engage.retweet_tweet(int(cfg.get("hunter_port", 10004)), row.get("url", ""), dry_run=False)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            entry["status"] = "posted"
            entry["posted_at"] = _ts()
            _btcmind.mark_author_repost(repost_seen, author)
            _increment_daily(repost_seen, "reposts")
            _mark_action(repost_seen, "repost")
            reposted += 1
            report["reposts_posted"].append(entry)
        else:
            entry["status"] = "post_failed"
            entry["error"] = res.get("error", "")
            report["repost_failures"].append(entry)
        if send_to_tg:
            msg_id = _send_repost_notice(cfg, entry)
            if msg_id:
                entry["telegram_notification_message_id"] = msg_id
        _queue_or_update(entry)
        _append_seen_action(home_seen, str(row.get("id")), entry["status"])

    for row in quote_candidates:
        if quoted >= max_quote_run or not args.queue_quotes or args.dry_run:
            break
        draft = generate_btcmind_quote(row, row.get("ai_decision") or {}, max_chars=max_chars)
        qt = (draft.get("reply") or "").strip()
        if not qt or len(qt) < 24:
            _append_seen_action(home_seen, str(row.get("id")), "skip_empty_quote_draft")
            continue
        entry = _entry_base(cfg, row, "quote")
        entry.update({
            "reply_text": qt,
            "op_summary": draft.get("op_summary", ""),
            "reply_angle": draft.get("reply_angle", ""),
            "status": "pending",
            "needs_human_approval": True,
        })
        safety = _btcmind.check_entry("quote", entry, cfg, require_autonomy=False)
        entry["safety_verdict"] = "approve" if safety.get("ok") else "block"
        entry["safety_reason"] = safety.get("reason", "")
        entry["safety_issues"] = safety.get("issues", [])
        entry["safety_policy"] = "btcmind_autonomy"
        if not safety.get("ok"):
            entry["status"] = "blocked_safety"
            _queue_or_update(entry)
            _append_seen_action(home_seen, str(row.get("id")), "blocked_quote_safety")
            continue
        _queue_or_update(entry)
        if send_to_tg:
            msg_id = _send_quote_card(cfg, entry)
            if msg_id:
                entry["telegram_message_id"] = msg_id
                _queue_or_update(entry)
        quoted += 1
        _increment_daily(home_seen, "quote_drafts")
        _append_seen_action(home_seen, str(row.get("id")), "queued_quote")
        report["quotes_queued"].append(entry)

    _save_json(HOME_SEEN_PATH, home_seen)
    _save_json(REPOST_SEEN_PATH, repost_seen)
    report["candidate_counts"] = {
        "quote_candidates": len(quote_candidates),
        "repost_candidates": len(repost_candidates),
        "quote_slots_remaining_after_run": max_quote_run - quoted,
        "repost_slots_remaining_after_run": max_reposts - reposted,
    }


def write_report(rows: list[dict], report: dict, json_path: str, md_path: str):
    payload = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "scraped_posts": len(rows),
            "with_images": sum(1 for r in rows if r.get("has_image")),
            "with_videos": sum(1 for r in rows if r.get("has_video")),
            "media_dependent": sum(1 for r in rows if _media_dependent(r)),
            "ai_reviewed": sum(1 for r in rows if r.get("ai_decision")),
            "deterministic_actions": {},
            "ai_actions": {},
            "quotes_queued": len(report.get("quotes_queued") or []),
            "reposts_posted": len(report.get("reposts_posted") or []),
            "repost_failures": len(report.get("repost_failures") or []),
        },
        "candidate_counts": report.get("candidate_counts") or {},
        "quotes_queued": report.get("quotes_queued") or [],
        "reposts_posted": report.get("reposts_posted") or [],
        "repost_failures": report.get("repost_failures") or [],
        "rows": rows,
    }
    for r in rows:
        a = (r.get("deterministic_classification") or {}).get("action") or "unknown"
        payload["summary"]["deterministic_actions"][a] = payload["summary"]["deterministic_actions"].get(a, 0) + 1
        ai = (r.get("ai_decision") or {}).get("action")
        if ai:
            payload["summary"]["ai_actions"][ai] = payload["summary"]["ai_actions"].get(ai, 0) + 1
    _save_json(json_path, payload)
    _save_json(os.path.join(STATE_DIR, "btcmind_home_timeline_scout_latest.json"), payload)

    lines = [
        "# BTCMind Home / For You Scout",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Summary",
        "",
        f"- scraped_posts: {len(rows)}",
        f"- with_images: {payload['summary']['with_images']}",
        f"- with_videos: {payload['summary']['with_videos']}",
        f"- media_dependent: {payload['summary']['media_dependent']}",
        f"- ai_reviewed: {payload['summary']['ai_reviewed']}",
        f"- deterministic_actions: {payload['summary']['deterministic_actions']}",
        f"- ai_actions: {payload['summary']['ai_actions']}",
        f"- quotes_queued: {payload['summary']['quotes_queued']}",
        f"- reposts_posted: {payload['summary']['reposts_posted']}",
        f"- repost_failures: {payload['summary']['repost_failures']}",
        "",
        "## AI Quote Candidates",
        "",
    ]
    ai_quotes = [r for r in rows if (r.get("ai_decision") or {}).get("action") == "quote_candidate"]
    if not ai_quotes:
        lines.append("- none")
    for r in ai_quotes[:12]:
        d = r.get("ai_decision") or {}
        lines.append(f"- @{r.get('author')} ({r.get('likes', 0)} likes, video={bool(r.get('has_video'))}, image={bool(r.get('has_image'))}): {r.get('url')}")
        lines.append(f"  - reason: {d.get('reason', '')}")
        if d.get("quote_angle"):
            lines.append(f"  - angle: {d.get('quote_angle')}")
        lines.append(f"  - text: {_compact(r.get('analysis_text') or '', 260)}")
    lines.extend(["", "## AI Plain Repost Candidates", ""])
    ai_reposts = [r for r in rows if (r.get("ai_decision") or {}).get("action") == "plain_repost_candidate"]
    if not ai_reposts:
        lines.append("- none")
    for r in ai_reposts[:12]:
        d = r.get("ai_decision") or {}
        lines.append(f"- @{r.get('author')} ({r.get('likes', 0)} likes): {r.get('url')}")
        lines.append(f"  - reason: {d.get('reason', '')}")
        lines.append(f"  - deterministic: {(r.get('deterministic_classification') or {}).get('action')} / {(r.get('deterministic_classification') or {}).get('reason')}")
        lines.append(f"  - text: {_compact(r.get('analysis_text') or '', 260)}")
    lines.extend(["", "## Top Media-Heavy Posts", ""])
    media_rows = [r for r in rows if r.get("has_video") or r.get("has_image")]
    media_rows.sort(
        key=lambda r: (
            1 if r.get("has_video") else 0,
            int(r.get("crypto_score") or 0),
            int(r.get("likes") or 0) + 2 * int(r.get("reposts") or 0) + 3 * int(r.get("replies") or 0),
        ),
        reverse=True,
    )
    if not media_rows:
        lines.append("- none")
    for r in media_rows[:12]:
        ai = r.get("ai_decision") or {}
        det = r.get("deterministic_classification") or {}
        lines.append(f"- @{r.get('author')} ({r.get('likes', 0)} likes, video={bool(r.get('has_video'))}, image={bool(r.get('has_image'))}, media_dependent={_media_dependent(r)}): {r.get('url')}")
        lines.append(f"  - ai: {ai.get('action', 'not_reviewed')} / {ai.get('reason', '')}")
        lines.append(f"  - deterministic: {det.get('action')} / {det.get('reason')}")
        if r.get("media_text"):
            lines.append(f"  - media: {_compact(r.get('media_text') or '', 260)}")
        lines.append(f"  - text: {_compact(r.get('analysis_text') or '', 260)}")
    lines.extend(["", "## Files", "", f"- JSON: `{json_path}`"])
    with open(md_path, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--target-posts", type=int, default=0)
    ap.add_argument("--max-scrolls", type=int, default=0)
    ap.add_argument("--max-refreshes", type=int, default=0)
    ap.add_argument("--wait", type=float, default=1.15)
    ap.add_argument("--max-ai-candidates", type=int, default=0)
    ap.add_argument("--max-quote-drafts", type=int, default=0)
    ap.add_argument("--max-reposts", type=int, default=0)
    ap.add_argument("--queue-quotes", action="store_true")
    ap.add_argument("--post-reposts", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    args = ap.parse_args()

    cfg = _load_json(args.config, {})
    if not cfg:
        raise SystemExit(f"config not found or invalid: {args.config}")
    hcfg = cfg.get("home_timeline_scout") or {}
    if not hcfg.get("enabled", True):
        _log("home_timeline_scout disabled in config; exit")
        return 0
    policy, _facts = _btcmind.load_policy(cfg)
    if not policy:
        raise SystemExit("BTCMind autonomy policy is missing or disabled")
    port = int(cfg.get("hunter_port", 10004))
    if not _chrome.ping(port):
        raise SystemExit(f"Chrome debug port {port} unavailable")

    target_posts = int(args.target_posts or hcfg.get("target_posts") or 200)
    max_scrolls = int(args.max_scrolls or hcfg.get("max_scrolls") or 120)
    max_refreshes = int(args.max_refreshes or hcfg.get("max_refreshes") or 20)
    max_ai = int(args.max_ai_candidates or hcfg.get("max_ai_candidates") or 30)
    handle = str(cfg.get("hunter_handle") or "btcmind101").lstrip("@")
    if args.post_reposts and (
        (
            (cfg.get("lane_policy") if isinstance(cfg.get("lane_policy"), dict) else {}).get("home_repost")
            or (cfg.get("publish_channel") if isinstance(cfg.get("publish_channel"), dict) else {}).get("repost")
            or "review_only"
        ) != "browser"
        or not bool((cfg.get("browser") or {}).get("allow_autonomous_publish", cfg.get("browser_public_actions_enabled", True)))
    ):
        args.post_reposts = False
        _log("browser public actions disabled in config; home scout will not post reposts")
        args.post_reposts = False

    _log(f"btcmind home scout - target_posts={target_posts}, max_scrolls={max_scrolls}, max_refreshes={max_refreshes}, ai={not args.no_ai}, queue_quotes={args.queue_quotes}, post_reposts={args.post_reposts}, dry_run={args.dry_run}")
    rows = scrape_home(port, policy, target_posts, max_scrolls, max_refreshes, args.wait, handle)
    _log(f"scraped home posts: {len(rows)}")

    if not args.no_ai:
        try:
            decisions = ai_review(rows, max_ai)
            for r in rows:
                if str(r.get("id")) in decisions:
                    decision = decisions[str(r.get("id"))]
                    block_reason = _home_post_filter_reason(r, decision.get("action") or "", cfg)
                    if block_reason:
                        decision = {
                            "action": "skip",
                            "reason": f"home_post_filter:{block_reason}",
                            "quote_angle": "",
                            "risk_flags": [block_reason],
                        }
                    r["ai_decision"] = decision
            _log(f"ai reviewed candidates: {len(decisions)}")
        except Exception as e:
            _log(f"AI review failed: {e}")
    else:
        _log("AI review disabled")

    report = {"quotes_queued": [], "reposts_posted": [], "repost_failures": []}
    route_candidates(cfg, rows, args, report)

    stamp = _slug_ts()
    json_path = os.path.join(STATE_DIR, f"btcmind_home_timeline_scout_{stamp}.json")
    md_path = os.path.join(STATE_DIR, f"btcmind_home_timeline_scout_{stamp}.md")
    write_report(rows, report, json_path, md_path)
    _log(f"wrote JSON report: {json_path}")
    _log(f"wrote markdown report: {md_path}")
    _log(f"done - scraped={len(rows)}, quotes_queued={len(report['quotes_queued'])}, reposts_posted={len(report['reposts_posted'])}, repost_failures={len(report['repost_failures'])}")
    print(json.dumps({
        "scraped_posts": len(rows),
        "quotes_queued": len(report["quotes_queued"]),
        "reposts_posted": len(report["reposts_posted"]),
        "repost_failures": len(report["repost_failures"]),
        "json_report": json_path,
        "md_report": md_path,
    }, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
