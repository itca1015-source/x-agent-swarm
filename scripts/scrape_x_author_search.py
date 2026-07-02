#!/usr/bin/env python3
"""Scrape an author's X posts/replies from Search Latest via Chrome CDP."""
import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock
from scrape_x_profile_tabs import (
    category_counts,
    extract,
    merge_record,
    sort_records,
    trim_preserving_categories,
)


def scroll_position(ws) -> tuple[int, int]:
    raw = chrome.eval_js(ws, "JSON.stringify({y: Math.round(window.scrollY), h: document.body.scrollHeight})")
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    return int(obj.get("y") or 0), int(obj.get("h") or 0)


def load_existing(path: str) -> dict[str, dict]:
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        rows = json.load(f)
    records: dict[str, dict] = {}
    for row in rows:
        key = row.get("id")
        if key:
            records[key] = row
    return records


def collect_search(
    ws,
    port: int,
    handle: str,
    label: str,
    query: str,
    target_total: int,
    max_scrolls: int,
    idle_limit: int,
    wait: float,
    records: dict[str, dict],
    time_window: Optional[dict] = None,
) -> None:
    q = urllib.parse.quote_plus(query)
    url = f"https://x.com/search?q={q}&src=typed_query&f=live"
    print(f"[search] opening {label}: {query}", file=sys.stderr, flush=True)
    chrome.navigate(ws, url, wait=wait + 3)
    chrome.set_viewport(ws, 1400, 2200)
    time.sleep(1.0)

    no_new = 0
    last_y = -1
    last_h = -1
    for i in range(max_scrolls + 1):
        now = datetime.now(timezone.utc)
        before = len(records)
        rows = extract(ws, handle, label, f"search_{label}", now)
        for row in rows:
            row["source"] = "search"
            row["source_kind"] = f"search_{label}"
            row["source_tab"] = label
            if time_window:
                row["source_time_window"] = time_window
            if label == "search_replies" and row.get("category") == "original":
                row["category"] = "reply"
                row["is_reply"] = True
                row["reply_inferred_from_search"] = True
            merge_record(records, row)
        gained = len(records) - before
        y, h = scroll_position(ws)
        print(
            "[search] "
            f"{label} scroll={i} unique={len(records)} new={gained} "
            f"cat={category_counts(records.values())} y={y}/{h}",
            file=sys.stderr,
            flush=True,
        )
        if target_total and len(records) >= target_total:
            return

        if gained == 0:
            no_new += 1
        else:
            no_new = 0
        if no_new >= idle_limit and y == last_y and h == last_h:
            print(f"[search] {label} idle after {no_new} scrolls; moving on", file=sys.stderr, flush=True)
            return

        last_y, last_h = y, h
        chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.86))")
        time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10004)
    ap.add_argument("--handle", default="aixbt_agent")
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-scrolls", type=int, default=320)
    ap.add_argument("--idle-limit", type=int, default=18)
    ap.add_argument("--wait", type=float, default=1.15)
    ap.add_argument("--time-slices", action="store_true", help="split from: search into backward UTC time windows")
    ap.add_argument("--lookback-hours", type=int, default=24)
    ap.add_argument("--window-minutes", type=int, default=60)
    ap.add_argument("--input", default="")
    ap.add_argument("--output", default=os.path.join(ROOT_DIR, "state", "aixbt_agent_x_posts_replies_2026-06-02.json"))
    args = ap.parse_args()

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    records = load_existing(args.input or output)

    ws = chrome.connect(args.port, timeout=60)
    try:
        with chrome_lock(args.port, timeout=7200):
            if args.time_slices:
                window = timedelta(minutes=max(5, args.window_minutes))
                end = datetime.now(timezone.utc)
                end = end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                earliest = end - timedelta(hours=args.lookback_hours)
                while end > earliest:
                    start = max(earliest, end - window)
                    start_ts = int(start.timestamp())
                    end_ts = int(end.timestamp())
                    query = f"from:{args.handle} since_time:{start_ts} until_time:{end_ts}"
                    collect_search(
                        ws,
                        args.port,
                        args.handle,
                        "search_window",
                        query,
                        args.target,
                        args.max_scrolls,
                        args.idle_limit,
                        args.wait,
                        records,
                        {
                            "start_utc": start.isoformat(),
                            "end_utc": end.isoformat(),
                            "query": query,
                        },
                    )
                    if args.target and len(records) >= args.target:
                        break
                    end = start
            else:
                queries = [
                    ("search_all", f"from:{args.handle}"),
                    ("search_replies", f"from:{args.handle} filter:replies"),
                ]
                for label, query in queries:
                    collect_search(
                        ws,
                        args.port,
                        args.handle,
                        label,
                        query,
                        args.target,
                        args.max_scrolls,
                        args.idle_limit,
                        args.wait,
                        records,
                    )
                    if args.target and len(records) >= args.target:
                        break
    finally:
        try:
            ws.close()
        except Exception:
            pass

    rows = trim_preserving_categories(sort_records(list(records.values())), args.limit)
    with open(output, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        json.dumps(
            {
                "output": output,
                "records": len(rows),
                "categories": category_counts(rows),
                "tabs": {
                    "posts": sum(1 for r in rows if "posts" in r.get("tabs_seen", [])),
                    "replies": sum(1 for r in rows if "replies" in r.get("tabs_seen", [])),
                    "search_all": sum(1 for r in rows if "search_all" in r.get("tabs_seen", [])),
                    "search_replies": sum(1 for r in rows if "search_replies" in r.get("tabs_seen", [])),
                    "search_window": sum(1 for r in rows if "search_window" in r.get("tabs_seen", [])),
                },
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
