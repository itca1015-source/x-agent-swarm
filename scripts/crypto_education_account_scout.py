#!/usr/bin/env python3
"""Read-only X scout for popular educational crypto accounts."""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock


STATE_DIR = os.path.join(ROOT_DIR, "state")

DEFAULT_QUERIES = [
    '"crypto education"',
    '"learn crypto"',
    '"crypto explained"',
    '"DeFi explained"',
    '"onchain analysis"',
    '"crypto research"',
    '"Bitcoin education"',
    '"stablecoin analysis"',
    '"blockchain education"',
    '"web3 security"',
    '"smart contract security"',
    '"crypto beginners"',
]

SEED_HANDLES = [
    "CryptoCred",
    "DefiIgnas",
    "TheDeFinvestor",
    "Route2FI",
    "EmperorBTC",
    "OnchainLens",
    "lookonchain",
    "DefiLlama",
    "tokenterminal",
    "nansen_ai",
    "glassnode",
    "Dune",
    "MessariCrypto",
    "BanklessHQ",
]

BLOCKED_HANDLES = {
    "x",
    "premium",
    "verified",
    "i",
}

EDU_TERMS = [
    "education",
    "educational",
    "learn",
    "explained",
    "guide",
    "tutorial",
    "course",
    "research",
    "analysis",
    "analytics",
    "onchain",
    "on-chain",
    "defi",
    "bitcoin",
    "ethereum",
    "blockchain",
    "smart contract",
    "security",
    "wallet",
    "stablecoin",
    "tokenomics",
    "beginner",
    "thread",
]

SPAM_TERMS = [
    "100x",
    "airdrop hunter",
    "giveaway",
    "signals",
    "vip",
    "dm for",
    "pump",
    "gem calls",
]


def parse_count(raw: str) -> int:
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


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


SEARCH_JS = r"""
(function() {
    function parseMetric(el, testid) {
        var node = el.querySelector('[data-testid="' + testid + '"]');
        if (!node) return '';
        var label = node.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+(Replies|Reply|reposts?|likes?|bookmarks?|views?)/i);
        if (m) return m[1] + (m[2] || '');
        return (node.innerText || '').replace(/[^0-9KMBkmb.,]/g, '');
    }
    var out = [];
    document.querySelectorAll('article[data-testid="tweet"]').forEach(function(el) {
        try {
            var all = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
            if (!all || /Promoted/i.test(all)) return;
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            var timeEl = el.querySelector('time');
            var link = timeEl ? timeEl.closest('a[href*="/status/"]') : null;
            if (!link) link = el.querySelector('a[href*="/status/"]');
            if (!link || !link.href) return;
            var m = link.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
            if (!m) return;
            out.push({
                author: m[1],
                id: m[2],
                url: 'https://x.com/' + m[1] + '/status/' + m[2],
                text: text.slice(0, 900),
                all_text: all.slice(0, 1500),
                likes_raw: parseMetric(el, 'like'),
                replies_raw: parseMetric(el, 'reply'),
                reposts_raw: parseMetric(el, 'retweet'),
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


PROFILE_JS = r"""
(function() {
    function compact(el) {
        return (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
    }
    function parseCount(s) {
        s = (s || '').replace(/,/g, '').trim();
        var mult = 1;
        if (/[Kk]$/.test(s)) { mult = 1000; s = s.slice(0, -1); }
        else if (/[Mm]$/.test(s)) { mult = 1000000; s = s.slice(0, -1); }
        else if (/[Bb]$/.test(s)) { mult = 1000000000; s = s.slice(0, -1); }
        var n = parseFloat(s);
        return isNaN(n) ? 0 : Math.round(n * mult);
    }
    function metric(root, testid) {
        var node = root.querySelector('[data-testid="' + testid + '"]');
        if (!node) return '';
        var label = node.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+(Replies|Reply|reposts?|likes?|bookmarks?|views?)/i);
        if (m) return m[1] + (m[2] || '');
        return (node.innerText || '').replace(/[^0-9KMBkmb.,]/g, '');
    }
    var followers = 0;
    var links = document.querySelectorAll('a[href$="/followers"], a[href$="/verified_followers"], a[href*="/followers"]');
    for (var i = 0; i < links.length; i++) {
        var txt = (links[i].innerText || links[i].textContent || '');
        var parent = links[i].parentElement ? links[i].parentElement.innerText : '';
        var m = (txt + ' ' + parent).replace(/\s+/g, ' ').match(/([0-9][0-9,.]*[KMBkmb]?)\s*Followers?/i);
        if (m) { followers = parseCount(m[1]); break; }
    }
    var following = 0;
    for (var j = 0; j < links.length; j++) {
        var href = links[j].getAttribute('href') || '';
        if (!/\/following$/.test(href)) continue;
        var ft = links[j].innerText || links[j].textContent || '';
        var fm = ft.replace(/\s+/g, ' ').match(/([0-9][0-9,.]*[KMBkmb]?)/);
        if (fm) { following = parseCount(fm[1]); break; }
    }
    var name = compact(document.querySelector('[data-testid="UserName"]'));
    var bio = compact(document.querySelector('[data-testid="UserDescription"]'));
    var location = compact(document.querySelector('[data-testid="UserLocation"]'));
    var body = compact(document.body);
    var joined = '';
    var jm = body.match(/Joined\s+[A-Za-z]+\s+\d{4}|Joined\s+\d{4}/i);
    if (jm) joined = jm[0];
    var posts = [];
    document.querySelectorAll('article[data-testid="tweet"]').forEach(function(el) {
        try {
            var all = compact(el);
            if (!all || /Promoted/i.test(all)) return;
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            var timeEl = el.querySelector('time');
            var link = timeEl ? timeEl.closest('a[href*="/status/"]') : null;
            if (!link) link = el.querySelector('a[href*="/status/"]');
            if (!link || !link.href) return;
            var m = link.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
            if (!m) return;
            posts.push({
                author: m[1],
                id: m[2],
                url: 'https://x.com/' + m[1] + '/status/' + m[2],
                text: text.slice(0, 900),
                all_text: all.slice(0, 1400),
                likes_raw: metric(el, 'like'),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
            });
        } catch(e) {}
    });
    return JSON.stringify({
        name: name,
        bio: bio,
        location: location,
        followers: followers,
        following: following,
        joined: joined,
        posts: posts.slice(0, 12)
    });
})()
"""


def read_json(raw: str, default):
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def collect_search(ws, port: int, query: str, mode: str, scrolls: int, wait: float) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    url = f"https://x.com/search?q={q}&src=typed_query&f={mode}"
    print(f"[search] {mode}: {query}", file=sys.stderr, flush=True)
    with chrome_lock(port, timeout=1800):
        chrome.navigate(ws, url, wait=wait + 1.5)
        chrome.set_viewport(ws, 1400, 2200)
        rows = []
        seen = set()
        for idx in range(scrolls + 1):
            batch = read_json(chrome.eval_js(ws, SEARCH_JS), [])
            gained = 0
            for row in batch:
                key = row.get("id")
                if not key or key in seen:
                    continue
                seen.add(key)
                row["query"] = query
                row["mode"] = mode
                row["likes"] = parse_count(row.get("likes_raw", ""))
                row["replies"] = parse_count(row.get("replies_raw", ""))
                row["reposts"] = parse_count(row.get("reposts_raw", ""))
                rows.append(row)
                gained += 1
            print(f"[search]   scroll={idx} rows={len(rows)} new={gained}", file=sys.stderr, flush=True)
            chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.88))")
            time.sleep(wait)
    return rows


def fetch_profile(ws, port: int, handle: str, scrolls: int, wait: float) -> dict:
    with chrome_lock(port, timeout=1800):
        chrome.navigate(ws, f"https://x.com/{handle}", wait=wait + 1.5)
        chrome.set_viewport(ws, 1400, 2200)
        for _ in range(scrolls):
            chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.75))")
            time.sleep(wait)
        data = read_json(chrome.eval_js(ws, PROFILE_JS), {})
    posts = []
    seen = set()
    for post in data.get("posts") or []:
        if (post.get("author") or "").lower() != handle.lower():
            continue
        if post.get("id") in seen:
            continue
        seen.add(post.get("id"))
        post["likes"] = parse_count(post.get("likes_raw", ""))
        post["replies"] = parse_count(post.get("replies_raw", ""))
        post["reposts"] = parse_count(post.get("reposts_raw", ""))
        posts.append(post)
    data["posts"] = posts
    data["handle"] = handle
    return data


def education_score(profile: dict, discovery: dict) -> tuple[int, list[str]]:
    texts = [
        profile.get("bio", ""),
        profile.get("name", ""),
        " ".join(discovery.get("sample_texts", [])),
        " ".join(p.get("text", "") for p in profile.get("posts", [])),
    ]
    blob = " ".join(texts).lower()
    hits = sorted({term for term in EDU_TERMS if term in blob})
    spam_hits = sorted({term for term in SPAM_TERMS if term in blob})
    score = len(hits) * 8 + min(24, len(discovery.get("queries", [])) * 4)
    score -= len(spam_hits) * 12
    return score, hits


def rank_row(profile: dict, discovery: dict) -> dict:
    posts = profile.get("posts") or []
    followers = int(profile.get("followers") or 0)
    avg_likes = sum(p.get("likes", 0) for p in posts) / len(posts) if posts else 0
    avg_replies = sum(p.get("replies", 0) for p in posts) / len(posts) if posts else 0
    avg_reposts = sum(p.get("reposts", 0) for p in posts) / len(posts) if posts else 0
    edu_score, edu_hits = education_score(profile, discovery)
    reach_score = min(60, followers / 50_000)
    engagement_score = min(45, avg_likes / 15) + min(20, avg_replies / 5) + min(20, avg_reposts / 3)
    discovery_score = min(30, discovery.get("search_hits", 0) * 3 + discovery.get("top_likes", 0) / 25)
    total = round(edu_score + reach_score + engagement_score + discovery_score, 2)
    sample_posts = sorted(posts, key=lambda p: (p.get("likes", 0), p.get("reposts", 0)), reverse=True)[:2]
    return {
        "handle": profile["handle"],
        "profile_url": f"https://x.com/{profile['handle']}",
        "name": profile.get("name", ""),
        "bio": profile.get("bio", ""),
        "followers": followers,
        "followers_fmt": fmt_count(followers),
        "joined": profile.get("joined", ""),
        "posts_seen": len(posts),
        "avg_likes": round(avg_likes, 1),
        "avg_replies": round(avg_replies, 1),
        "avg_reposts": round(avg_reposts, 1),
        "search_hits": discovery.get("search_hits", 0),
        "queries": sorted(discovery.get("queries", [])),
        "education_terms": edu_hits,
        "score": total,
        "sample_posts": [
            {
                "url": p.get("url", ""),
                "text": compact_text(p.get("text", ""))[:240],
                "likes": p.get("likes", 0),
                "reposts": p.get("reposts", 0),
                "replies": p.get("replies", 0),
            }
            for p in sample_posts
        ],
    }


def write_outputs(rows: list[dict], all_search_rows: list[dict], output_json: str, output_csv: str):
    os.makedirs(os.path.dirname(output_json), exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "accounts_scored": len(rows),
            "search_posts_seen": len(all_search_rows),
        },
        "accounts": rows,
        "search_rows": all_search_rows,
    }
    with open(output_json, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(output_csv, "w", newline="") as f:
        fields = [
            "score",
            "handle",
            "followers_fmt",
            "followers",
            "avg_likes",
            "avg_replies",
            "avg_reposts",
            "search_hits",
            "education_terms",
            "queries",
            "profile_url",
            "bio",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            out = {k: row.get(k, "") for k in fields}
            out["education_terms"] = " | ".join(row.get("education_terms", []))
            out["queries"] = " | ".join(row.get("queries", []))
            w.writerow(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=10004)
    p.add_argument("--max-profiles", type=int, default=35)
    p.add_argument("--search-scrolls", type=int, default=2)
    p.add_argument("--profile-scrolls", type=int, default=2)
    p.add_argument("--wait", type=float, default=1.3)
    p.add_argument("--queries", default="")
    p.add_argument("--handles", default="", help="comma-separated handles to profile directly after/without search")
    p.add_argument("--skip-search", action="store_true")
    p.add_argument("--include-live", action="store_true")
    p.add_argument("--output-json", default=os.path.join(STATE_DIR, "crypto_education_accounts_2026-06-08.json"))
    p.add_argument("--output-csv", default=os.path.join(STATE_DIR, "crypto_education_accounts_2026-06-08.csv"))
    args = p.parse_args()

    queries = [q.strip() for q in args.queries.split("|") if q.strip()] or DEFAULT_QUERIES
    modes = ["top", "live"] if args.include_live else ["top"]

    ws = chrome.connect(args.port, timeout=60)
    all_search_rows = []
    discoveries = defaultdict(lambda: {
        "search_hits": 0,
        "top_likes": 0,
        "queries": set(),
        "sample_texts": [],
        "sample_urls": [],
    })
    try:
        if not args.skip_search:
            for query in queries:
                for mode in modes:
                    rows = collect_search(ws, args.port, query, mode, args.search_scrolls, args.wait)
                    all_search_rows.extend(rows)
                    for row in rows:
                        handle = (row.get("author") or "").strip().lstrip("@")
                        if not handle or handle.lower() in BLOCKED_HANDLES:
                            continue
                        d = discoveries[handle]
                        d["search_hits"] += 1
                        d["top_likes"] = max(d["top_likes"], row.get("likes", 0))
                        d["queries"].add(query)
                        if len(d["sample_texts"]) < 5:
                            d["sample_texts"].append(row.get("text") or row.get("all_text") or "")
                        if row.get("url") and len(d["sample_urls"]) < 5:
                            d["sample_urls"].append(row["url"])
                    time.sleep(random.uniform(0.8, 1.8))

        direct_handles = [h.strip().lstrip("@") for h in args.handles.split(",") if h.strip()]
        seed_handles = direct_handles or SEED_HANDLES
        for handle in seed_handles:
            d = discoveries[handle]
            d["queries"].add("direct_seed" if direct_handles else "seed")
            d["search_hits"] += 1

        ordered = sorted(
            discoveries.items(),
            key=lambda kv: (kv[1]["search_hits"], kv[1]["top_likes"]),
            reverse=True,
        )[: args.max_profiles]

        rows = []
        for idx, (handle, discovery) in enumerate(ordered, 1):
            discovery["queries"] = set(discovery["queries"])
            print(f"[profile] {idx}/{len(ordered)} @{handle}", file=sys.stderr, flush=True)
            try:
                profile = fetch_profile(ws, args.port, handle, args.profile_scrolls, args.wait)
            except Exception as e:
                print(f"[profile]   error @{handle}: {e}", file=sys.stderr, flush=True)
                continue
            serial_discovery = {
                "search_hits": discovery["search_hits"],
                "top_likes": discovery["top_likes"],
                "queries": sorted(discovery["queries"]),
                "sample_texts": discovery["sample_texts"],
                "sample_urls": discovery["sample_urls"],
            }
            row = rank_row(profile, serial_discovery)
            if row["followers"] <= 0:
                continue
            if row["score"] < 30 and row["followers"] < 25_000:
                continue
            rows.append(row)
            time.sleep(random.uniform(1.0, 2.5))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    rows.sort(key=lambda r: (r["score"], r["followers"], r["avg_likes"]), reverse=True)
    write_outputs(rows, all_search_rows, args.output_json, args.output_csv)
    print(json.dumps({
        "output_json": args.output_json,
        "output_csv": args.output_csv,
        "accounts": len(rows),
        "search_posts_seen": len(all_search_rows),
        "top_handles": [r["handle"] for r in rows[:12]],
    }, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
