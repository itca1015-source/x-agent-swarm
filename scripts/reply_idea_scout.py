"""Recent reply-idea scout for Hunter.

Collects recent high-engagement posts and standalone replies in Hunter's
target niche and stores them as raw idea/context examples. It does not post
anything.

Outputs:
  state/reply_idea_bank_guohunter95258.json
  state/reply_idea_bank_guohunter95258.latest.csv

Safety:
  TEST_ONLY is required for small samples.
  APPROVED_OFFICIAL_RUN is required for official-scale collection.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
from lock import chrome_lock

STATE_DIR = os.path.join(ROOT_DIR, "state")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "reply_idea_scout")
HUNTER_PORT = 10000
HUNTER_HANDLE = "GuoHunter95258"

DEFAULT_KEYWORDS = [
    '"Claude Code"',
    '"AI agent"',
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


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _parse_count(raw: str) -> int:
    if not raw:
        return 0
    s = str(raw).strip().replace(",", "").upper()
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


def _age_hours(iso: str) -> float:
    if not iso:
        return 999999.0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 999999.0


def _status_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


def _engagement(likes: int, reposts: int, replies: int) -> int:
    return likes + reposts + replies


def _bank_paths(handle: str) -> tuple[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", handle).lower()
    return (
        os.path.join(STATE_DIR, f"reply_idea_bank_{safe}.json"),
        os.path.join(STATE_DIR, f"reply_idea_bank_{safe}.latest.csv"),
    )


SEARCH_POSTS_JS = r"""
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
            var replyEl = el.querySelector('[data-testid="reply"]');
            var rtEl = el.querySelector('[data-testid="retweet"]');
            var likeEl = el.querySelector('[data-testid="like"]');
            var timeEl = el.querySelector('time');
            out.push({
                author: author,
                url: url,
                text: text.slice(0, 1200),
                replies_raw: replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                reposts_raw: rtEl ? rtEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                likes_raw: likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
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
            if (!text) return;
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
            var urlEl = el.querySelector('a[href*="/status/"]');
            var replyEl = el.querySelector('[data-testid="reply"]');
            var rtEl = el.querySelector('[data-testid="retweet"]');
            var likeEl = el.querySelector('[data-testid="like"]');
            var timeEl = el.querySelector('time');
            out.push({
                position: idx,
                author: author,
                url: urlEl ? urlEl.href : '',
                text: text.slice(0, 1200),
                replies_raw: replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                reposts_raw: rtEl ? rtEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                likes_raw: likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g, '') : '',
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


def _parse_post(row: dict, source_query: str = "") -> dict:
    return {
        "source_query": source_query,
        "source_author": (row.get("author") or "").lstrip("@"),
        "source_url": row.get("url", ""),
        "source_id": _status_id(row.get("url", "")),
        "source_text": row.get("text", ""),
        "source_likes": _parse_count(row.get("likes_raw", "")),
        "source_replies": _parse_count(row.get("replies_raw", "")),
        "source_reposts": _parse_count(row.get("reposts_raw", "")),
        "source_iso": row.get("iso", ""),
        "source_age_hours": round(_age_hours(row.get("iso", "")), 2),
    }


def collect_source_posts(ws, port: int, keywords: list[str], handles: list[str],
                         max_age_hours: int, max_sources: int,
                         search_scrolls: int) -> list[dict]:
    sources = {}
    for kw in keywords:
        for mode in ("top", "live"):
            q = urllib.parse.quote_plus(kw)
            url = f"https://x.com/search?q={q}&src=typed_query&f={mode}"
            _log(f"search {mode}: {kw}")
            with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
                _chrome.navigate(ws, url, wait=4.0)
                for _ in range(search_scrolls):
                    raw = _chrome.eval_js(ws, SEARCH_POSTS_JS)
                    try:
                        rows = json.loads(raw) if raw else []
                    except Exception:
                        rows = []
                    for row in rows:
                        post = _parse_post(row, f"{mode}:{kw}")
                        if not post["source_id"] or post["source_age_hours"] > max_age_hours:
                            continue
                        sources.setdefault(post["source_id"], post)
                    _chrome.eval_js(ws, "window.scrollBy(0, 1400)")
                    time.sleep(1.5)
            if len(sources) >= max_sources:
                break
        if len(sources) >= max_sources:
            break

    for handle in handles:
        h = handle.strip().lstrip("@")
        if not h:
            continue
        url = f"https://x.com/{h}"
        _log(f"profile: @{h}")
        with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
            _chrome.navigate(ws, url, wait=4.0)
            _chrome.eval_js(ws, "window.scrollBy(0, 1000)")
            time.sleep(1.5)
            raw = _chrome.eval_js(ws, SEARCH_POSTS_JS)
        try:
            rows = json.loads(raw) if raw else []
        except Exception:
            rows = []
        for row in rows:
            post = _parse_post(row, f"profile:@{h}")
            if not post["source_id"] or post["source_age_hours"] > max_age_hours:
                continue
            sources.setdefault(post["source_id"], post)
            if len(sources) >= max_sources:
                break
        if len(sources) >= max_sources:
            break

    out = list(sources.values())
    out.sort(
        key=lambda p: _engagement(p["source_likes"], p["source_reposts"], p["source_replies"]),
        reverse=True,
    )
    return out[:max_sources]


def collect_replies_for_source(ws, port: int, source: dict, max_replies: int,
                               min_engagement: int, max_age_hours: int,
                               scrolls: int, source_ids: set[str]) -> list[dict]:
    with chrome_lock(port, on_wait=lambda s: _log(f"  lock wait {s:.0f}s")):
        _chrome.navigate(ws, source["source_url"], wait=4.0)
        for _ in range(scrolls):
            _chrome.eval_js(ws, "window.scrollBy(0, 1500)")
            time.sleep(1.5)
        raw = _chrome.eval_js(ws, THREAD_REPLIES_JS)
    try:
        cards = json.loads(raw) if raw else []
    except Exception:
        cards = []

    replies = []
    source_author_l = source.get("source_author", "").lower()
    for c in cards:
        author = (c.get("author") or "").lstrip("@")
        if not author or author.lower() == source_author_l:
            continue
        item_url = c.get("url", "")
        item_id = _status_id(item_url)
        if not item_id or item_id == source.get("source_id") or item_id in source_ids:
            continue
        item_age_hours = _age_hours(c.get("iso", ""))
        if item_age_hours > max_age_hours:
            continue
        text = (c.get("text") or "").strip()
        if len(text) < 25:
            continue
        likes = _parse_count(c.get("likes_raw", ""))
        reposts = _parse_count(c.get("reposts_raw", ""))
        reply_count = _parse_count(c.get("replies_raw", ""))
        total_engagement = _engagement(likes, reposts, reply_count)
        if total_engagement < min_engagement:
            continue
        replies.append({
            "item_type": "reply",
            "item_author": author,
            "item_url": item_url,
            "item_id": item_id,
            "item_text": text,
            "item_likes": likes,
            "item_replies": reply_count,
            "item_reposts": reposts,
            "total_engagement": total_engagement,
            "item_iso": c.get("iso", ""),
            "item_age_hours": round(item_age_hours, 2),
            "audience": "",
            "why_saved": "",
            "why_Hunter_can_use_it": "",
            "suggested_use": "",
            **source,
            "reply_author": author,
            "reply_url": item_url,
            "reply_id": item_id,
            "reply_text": text,
            "reply_likes": likes,
            "reply_replies": reply_count,
            "reply_reposts": reposts,
            "reply_score": total_engagement,
            "reply_position": c.get("position", -1),
            "reply_iso": c.get("iso", ""),
            "reply_age_hours": round(item_age_hours, 2),
        })
    replies.sort(key=lambda r: (r["total_engagement"], r["item_reposts"], r["item_replies"], r["item_likes"]), reverse=True)
    return replies[:max_replies]


def source_post_to_item(source: dict, min_engagement: int) -> Optional[dict]:
    total_engagement = _engagement(source["source_likes"], source["source_reposts"], source["source_replies"])
    if total_engagement < min_engagement:
        return None
    return {
        "item_type": "post",
        "item_author": source["source_author"],
        "item_url": source["source_url"],
        "item_id": source["source_id"],
        "item_text": source["source_text"],
        "item_likes": source["source_likes"],
        "item_replies": source["source_replies"],
        "item_reposts": source["source_reposts"],
        "total_engagement": total_engagement,
        "item_iso": source["source_iso"],
        "item_age_hours": source["source_age_hours"],
        "audience": "",
        "why_saved": "",
        "why_Hunter_can_use_it": "",
        "suggested_use": "",
        **source,
    }


def merge_bank(bank_path: str, examples: list[dict], replace: bool = False) -> dict:
    bank = _load_json(bank_path, {"updated_at": "", "examples": []})
    existing = {}
    if not replace:
        existing = {e.get("item_url") or e.get("reply_url") or e.get("source_url") or e.get("item_id"): e for e in bank.get("examples", [])}
    for ex in examples:
        key = ex.get("item_url") or ex.get("reply_url") or ex.get("source_url") or ex.get("item_id")
        if key:
            existing[key] = ex
    merged = list(existing.values())
    merged.sort(key=lambda r: (r.get("total_engagement", 0), r.get("item_reposts", 0), r.get("item_replies", 0)), reverse=True)
    bank = {
        "updated_at": _ts(),
        "count": len(merged),
        "examples": merged[:1000],
    }
    _save_json(bank_path, bank)
    return bank


def write_latest_csv(path: str, examples: list[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [
        "item_type", "total_engagement", "item_reposts", "item_replies", "item_likes",
        "item_author", "item_text", "item_url", "item_age_hours",
        "audience", "why_saved", "why_Hunter_can_use_it", "suggested_use",
        "source_author", "source_text", "source_url",
        "source_reposts", "source_replies", "source_likes", "source_age_hours",
        "source_query",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for ex in examples:
            w.writerow({k: ex.get(k, "") for k in cols})


def unique_item_count(examples: list[dict]) -> int:
    return len({e.get("item_url") or e.get("item_id") for e in examples if e.get("item_url") or e.get("item_id")})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=HUNTER_PORT)
    p.add_argument("--handle", default=HUNTER_HANDLE)
    p.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS))
    p.add_argument("--source-handles", default="")
    p.add_argument("--max-age-hours", type=int, default=72)
    p.add_argument("--max-sources", type=int, default=40)
    p.add_argument("--max-replies-per-source", type=int, default=8)
    p.add_argument("--min-engagement", type=int, default=500)
    p.add_argument("--thread-scrolls", type=int, default=4)
    p.add_argument("--search-scrolls", type=int, default=4)
    p.add_argument("--target-items", type=int, default=15)
    p.add_argument("--replace-bank", action="store_true")
    p.add_argument("--approval-token", default="")
    args = p.parse_args()

    is_test = (
        args.approval_token == "TEST_ONLY"
        and args.max_sources <= 3
        and args.max_replies_per_source <= 5
        and args.thread_scrolls <= 2
        and args.search_scrolls <= 2
    )
    is_official = args.max_sources > 3 or args.max_replies_per_source > 5 or args.thread_scrolls > 2 or args.search_scrolls > 2
    if is_official and args.approval_token != "APPROVED_OFFICIAL_RUN":
        raise SystemExit("official-scale idea scout blocked: run TEST_ONLY first and get approval")
    if not is_test and args.approval_token != "APPROVED_OFFICIAL_RUN":
        raise SystemExit("missing approval token: use TEST_ONLY for sample runs")

    if not _chrome.ping(args.port):
        raise SystemExit(f"Chrome port {args.port} unavailable")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    handles = [h.strip() for h in args.source_handles.split(",") if h.strip()]
    bank_path, csv_path = _bank_paths(args.handle)

    ws = _chrome.connect(args.port)
    try:
        sources = collect_source_posts(ws, args.port, keywords, handles, args.max_age_hours, args.max_sources, args.search_scrolls)
        source_ids = {s.get("source_id") for s in sources if s.get("source_id")}
        _log(f"source posts selected: {len(sources)}")
        examples = []
        for i, src in enumerate(sources, 1):
            source_item = source_post_to_item(src, args.min_engagement)
            if source_item:
                examples.append(source_item)
            _log(
                f"[{i}/{len(sources)}] @{src['source_author']} "
                f"r={src['source_replies']} rt={src['source_reposts']} "
                f"likes={src['source_likes']} age={src['source_age_hours']}h"
            )
            examples.extend(collect_replies_for_source(
                ws, args.port, src, args.max_replies_per_source,
                args.min_engagement, args.max_age_hours, args.thread_scrolls, source_ids,
            ))
            if unique_item_count(examples) >= args.target_items:
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass

    examples.sort(key=lambda r: (r["total_engagement"], r["item_reposts"], r["item_replies"], r["item_likes"]), reverse=True)
    deduped = {}
    for ex in examples:
        deduped.setdefault(ex.get("item_url") or ex.get("item_id"), ex)
    examples = list(deduped.values())[:args.target_items]
    bank = merge_bank(bank_path, examples, replace=args.replace_bank)
    write_latest_csv(csv_path, examples)

    _log(f"stored {len(examples)} latest examples; bank has {bank['count']} examples")
    _log(f"json: {bank_path}")
    _log(f"csv: {csv_path}")
    for ex in examples[:10]:
        _log(
            f"  {ex['item_type']} engagement={ex['total_engagement']} "
            f"rt={ex['item_reposts']} replies={ex['item_replies']} "
            f"likes={ex['item_likes']} @{ex['item_author']}: {ex['item_text'][:110]}"
        )


if __name__ == "__main__":
    main()
