"""Normalize X analytics CSV, enrich with local posting metadata, analyze,
and emit apply-ready recommendations for Hunter's workflow.

Default behavior is analysis-only. Pass --apply-config to make conservative
config edits based on the generated recommendations.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Dict, List, Tuple


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
import env; env.load()

QUEUE_PATH = ROOT_DIR / "state" / "reply_queue.json"
ENGAGEMENT_PATH = ROOT_DIR / "state" / "reply_engagement.json"
WINNING_REPLIES_PATH = ROOT_DIR / "state" / "winning_replies.json"
DEFAULT_CONFIG = ROOT_DIR / "accounts" / "GuoHunter95258" / "engage_config.json"


METRIC_COLUMNS = {
    "post_id": "Post id",
    "date": "Date",
    "post_text": "Post text",
    "post_link": "Post Link",
    "impressions": "Impressions",
    "likes": "Likes",
    "engagements": "Engagements",
    "bookmarks": "Bookmarks",
    "shares": "Shares",
    "new_follows": "New follows",
    "replies": "Replies",
    "reposts": "Reposts",
    "profile_visits": "Profile visits",
    "detail_expands": "Detail Expands",
    "url_clicks": "URL Clicks",
    "hashtag_clicks": "Hashtag Clicks",
    "permalink_clicks": "Permalink Clicks",
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def _write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_json(path: Path, payload):
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _to_int(raw) -> int:
    try:
        if raw is None or raw == "":
            return 0
        return int(str(raw).replace(",", "").strip())
    except Exception:
        return 0


def _safe_div(num: float, den: float) -> float:
    return round(num / den, 6) if den else 0.0


def _latest_path(pattern: str):
    paths = list((ROOT_DIR / "state").glob(pattern))
    if not paths:
        return None
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[0]


def _parse_ts(raw: str):
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _extract_status_id(url: str) -> str:
    if not url or "/status/" not in url:
        return ""
    tail = url.split("/status/", 1)[1]
    return "".join(ch for ch in tail if ch.isdigit())


def _classify_length(text: str) -> str:
    n = len((text or "").strip())
    if n < 90:
        return "short"
    if n < 180:
        return "medium"
    return "long"


def _reply_taste_features(text: str) -> dict:
    text = (text or "").strip()
    lower = text.lower()
    words = [w.strip(".,:;!?()[]{}\"'`").lower() for w in text.split()]
    first_words = " ".join(words[:4])
    return {
        "chars": len(text),
        "words": len([w for w in words if w]),
        "has_question": "?" in text,
        "has_number": any(ch.isdigit() for ch in text),
        "first_person": any(w in {"i", "we", "our", "my", "us"} for w in words),
        "causal_or_conditional": any(w in {"because", "since", "when", "if", "unless", "until"} for w in words),
        "contrast": any(w in {"but", "though", "instead", "except", "vs", "versus"} for w in words),
        "multiline": "\n" in text,
        "opener": first_words,
    }


def _top_rows(counter: dict, limit: int = 12) -> List[dict]:
    rows = []
    for key, value in counter.items():
        rows.append({"key": key, "value": value})
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]


def _load_reply_taste_context(config: dict, handle: str = "") -> dict:
    """Build compact strategy context for the analytics LLM.

    This is intentionally computed at runtime instead of stored as a plan file.
    Multica analytics tasks get the same context automatically whenever they run.
    """
    winning = _load_json(WINNING_REPLIES_PATH, [])
    popular_path = _latest_path("popular_replies_study_*.json")
    popular_payload = _load_json(popular_path, {}) if popular_path else {}
    popular = popular_payload.get("replies", []) if isinstance(popular_payload, dict) else []
    engagement = _load_json(ENGAGEMENT_PATH, {})

    owner = handle or config.get("hunter_handle", "")
    account_specific_harvest = owner.lower() == "guohunter95258"

    target_winners = defaultdict(lambda: {"samples": 0, "op_responses": 0, "high_likes": 0, "max_reply_likes": 0})
    for row in winning if isinstance(winning, list) else []:
        if not account_specific_harvest:
            continue
        target = (row.get("target_handle") or "").lstrip("@").lower()
        if not target:
            continue
        b = target_winners[target]
        b["samples"] += 1
        reasons = set(row.get("reasons") or [])
        if "op_responded" in reasons:
            b["op_responses"] += 1
        if "high_likes" in reasons:
            b["high_likes"] += 1
        b["max_reply_likes"] = max(b["max_reply_likes"], int(row.get("reply_likes") or 0))

    responsive_targets = []
    for target, b in target_winners.items():
        if b["samples"] <= 0:
            continue
        responsive_targets.append({
            "target": target,
            "samples": b["samples"],
            "op_response_rate": round(b["op_responses"] / b["samples"], 3),
            "op_responses": b["op_responses"],
            "high_like_winners": b["high_likes"],
            "max_reply_likes": b["max_reply_likes"],
        })
    responsive_targets.sort(
        key=lambda r: (r["op_response_rate"], r["op_responses"], r["high_like_winners"], r["max_reply_likes"]),
        reverse=True,
    )

    local_targets = defaultdict(lambda: {"posts": 0, "engaged": 0, "likes": 0, "replies": 0, "reposts": 0})
    for rec in engagement.values() if isinstance(engagement, dict) else []:
        reply_url = rec.get("reply_url") or ""
        if owner and f"x.com/{owner}/status/" not in reply_url:
            continue
        target = (rec.get("target") or "").lstrip("@").lower()
        if not target:
            continue
        checks = rec.get("checks") or []
        latest = checks[-1] if checks else {}
        likes = int(rec.get("final_likes") or latest.get("likes") or 0)
        replies = int(rec.get("final_replies") or latest.get("replies") or 0)
        reposts = int(rec.get("final_rts") or latest.get("rts") or 0)
        b = local_targets[target]
        b["posts"] += 1
        b["likes"] += likes
        b["replies"] += replies
        b["reposts"] += reposts
        if likes + replies + reposts > 0:
            b["engaged"] += 1

    local_target_results = []
    for target, b in local_targets.items():
        local_target_results.append({
            "target": target,
            "posts": b["posts"],
            "engaged_posts": b["engaged"],
            "engaged_rate": round(b["engaged"] / b["posts"], 3) if b["posts"] else 0,
            "weighted_score": b["likes"] + 2 * b["replies"] + 3 * b["reposts"],
            "likes": b["likes"],
            "replies": b["replies"],
            "reposts": b["reposts"],
        })
    local_target_results.sort(key=lambda r: (r["engaged_rate"], r["weighted_score"], r["posts"]), reverse=True)

    features = []
    top_examples = []
    for row in sorted(popular, key=lambda r: int(r.get("reply_likes") or 0), reverse=True)[:120]:
        text = row.get("reply_text") or ""
        f = _reply_taste_features(text)
        if f["chars"]:
            features.append(f)
        if len(top_examples) < 10:
            top_examples.append({
                "op": row.get("op_handle", ""),
                "reply_author": row.get("reply_author", ""),
                "reply_likes": int(row.get("reply_likes") or 0),
                "reply_text": text[:260],
            })

    opener_counts = defaultdict(int)
    for f in features:
        if f["opener"]:
            opener_counts[f["opener"]] += 1

    def share(key: str) -> float:
        if not features:
            return 0.0
        return round(sum(1 for f in features if f.get(key)) / len(features), 3)

    taste_summary = {
        "source": str(popular_path) if popular_path else "",
        "sample_size": len(features),
        "median_chars": int(median([f["chars"] for f in features])) if features else 0,
        "median_words": int(median([f["words"] for f in features])) if features else 0,
        "question_share": share("has_question"),
        "number_share": share("has_number"),
        "first_person_share": share("first_person"),
        "causal_or_conditional_share": share("causal_or_conditional"),
        "contrast_share": share("contrast"),
        "multiline_share": share("multiline"),
        "common_openers": _top_rows(opener_counts, 10),
        "top_examples": top_examples,
    }

    account_handle = handle or config.get("hunter_handle", "")
    account_dir = ROOT_DIR / "accounts" / account_handle
    judgement_rubric = _load_text(account_dir / "analytics_judgement.md", "")

    if account_handle.lower() == "voc_ai":
        identity = "ecommerce operators using buyer language, review mining, Amazon listing objections, returns, and product feedback"
    else:
        identity = "builders/operators using AI agents for GTM, automation, and workflow execution"

    return {
        "judgement_rubric": judgement_rubric[:8000],
        "strategic_identity": identity,
        "primary_problem": "candidate-set entry before ranking; judge whether content creates retrieval edges in one narrow audience cluster",
        "evidence_thresholds": {
            "hunter": {
                "daily_replies": "80-120",
                "daily_quotes": "6-10",
                "op_responses_per_day": "8+",
                "profile_visits_by_day_5": "150+",
                "profile_visits_by_day_10": "300+",
                "follows_by_day_10_to_14": "34+/day",
                "top_5_replies_avg_impressions": "500+",
                "replies_over_1000_impressions_per_day": "2+",
            },
            "voc_ai": {
                "daily_replies": "80-150",
                "daily_quotes": "6-10",
                "op_responses_per_day": "10+",
                "top_5_replies_quotes_avg_impressions": "700+",
                "avg_post_impressions_by_day_7": "600+",
                "avg_post_impressions_by_day_14": "1000+",
            },
        },
        "algorithms_to_evaluate": {
            "responsive_hub_scout": "rank accounts by op_reply_rate, audience_overlap, small_account_reply_visibility, post_frequency, conversation_quality, minus crowding",
            "candidate_set_entry_score": "rank reply opportunities by relevance, author response probability, early window, thread velocity, profile-click potential, minus saturation",
            "bandit_allocation": "shift daily volume by reward = 3*follows + 2*profile_visits + 2*op_responses + likes + replies + reposts",
            "negative_signal_guard": "penalize off-topic, automated-sounding, repetitive, product-pitch, overcrowded-thread replies",
        },
        "reply_taste_model": taste_summary,
        "responsive_targets_from_harvest": responsive_targets[:15],
        "local_target_results": local_target_results[:20],
        "account_specific_data_caveat": (
            "" if account_specific_harvest or local_target_results
            else "No account-specific responsive-target history found for this account yet; treat target advice as a measurement agenda, not a conclusion."
        ),
        "analysis_instruction": (
            "Do not use generic labels like proof/constraint/question as the final insight. "
            "Infer subtle tone and mechanism: status mismatch, specificity density, conversational affordance, "
            "whether the reply gives OP an easy next move, whether it compresses lived detail, "
            "and whether it would trigger profile_click/follow_author from the target cluster."
        ),
    }


def _leading_mention(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("@"):
        return ""
    token = text.split()[0].strip()
    token = token.lstrip("@").rstrip(":;,.)]")
    return "".join(ch for ch in token if ch.isalnum() or ch == "_")


def normalize_csv(csv_path: Path) -> List[dict]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    normalized = []
    for raw in rows:
        row = {"raw": raw}
        for out_key, col in METRIC_COLUMNS.items():
            value = raw.get(col, "")
            row[out_key] = _to_int(value) if out_key not in {"date", "post_text", "post_link"} else value
        row["status_id"] = _extract_status_id(row["post_link"])
        text = row["post_text"] or ""
        row["char_count"] = len(text.strip())
        row["length_bucket"] = _classify_length(text)
        row["has_question"] = "?" in text
        row["has_number"] = any(ch.isdigit() for ch in text)
        row["starts_with_mention"] = text.lstrip().startswith("@")
        row["leading_mention"] = _leading_mention(text)
        row["engagement_rate"] = _safe_div(row["engagements"], row["impressions"])
        row["follow_rate"] = _safe_div(row["new_follows"], row["impressions"])
        row["profile_visit_rate"] = _safe_div(row["profile_visits"], row["impressions"])
        normalized.append(row)
    return normalized


def _build_queue_indexes(queue_rows: List[dict]) -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, List[dict]]]:
    by_reply_url = {}
    by_status_id = {}
    by_text = defaultdict(list)
    for row in queue_rows:
        reply_url = row.get("reply_url_actual") or ""
        if reply_url:
            by_reply_url[reply_url] = row
            sid = _extract_status_id(reply_url)
            if sid:
                by_status_id[sid] = row
        text = _normalize_text(row.get("reply_text", ""))
        if text:
            by_text[text].append(row)
    return by_reply_url, by_status_id, by_text


def _match_queue_row(row: dict, by_reply_url: dict, by_status_id: dict, by_text: dict):
    reply_url = row.get("post_link", "")
    if reply_url in by_reply_url:
        return by_reply_url[reply_url], "reply_url"
    sid = row.get("status_id", "")
    if sid and sid in by_status_id:
        return by_status_id[sid], "status_id"
    text = _normalize_text(row.get("post_text", ""))
    if text and text in by_text:
        return by_text[text][0], "reply_text"
    return None, ""


def enrich_rows(normalized_rows: List[dict], queue_rows: List[dict], engagement_map: dict, config: dict) -> List[dict]:
    target_accounts = {t.lower() for t in config.get("target_accounts", [])}
    by_reply_url, by_status_id, by_text = _build_queue_indexes(queue_rows)
    enriched = []
    for row in normalized_rows:
        queue_row, match_type = _match_queue_row(row, by_reply_url, by_status_id, by_text)
        eng = engagement_map.get(row.get("post_link", ""), {})
        merged = dict(row)
        inferred_target = row.get("leading_mention", "")
        inferred_kind = "reply" if row.get("starts_with_mention") else "original"
        inferred_source = "target" if inferred_target.lower() in target_accounts and inferred_target else "unknown"
        merged["match_type"] = match_type
        merged["queue_found"] = bool(queue_row)
        merged["engagement_found"] = bool(eng)
        merged["metadata_inferred"] = bool(inferred_target or inferred_kind)
        merged["queue_id"] = (queue_row or {}).get("id", eng.get("queue_id", ""))
        merged["kind"] = (queue_row or {}).get("kind") or eng.get("kind") or inferred_kind
        merged["source"] = (queue_row or {}).get("source") or inferred_source
        merged["source_keyword"] = (queue_row or {}).get("source_keyword", "")
        merged["target"] = (queue_row or {}).get("target") or eng.get("target") or inferred_target
        merged["target_url"] = (queue_row or {}).get("target_url") or eng.get("target_url", "")
        merged["reply_text"] = (queue_row or {}).get("reply_text") or eng.get("reply_text", merged["post_text"])
        merged["reply_angle"] = (queue_row or {}).get("reply_angle", "")
        merged["op_summary"] = (queue_row or {}).get("op_summary", "")
        merged["posted_at"] = (queue_row or {}).get("posted_at") or eng.get("posted_at", "")
        checks = eng.get("checks") or []
        latest = checks[-1] if checks else {}
        merged["scored_likes"] = latest.get("likes", eng.get("final_likes", 0))
        merged["scored_replies"] = latest.get("replies", eng.get("final_replies", 0))
        merged["scored_rts"] = latest.get("rts", eng.get("final_rts", 0))
        merged["engaged_local"] = bool(eng.get("engaged"))
        merged["finalized_local"] = bool(eng.get("final_at"))
        enriched.append(merged)
    return enriched


def _compact_rows(rows: List[dict], limit: int = 25) -> List[dict]:
    out = []
    for row in rows[:limit]:
        out.append({
            "post_link": row.get("post_link", ""),
            "kind": row.get("kind", ""),
            "source": row.get("source", ""),
            "source_keyword": row.get("source_keyword", ""),
            "target": row.get("target", ""),
            "impressions": row.get("impressions", 0),
            "engagements": row.get("engagements", 0),
            "new_follows": row.get("new_follows", 0),
            "profile_visits": row.get("profile_visits", 0),
            "detail_expands": row.get("detail_expands", 0),
            "engagement_rate": row.get("engagement_rate", 0),
            "posted_at": row.get("posted_at", ""),
            "post_text": (row.get("post_text", "") or "")[:240],
            "metadata_source": (
                "local" if (row.get("queue_found") or row.get("engagement_found"))
                else "csv_inferred"
            ),
        })
    return out


def _metric_summary(rows: List[dict]) -> dict:
    if not rows:
        return {"count": 0}
    totals = defaultdict(float)
    for row in rows:
        for key in (
            "impressions",
            "likes",
            "engagements",
            "new_follows",
            "profile_visits",
            "detail_expands",
            "replies",
            "reposts",
        ):
            totals[key] += row.get(key, 0) or 0
    count = len(rows)
    return {
        "count": count,
        "avg_impressions": round(totals["impressions"] / count, 2),
        "avg_engagements": round(totals["engagements"] / count, 2),
        "avg_profile_visits": round(totals["profile_visits"] / count, 2),
        "avg_detail_expands": round(totals["detail_expands"] / count, 2),
        "avg_new_follows": round(totals["new_follows"] / count, 2),
        "avg_replies": round(totals["replies"] / count, 2),
        "avg_reposts": round(totals["reposts"] / count, 2),
        "engagement_rate": _safe_div(totals["engagements"], totals["impressions"]),
        "follow_rate": _safe_div(totals["new_follows"], totals["impressions"]),
        "profile_visit_rate": _safe_div(totals["profile_visits"], totals["impressions"]),
    }


def _group_summary(rows: List[dict], field: str, min_count: int = 1) -> dict:
    buckets = defaultdict(list)
    for row in rows:
        key = row.get(field) or "unknown"
        buckets[str(key)].append(row)
    out = {}
    for key, bucket in buckets.items():
        if len(bucket) < min_count:
            continue
        out[key] = _metric_summary(bucket)
    return dict(sorted(out.items(), key=lambda kv: kv[1].get("avg_impressions", 0), reverse=True))


def analyze_rows(rows: List[dict], config: dict) -> dict:
    overall = _metric_summary(rows)
    matched = [r for r in rows if r.get("queue_found") or r.get("engagement_found")]
    metadata_rows = [r for r in rows if r.get("target") or r.get("source") or r.get("kind")]
    by_kind = _group_summary(rows, "kind")
    by_source = _group_summary(rows, "source")
    by_target = _group_summary([r for r in rows if r.get("target")], "target")
    by_keyword = _group_summary([r for r in rows if r.get("source_keyword")], "source_keyword")
    by_question = _group_summary(rows, "has_question")
    by_number = _group_summary(rows, "has_number")
    by_length = _group_summary(rows, "length_bucket")

    return {
        "generated_at": _now_iso(),
        "overall": overall,
        "matched_rows": len(matched),
        "metadata_rows": len(metadata_rows),
        "unmatched_rows": len(rows) - len(matched),
        "by_kind": by_kind,
        "by_source": by_source,
        "by_target": by_target,
        "by_keyword": by_keyword,
        "by_question": by_question,
        "by_number": by_number,
        "by_length": by_length,
        "sample_rows": _compact_rows(sorted(rows, key=lambda r: r.get("impressions", 0), reverse=True)),
        "current_config": {
            "blocked_handles": config.get("blocked_handles", []),
            "target_accounts": config.get("target_accounts", []),
            "keywords": config.get("keyword_engage", {}).get("keywords", []),
        },
        "growth_context": _load_reply_taste_context(config, config.get("hunter_handle", "")),
    }


def _llm_recommendations(analysis: dict) -> dict:
    prompt = f"""You are reviewing X analytics for an automated posting system.

Goal:
- Use judgment, not rigid rules.
- Suggest concrete strategy changes based on the analytics sample and metadata.
- Judge performance against the 14-day candidate-set-entry strategy in growth_context.
- The system is trying to make the account legible to one narrow audience cluster, not to chase broad AI virality.
- Prefer evidence of profile_click, follow_author, OP response, and repeated retrieval-edge formation over raw likes.
- Only propose config actions that fit one of these supported actions:
  - block_target
  - unblock_target
  - promote_keyword
  - demote_keyword
- You may ALSO give higher-level strategy suggestions like "post more originals than replies" in plain language.

Constraints:
- Be conservative about config mutations when sample size is weak.
- It is okay to make a strategic suggestion without a config action.
- If metadata is inferred from the CSV itself, you can still reason from it.
- Always return 1-3 strategy_suggestions unless the sample is completely unusable.
- Strategy suggestions can include posting mix, originality, target selection,
  reply style, topic choice, or experimentation ideas.
- Do NOT give generic reply advice. Use growth_context.reply_taste_model and the actual analytics rows to infer subtle tone/mechanism.
- For reply style, talk about the underlying mechanism, not generic labels.
- If target data is weak, say exactly what the next scout/analytics run must measure.
- Output STRICT JSON only.

Return schema:
{{
  "summary": "very short plain-English summary",
  "performance_judgement": {{
    "candidate_set_entry": "weak|mixed|improving|strong",
    "reason": "one short sentence",
    "evidence_to_watch_next_run": ["short metric", "short metric"]
  }},
  "config_actions": [
    {{"action":"block_target|unblock_target|promote_keyword|demote_keyword","target":"","keyword":"","reason":"","confidence":"low|medium|high"}}
  ],
  "strategy_suggestions": ["short sentence", "short sentence"],
  "responsive_target_candidates": [
    {{"target":"handle","why":"short reason","next_test":"short test"}}
  ],
  "reply_taste_updates": [
    {{"pattern":"specific subtle pattern","why_it_worked_or_failed":"short reason","instruction_change":"short instruction"}}
  ],
  "algorithm_updates": [
    {{"algorithm":"responsive_hub_scout|candidate_set_entry_score|bandit_allocation|negative_signal_guard","change":"short change","evidence":"short evidence"}}
  ],
  "evidence_notes": ["short sentence", "short sentence"]
}}

Analytics data:
{json.dumps(analysis, ensure_ascii=False, indent=2)}
"""
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start:end + 1])
            else:
                raise
    except Exception:
        data = None
    if data is None:
        try:
            import anthropic
            client = anthropic.Anthropic()
            fallback_prompt = f"""Review this X analytics sample and give 1-3 short strategic suggestions.

Rules:
- Plain text only.
- One suggestion per line.
- Focus on posting mix, target choice, or reply style.
- If no confident change is justified, say what to test next.

Data:
{json.dumps(analysis, ensure_ascii=False, indent=2)}
"""
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": fallback_prompt}],
            )
            text = msg.content[0].text.strip()
            suggestions = [line.lstrip("- ").strip() for line in text.splitlines() if line.strip()]
            data = {
                "summary": "no changes applied",
                "performance_judgement": {
                    "candidate_set_entry": "mixed",
                    "reason": "fallback path used; no structured judgement available",
                    "evidence_to_watch_next_run": [],
                },
                "config_actions": [],
                "strategy_suggestions": suggestions[:3],
                "responsive_target_candidates": [],
                "reply_taste_updates": [],
                "algorithm_updates": [],
                "evidence_notes": [],
            }
        except Exception:
            data = {
                "summary": "no changes applied",
                "performance_judgement": {
                    "candidate_set_entry": "mixed",
                    "reason": "LLM recommendation failed",
                    "evidence_to_watch_next_run": [],
                },
                "config_actions": [],
                "strategy_suggestions": [],
                "responsive_target_candidates": [],
                "reply_taste_updates": [],
                "algorithm_updates": [],
                "evidence_notes": [],
            }
    if data.get("strategy_suggestions") == []:
        try:
            import anthropic
            client = anthropic.Anthropic()
            nudge_prompt = f"""Give 2 short strategic suggestions from this X analytics sample.

Rules:
- Plain text only.
- One suggestion per line.
- Must suggest what to try next even if you would not mutate config yet.

Data:
{json.dumps(analysis, ensure_ascii=False, indent=2)}
"""
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": nudge_prompt}],
            )
            text = msg.content[0].text.strip()
            suggestions = [line.lstrip("- ").strip() for line in text.splitlines() if line.strip()]
            data["strategy_suggestions"] = suggestions[:3]
        except Exception:
            pass
    data.setdefault("summary", "no changes applied")
    data.setdefault("performance_judgement", {})
    data.setdefault("config_actions", [])
    data.setdefault("strategy_suggestions", [])
    data.setdefault("responsive_target_candidates", [])
    data.setdefault("reply_taste_updates", [])
    data.setdefault("algorithm_updates", [])
    data.setdefault("evidence_notes", [])
    return data


def build_apply_plan(recommendations: dict, config: dict) -> dict:
    blocked = list(config.get("blocked_handles", []))
    keywords = list(config.get("keyword_engage", {}).get("keywords", []))
    target_accounts = list(config.get("target_accounts", []))
    notes = []

    for rec in recommendations.get("config_actions", []):
        action = rec.get("action", "")
        target = (rec.get("target") or "").lstrip("@")
        kw = rec.get("keyword", "")
        reason = rec.get("reason", "")
        if action == "block_target" and target:
            if target not in blocked:
                blocked.append(target)
                notes.append(f"block @{target} because {reason}")
        elif action == "unblock_target" and target:
            if target in blocked:
                blocked = [b for b in blocked if b != target]
                notes.append(f"unblock @{target} because {reason}")
        elif action == "promote_keyword" and kw:
            if kw in keywords:
                keywords = [k for k in keywords if k != kw]
                keywords.insert(0, kw)
                notes.append(f"promote keyword {kw} because {reason}")
        elif action == "demote_keyword" and kw:
            if kw in keywords:
                keywords = [k for k in keywords if k != kw] + [kw]
                notes.append(f"demote keyword {kw} because {reason}")

    active_targets = [t for t in target_accounts if t not in blocked]
    return {
        "blocked_handles": blocked,
        "keyword_engage_keywords": keywords,
        "active_target_accounts": active_targets,
        "notes": notes,
    }


def maybe_apply_config(config_path: Path, config: dict, apply_plan: dict) -> bool:
    changed = False
    new_blocked = apply_plan.get("blocked_handles", [])
    if new_blocked != config.get("blocked_handles", []):
        config["blocked_handles"] = new_blocked
        changed = True

    kw_cfg = dict(config.get("keyword_engage", {}))
    new_keywords = apply_plan.get("keyword_engage_keywords", kw_cfg.get("keywords", []))
    if new_keywords != kw_cfg.get("keywords", []):
        kw_cfg["keywords"] = new_keywords
        config["keyword_engage"] = kw_cfg
        changed = True

    if changed:
        _write_json(config_path, config)
    return changed


def _final_summary(recommendations: dict, apply_plan: dict, config_changed: bool) -> str:
    notes = apply_plan.get("notes") or []
    judgement = recommendations.get("performance_judgement") or {}
    if notes:
        lines = ["changes applied:"]
        for note in notes:
            lines.append(f"- {note}")
        if judgement.get("candidate_set_entry"):
            lines.append(f"- candidate-set entry: {judgement.get('candidate_set_entry')} — {judgement.get('reason', '')}")
        for suggestion in recommendations.get("strategy_suggestions", [])[:3]:
            lines.append(f"- strategy: {suggestion}")
        for update in recommendations.get("algorithm_updates", [])[:2]:
            lines.append(f"- algorithm: {update.get('algorithm')}: {update.get('change')}")
        return "\n".join(lines) + "\n"
    if config_changed:
        return "changes applied, but no human-readable notes were generated\n"
    suggestions = recommendations.get("strategy_suggestions", [])[:3]
    extras = recommendations.get("algorithm_updates", [])[:2]
    taste = recommendations.get("reply_taste_updates", [])[:2]
    targets = recommendations.get("responsive_target_candidates", [])[:3]
    if suggestions or extras or taste or targets or judgement:
        lines = ["no config changes applied:"]
        if judgement.get("candidate_set_entry"):
            lines.append(f"- candidate-set entry: {judgement.get('candidate_set_entry')} — {judgement.get('reason', '')}")
        for suggestion in suggestions:
            lines.append(f"- {suggestion}")
        for target in targets:
            lines.append(f"- target test: @{target.get('target')} — {target.get('next_test') or target.get('why')}")
        for item in taste:
            lines.append(f"- taste: {item.get('instruction_change') or item.get('pattern')}")
        for update in extras:
            lines.append(f"- algorithm: {update.get('algorithm')}: {update.get('change')}")
        return "\n".join(lines) + "\n"
    return "no changes applied\n"


def run(csv_path: Path, config_path: Path, apply_config: bool = False) -> dict:
    config = _load_json(config_path, {})
    normalized = normalize_csv(csv_path)
    queue_rows = _load_json(QUEUE_PATH, [])
    engagement_map = _load_json(ENGAGEMENT_PATH, {})
    enriched = enrich_rows(normalized, queue_rows, engagement_map, config)
    analysis = analyze_rows(enriched, config)
    recommendations = _llm_recommendations(analysis)
    apply_plan = build_apply_plan(recommendations, config)
    config_changed = maybe_apply_config(config_path, config, apply_plan) if apply_config else False

    summary = _final_summary(recommendations, apply_plan, config_changed)

    return {
        "status": "ok",
        "csv_path": str(csv_path),
        "rows": len(normalized),
        "matched_rows": analysis["matched_rows"],
        "metadata_rows": analysis["metadata_rows"],
        "config_changed": config_changed,
        "recommendation_count": len(recommendations.get("config_actions", [])),
        "summary": summary,
        "analysis": analysis,
        "recommendations": recommendations,
        "apply_plan": apply_plan,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--apply-config", action="store_true")
    args = parser.parse_args()

    result = run(
        csv_path=Path(args.csv),
        config_path=Path(args.config),
        apply_config=args.apply_config,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
