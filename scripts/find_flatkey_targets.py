#!/usr/bin/env python3
"""Find Flatkey X posts worth replying to.

Discovery only: this script does not generate replies, mutate the reply queue,
or post. It uses the logged-in Flatkey Chrome profile over CDP.
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

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock


SEARCH_POSTS_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el) {
        try {
            var head = (el.innerText || '').slice(0, 400);
            if (/Promoted/i.test(head)) return;
            if (/reposted/i.test(head)) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            if (!text) return;

            var urlEl = el.querySelector('a[href*="/status/"]');
            var url = urlEl ? urlEl.href : '';
            if (!url) return;

            var authorMatch = url.match(/x\.com\/([A-Za-z0-9_]+)\/status\//);
            var author = authorMatch ? authorMatch[1] : '';

            var replyEl = el.querySelector('[data-testid="reply"]');
            var rtEl = el.querySelector('[data-testid="retweet"]');
            var likeEl = el.querySelector('[data-testid="like"]');
            var timeEl = el.querySelector('time');

            var userArea = el.querySelector('[data-testid="User-Name"]');
            var displayName = '';
            if (userArea) {
                var spans = userArea.querySelectorAll('span');
                if (spans.length) displayName = (spans[0].innerText || '').trim();
            }

            out.push({
                author: author,
                display_name: displayName,
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


POSITIVE_PATTERNS = [
    (r"\bclaude code\b", 8, "Claude Code"),
    (r"\bcursor\b", 5, "Cursor"),
    (r"\bopenrouter\b", 7, "OpenRouter"),
    (r"\bopenclaw\b", 6, "OpenClaw"),
    (r"\bcoding agent(s)?\b", 7, "coding agents"),
    (r"\bai agent(s)?\b", 4, "AI agents"),
    (r"\bagentic\b", 3, "agents"),
    (r"\btool call(s)?\b", 6, "tool calls"),
    (r"\btoken cost(s)?\b", 10, "token cost"),
    (r"\bllm cost(s)?\b", 10, "LLM cost"),
    (r"\bapi cost(s)?\b", 8, "API cost"),
    (r"\bapi credit(s)?\b", 8, "API credits"),
    (r"\bcredit(s)?\b", 2, "credits"),
    (r"\brate limit(s)?\b", 7, "rate limits"),
    (r"\bcontext window\b", 8, "context window"),
    (r"\bcontext engineering\b", 8, "context engineering"),
    (r"\bcontext\b", 3, "context"),
    (r"\bprompt caching\b", 8, "prompt caching"),
    (r"\bcache miss(es)?\b", 6, "cache misses"),
    (r"\bmodel routing\b", 10, "model routing"),
    (r"\bllm routing\b", 10, "LLM routing"),
    (r"\brouter\b", 4, "router"),
    (r"\bfallback(s)?\b", 3, "fallbacks"),
    (r"\bmodel switch(ing|es)?\b", 5, "model switching"),
    (r"\binput token(s)?\b", 7, "input tokens"),
    (r"\boutput token(s)?\b", 7, "output tokens"),
    (r"\btoken(s)?\b", 3, "tokens"),
    (r"\bretr(y|ies|ied)\b", 5, "retries"),
    (r"\bspend\b", 4, "spend"),
    (r"\bexpensive\b", 4, "expensive"),
    (r"\bpricing\b", 5, "pricing"),
    (r"\blatency\b", 3, "latency"),
    (r"\bcost per\b", 7, "cost per unit"),
]

CONTEXT_TERMS = [
    "llm", "claude", "openai", "anthropic", "gpt", "gemini", "api",
    "model", "prompt", "context", "cursor", "coding agent", "openrouter",
    "inference", "tool call", "agent", "tokens", "token",
]

NEGATIVE_TERMS = [
    "crypto", "web3", "tokenomics", "staking", "governance", "airdrop",
    "memecoin", "defi", "nft", "blockchain", "solana", "wallet", "dao",
    "coinbase", "binance", "yield farm", "liquidity pool",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _parse_count(raw: str) -> int:
    if not raw:
        return 0
    s = str(raw).strip().replace(",", "").upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1000000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1000000000, s[:-1]
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


def _normalize_row(row: dict, source_query: str, source_kind: str) -> dict:
    likes = _parse_count(row.get("likes_raw", ""))
    replies = _parse_count(row.get("replies_raw", ""))
    reposts = _parse_count(row.get("reposts_raw", ""))
    return {
        "id": _status_id(row.get("url", "")),
        "author": (row.get("author") or "").lstrip("@"),
        "display_name": row.get("display_name", ""),
        "url": row.get("url", ""),
        "text": (row.get("text") or "").strip(),
        "likes": likes,
        "replies": replies,
        "reposts": reposts,
        "engagement": likes + replies * 2 + reposts * 3,
        "iso": row.get("iso", ""),
        "age_hours": round(_age_hours(row.get("iso", "")), 2),
        "source_query": source_query,
        "source_kind": source_kind,
    }


def _has_llm_context(text_l: str) -> bool:
    return any(term in text_l for term in CONTEXT_TERMS)


def _score_relevance(text: str) -> tuple[int, list[str], list[str], str]:
    text_l = text.lower()
    labels = []
    score = 0
    for pattern, weight, label in POSITIVE_PATTERNS:
        if re.search(pattern, text_l):
            score += weight
            if label not in labels:
                labels.append(label)

    negatives = [term for term in NEGATIVE_TERMS if term in text_l]
    if negatives:
        score -= 6 * len(negatives)

    if negatives and "token" in text_l and not _has_llm_context(text_l):
        return score, labels, negatives, "crypto/tokenomics without LLM context"
    if score < 6:
        return score, labels, negatives, "not enough Flatkey relevance"
    return score, labels, negatives, ""


def _reply_angle(row: dict, labels: list[str]) -> str:
    label_set = set(labels)
    text_l = row["text"].lower()
    if "model routing" in label_set or "LLM routing" in label_set or "OpenRouter" in label_set or "router" in label_set:
        return "Ask whether routing is decided per step or per task, and tie it to fallback budgets and latency."
    if "Claude Code" in label_set or "rate limits" in label_set or "API credits" in label_set:
        return "Ask whether the bottleneck is context growth, retries, or credit ceilings; steer toward cost per completed task."
    if "context window" in label_set or "context engineering" in label_set or "context" in label_set:
        return "Call out stale context and prompt bloat; ask what should stay in context versus get summarized or cached."
    if "prompt caching" in label_set or "cache misses" in label_set:
        return "Ask where cache misses happen, especially when agents mutate prompts or tool state between steps."
    if "coding agents" in label_set or "AI agents" in label_set or "tool calls" in label_set or "Cursor" in label_set:
        return "Point at planning loops, edit/test retries, and tool-call overhead as the hidden spend."
    if "pricing" in label_set or "LLM cost" in label_set or "API cost" in label_set or "token cost" in label_set:
        return "Frame the practical metric as cost per completed task, not cheapest input token."
    if "expensive" in text_l or "spend" in text_l:
        return "Ask which workflow step is burning spend: long context, retries, or model choice."
    return "Add one concrete cost/routing mechanism and avoid pitching Flatkey directly."


def _draft_reply(row: dict, labels: list[str]) -> str:
    label_set = set(labels)
    if "model routing" in label_set or "LLM routing" in label_set or "OpenRouter" in label_set:
        return "The practical routing question is per-step vs per-task. A cheap model on the wrong step can cost more through retries than a stronger model once."
    if "Claude Code" in label_set or "rate limits" in label_set or "API credits" in label_set:
        return "The useful metric is cost per completed task, not tokens per prompt. Claude Code limits usually expose context growth and retry loops first."
    if "context window" in label_set or "context engineering" in label_set or "context" in label_set:
        return "The hidden cost is stale context that keeps getting re-sent. The hard part is deciding what stays live, what gets summarized, and what can be cached."
    if "prompt caching" in label_set or "cache misses" in label_set:
        return "Prompt caching only helps if the stable prefix actually stays stable. Agents often miss the cache by mutating instructions or tool state every step."
    if "coding agents" in label_set or "AI agents" in label_set or "tool calls" in label_set:
        return "Most agent spend hides in planning loops, tool calls, and retries. The better benchmark is cost per finished task, not cost per model call."
    return "The real comparison is cost per completed workflow. Token price matters less when the workflow burns budget through retries or unnecessary context."


def _accept_candidate(row: dict, args, blocked_handles: set[str]) -> tuple[bool, str, list[str], int]:
    if not row["id"] or not row["url"] or not row["author"] or not row["text"]:
        return False, "missing id/url/author/text", [], 0
    if row["author"].lower() in blocked_handles or row["author"].lower() == "flatkey":
        return False, "blocked/self author", [], 0
    if row["age_hours"] > args.max_age_hours:
        return False, f"too old ({row['age_hours']}h)", [], 0
    if row["likes"] < args.min_likes:
        return False, f"too few likes ({row['likes']})", [], 0
    if row["replies"] > args.max_replies:
        return False, f"too many replies ({row['replies']})", [], 0
    if len(row["text"]) < args.min_length:
        return False, "too short", [], 0

    relevance, labels, negatives, reason = _score_relevance(row["text"])
    if reason:
        return False, reason, labels, relevance
    if negatives:
        row["negative_terms"] = negatives
    return True, "", labels, relevance


def _collect_search(ws, port: int, query: str, mode: str, scrolls: int, wait: float) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    url = f"https://x.com/search?q={q}&src=typed_query&f={mode}"
    _log(f"search {mode}: {query}")
    rows = []
    with chrome_lock(port, timeout=1800, on_wait=lambda s: _log(f"  waiting for Chrome lock {s:.0f}s")):
        chrome.navigate(ws, url, wait=wait + 2.5)
        chrome.set_viewport(ws, 1400, 2200)
        for i in range(scrolls + 1):
            raw = chrome.eval_js(ws, SEARCH_POSTS_JS)
            try:
                batch = json.loads(raw) if raw else []
            except Exception:
                batch = []
            for row in batch:
                rows.append(_normalize_row(row, query, f"search_{mode}"))
            _log(f"  scroll {i}: captured {len(rows)} rows")
            chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.82))")
            time.sleep(wait)
    return rows


def _collect_profile(ws, port: int, handle: str, scrolls: int, wait: float) -> list[dict]:
    h = handle.strip().lstrip("@")
    if not h:
        return []
    _log(f"profile @{h}")
    rows = []
    with chrome_lock(port, timeout=1800, on_wait=lambda s: _log(f"  waiting for Chrome lock {s:.0f}s")):
        chrome.navigate(ws, f"https://x.com/{h}", wait=wait + 2.5)
        chrome.set_viewport(ws, 1400, 2200)
        for i in range(scrolls + 1):
            raw = chrome.eval_js(ws, SEARCH_POSTS_JS)
            try:
                batch = json.loads(raw) if raw else []
            except Exception:
                batch = []
            for row in batch:
                rows.append(_normalize_row(row, f"profile:@{h}", "target_profile"))
            _log(f"  scroll {i}: captured {len(rows)} rows")
            chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.82))")
            time.sleep(wait)
    return rows


def _default_queries(cfg: dict) -> list[str]:
    kws = list(cfg.get("keyword_engage", {}).get("keywords", []))
    extras = [
        '"Claude Code" "rate limit"',
        '"Claude Code" "credits"',
        '"Claude Code" "expensive"',
        '"coding agent" "expensive"',
        '"coding agent" "retries"',
        '"LLM" "cost per task"',
        '"prompt caching" "Claude"',
        '"context window" "cost"',
        '"OpenRouter" "routing"',
        '"OpenRouter" "fallback"',
        '"API spend" "LLM"',
        '"tool calls" "cost"',
    ]
    seen = set()
    out = []
    for q in kws + extras:
        q = str(q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def _write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = [
        "rank", "score", "author", "url", "likes", "replies", "reposts",
        "age_hours", "labels", "source_query", "reply_angle", "draft_reply",
        "text",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, row in enumerate(rows, 1):
            w.writerow({
                "rank": i,
                "score": row.get("score", 0),
                "author": row.get("author", ""),
                "url": row.get("url", ""),
                "likes": row.get("likes", 0),
                "replies": row.get("replies", 0),
                "reposts": row.get("reposts", 0),
                "age_hours": row.get("age_hours", ""),
                "labels": ", ".join(row.get("labels", [])),
                "source_query": row.get("source_query", ""),
                "reply_angle": row.get("reply_angle", ""),
                "draft_reply": row.get("draft_reply", ""),
                "text": row.get("text", ""),
            })


def _write_markdown(path: str, rows: list[dict], author_targets: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Flatkey Reply Targets",
        "",
        f"Generated: {_ts()}",
        "",
    ]
    for i, row in enumerate(rows, 1):
        labels = ", ".join(row.get("labels", []))
        lines.extend([
            f"## {i}. @{row['author']} - score {row['score']}",
            "",
            f"- URL: {row['url']}",
            f"- Age: {row['age_hours']}h; likes {row['likes']}; replies {row['replies']}; reposts {row['reposts']}",
            f"- Match: {labels}",
            f"- Source: {row['source_kind']} / {row['source_query']}",
            f"- Why respond: {row['reply_angle']}",
            f"- Draft: {row['draft_reply']}",
            "",
            row["text"],
            "",
        ])
    if author_targets:
        lines.extend(["# Candidate Authors To Add", ""])
        for row in author_targets:
            lines.append(f"- @{row['author']}: {row['count']} candidate(s), best score {row['best_score']}, labels {row['labels']}")
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _author_targets(rows: list[dict], configured_targets: set[str]) -> list[dict]:
    grouped = {}
    for row in rows:
        key = row["author"].lower()
        if key in configured_targets:
            continue
        g = grouped.setdefault(key, {
            "author": row["author"],
            "count": 0,
            "best_score": 0,
            "labels": set(),
        })
        g["count"] += 1
        g["best_score"] = max(g["best_score"], row.get("score", 0))
        g["labels"].update(row.get("labels", []))
    out = []
    for g in grouped.values():
        if g["count"] >= 2 or g["best_score"] >= 25:
            out.append({
                "author": g["author"],
                "count": g["count"],
                "best_score": g["best_score"],
                "labels": ", ".join(sorted(g["labels"])),
            })
    out.sort(key=lambda x: (x["count"], x["best_score"]), reverse=True)
    return out[:20]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT_DIR, "accounts", "flatkey", "engage_config.json"))
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--queries", default="", help="comma-separated queries; defaults to Flatkey config keywords plus extras")
    ap.add_argument("--max-queries", type=int, default=18)
    ap.add_argument("--results-per-query", type=int, default=20)
    ap.add_argument("--search-scrolls", type=int, default=2)
    ap.add_argument("--profile-scrolls", type=int, default=1)
    ap.add_argument("--include-target-profiles", action="store_true")
    ap.add_argument("--max-age-hours", type=float, default=48)
    ap.add_argument("--min-likes", type=int, default=2)
    ap.add_argument("--max-replies", type=int, default=220)
    ap.add_argument("--min-length", type=int, default=25)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--wait", type=float, default=1.35)
    ap.add_argument("--output", default=os.path.join(ROOT_DIR, "state", "flatkey_target_candidates.json"))
    ap.add_argument("--csv", default=os.path.join(ROOT_DIR, "state", "flatkey_target_candidates.csv"))
    ap.add_argument("--markdown", default=os.path.join(ROOT_DIR, "state", "flatkey_target_candidates.md"))
    args = ap.parse_args()

    cfg = _load_json(args.config, {})
    port = args.port or int(cfg.get("hunter_port") or 10006)
    if not chrome.ping(port):
        raise SystemExit(f"Chrome port {port} unavailable")

    configured_targets = {str(t).lower().lstrip("@") for t in cfg.get("target_accounts", [])}
    blocked_handles = {str(t).lower().lstrip("@") for t in cfg.get("blocked_handles", [])}

    queries = [q.strip() for q in args.queries.split(",") if q.strip()] if args.queries else _default_queries(cfg)
    queries = queries[: max(1, args.max_queries)]

    all_rows = []
    ws = chrome.connect(port, timeout=60)
    try:
        for query in queries:
            for mode in ("live", "top"):
                rows = _collect_search(ws, port, query, mode, args.search_scrolls, args.wait)
                all_rows.extend(rows[: args.results_per_query])
        if args.include_target_profiles:
            for handle in cfg.get("target_accounts", []):
                all_rows.extend(_collect_profile(ws, port, handle, args.profile_scrolls, args.wait))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    dedup = {}
    rejected = {}
    for row in all_rows:
        ok, reason, labels, relevance = _accept_candidate(row, args, blocked_handles)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        row["labels"] = labels
        row["relevance_score"] = relevance
        age_penalty = min(12, int(row["age_hours"] // 4))
        crowd_penalty = 6 if row["replies"] > 120 else 0
        target_boost = 4 if row["author"].lower() in configured_targets else 0
        row["score"] = relevance + min(24, row["engagement"] // 4) + target_boost - age_penalty - crowd_penalty
        row["reply_angle"] = _reply_angle(row, labels)
        row["draft_reply"] = _draft_reply(row, labels)
        existing = dedup.get(row["id"])
        if not existing or row["score"] > existing["score"]:
            dedup[row["id"]] = row

    rows = list(dedup.values())
    rows.sort(key=lambda r: (r["score"], r["relevance_score"], r["engagement"]), reverse=True)
    rows = rows[: args.limit]
    author_targets = _author_targets(rows, configured_targets)

    report = {
        "generated_at": _ts(),
        "account": cfg.get("hunter_handle", "flatkey"),
        "port": port,
        "query_count": len(queries),
        "raw_rows": len(all_rows),
        "candidate_count": len(rows),
        "rejected": rejected,
        "candidates": rows,
        "candidate_authors_to_add": author_targets,
    }
    _save_json(args.output, report)
    _write_csv(args.csv, rows)
    _write_markdown(args.markdown, rows, author_targets)

    _log(json.dumps({
        "output": args.output,
        "csv": args.csv,
        "markdown": args.markdown,
        "raw_rows": len(all_rows),
        "candidates": len(rows),
        "rejected": rejected,
    }, indent=2))

    for i, row in enumerate(rows[:10], 1):
        _log(
            f"{i}. @{row['author']} score={row['score']} "
            f"likes={row['likes']} replies={row['replies']} age={row['age_hours']}h "
            f"{row['url']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
