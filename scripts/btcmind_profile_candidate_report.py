#!/usr/bin/env python3
"""Classify cached profile-study posts for BTCMind quote/repost review.

This is read-only. It does not open X, send Telegram cards, or post anything.
It uses the same deterministic BTCMind autonomy policy as live repost scouting.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import btcmind_autonomy as _btcmind

DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "engage_config.json")
DEFAULT_GLOB = os.path.join(ROOT_DIR, "state", "*_profile_quote_repost_study_2026-06-06.json")
DEFAULT_OUTPUT = os.path.join(ROOT_DIR, "state", "btcmind_profile_candidate_report_2026-06-06.json")
DEFAULT_MD = os.path.join(ROOT_DIR, "state", "btcmind_profile_candidate_report_2026-06-06.md")


def _load(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _profile_from_path(path: str) -> str:
    name = os.path.basename(path)
    return name.split("_profile_", 1)[0]


def _score(row: dict) -> float:
    likes = int(row.get("likes") or 0)
    reposts = int(row.get("reposts") or 0)
    replies = int(row.get("replies") or 0)
    age = float(row.get("age_hours") or 999)
    freshness = max(0.0, 72.0 - age) / 12.0
    return likes + reposts * 4 + replies * 2 + freshness


def _candidate_from_row(row: dict, source_profile: str, source_file: str) -> dict:
    text = str(row.get("text") or "").strip()
    if len(text) < 30:
        text = str(row.get("all_text") or "").strip()
    return {
        "id": row.get("id") or "",
        "url": row.get("url") or "",
        "author": row.get("author") or source_profile,
        "text": text,
        "likes": int(row.get("likes") or 0),
        "reposts": int(row.get("reposts") or 0),
        "replies": int(row.get("replies") or 0),
        "age_minutes": int(float(row.get("age_hours") or 999) * 60),
        "age_hours": row.get("age_hours"),
        "profile_category": row.get("category") or "",
        "source_profile": source_profile,
        "source_file": source_file,
    }


def _short(text: str, limit: int = 260) -> str:
    return " ".join(str(text or "").split())[:limit]


def _markdown(report: dict) -> str:
    lines = [
        "# BTCMind Profile Candidate Report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    for k, v in report["summary"].items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## Plain Repost Candidates", ""])
    for c in report["plain_repost_candidates"]:
        lines.append(
            f"- @{c['author']} ({c['likes']} likes, {c.get('age_hours')}h, {c['source_profile']}): "
            f"{c['url']}\n  - {c['reason']}\n  - {_short(c['text'])}"
        )
    if not report["plain_repost_candidates"]:
        lines.append("- none")
    lines.extend(["", "## Quote Candidates For Approval", ""])
    for c in report["quote_candidates"]:
        lines.append(
            f"- @{c['author']} ({c['likes']} likes, {c.get('age_hours')}h, {c['source_profile']}): "
            f"{c['url']}\n  - {c['reason']}\n  - {_short(c['text'])}"
        )
    if not report["quote_candidates"]:
        lines.append("- none")
    lines.extend(["", "## Skipped Examples", ""])
    for c in report["skipped_examples"][:10]:
        lines.append(
            f"- @{c['author']} [{c['reason']}]: {c['url']}\n  - {_short(c['text'], 180)}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--input-glob", default=DEFAULT_GLOB)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--md-output", default=DEFAULT_MD)
    ap.add_argument("--min-likes", type=int, default=25)
    ap.add_argument("--max-age-hours", type=float, default=0.0,
                    help="0 means use repost_scout/policy max age")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    cfg = _load(args.config, {})
    policy, _facts = _btcmind.load_policy(cfg)
    if not policy:
        raise SystemExit("BTCMind policy missing")
    rcfg = cfg.get("repost_scout") or {}
    repost_rules = policy.get("repost_rules") or {}
    max_age_hours = float(args.max_age_hours or 0)
    if max_age_hours <= 0:
        max_age_minutes = int(rcfg.get("max_post_age_minutes") or repost_rules.get("maximum_age_minutes") or 720)
        max_age_hours = max_age_minutes / 60.0

    rows_seen = 0
    considered = 0
    classified: list[dict] = []
    for path in sorted(glob.glob(args.input_glob)):
        source_profile = _profile_from_path(path)
        for row in _load(path, []):
            rows_seen += 1
            if row.get("category") == "reply":
                continue
            if float(row.get("age_hours") or 999) > max_age_hours:
                continue
            if int(row.get("likes") or 0) < args.min_likes:
                continue
            c = _candidate_from_row(row, source_profile, path)
            if len(c["text"]) < 30:
                continue
            considered += 1
            verdict = _btcmind.classify_candidate(c, policy)
            c.update({
                "action": verdict.get("action", "skip"),
                "reason": verdict.get("reason", ""),
                "signals": verdict.get("signals", []),
                "score": round(_score(c), 2),
            })
            classified.append(c)

    classified.sort(key=lambda x: (x["action"] == "plain_repost_candidate", x["score"]), reverse=True)
    plain = [c for c in classified if c["action"] == "plain_repost_candidate"][: args.limit]
    quotes = sorted(
        [c for c in classified if c["action"] == "quote_candidate"],
        key=lambda x: x["score"],
        reverse=True,
    )[: args.limit]
    skips = sorted(
        [c for c in classified if c["action"] == "skip"],
        key=lambda x: x["score"],
        reverse=True,
    )[: args.limit]

    summary = {
        "source_files": len(glob.glob(args.input_glob)),
        "rows_seen": rows_seen,
        "considered": considered,
        "plain_repost_candidate": len([c for c in classified if c["action"] == "plain_repost_candidate"]),
        "quote_candidate": len([c for c in classified if c["action"] == "quote_candidate"]),
        "skip": len([c for c in classified if c["action"] == "skip"]),
    }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": os.path.abspath(args.config),
        "input_glob": args.input_glob,
        "max_age_hours": max_age_hours,
        "summary": summary,
        "plain_repost_candidates": plain,
        "quote_candidates": quotes,
        "skipped_examples": skips,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(args.md_output, "w") as f:
        f.write(_markdown(report))

    print(json.dumps({
        "output": args.output,
        "md_output": args.md_output,
        "summary": summary,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
