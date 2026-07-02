"""Flatkey autonomous content runner.

Finds public X posts inside Flatkey's AI-agent infrastructure/operator lane,
classifies them, and optionally queues quote drafts for review. It does not post
originals or quotes automatically. Reposts are queued for review when --queue is
enabled; plain reposts require an explicit CLI flag to post without review.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
STATE_DIR = os.path.join(ROOT_DIR, "state")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")
SEEN_PATH = os.path.join(STATE_DIR, "flatkey_autonomous_seen.json")
PLAYBOOK_PATH = os.path.join(ROOT_DIR, "accounts", "flatkey", "playbook.md")

sys.path.insert(0, SCRIPTS_DIR)

import chrome as _chrome
import engage as _engage
import env as _env
import generate as _generate
import quote_scout as _quote_scout
import telegram as _telegram
from lock import file_lock

_env.load()


WEAK_QUOTE_PHRASES = [
    "agent workflows need",
    "worth tracking",
    "the hard part is",
    "operator-grade automation",
    "infrastructure problem",
    "coding loops scale",
    "persistent memory flips",
    "routing only wins",
    "routing becomes",
    "how do you",
    "what's the",
    "what is the",
]

JARGON_TERMS = [
    "agent", "agents", "workflow", "workflows", "infrastructure", "context",
    "routing", "permission", "permissions", "verification", "runtime",
    "automation", "cost", "token", "tokens", "model", "models", "state",
    "sandbox", "credential", "credentials", "orchestrator", "fallback",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _json_from_model(raw: str, default):
    text = (raw or "").strip()
    if not text:
        return default
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    try:
        return json.loads(text)
    except Exception:
        return default


def _account_path(rel_path: str) -> str:
    return rel_path if os.path.isabs(rel_path) else os.path.join(ROOT_DIR, rel_path)


def _norm_handle(handle: str) -> str:
    return (handle or "").strip().lstrip("@").lower()


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _load_configs():
    account_cfg = _load_json(os.path.join(ROOT_DIR, "accounts", "flatkey", "config.json"), {})
    engage_cfg = _load_json(os.path.join(ROOT_DIR, "accounts", "flatkey", "engage_config.json"), {})
    policy_path = _account_path(account_cfg.get(
        "autonomous_content_policy",
        "accounts/flatkey/autonomous_content_policy.json",
    ))
    policy = _load_json(policy_path, {})
    if account_cfg.get("handle") != "flatkey" or engage_cfg.get("hunter_handle") != "flatkey":
        raise SystemExit("flatkey configs are missing or mismatched")
    return account_cfg, engage_cfg, policy


def _approved_sources(policy: dict) -> set:
    clusters = policy.get("approved_source_clusters", {})
    handles = []
    for values in clusters.values():
        handles.extend(values or [])
    return {_norm_handle(h) for h in handles}


def _product_claim_violation(text: str) -> str:
    """Conservative Flatkey-specific safety gate for autonomous drafts."""
    hay = _norm_text(text)
    blocked_patterns = [
        (r"\bflatkey\b.*\b(live|launched|available|shipping|supports?|integrates?)\b", "product_live_or_support_claim"),
        (r"\b(flatkey|we)\b.*\b(save[sd]?|saving|cut|reduce[sd]?)\b.*\b\d+[%$]?", "savings_claim"),
        (r"\b(flatkey|we)\b.*\b(api key|credits?|waitlist|access|try it|get started)\b", "access_or_credits_claim"),
        (r"\b(customer|user|usage|served|requests?|api calls?)\b.*\b(flatkey|we)\b", "customer_or_usage_claim"),
        (r"\b(staking|governance|airdrop|tokenomics|token utility)\b", "crypto_tokenomics"),
        (r"\bthis is huge\b|\bgame changer\b|\bfuture of ai\b", "generic_hype"),
    ]
    for pattern, reason in blocked_patterns:
        if re.search(pattern, hay):
            return reason
    return ""


def _draft_violation(text: str) -> str:
    """Safety gate for generated Flatkey text before it can enter review."""
    product_violation = _product_claim_violation(text)
    if product_violation:
        return product_violation
    hay = _norm_text(text)
    if re.search(r"(\$\s*\d+|\b\d+(?:\.\d+)?\s*(?:x|%|k|m|b|tokens?|credits?|calls?|requests?|users?|customers?|prs?)\b)", hay):
        return "unsupported_numeric_claim"
    return ""


def _quote_quality_issues(text: str) -> list[str]:
    """Reject drafts that are safe but unlikely to earn public attention."""
    raw = re.sub(r"\s+", " ", text or "").strip()
    hay = raw.lower()
    issues: list[str] = []
    if not raw:
        return ["empty"]
    if len(raw) < 70:
        issues.append("too_short_to_land_a_take")
    if len(raw) > 240:
        issues.append("too_long_for_quote_hook")
    if len([s for s in re.split(r"[.!?]+", raw) if s.strip()]) > 2:
        issues.append("too_many_sentences")
    if raw.count("?") > 1:
        issues.append("too_many_questions")
    if raw.endswith("?"):
        issues.append("ends_as_question_instead_of_point_of_view")
    if sum(raw.count(ch) for ch in [",", ";", ":"]) >= 5:
        issues.append("too_listy_or_clause_heavy")
    for phrase in WEAK_QUOTE_PHRASES:
        if phrase in hay:
            issues.append(f"weak_phrase:{phrase}")
    if re.match(r"^(?:[a-z0-9+/#.-]+\s+){0,3}agents?\s+need\b", hay):
        issues.append("generic_agents_need_opening")
    if re.match(r"^(?:model\s+)?routing\s+(only wins|becomes|needs|matters)\b", hay):
        issues.append("generic_routing_opening")
    jargon_hits = sum(1 for term in JARGON_TERMS if re.search(rf"\b{re.escape(term)}\b", hay))
    if jargon_hits >= 7:
        issues.append("jargon_dense")
    if not any(marker in hay for marker in [
        "not ", "isn't", "is not", "but ", "until ", "once ", "when ",
        "without ", "instead", "moves", "moved", "turns", "breaks",
        "leaks", "burns", "hides", "reveals", "cheap ", "expensive",
        "most ", "real ",
    ]):
        issues.append("no_tension_or_contrast")
    if re.search(r"\b(need|needs|should|must)\b.{0,60}\b(before|when|if|without)\b", hay) and jargon_hits >= 5:
        issues.append("sounds_like_internal_policy_note")
    return issues


def _quote_pull_issue(engage_cfg: dict, candidate: dict, approved_sources: set) -> str:
    """Quote tweets should borrow existing reach unless the source is strategic."""
    qcfg = engage_cfg.get("autonomous_quote_quality") or {}
    min_likes = int(qcfg.get("min_quote_likes") or 40)
    min_replies = int(qcfg.get("min_quote_replies") or 8)
    min_reposts = int(qcfg.get("min_quote_reposts") or 5)
    author = _norm_handle(candidate.get("author", ""))
    if author in approved_sources:
        return ""
    likes = int(candidate.get("likes") or 0)
    replies = int(candidate.get("replies") or 0)
    reposts = int(candidate.get("reposts") or candidate.get("retweets") or 0)
    if likes >= min_likes:
        return ""
    if replies >= min_replies and likes >= max(10, min_likes // 4):
        return ""
    if reposts >= min_reposts:
        return ""
    return f"low_public_pull:{likes}likes/{replies}replies/{reposts}reposts"


def _signals(text: str, author: str, approved: set) -> set:
    hay = _norm_text(text)
    found = set()
    signal_terms = {
        "cost": ["token cost", "llm cost", "api cost", "spend", "expensive", "bill", "pricing"],
        "credits": ["api credits", "credits", "usage credits"],
        "rate_limit": ["rate limit", "rate-limit", "limit reset", "5h cap"],
        "context": ["context window", "context", "1m context", "200k"],
        "routing": ["model routing", "llm routing", "router", "fallback", "route"],
        "caching": ["prompt caching", "cache"],
        "tool_calls": ["tool calls", "tool-call", "mcp"],
        "benchmark": ["benchmark", "comparison", "eval", "latency", "cost per"],
        "coding_agent": ["claude code", "cursor", "openclaw", "replit", "bolt", "v0", "devin", "windsurf"],
        "agent_infra": ["agent runtime", "local agent", "computer use", "browser use", "memory", "tracing"],
        "trust_security": ["prompt injection", "credential", "credentials", "sandbox", "permission", "permissions", "oauth"],
        "agent_payments": ["x402", "micropayment", "micropayments", "payment", "payments", "metering", "wallet"],
    }
    for label, terms in signal_terms.items():
        if any(term in hay for term in terms):
            found.add(label)
    if _norm_handle(author) in approved:
        found.add("approved_source")
    return found


def _classify(candidate: dict, approved_sources: set) -> dict:
    text = candidate.get("text", "")
    author = candidate.get("author", "")
    sigs = _signals(text, author, approved_sources)
    if not sigs:
        return {"action": "skip", "reason": "no_flatkey_cost_or_routing_signal", "signals": []}
    if _product_claim_violation(text):
        return {"action": "skip", "reason": "source_text_hits_blocked_topic", "signals": sorted(sigs)}
    if {"benchmark", "routing", "trust_security", "agent_payments"} & sigs or ("approved_source" in sigs and {"cost", "context", "rate_limit", "coding_agent", "agent_infra"} & sigs):
        return {"action": "quote_candidate", "reason": "can_add_agent_operator_lens", "signals": sorted(sigs)}
    if {"cost", "credits", "rate_limit", "context", "coding_agent", "caching", "tool_calls", "agent_infra"} & sigs:
        return {"action": "plain_repost_candidate", "reason": "source_already_carries_public_cost_or_workflow_signal", "signals": sorted(sigs)}
    return {"action": "reply_candidate", "reason": "adjacent_but_needs_conversation_context", "signals": sorted(sigs)}


def _seen(candidate: dict, seen: dict, window_hours: int = 72) -> bool:
    tid = candidate.get("id") or candidate.get("url")
    if not tid or tid not in seen:
        return False
    try:
        then = datetime.fromisoformat(seen[tid])
    except Exception:
        return False
    return (datetime.now() - then).total_seconds() < window_hours * 3600


def _fetch_all(port: int, keywords: list[str], per_keyword: int, min_likes: int, max_age_minutes: int) -> list[dict]:
    ws = _chrome.connect(port)
    rows = []
    try:
        for kw in keywords:
            print(f"[{_ts()}] search {kw}", flush=True)
            candidates = _quote_scout._fetch_candidates(ws, port, kw, per_keyword)
            kept = 0
            for c in candidates:
                c["source_keyword"] = kw
                if c.get("likes", 0) < min_likes:
                    continue
                if c.get("age_minutes", 999999) > max_age_minutes:
                    continue
                if len(c.get("text", "")) < 30:
                    continue
                rows.append(c)
                kept += 1
            print(f"[{_ts()}] search {kw}: kept {kept}/{len(candidates)}", flush=True)
            time.sleep(random.uniform(1.5, 3.0))
    finally:
        try:
            ws.close()
        except Exception:
            pass
    rows.sort(key=lambda c: (c.get("likes", 0), c.get("replies", 0)), reverse=True)
    return rows


def _generate_flatkey_quote(engage_cfg: dict, candidate: dict, max_chars: int) -> dict:
    playbook = _load_text(PLAYBOOK_PATH)
    source = re.sub(r"\s+", " ", candidate.get("text", "")).strip()
    quote_limit = min(max_chars, 240)
    prompt = f"""You operate the Flatkey X account.

Flatkey playbook:
{playbook[:3400]}

Write three candidate quote-tweet comments for the source post below, then choose the strongest one.

Source metrics:
- likes: {candidate.get('likes', 0)}
- replies: {candidate.get('replies', 0)}
- age_minutes: {candidate.get('age_minutes', 0)}

Growth-quality rules:
- The quote must be follow-worthy for AI builders/operators, not merely safe.
- Lead with a tension, mechanism, or reversal: "not X, Y", "the bottleneck moved from X to Y", "once X happens, Y breaks", or a similarly sharp point.
- Make one point only. No shopping lists. No internal architecture memo tone.
- Use concrete words from the source post when possible. Avoid generic phrases like "agent workflows", "operator-grade", "worth tracking", and "infrastructure problem".
- Best range: 100-210 characters. Absolute max: {quote_limit}.
- No hashtags, emojis, URLs, or @-mentions.
- Do not mention Flatkey.
- Do not claim product capabilities, launch, integrations, users, access, benchmarks, savings, or private data.
- Do not summarize the source. Add a non-obvious mechanism, caveat, workflow implication, or cost/routing/security/payment lens.
- Do not end with a question.
- If the source is too generic or too weak to support a good quote, return an empty reply.

Source post by @{candidate.get('author')}:
{source[:1500]}

Return STRICT JSON only:
{{
  "op_summary": "one short source summary",
  "reply_angle": "one short description of the chosen angle",
  "variants": [
    {{"reply": "candidate quote", "why_it_can_pull_attention": "short reason"}}
  ],
  "reply": "the strongest candidate quote"
}}
"""
    msg = _generate._client_get().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1100,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _json_from_model(msg.content[0].text.strip(), {})
    candidates = []
    chosen = str(data.get("reply") or "").strip().strip('"')
    if chosen:
        candidates.append(chosen)
    for item in data.get("variants") or []:
        txt = str((item or {}).get("reply") or "").strip().strip('"')
        if txt and txt not in candidates:
            candidates.append(txt)

    reply = ""
    block_reason = ""
    for cand in candidates:
        cand = re.sub(r"\s+", " ", cand).strip()
        if len(cand) > quote_limit:
            cand = cand[:quote_limit].rsplit(" ", 1)[0].rstrip() + "..."
        violation = _draft_violation(cand)
        issues = _quote_quality_issues(cand)
        if not violation and not issues and not _chrome.text_repeats_itself(cand):
            reply = cand
            break
        block_reason = violation or ",".join(issues[:3]) or "repeating_draft"

    if not reply:
        data["reply_angle"] = f"blocked_quality:{block_reason or 'no_passing_variant'}"
    return {
        "op_summary": str(data.get("op_summary") or "")[:240],
        "reply_angle": str(data.get("reply_angle") or "")[:240],
        "reply": reply,
    }


def _draft_quote(engage_cfg: dict, candidate: dict, max_chars: int) -> dict:
    try:
        draft = _generate_flatkey_quote(engage_cfg, candidate, max_chars)
    except Exception as e:
        return {"ok": False, "reason": f"model_error:{e}", "draft": {}}
    text = (draft.get("reply") or "").strip()
    violation = _draft_violation(text)
    if violation:
        return {"ok": False, "reason": violation, "draft": draft}
    issues = _quote_quality_issues(text)
    if issues:
        return {"ok": False, "reason": ",".join(issues[:3]), "draft": draft}
    if not text or _chrome.text_repeats_itself(text):
        return {"ok": False, "reason": "weak_or_repeating_draft", "draft": draft}
    return {"ok": True, "reason": "", "draft": draft}


def _send_flatkey_card(entry: dict):
    token = os.environ.get("TELEGRAM_BOT_TOKEN_FLATKEY", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_FLATKEY", "")
    if not token or not chat_id:
        print("telegram send skipped: TELEGRAM_BOT_TOKEN_FLATKEY or TELEGRAM_CHAT_ID_FLATKEY is missing", flush=True)
        return
    try:
        msg_id = _telegram.send_reply_card(entry, bot_token=token, chat_id=chat_id)
    except Exception as e:
        print(f"telegram send failed: {e}", flush=True)
        return
    if not msg_id:
        print("telegram send returned no message_id", flush=True)
        return
    entry["telegram_message_id"] = msg_id
    with file_lock("reply_queue", on_wait=lambda s: print(f"lock wait {s}", flush=True)):
        queue = _load_json(QUEUE_PATH, [])
        for i, item in enumerate(queue):
            if item.get("id") == entry["id"]:
                queue[i] = entry
                _save_json(QUEUE_PATH, queue)
                break


def _queue_entry(entry: dict) -> str:
    with file_lock("reply_queue", on_wait=lambda s: print(f"lock wait {s}", flush=True)):
        queue = _load_json(QUEUE_PATH, [])
        queue.append(entry)
        _save_json(QUEUE_PATH, queue)
    _send_flatkey_card(entry)
    return entry["id"]


def _base_candidate_entry(kind: str, candidate: dict, classification: dict) -> dict:
    prefix = "flatkey_qt" if kind == "quote" else "flatkey_rp"
    return {
        "id": f"{prefix}_{int(time.time())}_{(candidate.get('id') or 'x')[-8:]}",
        "account": "flatkey",
        "kind": kind,
        "source": "flatkey_autonomous_execute",
        "source_keyword": candidate.get("source_keyword", ""),
        "target": (candidate.get("author") or "").lstrip("@"),
        "target_url": candidate.get("url", ""),
        "target_text": candidate.get("text", ""),
        "post_likes": candidate.get("likes", 0),
        "post_replies": candidate.get("replies", 0),
        "post_age_min": candidate.get("age_minutes", 0),
        "classification": classification,
        "status": "pending",
        "needs_human_approval": True,
        "queued_at": _ts(),
        "telegram_message_id": 0,
    }


def _queue_quote(engage_cfg: dict, candidate: dict, draft: dict, classification: dict) -> str:
    entry = {
        **_base_candidate_entry("quote", candidate, classification),
        "reply_text": (draft.get("reply") or "").strip(),
        "op_summary": draft.get("op_summary", ""),
        "reply_angle": draft.get("reply_angle", ""),
    }
    return _queue_entry(entry)


def _queue_repost(candidate: dict, classification: dict) -> str:
    entry = {
        **_base_candidate_entry("repost", candidate, classification),
        "reply_text": "",
        "op_summary": "Source already carries the Flatkey-relevant signal.",
        "reply_angle": "Plain repost after approval.",
    }
    return _queue_entry(entry)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="print classifications/drafts and do not write or post; this is the default")
    parser.add_argument("--queue", action="store_true",
                        help="queue safe quote and repost candidates for human review")
    parser.add_argument("--post-plain-reposts", action="store_true",
                        help="actually plain-repost eligible candidates")
    parser.add_argument("--keywords-per-run", type=int, default=6)
    parser.add_argument("--results-per-keyword", type=int, default=8)
    parser.add_argument("--min-likes", type=int, default=8)
    parser.add_argument("--max-age-minutes", type=int, default=720)
    parser.add_argument("--max-quotes", type=int, default=3)
    parser.add_argument("--max-reposts", type=int, default=5)
    args = parser.parse_args()

    account_cfg, engage_cfg, policy = _load_configs()
    port = int(account_cfg.get("chrome_port") or engage_cfg["hunter_port"])
    max_chars = int((engage_cfg.get("generation") or {}).get("max_post_chars", 280))
    approved = _approved_sources(policy)
    seen = _load_json(SEEN_PATH, {})

    keywords = policy.get("approved_search_queries") or engage_cfg.get("keyword_engage", {}).get("keywords", [])
    if not keywords:
        raise SystemExit("no approved search queries configured")
    chosen = random.sample(keywords, min(args.keywords_per_run, len(keywords)))

    print(f"[{_ts()}] flatkey autonomous run")
    print(f"keywords: {', '.join(chosen)}")
    print(f"queue={args.queue} post_plain_reposts={args.post_plain_reposts}")

    candidates = _fetch_all(port, chosen, args.results_per_keyword, args.min_likes, args.max_age_minutes)
    print(f"candidates after filters: {len(candidates)}")

    quote_count = 0
    repost_count = 0
    touched = 0

    for c in candidates:
        if _seen(c, seen):
            continue
        mark_seen = False
        classification = _classify(c, approved)
        action = classification["action"]
        author = (c.get("author") or "").lstrip("@")
        print()
        print(f"@{author} {c.get('likes', 0)} likes {c.get('age_minutes', '?')}m [{action}]")
        print(f"reason: {classification['reason']} | signals={','.join(classification['signals'])}")
        print(f"url: {c.get('url', '')}")
        print(f"text: {c.get('text', '')[:280]}")

        if action == "quote_candidate" and quote_count < args.max_quotes:
            pull_issue = _quote_pull_issue(engage_cfg, c, approved)
            if pull_issue:
                print(f"quote candidate skipped: {pull_issue}")
                mark_seen = True
            else:
                drafted = _draft_quote(engage_cfg, c, max_chars)
                if drafted["ok"]:
                    draft = drafted["draft"]
                    print(f"quote draft: {(draft.get('reply') or '').strip()}")
                    if args.queue:
                        qid = _queue_quote(engage_cfg, c, draft, classification)
                        print(f"queued: {qid}")
                        mark_seen = True
                    quote_count += 1
                    touched += 1
                else:
                    print(f"quote draft skipped: {drafted['reason']}")
                    mark_seen = True
        elif action == "plain_repost_candidate" and repost_count < args.max_reposts:
            if args.queue:
                qid = _queue_repost(c, classification)
                print(f"queued repost approval: {qid}")
                repost_count += 1
                touched += 1
                mark_seen = True
            elif args.post_plain_reposts:
                res = _engage.retweet_tweet(port, c.get("url", ""), dry_run=False)
                print(f"plain repost result: {res}")
                repost_count += 1
                touched += 1
                mark_seen = True

        if mark_seen:
            seen[c.get("id") or c.get("url")] = datetime.now().isoformat()

    if args.queue or args.post_plain_reposts:
        _save_json(SEEN_PATH, seen)
    print()
    repost_label = "repost_approvals" if args.queue and not args.post_plain_reposts else "plain_reposts"
    print(f"done. quote_drafts={quote_count} {repost_label}={repost_count} touched={touched}")


if __name__ == "__main__":
    main()
