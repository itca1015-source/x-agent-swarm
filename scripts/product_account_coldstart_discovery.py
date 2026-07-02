#!/usr/bin/env python3
"""Gather recent AI startup/product-account candidates from X search results.

Search queries are used only for discovery. Product-account qualification is
done by semantic review of the saved profile evidence, not keyword matching.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
import fetch
from flatkey_home_coldstart_scout import fetch_profile_meta, fmt_count


STATE_DIR = os.path.join(ROOT_DIR, "state")
AS_OF = date(2026, 6, 6)

DEFAULT_QUERIES = [
    '"AI agents" "we launched"',
    '"AI agent" "try it"',
    '"AI agent" "free invite"',
    '"AI agents" "waitlist"',
    '"AI agent" "public beta"',
    '"AI copilot" "we launched"',
    '"AI assistant" "we launched"',
    '"AI workspace" "we launched"',
    '"AI browser" "we launched"',
    '"AI sales" "we launched"',
    '"AI support" "we launched"',
    '"AI receptionist" "we launched"',
    '"AI SDR" "we launched"',
    '"AI data" "we launched"',
    '"AI coding" "we launched"',
    '"AI product" "we launched"',
    '"agent platform" "we launched"',
    '"team of AI agents"',
    '"comment" "invite" "AI agents"',
    '"launching" "AI agents"',
]


def state_path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def compact(s: str, n: int = 260) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "..."


def handle_from_url(url: str) -> str:
    m = re.search(r"x\.com/([A-Za-z0-9_]{1,20})/status/", url or "")
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10004)
    ap.add_argument("--limit-per-query", type=int, default=20)
    ap.add_argument("--max-profiles", type=int, default=80)
    ap.add_argument("--max-posts", type=int, default=2500)
    ap.add_argument("--handles", default="", help="comma-separated handles to profile directly; skips search")
    ap.add_argument("--profile-retries", type=int, default=3)
    ap.add_argument("--output", default=state_path("ai_product_account_coldstart_discovery_2026-06-06.json"))
    args = ap.parse_args()

    authors: dict[str, dict] = {}
    direct_handles = [h.strip().lstrip("@") for h in args.handles.split(",") if h.strip()]
    if direct_handles:
        for handle in direct_handles:
            authors[handle] = {
                "handle": handle,
                "discovery_queries": ["direct_semantic_reprofile"],
                "discovery_urls": [],
                "discovery_texts": [],
                "discovery_likes": 0,
            }
    else:
        for query in DEFAULT_QUERIES:
            for mode in ("top", "live"):
                print(f"[discover] search {mode}: {query}", flush=True)
                try:
                    tweets = fetch.search(args.port, query, mode=mode, limit=args.limit_per_query)
                except Exception as e:
                    print(f"[discover] search error: {e}", flush=True)
                    continue
                for t in tweets:
                    handle = handle_from_url(t.get("url", ""))
                    if not handle:
                        continue
                    entry = authors.setdefault(handle, {
                        "handle": handle,
                        "discovery_queries": [],
                        "discovery_urls": [],
                        "discovery_texts": [],
                        "discovery_likes": 0,
                    })
                    if query not in entry["discovery_queries"]:
                        entry["discovery_queries"].append(query)
                    if t.get("url") and t["url"] not in entry["discovery_urls"]:
                        entry["discovery_urls"].append(t["url"])
                        entry["discovery_texts"].append(t.get("text", ""))
                    entry["discovery_likes"] = max(entry["discovery_likes"], int(t.get("likes") or 0))
                time.sleep(1.2)

    ordered = sorted(authors.values(), key=lambda r: (len(r["discovery_queries"]), r["discovery_likes"]), reverse=True)
    print(f"[discover] unique authors={len(ordered)} profiling={min(len(ordered), args.max_profiles)}", flush=True)

    rows = []
    ws = chrome.connect(args.port, timeout=60)
    try:
        for i, discovery in enumerate(ordered[: args.max_profiles], 1):
            handle = discovery["handle"]
            print(f"[discover] profile [{i}/{min(len(ordered), args.max_profiles)}] @{handle}", flush=True)
            meta = {}
            for attempt in range(1, max(1, args.profile_retries) + 1):
                try:
                    meta = fetch_profile_meta(ws, args.port, handle, AS_OF)
                except Exception as e:
                    print(f"[discover] profile error @{handle} attempt={attempt}: {e}", flush=True)
                    meta = {}
                if meta and (meta.get("followers") or meta.get("joined") or meta.get("posts_count")):
                    if meta.get("joined") and meta.get("followers") and meta.get("posts_count"):
                        break
                time.sleep(3.0 + attempt)
            if not meta:
                continue
            row = {**discovery, **meta}
            months = int(row.get("months_since_joined") or 0)
            row["qualifies_recent_fast"] = bool(
                row.get("joined_within_year")
                and row.get("followers", 0) > row.get("follower_velocity_threshold", 0)
            )
            row["posts_pass"] = 0 < int(row.get("posts_count") or 0) <= args.max_posts
            row["semantic_review_required"] = True
            rows.append(row)
            with open(args.output, "w") as f:
                json.dump(rows, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(
                "[discover] @{h} followers={f} joined={j} months={m} threshold={t} posts={p} fast={q}".format(
                    h=handle,
                    f=fmt_count(row.get("followers", 0)),
                    j=row.get("joined") or "?",
                    m=months or "?",
                    t=row.get("follower_velocity_threshold") or "?",
                    p=row.get("posts_raw") or row.get("posts_count") or "?",
                    q=row["qualifies_recent_fast"],
                ),
                flush=True,
            )
            time.sleep(0.7)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    # The remainder used to start the profiling pass after search. Direct mode
    # and normal mode now share the profiling path above.
    rows.sort(
        key=lambda r: (
            bool(r.get("qualifies_recent_fast")),
            bool(r.get("posts_pass")),
            len(r.get("discovery_queries") or []),
            int(r.get("followers") or 0),
        ),
        reverse=True,
    )
    with open(args.output, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")

    qualified = [
        r for r in rows
        if r.get("qualifies_recent_fast") and r.get("posts_pass")
    ]
    print(json.dumps({
        "output": args.output,
        "authors": len(rows),
        "numeric_rule_candidates_for_semantic_review": len(qualified),
        "top_numeric_candidates": [
            {
                "handle": r["handle"],
                "followers": r["followers"],
                "joined": r["joined"],
                "threshold": r["follower_velocity_threshold"],
                "posts": r["posts_count"],
                "bio": compact(r.get("bio", ""), 160),
                "queries": r.get("discovery_queries", [])[:3],
            }
            for r in qualified[:10]
        ],
    }, indent=2), flush=True)
    return 0

    for query in DEFAULT_QUERIES:
        for mode in ("top", "live"):
            print(f"[discover] search {mode}: {query}", flush=True)
            try:
                tweets = fetch.search(args.port, query, mode=mode, limit=args.limit_per_query)
            except Exception as e:
                print(f"[discover] search error: {e}", flush=True)
                continue
            for t in tweets:
                handle = handle_from_url(t.get("url", ""))
                if not handle:
                    continue
                entry = authors.setdefault(handle, {
                    "handle": handle,
                    "discovery_queries": [],
                    "discovery_urls": [],
                    "discovery_texts": [],
                    "discovery_likes": 0,
                })
                if query not in entry["discovery_queries"]:
                    entry["discovery_queries"].append(query)
                if t.get("url") and t["url"] not in entry["discovery_urls"]:
                    entry["discovery_urls"].append(t["url"])
                    entry["discovery_texts"].append(t.get("text", ""))
                entry["discovery_likes"] = max(entry["discovery_likes"], int(t.get("likes") or 0))
            time.sleep(1.2)

    ordered = sorted(authors.values(), key=lambda r: (len(r["discovery_queries"]), r["discovery_likes"]), reverse=True)
    print(f"[discover] unique authors={len(ordered)} profiling={min(len(ordered), args.max_profiles)}", flush=True)

    rows = []
    ws = chrome.connect(args.port, timeout=60)
    try:
        for i, discovery in enumerate(ordered[: args.max_profiles], 1):
            handle = discovery["handle"]
            print(f"[discover] profile [{i}/{min(len(ordered), args.max_profiles)}] @{handle}", flush=True)
            try:
                meta = fetch_profile_meta(ws, args.port, handle, AS_OF)
            except Exception as e:
                print(f"[discover] profile error @{handle}: {e}", flush=True)
                continue
            row = {**discovery, **meta}
            months = int(row.get("months_since_joined") or 0)
            row["qualifies_recent_fast"] = bool(
                row.get("joined_within_year")
                and row.get("followers", 0) > row.get("follower_velocity_threshold", 0)
            )
            row["posts_pass"] = 0 < int(row.get("posts_count") or 0) <= args.max_posts
            row["semantic_review_required"] = True
            rows.append(row)
            print(
                "[discover] @{h} followers={f} joined={j} months={m} threshold={t} posts={p} fast={q}".format(
                    h=handle,
                    f=fmt_count(row.get("followers", 0)),
                    j=row.get("joined") or "?",
                    m=months or "?",
                    t=row.get("follower_velocity_threshold") or "?",
                    p=row.get("posts_raw") or row.get("posts_count") or "?",
                    q=row["qualifies_recent_fast"],
                ),
                flush=True,
            )
            time.sleep(0.7)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    rows.sort(
        key=lambda r: (
            bool(r.get("qualifies_recent_fast")),
            bool(r.get("posts_pass")),
            len(r.get("discovery_queries") or []),
            int(r.get("followers") or 0),
        ),
        reverse=True,
    )
    with open(args.output, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")

    qualified = [
        r for r in rows
        if r.get("qualifies_recent_fast") and r.get("posts_pass")
    ]
    print(json.dumps({
        "output": args.output,
        "authors": len(rows),
        "numeric_rule_candidates_for_semantic_review": len(qualified),
        "top_numeric_candidates": [
            {
                "handle": r["handle"],
                "followers": r["followers"],
                "joined": r["joined"],
                "threshold": r["follower_velocity_threshold"],
                "posts": r["posts_count"],
                "bio": compact(r.get("bio", ""), 160),
                "queries": r.get("discovery_queries", [])[:3],
            }
            for r in qualified[:10]
        ],
    }, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
