#!/usr/bin/env python3
"""Collect a Nansen-specific X sample for reverse-engineering autopilots.

Read-only CDP scraper. It collects profile, original, reply, quote, and
community-mention search views, preserving source/category labels so analysis
can map examples back to autopilot requirements.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock


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


EXTRACT_JS = r"""
(function() {
    function metric(el, testid) {
        var btn = el.querySelector('[data-testid="' + testid + '"]');
        if (!btn) return '';
        var label = btn.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+(Replies|Reply|reposts?|likes?|bookmarks?|views?)/i);
        if (m) return m[1] + (m[2] || '');
        return (btn.innerText || '').replace(/[^0-9KMB.,]/g, '');
    }
    function visibleText(el) {
        return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    }
    function attrs(el, sel, attr) {
        return Array.from(el.querySelectorAll(sel)).map(function(a) {
            return a.getAttribute(attr) || '';
        }).filter(Boolean);
    }
    var arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    var out = [];
    arts.forEach(function(el) {
        try {
            var all = visibleText(el);
            if (/Promoted/i.test(all)) return;
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            if (!text) return;

            var links = Array.from(el.querySelectorAll('a[href*="/status/"]'));
            var url = '';
            var id = '';
            for (var i = 0; i < links.length; i++) {
                var m = links[i].href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
                if (m) { url = links[i].href.split('?')[0]; id = m[2]; break; }
            }
            if (!id) return;
            var authorMatch = url.match(/x\.com\/([A-Za-z0-9_]+)\/status\//);
            var author = authorMatch ? authorMatch[1] : '';
            var timeEl = el.querySelector('time');
            var iso = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
            var hrefs = attrs(el, 'a[href]', 'href').map(function(h) { return h.split('?')[0]; });

            out.push({
                id: id,
                url: url,
                author: author,
                iso: iso,
                text: text,
                all_text: all.slice(0, 2600),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                likes_raw: metric(el, 'like'),
                bookmarks_raw: metric(el, 'bookmark'),
                views_raw: metric(el, 'analytics'),
                is_reply: /Replying to/i.test(all),
                is_repost_context: /reposted/i.test(all),
                is_pinned: /Pinned/i.test(all),
                has_image: !!el.querySelector('[data-testid="tweetPhoto"], img[src*="twimg.com/media"]'),
                has_video: !!el.querySelector('video'),
                has_card: !!el.querySelector('[data-testid="card.wrapper"]'),
                has_gif: /GIF/i.test(all),
                hrefs: Array.from(new Set(hrefs)).slice(0, 20)
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,20})")
CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{1,12}")


def enrich(records, source, source_kind, handle):
    out = []
    handle_l = handle.lower()
    for r in records:
        r["source"] = source
        r["source_kind"] = source_kind
        r["likes"] = parse_count(r.get("likes_raw", ""))
        r["reposts"] = parse_count(r.get("reposts_raw", ""))
        r["replies"] = parse_count(r.get("replies_raw", ""))
        r["bookmarks"] = parse_count(r.get("bookmarks_raw", ""))
        r["views"] = parse_count(r.get("views_raw", ""))
        text = (r.get("text") or "") + " " + (r.get("all_text") or "")
        r["mentions"] = sorted(set(m.lower() for m in MENTION_RE.findall(text)))
        r["cashtags"] = sorted(set(c.upper() for c in CASHTAG_RE.findall(text)))
        r["is_authored_by_handle"] = (r.get("author") or "").lower() == handle_l
        try:
            dt = datetime.fromisoformat((r.get("iso") or "").replace("Z", "+00:00"))
            r["hour_utc"] = dt.hour
            r["weekday_utc"] = dt.strftime("%a")
            r["age_hours"] = round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
        except Exception:
            r["hour_utc"] = None
            r["weekday_utc"] = ""
            r["age_hours"] = None
        out.append(r)
    return out


def extract(ws, source, source_kind, handle):
    raw = chrome.eval_js(ws, EXTRACT_JS)
    try:
        records = json.loads(raw) if raw else []
    except Exception:
        records = []
    return enrich(records, source, source_kind, handle)


def collect_page(ws, port, url, source, source_kind, handle, max_items, max_scrolls, wait):
    with chrome_lock(port):
        chrome.navigate(ws, url, wait=wait)
        chrome.set_viewport(ws, 1400, 1900)
        time.sleep(1.0)
        seen = {}
        idle = 0
        for i in range(max_scrolls):
            before = len(seen)
            for r in extract(ws, source, source_kind, handle):
                seen.setdefault(r.get("id"), r)
            after = len(seen)
            if after >= max_items:
                break
            idle = idle + 1 if after == before else 0
            if idle >= 10:
                break
            chrome.eval_js(ws, "window.scrollBy(0, 1650)")
            time.sleep(1.25)
        for r in extract(ws, source, source_kind, handle):
            seen.setdefault(r.get("id"), r)
    return list(seen.values())[:max_items]


def search_url(query, mode):
    return (
        "https://x.com/search?q="
        + urllib.parse.quote_plus(query)
        + f"&src=typed_query&f={mode}"
    )


def merge_records(groups, handle):
    by_id = {}
    for group_name, rows in groups.items():
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            cur = by_id.setdefault(rid, dict(r))
            cur.setdefault("sources", [])
            if group_name not in cur["sources"]:
                cur["sources"].append(group_name)
            # Prefer profile repost context, because search result cards can lose it.
            if r.get("is_repost_context"):
                cur["is_repost_context"] = True
                cur["source_kind"] = "repost"
            # Keep strongest engagement parse if duplicate cards differ.
            for k in ("likes", "reposts", "replies", "bookmarks", "views"):
                cur[k] = max(cur.get(k) or 0, r.get(k) or 0)
            for k in ("mentions", "cashtags", "hrefs"):
                cur[k] = sorted(set((cur.get(k) or []) + (r.get(k) or [])))

    handle_l = handle.lower()
    for r in by_id.values():
        sources = set(r.get("sources") or [])
        authored = (r.get("author") or "").lower() == handle_l
        if r.get("is_repost_context") and not authored:
            category = "repost"
        elif "quotes_top" in sources or "quotes_live" in sources:
            category = "quote"
        elif "replies_top" in sources or "replies_live" in sources or (authored and r.get("is_reply")):
            category = "reply"
        elif authored:
            category = "original"
        else:
            category = "community_mention"
        r["category"] = category
    return list(by_id.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10004)
    ap.add_argument("--handle", default="nansen_ai")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--scrolls", type=int, default=70)
    ap.add_argument("--wait", type=float, default=5.0)
    ap.add_argument("--out", default=os.path.join(ROOT_DIR, "state", "nansen_reverse_engineering_500.json"))
    args = ap.parse_args()

    handle = args.handle.lstrip("@")
    profile_url = f"https://x.com/{handle}"
    queries = [
        ("profile", "profile", profile_url, args.target, args.scrolls),
        ("originals_top", "original", search_url(f"from:{handle} -filter:replies", "top"), 240, args.scrolls),
        ("originals_live", "original", search_url(f"from:{handle} -filter:replies", "live"), 240, args.scrolls),
        ("replies_top", "reply", search_url(f"from:{handle} filter:replies", "top"), 180, max(35, args.scrolls // 2)),
        ("replies_live", "reply", search_url(f"from:{handle} filter:replies", "live"), 180, max(35, args.scrolls // 2)),
        ("quotes_top", "quote", search_url(f"from:{handle} filter:quote", "top"), 160, max(35, args.scrolls // 2)),
        ("quotes_live", "quote", search_url(f"from:{handle} filter:quote", "live"), 160, max(35, args.scrolls // 2)),
        ("mentions_top", "community_mention", search_url(f"@{handle} -from:{handle}", "top"), 160, max(25, args.scrolls // 3)),
        ("mentions_live", "community_mention", search_url(f"@{handle} -from:{handle}", "live"), 160, max(25, args.scrolls // 3)),
    ]

    ws = chrome.connect(args.port, timeout=60)
    groups = {}
    try:
        for name, kind, url, max_items, scrolls in queries:
            print(f"[{datetime.now():%H:%M:%S}] collect {name} max={max_items} scrolls={scrolls}", file=sys.stderr, flush=True)
            groups[name] = collect_page(ws, args.port, url, name, kind, handle, max_items, scrolls, args.wait)
            print(f"[{datetime.now():%H:%M:%S}] {name}: {len(groups[name])}", file=sys.stderr, flush=True)
    finally:
        ws.close()

    merged = merge_records(groups, handle)
    counts = {}
    for r in merged:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "handle": handle,
        "target": args.target,
        "counts": counts,
        "group_counts": {k: len(v) for k, v in groups.items()},
        "groups": groups,
        "records": merged,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps({"out": args.out, "counts": counts, "group_counts": result["group_counts"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
