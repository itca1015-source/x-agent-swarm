#!/usr/bin/env python3
"""Scout public X posting patterns for competitor accounts.

Read-only CDP scraper for profile timelines and account-specific search results.
It prints JSON so the analysis can be repeated without changing account state.
"""
import argparse
import json
import os
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
            var replyContext = /Replying to/i.test(all);
            var repostContext = /reposted/i.test(all);
            var pinnedContext = /Pinned/i.test(all);
            out.push({
                id: id,
                url: url,
                author: author,
                iso: iso,
                text: text,
                all_text: all.slice(0, 1600),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                likes_raw: metric(el, 'like'),
                bookmarks_raw: metric(el, 'bookmark'),
                views_raw: metric(el, 'analytics'),
                is_reply: replyContext,
                is_repost_context: repostContext,
                is_pinned: pinnedContext
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


THREAD_JS = r"""
(function() {
    var handle = arguments[0];
    var arts = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    var out = [];
    arts.forEach(function(el) {
        var textEl = el.querySelector('[data-testid="tweetText"]');
        var text = textEl ? textEl.innerText.trim() : '';
        if (!text) return;
        var urlEl = Array.from(el.querySelectorAll('a[href*="/status/"]')).find(function(a) {
            return a.href.indexOf('/' + handle + '/status/') >= 0;
        });
        if (!urlEl) return;
        var id = (urlEl.href.match(/status\/([0-9]+)/) || [])[1] || '';
        var timeEl = el.querySelector('time');
        out.push({
            id: id,
            url: urlEl.href.split('?')[0],
            iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
            text: text
        });
    });
    return JSON.stringify(out);
})()
"""


def enrich(records):
    seen = set()
    out = []
    for r in records:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        r["likes"] = parse_count(r.get("likes_raw", ""))
        r["reposts"] = parse_count(r.get("reposts_raw", ""))
        r["replies"] = parse_count(r.get("replies_raw", ""))
        r["bookmarks"] = parse_count(r.get("bookmarks_raw", ""))
        r["views"] = parse_count(r.get("views_raw", ""))
        iso = r.get("iso") or ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            r["hour_utc"] = dt.hour
            r["weekday_utc"] = dt.strftime("%a")
            r["age_hours"] = round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
        except Exception:
            r["hour_utc"] = None
            r["weekday_utc"] = ""
            r["age_hours"] = None
        out.append(r)
    return out


def extract(ws):
    raw = chrome.eval_js(ws, EXTRACT_JS)
    try:
        return enrich(json.loads(raw) if raw else [])
    except Exception:
        return []


def collect_profile(ws, port, handle, scrolls, wait):
    with chrome_lock(port):
        chrome.navigate(ws, f"https://x.com/{handle}", wait=wait)
        chrome.set_viewport(ws, 1400, 1800)
        time.sleep(1)
        records = []
        for i in range(scrolls):
            records.extend(extract(ws))
            chrome.eval_js(ws, "window.scrollBy(0, 1400)")
            time.sleep(1.4)
        records.extend(extract(ws))
    return enrich(records)


def collect_search(ws, port, handle, query_suffix, mode, scrolls, wait):
    q = f"from:{handle} {query_suffix}".strip()
    url = f"https://x.com/search?q={urllib.parse.quote_plus(q)}&src=typed_query&f={mode}"
    with chrome_lock(port):
        chrome.navigate(ws, url, wait=wait)
        chrome.set_viewport(ws, 1400, 1800)
        time.sleep(1)
        records = []
        for i in range(scrolls):
            records.extend(extract(ws))
            chrome.eval_js(ws, "window.scrollBy(0, 1400)")
            time.sleep(1.4)
        records.extend(extract(ws))
    return enrich(records)


def collect_thread(ws, port, handle, url, wait):
    with chrome_lock(port):
        chrome.navigate(ws, url, wait=wait)
        chrome.set_viewport(ws, 1400, 2200)
        time.sleep(1.5)
        raw = chrome.eval_js(ws, THREAD_JS.replace("arguments[0]", json.dumps(handle)))
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    seen = set()
    out = []
    for r in rows:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10004)
    ap.add_argument("--handles", nargs="+", required=True)
    ap.add_argument("--scrolls", type=int, default=10)
    ap.add_argument("--wait", type=float, default=5.0)
    ap.add_argument("--threads", type=int, default=5)
    args = ap.parse_args()

    ws = chrome.connect(args.port, timeout=60)
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "accounts": {}}
    try:
        for handle in args.handles:
            profile = collect_profile(ws, args.port, handle, args.scrolls, args.wait)
            top = collect_search(ws, args.port, handle, "-filter:replies", "top", args.scrolls, args.wait)
            live = collect_search(ws, args.port, handle, "-filter:replies", "live", max(4, args.scrolls // 2), args.wait)
            by_likes = sorted(
                [r for r in top + profile if r.get("author", "").lower() == handle.lower() and not r.get("is_reply")],
                key=lambda r: (r.get("likes") or 0, r.get("reposts") or 0, r.get("replies") or 0),
                reverse=True,
            )
            threads = []
            for r in by_likes[: args.threads]:
                rows = collect_thread(ws, args.port, handle, r["url"], args.wait)
                threads.append({"root": r, "posts": rows})
            result["accounts"][handle] = {
                "profile": profile,
                "top_search": top,
                "live_search": live,
                "top_threads": threads,
            }
    finally:
        ws.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
