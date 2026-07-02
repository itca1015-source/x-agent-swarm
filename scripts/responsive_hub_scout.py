"""Responsive hub scout for the X growth retrieval-map sprint.

Finds candidate accounts in the account's target identity, then scores whether they
are likely to create useful candidate-set entry:

- OP replies to commenters
- non-famous replies get visible engagement
- audience overlaps with Hunter's target cluster
- account posts often enough to test
- threads are not too crowded

Outputs state/retrieval_map_<handle>.json and state/retrieval_map_<handle>.csv.
Intended to run as a one-shot Multica task, not a launchd daemon.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
import login as _login
from lock import chrome_lock


DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "GuoHunter95258", "engage_config.json")
STATE_DIR = os.path.join(ROOT_DIR, "state")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "responsive_hub_scout")

HUNTER_HANDLE = "GuoHunter95258"
HUNTER_PORT = 10000

HUNTER_IDENTITY_TERMS = [
    "ai agent", "agents", "agentic", "gtm", "go-to-market", "automation",
    "workflow", "mcp", "claude code", "coding agent", "browser agent",
    "sales", "marketing", "distribution", "operator", "founder",
]

VOC_IDENTITY_TERMS = [
    "amazon", "fba", "seller", "reviews", "review", "customer reviews",
    "product reviews", "listing", "ppc", "seller central", "private label",
    "shopify", "ecommerce", "dtc", "retention", "returns", "cro", "aov",
    "buyer", "customer feedback", "conversion", "product feedback",
]

VOC_PRIORITY_KEYWORDS = [
    '"Amazon seller"',
    '"Amazon reviews"',
    '"Amazon FBA"',
    '"private label" Amazon',
    '"Amazon listing"',
    '"seller central"',
    '"product reviews"',
    '"customer reviews"',
    '"ecommerce founder"',
    '"Shopify" store',
]

HUNTER_PRIORITY_KEYWORDS = [
    '"AI agent" GTM',
    '"GTM agent"',
    '"agentic marketing"',
    '"marketing automation" AI',
    '"Claude Code" workflow',
    '"MCP" agent workflow',
    '"AI SDR"',
    '"content distribution" AI',
    '"browser agent"',
    '"agentic workflow"',
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _parse_count(s: str) -> int:
    if not s:
        return 0
    s = str(s).strip().replace(",", "").upper()
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


def _age_minutes(dt_str: str) -> int:
    if not dt_str:
        return 99999
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() / 60))
    except Exception:
        return 99999


def _ensure_chrome(port: int, handle: str, relaunch: bool = False) -> bool:
    if _chrome.ping(port):
        return True
    if not relaunch:
        _log(f"chrome on port {port} ({handle}) down; fail-fast because relaunch is disabled")
        return False
    _log(f"chrome on port {port} ({handle}) down; relaunching")
    profile_dir = os.path.join(ROOT_DIR, "chrome-profiles", handle)
    if not os.path.exists(profile_dir):
        _log(f"  no profile dir at {profile_dir}")
        return False
    try:
        _login.launch_chrome(port, profile_dir)
        _login.ensure_page_tab(port)
        return _chrome.ping(port)
    except Exception as e:
        _log(f"  relaunch failed: {e}")
        return False


SEARCH_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el) {
        try {
            var head = (el.innerText || '').slice(0, 300);
            if (/Promoted/i.test(head)) return;
            if (/Replying to/i.test(head)) return;
            if (/reposted/i.test(head)) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            var urlEl = el.querySelector('a[href*="/status/"]');
            var url = urlEl ? urlEl.href : '';
            if (!text || !url) return;

            var authorMatch = url.match(/x\.com\/([A-Za-z0-9_]+)\/status\//);
            var author = authorMatch ? authorMatch[1] : '';
            var likeEl = el.querySelector('[data-testid="like"]');
            var replyEl = el.querySelector('[data-testid="reply"]');
            var timeEl = el.querySelector('time');
            out.push({
                author: author,
                url: url,
                text: text.slice(0, 700),
                likes_raw: likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                replies_raw: replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                datetime: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


FOLLOWERS_JS = r"""
(function() {
    function num(s) {
        s = (s || '').replace(/,/g, '').trim();
        var mult = 1;
        if (/[Kk]$/.test(s)) { mult = 1000; s = s.slice(0, -1); }
        if (/[Mm]$/.test(s)) { mult = 1000000; s = s.slice(0, -1); }
        if (/[Bb]$/.test(s)) { mult = 1000000000; s = s.slice(0, -1); }
        var n = parseFloat(s);
        return isNaN(n) ? 0 : Math.round(n * mult);
    }
    var links = document.querySelectorAll('a[href$="/followers"], a[href$="/verified_followers"], a[href*="/followers"]');
    for (var i = 0; i < links.length; i++) {
        var t = (links[i].innerText || links[i].textContent || '');
        var p = links[i].parentElement ? links[i].parentElement.innerText : '';
        var m = (t + ' ' + p).replace(/\s+/g, ' ').match(/([0-9][0-9,.]*[KMB]?)\s*Followers?/i);
        if (m) return String(num(m[1]));
    }
    return '';
})()
"""


PROFILE_POSTS_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el) {
        try {
            var head = (el.innerText || '').slice(0, 350);
            if (/Promoted/i.test(head)) return;
            if (/Replying to/i.test(head)) return;
            if (/reposted/i.test(head)) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            var urlEl = el.querySelector('a[href*="/status/"]');
            var url = urlEl ? urlEl.href : '';
            if (!url || !text) return;

            var replyEl = el.querySelector('[data-testid="reply"]');
            var likeEl = el.querySelector('[data-testid="like"]');
            var rtEl = el.querySelector('[data-testid="retweet"]');
            var timeEl = el.querySelector('time');
            out.push({
                url: url,
                text: text.slice(0, 900),
                replies_raw: replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                likes_raw: likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                retweets_raw: rtEl ? rtEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                datetime: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


THREAD_REPLIES_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el, idx) {
        try {
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            var userArea = el.querySelector('[data-testid="User-Name"]');
            var author = '';
            if (userArea) {
                var links = userArea.querySelectorAll('a[href^="/"]');
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].getAttribute('href');
                    var m = href.match(/^\/([A-Za-z0-9_]+)$/);
                    if (m) { author = m[1]; break; }
                }
            }
            var likeEl = el.querySelector('[data-testid="like"]');
            var urlEl = el.querySelector('a[href*="/status/"]');
            out.push({
                position: idx,
                author: author,
                text: text.slice(0, 500),
                likes_raw: likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                url: urlEl ? urlEl.href : ''
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


def _identity_terms(handle: str) -> list[str]:
    return VOC_IDENTITY_TERMS if handle.lower() == "voc_ai" else HUNTER_IDENTITY_TERMS


def _priority_keywords(handle: str) -> list[str]:
    return VOC_PRIORITY_KEYWORDS if handle.lower() == "voc_ai" else HUNTER_PRIORITY_KEYWORDS


def _output_paths(handle: str) -> tuple[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", handle).lower()
    return (
        os.path.join(STATE_DIR, f"retrieval_map_{safe}.json"),
        os.path.join(STATE_DIR, f"retrieval_map_{safe}.csv"),
    )


def _identity_overlap(texts: list[str], terms: list[str]) -> float:
    blob = " ".join(texts).lower()
    if not blob:
        return 0.0
    hits = sum(1 for term in terms if term in blob)
    return min(1.0, hits / 6.0)


def _keywords_from_config(cfg: dict, handle: str, limit: int) -> list[str]:
    kws = []
    for block in ("keyword_engage", "quote_scout"):
        for kw in (cfg.get(block, {}) or {}).get("keywords", []):
            if kw not in kws:
                kws.append(kw)
    priority = _priority_keywords(handle)
    merged = priority + [k for k in kws if k not in priority]
    return merged[:limit]


def collect_authors(ws, port: int, account_handle: str, cfg: dict, keyword_limit: int, results_per_keyword: int) -> dict:
    authors = {}
    keywords = _keywords_from_config(cfg, account_handle, keyword_limit)
    for kw in keywords:
        for mode in ("live", "top"):
            q = urllib.parse.quote_plus(kw)
            url = f"https://x.com/search?q={q}&src=typed_query&f={mode}"
            _log(f"search {mode}: {kw}")
            with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
                _chrome.navigate(ws, url, wait=4.0)
                time.sleep(1.5)
                _chrome.eval_js(ws, "window.scrollBy(0, 900)")
                time.sleep(1.5)
                raw = _chrome.eval_js(ws, SEARCH_JS)
            try:
                rows = json.loads(raw) if raw else []
            except Exception:
                rows = []
            for row in rows[:results_per_keyword]:
                handle = (row.get("author") or "").lstrip("@")
                if not handle or handle.lower() == account_handle.lower():
                    continue
                likes = _parse_count(row.get("likes_raw", ""))
                if likes < 3:
                    continue
                rec = authors.setdefault(handle, {
                    "handle": handle,
                    "seen_keywords": set(),
                    "sample_urls": [],
                    "sample_texts": [],
                })
                rec["seen_keywords"].add(kw)
                if len(rec["sample_urls"]) < 4:
                    rec["sample_urls"].append(row.get("url", ""))
                    rec["sample_texts"].append(row.get("text", ""))
            time.sleep(random.uniform(2.0, 4.0))

    for seed in cfg.get("target_accounts", []):
        h = seed.lstrip("@")
        rec = authors.setdefault(h, {"handle": h, "seen_keywords": set(), "sample_urls": [], "sample_texts": []})
        rec["seen_keywords"].add("existing_target")

    return authors


def authors_from_handles(handles: list[str]) -> dict:
    authors = {}
    for raw in handles:
        h = raw.strip().lstrip("@")
        if not h:
            continue
        authors[h] = {
            "handle": h,
            "seen_keywords": {"manual_seed"},
            "sample_urls": [],
            "sample_texts": [],
        }
    return authors


def fetch_profile(ws, port: int, handle: str, posts_per_author: int) -> dict:
    with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
        _chrome.navigate(ws, f"https://x.com/{handle}", wait=4.0)
        for _ in range(2):
            _chrome.eval_js(ws, "window.scrollBy(0, 1200)")
            time.sleep(1.5)
        followers_raw = _chrome.eval_js(ws, FOLLOWERS_JS)
        raw = _chrome.eval_js(ws, PROFILE_POSTS_JS)
    try:
        posts = json.loads(raw) if raw else []
    except Exception:
        posts = []
    out = []
    seen = set()
    for p in posts:
        url = p.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        p["replies"] = _parse_count(p.get("replies_raw", ""))
        p["likes"] = _parse_count(p.get("likes_raw", ""))
        p["retweets"] = _parse_count(p.get("retweets_raw", ""))
        p["age_minutes"] = _age_minutes(p.get("datetime", ""))
        out.append(p)
        if len(out) >= posts_per_author:
            break
    return {
        "followers": int(followers_raw or 0),
        "posts": out,
    }


def inspect_thread(ws, port: int, handle: str, post_url: str, scrolls: int) -> dict:
    with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
        _chrome.navigate(ws, post_url, wait=4.0)
        for _ in range(scrolls):
            _chrome.eval_js(ws, "window.scrollBy(0, 1300)")
            time.sleep(1.5)
        raw = _chrome.eval_js(ws, THREAD_REPLIES_JS)
    try:
        cards = json.loads(raw) if raw else []
    except Exception:
        cards = []

    handle_l = handle.lower()
    replies = []
    op_response_events = 0
    for idx, c in enumerate(cards):
        author = (c.get("author") or "").lower()
        likes = _parse_count(c.get("likes_raw", ""))
        if idx > 0 and author == handle_l:
            op_response_events += 1
        if idx > 0 and author and author != handle_l:
            replies.append({"author": author, "likes": likes, "text": c.get("text", "")})

    visible = [r for r in replies if r["likes"] >= 1]
    strong = [r for r in replies if r["likes"] >= 3]
    return {
        "reply_cards": len(replies),
        "op_response_events": op_response_events,
        "has_op_response": op_response_events > 0,
        "visible_reply_count": len(visible),
        "strong_reply_count": len(strong),
        "max_reply_likes": max([r["likes"] for r in replies], default=0),
    }


def _crowding_penalty(avg_replies: float) -> float:
    if avg_replies <= 80:
        return 0.0
    if avg_replies <= 150:
        return 0.08
    if avg_replies <= 300:
        return 0.18
    return 0.30


def _bucket(followers: int, score: float) -> str:
    if followers >= 100_000:
        return "reach_hub"
    if followers >= 5_000:
        return "responsive_mid_size_hub" if score >= 0.45 else "mid_size_watchlist"
    return "small_high_intent_operator" if score >= 0.35 else "small_watchlist"


def score_author(author: dict, profile: dict, thread_stats: list[dict], account_handle: str) -> dict:
    posts = profile.get("posts", [])
    followers = profile.get("followers", 0)
    avg_replies = sum(p.get("replies", 0) for p in posts) / len(posts) if posts else 0
    avg_likes = sum(p.get("likes", 0) for p in posts) / len(posts) if posts else 0
    recent_posts = sum(1 for p in posts if p.get("age_minutes", 99999) <= 48 * 60)

    inspected = len(thread_stats)
    op_reply_rate = sum(1 for s in thread_stats if s.get("has_op_response")) / inspected if inspected else 0
    visible_rates = [
        min(1.0, s.get("visible_reply_count", 0) / max(1, s.get("reply_cards", 0)))
        for s in thread_stats
    ]
    small_visibility = sum(visible_rates) / len(visible_rates) if visible_rates else 0
    audience_overlap = _identity_overlap(
        author.get("sample_texts", []) + [p.get("text", "") for p in posts],
        _identity_terms(account_handle),
    )
    post_frequency = min(1.0, recent_posts / 3.0)
    conversation_quality = min(1.0, (avg_replies / 30.0) * 0.65 + (avg_likes / 100.0) * 0.35)
    penalty = _crowding_penalty(avg_replies)

    responsive_score = (
        0.30 * op_reply_rate
        + 0.25 * audience_overlap
        + 0.20 * small_visibility
        + 0.15 * post_frequency
        + 0.10 * conversation_quality
        - penalty
    )
    responsive_score = max(0.0, min(1.0, responsive_score))

    return {
        "handle": author["handle"],
        "followers": followers,
        "bucket": _bucket(followers, responsive_score),
        "responsive_score": round(responsive_score, 4),
        "op_reply_rate": round(op_reply_rate, 4),
        "audience_overlap": round(audience_overlap, 4),
        "small_account_reply_visibility": round(small_visibility, 4),
        "post_frequency": round(post_frequency, 4),
        "conversation_quality": round(conversation_quality, 4),
        "crowding_penalty": round(penalty, 4),
        "posts_seen": len(posts),
        "threads_inspected": inspected,
        "avg_replies": round(avg_replies, 2),
        "avg_likes": round(avg_likes, 2),
        "recent_posts_48h": recent_posts,
        "max_reply_likes_seen": max([s.get("max_reply_likes", 0) for s in thread_stats], default=0),
        "seen_keywords": sorted(author.get("seen_keywords", [])),
        "sample_urls": author.get("sample_urls", []),
    }


def write_outputs(rows: list[dict], summary: dict, output_json: str, output_csv: str):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {"generated_at": _ts(), "summary": summary, "accounts": rows}
    with open(output_json, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(output_csv, "w", newline="") as f:
        cols = [
            "handle", "bucket", "responsive_score", "followers", "op_reply_rate",
            "audience_overlap", "small_account_reply_visibility", "post_frequency",
            "conversation_quality", "crowding_penalty", "posts_seen",
            "threads_inspected", "avg_replies", "avg_likes", "recent_posts_48h",
            "max_reply_likes_seen", "seen_keywords",
        ]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            out = {k: row.get(k, "") for k in cols}
            out["seen_keywords"] = " | ".join(row.get("seen_keywords", []))
            w.writerow(out)
    _log(f"wrote {output_json}")
    _log(f"wrote {output_csv}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--max-authors", type=int, default=200)
    p.add_argument("--keyword-limit", type=int, default=14)
    p.add_argument("--results-per-keyword", type=int, default=12)
    p.add_argument("--posts-per-author", type=int, default=5)
    p.add_argument("--threads-per-author", type=int, default=3)
    p.add_argument("--thread-scrolls", type=int, default=2)
    p.add_argument("--allow-relaunch", action="store_true",
                   help="allow launching Chrome if the account port is down")
    p.add_argument("--approval-token", default="",
                   help="required for official-scale runs; use TEST_ONLY for small samples")
    p.add_argument("--handles", default="",
                   help="comma-separated handles to inspect directly, skipping search")
    args = p.parse_args()

    cfg = _load_json(args.config, {})
    port = int(cfg.get("hunter_port", HUNTER_PORT))
    handle = cfg.get("hunter_handle", HUNTER_HANDLE)
    output_json, output_csv = _output_paths(handle)

    direct_handles = [h for h in args.handles.split(",") if h.strip()]
    max_test_authors = 6 if direct_handles else 5
    is_small_test = (
        args.approval_token == "TEST_ONLY"
        and args.max_authors <= max_test_authors
        and args.keyword_limit <= 3
        and args.results_per_keyword <= 5
        and args.posts_per_author <= 3
        and args.threads_per_author <= 1
        and args.thread_scrolls <= 1
    )
    is_official = args.max_authors > max_test_authors or args.keyword_limit > 3 or args.results_per_keyword > 5
    if is_official and args.approval_token != "APPROVED_OFFICIAL_RUN":
        raise SystemExit(
            "official-scale run blocked: first run a TEST_ONLY sample and get human approval"
        )
    if not is_small_test and args.approval_token != "APPROVED_OFFICIAL_RUN":
        raise SystemExit("missing approval token: use TEST_ONLY for sample runs")

    if not _ensure_chrome(port, handle, relaunch=args.allow_relaunch):
        raise SystemExit(f"{handle} Chrome unavailable")

    ws = _chrome.connect(port)
    rows = []
    try:
        if args.handles:
            authors = authors_from_handles(args.handles.split(","))
            _log(f"manual seed handles: {', '.join(authors.keys())}")
        else:
            authors = collect_authors(ws, port, handle, cfg, args.keyword_limit, args.results_per_keyword)
        _log(f"collected {len(authors)} candidate authors")
        handles = list(authors.keys())[:args.max_authors]
        for i, h in enumerate(handles, 1):
            _log(f"[{i}/{len(handles)}] inspect @{h}")
            try:
                profile = fetch_profile(ws, port, h, args.posts_per_author)
            except Exception as e:
                _log(f"  profile error: {e}")
                continue
            posts = profile.get("posts", [])
            thread_stats = []
            for post in sorted(posts, key=lambda p: p.get("replies", 0), reverse=True)[:args.threads_per_author]:
                if post.get("replies", 0) < 2:
                    continue
                try:
                    thread_stats.append(inspect_thread(ws, port, h, post["url"], args.thread_scrolls))
                except Exception as e:
                    _log(f"  thread error: {e}")
                time.sleep(random.uniform(1.5, 3.0))
            if not posts:
                continue
            rows.append(score_author(authors[h], profile, thread_stats, handle))
            time.sleep(random.uniform(2.0, 4.0))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    rows.sort(key=lambda r: (r["responsive_score"], r["op_reply_rate"], r["audience_overlap"]), reverse=True)
    summary = {
        "accounts_scored": len(rows),
        "accounts_with_op_reply_behavior": sum(1 for r in rows if r["op_reply_rate"] > 0),
        "priority_test_pool": [r["handle"] for r in rows[:20]],
        "reach_hubs": [r["handle"] for r in rows if r["bucket"] == "reach_hub"][:20],
        "responsive_mid_size_hubs": [r["handle"] for r in rows if r["bucket"] == "responsive_mid_size_hub"][:60],
        "small_high_intent_operators": [r["handle"] for r in rows if r["bucket"] == "small_high_intent_operator"][:120],
    }
    write_outputs(rows, summary, output_json, output_csv)

    _log("top 20 priority test pool:")
    for r in rows[:20]:
        _log(
            f"  @{r['handle']:<20} score={r['responsive_score']:.2f} "
            f"op={r['op_reply_rate']:.2f} overlap={r['audience_overlap']:.2f} "
            f"small_vis={r['small_account_reply_visibility']:.2f} bucket={r['bucket']}"
        )


if __name__ == "__main__":
    main()
