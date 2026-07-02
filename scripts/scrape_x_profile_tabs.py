#!/usr/bin/env python3
"""Scrape an X profile's Posts and Replies tabs via Chrome CDP.

Read-only scraper intended for account timeline research. It collects cards
rendered in the browser, preserves raw visible text/metrics, classifies the
profile action, and writes a JSON array.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock


EXTRACT_JS = r"""
(function() {
    var handle = __HANDLE__;
    var handleLower = handle.toLowerCase();

    function compactText(el) {
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

    function rawMetric(el, testid) {
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

    function statusFromAnchor(a) {
        if (!a || !a.href) return null;
        var m = a.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
        if (!m) return null;
        return {url: 'https://x.com/' + m[1] + '/status/' + m[2], author: m[1], id: m[2]};
    }

    var articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    var out = [];
    articles.forEach(function(el) {
        try {
            var all = visibleText(el);
            if (!all || /\bPromoted\b/i.test(all)) return;

            var timeEl = el.querySelector('time');
            var main = statusFromAnchor(timeEl ? timeEl.closest('a[href*="/status/"]') : null);
            var statusLinks = Array.from(el.querySelectorAll('a[href*="/status/"]'))
                .map(statusFromAnchor)
                .filter(Boolean);
            if (!main && statusLinks.length) main = statusLinks[0];
            if (!main || !main.id) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? compactText(textEl) : '';
            var hrefs = unique(Array.from(el.querySelectorAll('a[href]')).map(normalizedHref));
            var statusIds = unique(statusLinks.map(function(x) { return x.id; }));
            var otherStatus = statusLinks.some(function(x) {
                return x.id !== main.id || x.author.toLowerCase() !== main.author.toLowerCase();
            });

            var lowerAll = all.toLowerCase();
            var isReply = /\bReplying to\b/i.test(all);
            var isRepost = /\breposted\b/i.test(all) && lowerAll.indexOf('reposted') >= 0;
            var isQuote = !isRepost && (/\bQuote\b/.test(all) || otherStatus);
            var authored = main.author.toLowerCase() === handleLower;

            var category = 'original';
            if (isRepost && !authored) category = 'repost';
            else if (isReply) category = 'reply';
            else if (isQuote) category = 'quote';
            else if (!authored) category = 'context';

            out.push({
                id: main.id,
                url: main.url,
                author: main.author,
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                text: text,
                all_text: all.slice(0, 5000),
                replies_raw: rawMetric(el, 'reply'),
                reposts_raw: rawMetric(el, 'retweet'),
                likes_raw: rawMetric(el, 'like'),
                bookmarks_raw: rawMetric(el, 'bookmark'),
                views_raw: analyticsMetric(el),
                is_reply: isReply,
                is_repost_context: isRepost,
                is_pinned: /\bPinned\b/i.test(all),
                is_quote: isQuote,
                category: category,
                has_image: !!el.querySelector('[data-testid="tweetPhoto"], a[href*="/photo/"]'),
                has_video: !!el.querySelector('[data-testid="videoPlayer"], video'),
                has_card: !!el.querySelector('[data-testid="card.wrapper"], a[data-testid="card.wrapper"]'),
                has_gif: !!el.querySelector('[data-testid="gif"], [aria-label*="GIF" i]'),
                hrefs: hrefs,
                status_ids_in_card: statusIds,
                source: 'profile',
                source_kind: '__SOURCE_KIND__',
                source_tab: '__SOURCE_TAB__',
                is_authored_by_handle: authored
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


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


def extract_mentions(text: str) -> list[str]:
    seen = set()
    out = []
    for m in re.finditer(r"@([A-Za-z0-9_]{1,15})", text or ""):
        value = m.group(1).lower()
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def extract_cashtags(text: str) -> list[str]:
    seen = set()
    out = []
    for m in re.finditer(r"\$[A-Za-z][A-Za-z0-9_]*", text or ""):
        value = m.group(0).upper()
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def enrich(records: list[dict], now: datetime) -> list[dict]:
    for r in records:
        r["likes"] = parse_count(r.get("likes_raw", ""))
        r["reposts"] = parse_count(r.get("reposts_raw", ""))
        r["replies"] = parse_count(r.get("replies_raw", ""))
        r["bookmarks"] = parse_count(r.get("bookmarks_raw", ""))
        r["views"] = parse_count(r.get("views_raw", ""))
        all_text = r.get("all_text") or ""
        r["mentions"] = extract_mentions(all_text)
        r["cashtags"] = extract_cashtags(all_text)
        iso = r.get("iso") or ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            r["hour_utc"] = dt.hour
            r["weekday_utc"] = dt.strftime("%a")
            r["age_hours"] = round((now - dt).total_seconds() / 3600, 1)
        except Exception:
            r["hour_utc"] = None
            r["weekday_utc"] = ""
            r["age_hours"] = None
    return records


def extract(ws, handle: str, source_tab: str, source_kind: str, now: datetime) -> list[dict]:
    js = (
        EXTRACT_JS
        .replace("__HANDLE__", json.dumps(handle))
        .replace("__SOURCE_TAB__", source_tab)
        .replace("__SOURCE_KIND__", source_kind)
    )
    raw = chrome.eval_js(ws, js)
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    rows = [
        r for r in rows
        if r.get("is_authored_by_handle") or r.get("is_repost_context")
    ]
    return enrich(rows, now)


def merge_record(records: dict[str, dict], row: dict) -> bool:
    key = row.get("id")
    if not key:
        return False
    existing = records.get(key)
    if not existing:
        row["tabs_seen"] = [row.get("source_tab", "")]
        row["first_seen_tab"] = row.get("source_tab", "")
        records[key] = row
        return True

    tab = row.get("source_tab", "")
    if tab and tab not in existing.setdefault("tabs_seen", []):
        existing["tabs_seen"].append(tab)
    existing["is_pinned"] = bool(existing.get("is_pinned") or row.get("is_pinned"))
    existing["has_image"] = bool(existing.get("has_image") or row.get("has_image"))
    existing["has_video"] = bool(existing.get("has_video") or row.get("has_video"))
    existing["has_card"] = bool(existing.get("has_card") or row.get("has_card"))
    existing["has_gif"] = bool(existing.get("has_gif") or row.get("has_gif"))
    if len(row.get("all_text", "")) > len(existing.get("all_text", "")):
        for field in ("all_text", "text", "hrefs", "mentions", "cashtags", "status_ids_in_card"):
            existing[field] = row.get(field, existing.get(field))
    return False


def scroll_position(ws) -> tuple[int, int]:
    raw = chrome.eval_js(ws, "JSON.stringify({y: Math.round(window.scrollY), h: document.body.scrollHeight})")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    return int(obj.get("y") or 0), int(obj.get("h") or 0)


def collect_tab(
    ws,
    port: int,
    handle: str,
    tab: str,
    target_tab_count: int,
    max_scrolls: int,
    idle_limit: int,
    wait: float,
    records: dict[str, dict],
) -> None:
    source_kind = "profile_posts" if tab == "posts" else "profile_replies"
    url = f"https://x.com/{handle}" if tab == "posts" else f"https://x.com/{handle}/with_replies"
    print(f"[scrape] opening {tab}: {url}", file=sys.stderr, flush=True)
    chrome.navigate(ws, url, wait=wait + 2)
    chrome.set_viewport(ws, 1400, 2200)
    time.sleep(1.0)

    no_new = 0
    last_y = -1
    last_h = -1
    for i in range(max_scrolls + 1):
        now = datetime.now(timezone.utc)
        before = len(records)
        for row in extract(ws, handle, tab, source_kind, now):
            merge_record(records, row)
        gained = len(records) - before
        y, h = scroll_position(ws)
        counts = category_counts(records.values())
        tab_unique = first_seen_tab_count(records.values(), tab)
        print(
            "[scrape] "
            f"{tab} scroll={i} unique={len(records)} tab_unique={tab_unique} new={gained} "
            f"cat={counts} y={y}/{h}",
            file=sys.stderr,
            flush=True,
        )
        if target_tab_count and tab_unique >= target_tab_count:
            return

        if gained == 0 and y == last_y and h == last_h:
            no_new += 1
        elif gained == 0:
            no_new += 1
        else:
            no_new = 0
        if no_new >= idle_limit:
            print(f"[scrape] {tab} idle after {no_new} scrolls; moving on", file=sys.stderr, flush=True)
            return

        last_y, last_h = y, h
        chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.82))")
        time.sleep(wait)


def category_counts(rows) -> dict[str, int]:
    counts = {"original": 0, "quote": 0, "reply": 0, "repost": 0}
    for r in rows:
        c = r.get("category") or "original"
        counts[c] = counts.get(c, 0) + 1
    return counts


def tab_count(rows, tab: str) -> int:
    return sum(1 for r in rows if tab in (r.get("tabs_seen") or []))


def first_seen_tab_count(rows, tab: str) -> int:
    return sum(1 for r in rows if r.get("first_seen_tab") == tab)


def sort_records(rows: list[dict]) -> list[dict]:
    def key(row):
        iso = row.get("iso") or ""
        return (iso, row.get("id") or "")
    return sorted(rows, key=key, reverse=True)


def trim_preserving_categories(rows: list[dict], limit: int) -> list[dict]:
    if not limit or len(rows) <= limit:
        return rows
    trimmed = rows[:limit]
    have = {r.get("category") for r in trimmed}
    all_have = {r.get("category") for r in rows}
    missing = [c for c in ("original", "quote", "reply", "repost") if c in all_have and c not in have]
    if not missing:
        return trimmed

    protected_ids = set()
    for c in ("original", "quote", "reply", "repost"):
        for r in trimmed:
            if r.get("category") == c:
                protected_ids.add(r.get("id"))
                break

    for c in missing:
        replacement = next((r for r in rows[limit:] if r.get("category") == c), None)
        if not replacement:
            continue
        for idx in range(len(trimmed) - 1, -1, -1):
            victim = trimmed[idx]
            if victim.get("id") not in protected_ids:
                trimmed[idx] = replacement
                protected_ids.add(replacement.get("id"))
                break
    return sort_records(trimmed)


def infer_replies_from_tabs(rows: list[dict]) -> None:
    """X's Replies tab sometimes omits the visible 'Replying to' line.

    After both tabs have been collected, an OpenRouter-authored card that only
    appeared on Replies and is not a quote/repost is a profile reply.
    """
    for r in rows:
        tabs = set(r.get("tabs_seen") or [])
        if (
            r.get("is_authored_by_handle")
            and r.get("category") == "original"
            and "replies" in tabs
            and "posts" not in tabs
            and not r.get("is_quote")
            and not r.get("is_repost_context")
        ):
            r["category"] = "reply"
            r["is_reply"] = True
            r["reply_inferred_from_replies_tab"] = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10004)
    ap.add_argument("--handle", default="OpenRouter")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--per-tab-limit", type=int, default=0)
    ap.add_argument("--max-scrolls", type=int, default=260)
    ap.add_argument("--idle-limit", type=int, default=25)
    ap.add_argument("--wait", type=float, default=1.25)
    ap.add_argument("--output", default=os.path.join(ROOT_DIR, "state", "openrouter_x_posts.json"))
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    records: dict[str, dict] = {}
    ws = chrome.connect(args.port, timeout=60)
    try:
        with chrome_lock(args.port, timeout=7200):
            per_tab_limit = args.per_tab_limit or max(75, int(args.limit * 0.7))
            collect_tab(ws, args.port, args.handle, "posts", per_tab_limit, args.max_scrolls, args.idle_limit, args.wait, records)
            collect_tab(ws, args.port, args.handle, "replies", per_tab_limit, args.max_scrolls, args.idle_limit, args.wait, records)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    rows = list(records.values())
    infer_replies_from_tabs(rows)
    rows = sort_records(rows)
    rows = trim_preserving_categories(rows, args.limit)
    with open(args.output, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        json.dumps({
            "output": args.output,
            "records": len(rows),
            "categories": category_counts(rows),
            "tabs": {
                "posts": sum(1 for r in rows if "posts" in r.get("tabs_seen", [])),
                "replies": sum(1 for r in rows if "replies" in r.get("tabs_seen", [])),
            },
        }, indent=2),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
