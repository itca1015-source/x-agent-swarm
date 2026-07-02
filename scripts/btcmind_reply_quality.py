from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Iterable, Mapping


X_ALGO_EVIDENCE = (
    "x_algorithm: SpamEapiLowFollowerClassifier renders ThreadRenderer.render(post)",
    "x_algorithm: reply ranking renders thread context before scoring",
    "x_algorithm: exact spam prompt and production thresholds are not public",
)

RISK_ORDER = {
    "auto_ok": 0,
    "approval_only": 1,
    "needs_rewrite": 2,
    "block": 3,
}

STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been", "before",
    "being", "between", "but", "can", "could", "did", "does", "doing", "for",
    "from", "have", "how", "into", "just", "like", "more", "most", "not",
    "now", "only", "other", "over", "really", "same", "should", "than",
    "that", "the", "their", "them", "there", "these", "they", "this", "those",
    "through", "under", "was", "what", "when", "where", "which", "while",
    "who", "why", "will", "with", "would", "your",
}

GENERIC_TOPIC_WORDS = {
    "adoption", "automation", "bottleneck", "constraint", "crypto", "data",
    "market", "mechanism", "metric", "signal", "system", "team", "teams",
    "user", "users",
}

GENERIC_PATTERNS = (
    (r"\bhidden constraint\b", "hidden_constraint_template", 0.25),
    (r"\breal constraint\b", "real_constraint_template", 0.25),
    (r"\breal signal\b", "real_signal_template", 0.25),
    (r"\breal bottleneck\b", "real_bottleneck_template", 0.25),
    (r"\breal issue\b", "real_issue_template", 0.20),
    (r"\bwhat changed\b", "what_changed_template", 0.22),
    (r"\bthis only works if\b", "only_works_if_template", 0.22),
    (r"\botherwise\b", "otherwise_template", 0.12),
    (r"\bmost teams measure\b", "most_teams_measure_template", 0.25),
    (r"\bcurious whether\b", "curious_whether_template", 0.18),
    (r"\bthe question is\b", "question_is_template", 0.14),
    (r"\bthe default take\b", "default_take_template", 0.14),
    (r"\bnot .{0,40}\bbut\b", "not_x_but_y_template", 0.18),
    (r"\bnot (?:a |an )?.{0,45}\bproblem\b.{0,12}\b(that'?s|that is)\b.{0,35}\bproblem\b", "consultant_contrast_problem_template", 0.28),
    (r"\bcomms problem\b", "comms_problem_template", 0.14),
    (r"\bpermissions? problem\b", "forced_permissions_problem", 0.22),
    (r"\bwho actually decides\b", "abstract_accountability_question", 0.18),
    (r"\boptimizes? for\b", "abstract_optimization_framing", 0.12),
    (r"\bmost teams\b", "most_teams_template", 0.22),
    (r"\bthe interesting part is\b", "interesting_part_template", 0.14),
    (r"\bwhat matters is\b", "what_matters_template", 0.16),
    (r"\bthe failure mode is\b", "failure_mode_template", 0.16),
    (r"\bthe operator question\b", "operator_question_template", 0.18),
)

AUTHORITY_CLAIM_PATTERNS = (
    r"\bwe\s+(built|build|tracked|track|saw|see|observed|observe|measured|measure|found|tested|test|mapped|monitored|shipped|ship)\b",
    r"\bour\s+(data|dashboard|model|system|tracking|research|tests?)\b",
    r"\bdata\s+(shows|showed|suggests|suggested)\b",
)

TRADING_TERMS = {
    "buy", "sell", "long", "short", "entry", "entries", "target", "targets",
    "stop", "stoploss", "leverage", "liquidation", "liquidations", "pump",
    "dump", "moon", "breakout", "takeprofit",
}

DIRECT_ADVICE_PATTERNS = (
    r"\b(buy|sell|long|short)\s+\$?[a-z0-9_]{2,}\b",
    r"\b(go|going)\s+(long|short)\b",
    r"\b(entry|entries)\s+(is|are|at|around)\b",
    r"\b(target|targets)\s+(is|are|at|around)\b",
    r"\b(stop|stop loss|stoploss)\s+(is|at|around)\b",
    r"\b(this|it)\s+(will|should)\s+(pump|dump|moon|break out)\b",
)

NON_ADVISORY_MARKERS = (
    "market structure",
    "risk management",
    "liquidity",
    "derivatives",
    "positioning",
    "mechanism",
    "flow",
    "flows",
)

STRUCTURE_TOPIC_WORDS = (
    "bitcoin", "btc", "ethereum", "eth", "stablecoin", "stablecoins", "wallet",
    "wallets", "onchain", "on-chain", "token", "tokens", "exchange", "exchanges",
    "liquidity", "liquidation", "liquidations", "bridge", "bridging", "custody",
    "treasury", "treasuries", "etf", "derivatives", "perp", "perps", "defi",
    "solana", "base", "rwa", "compliance", "security",
)


def _cfg(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return cfg if isinstance(cfg, Mapping) else {}


def _quality_cfg(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    root = _cfg(cfg)
    qcfg = root.get("reply_quality_audit")
    return qcfg if isinstance(qcfg, Mapping) else {}


def _account_label(cfg: Mapping[str, Any] | None) -> str:
    root = _cfg(cfg)
    qcfg = _quality_cfg(cfg)
    label = str(
        qcfg.get("account_label")
        or root.get("telegram_label")
        or root.get("hunter_handle")
        or root.get("x_handle")
        or "this account"
    ).strip().lstrip("@")
    return label or "this account"


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_'-]*|\$[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?%?", text or "")


def _anchor_tokens(text: str) -> set[str]:
    anchors: set[str] = set()
    for raw in _words(text):
        token = raw.strip("$").strip("'").lower()
        if not token or token in STOPWORDS:
            continue
        if token.isdigit() or token.endswith("%"):
            anchors.add(token)
            continue
        if len(token) >= 4 or token in {"btc", "eth", "etf", "rwa", "defi"}:
            anchors.add(token)
    return anchors


def _number_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in re.finditer(r"\b\d+(?:\.\d+)?%?\b", text or "")}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _pattern_hits(text: str, patterns: Iterable[tuple[str, str, float]]) -> list[dict[str, Any]]:
    low = (text or "").lower()
    hits = []
    for pattern, label, weight in patterns:
        if re.search(pattern, low):
            hits.append({"label": label, "weight": weight})
    return hits


def _generic_template_score(reply_text: str, context_score: float) -> tuple[float, list[str]]:
    hits = _pattern_hits(reply_text, GENERIC_PATTERNS)
    score = sum(float(h["weight"]) for h in hits)
    labels = [str(h["label"]) for h in hits]

    tokens = _anchor_tokens(reply_text)
    generic_overlap = len(tokens & GENERIC_TOPIC_WORDS)
    if generic_overlap >= 2:
        score += 0.12
        labels.append("generic_topic_words")
    if context_score < 0.25 and score > 0:
        score += 0.18
        labels.append("template_without_thread_anchor")
    if context_score < 0.35 and any(
        label in labels
        for label in {
            "consultant_contrast_problem_template",
            "forced_permissions_problem",
            "abstract_accountability_question",
            "abstract_optimization_framing",
        }
    ):
        score += 0.18
        labels.append("abstract_reframe_without_enough_anchors")
    return _clamp(score), labels


def _thread_context_score(target_text: str, reply_text: str) -> tuple[float, list[str]]:
    target_tokens = _anchor_tokens(target_text)
    reply_tokens = _anchor_tokens(reply_text)
    if not target_tokens or not reply_tokens:
        return 0.0, []

    overlap = sorted(target_tokens & reply_tokens)
    denominator = max(2, min(6, len(target_tokens)))
    score = min(1.0, len(overlap) / denominator)

    target_numbers = _number_tokens(target_text)
    reply_numbers = _number_tokens(reply_text)
    if target_numbers and target_numbers & reply_numbers:
        score += 0.2
        overlap.extend(sorted(target_numbers & reply_numbers))

    reply_word_count = len(_words(reply_text))
    if reply_word_count <= 6:
        score -= 0.15
    elif reply_word_count >= 12 and len(overlap) >= 2:
        score += 0.1

    return _clamp(score), overlap[:10]


def _authority_claims(reply_text: str) -> list[str]:
    low = reply_text or ""
    claims = []
    for pattern in AUTHORITY_CLAIM_PATTERNS:
        for match in re.finditer(pattern, low, flags=re.IGNORECASE):
            claims.append(match.group(0).strip())
    return claims[:5]


def _trading_flags(reply_text: str) -> tuple[list[str], list[str]]:
    low = (reply_text or "").lower()
    direct = []
    for pattern in DIRECT_ADVICE_PATTERNS:
        for match in re.finditer(pattern, low):
            direct.append(match.group(0).strip())

    terms = sorted({t for t in TRADING_TERMS if re.search(rf"\b{re.escape(t)}\b", low)})
    non_advisory = [m for m in NON_ADVISORY_MARKERS if m in low]
    if non_advisory and not direct:
        terms = terms[:2]
    return direct[:5], terms[:8]


def structure_signature(text: str) -> str:
    low = (text or "").lower()
    low = re.sub(r"https?://\S+", " <url> ", low)
    low = re.sub(r"[@#]\w+", " <tag> ", low)
    low = re.sub(r"\$[a-z][a-z0-9_]*", " <ticker> ", low)
    low = re.sub(r"\b\d+(?:\.\d+)?%?\b", " <num> ", low)
    for word in STRUCTURE_TOPIC_WORDS:
        low = re.sub(rf"\b{re.escape(word)}\b", " <topic> ", low)
    tokens = re.findall(r"[a-z<>]+", low)
    normalized = []
    for token in tokens:
        if token in STOPWORDS:
            normalized.append(token)
        elif token.startswith("<"):
            normalized.append(token)
        elif len(token) >= 7:
            normalized.append("<term>")
        else:
            normalized.append(token)
    return " ".join(normalized[:22]).strip()


def _same_day(entry: Mapping[str, Any], today: str) -> bool:
    queued_at = str(entry.get("queued_at") or entry.get("posted_at") or "")
    return queued_at.startswith(today)


def _recent_structure_count(
    signature: str,
    recent_entries: Iterable[Mapping[str, Any]] | None,
    today: str,
) -> int:
    if not signature or not recent_entries:
        return 0
    count = 0
    for item in recent_entries:
        if not isinstance(item, Mapping) or not _same_day(item, today):
            continue
        other = str(item.get("reply_structure_signature") or "")
        if not other and item.get("reply_text"):
            other = structure_signature(str(item.get("reply_text") or ""))
        if other == signature:
            count += 1
    return count


def audit_reply(
    entry: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    recent_entries: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    qcfg = _quality_cfg(cfg)
    enabled = bool(qcfg.get("enabled", True))
    target_text = str(entry.get("target_text") or "")
    reply_text = str(entry.get("reply_text") or "")

    context_score, anchors = _thread_context_score(target_text, reply_text)
    generic_score, generic_hits = _generic_template_score(reply_text, context_score)
    claims = _authority_claims(reply_text)
    direct_trading, trading_terms = _trading_flags(reply_text)
    signature = structure_signature(reply_text)
    today = date.today().isoformat()
    repeated_count = _recent_structure_count(signature, recent_entries, today)

    min_context = float(qcfg.get("min_thread_context_score", 0.42))
    max_generic = float(qcfg.get("max_generic_template_score", 0.48))
    max_structure = int(qcfg.get("max_repeated_structure_per_day", 2) or 2)
    max_chars = int(qcfg.get("max_reply_chars") or _cfg(cfg).get("generation", {}).get("max_reply_chars") or 0)
    max_words = int(qcfg.get("max_reply_words", 0) or 0)
    max_lines = int(qcfg.get("max_reply_lines", 0) or 0)
    block_claims = bool(qcfg.get("block_unsupported_authority_claims", True))
    block_advice = bool(qcfg.get("block_direct_market_advice", True))

    reasons: list[str] = []
    hard_block = False
    needs_rewrite = False
    approval_only = False

    if not enabled:
        risk_class = "auto_ok"
        reasons.append("reply_quality_audit_disabled")
    else:
        if context_score < min_context:
            needs_rewrite = True
            reasons.append(f"low_thread_context:{context_score:.2f}")
        if generic_score > max_generic:
            needs_rewrite = True
            reasons.append(f"generic_template:{generic_score:.2f}")
        if repeated_count >= max_structure:
            needs_rewrite = True
            reasons.append(f"repeated_structure:{repeated_count + 1}_today")
        if max_chars and len(reply_text) > max_chars:
            needs_rewrite = True
            reasons.append(f"too_long_chars:{len(reply_text)}>{max_chars}")
        if max_words and len(_words(reply_text)) > max_words:
            needs_rewrite = True
            reasons.append(f"too_many_words:{len(_words(reply_text))}>{max_words}")
        line_count = len([line for line in reply_text.splitlines() if line.strip()])
        if max_lines and line_count > max_lines:
            needs_rewrite = True
            reasons.append(f"too_many_lines:{line_count}>{max_lines}")
        if claims:
            reasons.append("unsupported_authority_claim:" + ", ".join(claims[:2]))
            if block_claims:
                hard_block = True
            else:
                needs_rewrite = True
        if direct_trading:
            reasons.append("direct_market_advice:" + ", ".join(direct_trading[:2]))
            if block_advice:
                hard_block = True
            else:
                needs_rewrite = True
        elif trading_terms:
            approval_only = True
            reasons.append("trading_language:" + ", ".join(trading_terms[:4]))

        if hard_block:
            risk_class = "block"
        elif needs_rewrite:
            risk_class = "needs_rewrite"
        elif approval_only:
            risk_class = "approval_only"
        else:
            risk_class = "auto_ok"

    score = 100
    score -= int((1.0 - context_score) * 35)
    score -= int(generic_score * 30)
    score -= min(20, repeated_count * 8)
    if claims:
        score -= 25
    if direct_trading:
        score -= 30
    elif trading_terms:
        score -= 10
    if max_chars and len(reply_text) > max_chars:
        score -= 15
    if max_words and len(_words(reply_text)) > max_words:
        score -= 10

    return {
        "enabled": enabled,
        "reply_risk_class": risk_class,
        "score": max(0, min(100, score)),
        "thread_context_score": round(context_score, 3),
        "thread_anchor_terms": anchors,
        "generic_template_score": round(generic_score, 3),
        "generic_template_hits": generic_hits,
        "unsupported_claim_flags": claims,
        "direct_market_advice_flags": direct_trading,
        "trading_language_flags": trading_terms,
        "reply_structure_signature": signature,
        "same_structure_count_today": repeated_count,
        "max_repeated_structure_per_day": max_structure,
        "reasons": reasons,
        "x_algo_evidence_basis": list(X_ALGO_EVIDENCE),
    }


def _risk_rank(risk_class: str | None) -> int:
    return RISK_ORDER.get(str(risk_class or "").strip().lower(), 0)


def _risk_from_rank(rank: int) -> str:
    for label, value in RISK_ORDER.items():
        if value == rank:
            return label
    return "auto_ok"


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_ai_judge(data: Mapping[str, Any]) -> dict[str, Any]:
    risk = str(data.get("reply_risk_class") or "approval_only").strip().lower()
    if risk not in RISK_ORDER:
        risk = "approval_only"
    try:
        score = int(float(data.get("score", 50)))
    except Exception:
        score = 50

    def _list(name: str) -> list[str]:
        value = data.get(name)
        if isinstance(value, list):
            return [str(x).strip()[:160] for x in value if str(x).strip()][:8]
        if value:
            return [str(value).strip()[:160]]
        return []

    def _float(name: str, default: float) -> float:
        try:
            return _clamp(float(data.get(name, default)))
        except Exception:
            return default

    return {
        "enabled": True,
        "reply_risk_class": risk,
        "score": max(0, min(100, score)),
        "thread_context_score": round(_float("thread_context_score", 0.5), 3),
        "generic_template_score": round(_float("generic_template_score", 0.5), 3),
        "unsupported_claim_flags": _list("unsupported_claim_flags"),
        "direct_market_advice_flags": _list("direct_market_advice_flags"),
        "trading_language_flags": _list("trading_language_flags"),
        "reasons": _list("reasons"),
        "rewrite_instruction": str(data.get("rewrite_instruction") or "").strip()[:400],
        "x_algo_evidence_basis": list(X_ALGO_EVIDENCE),
    }


def ai_judge_reply(
    entry: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    deterministic_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    qcfg = _quality_cfg(cfg)
    if not bool(qcfg.get("ai_judge_enabled", False)):
        return {"enabled": False}

    target_text = str(entry.get("target_text") or "")[:1200]
    reply_text = str(entry.get("reply_text") or "")[:800]
    source = str(entry.get("source") or "")
    target = str(entry.get("target") or "")
    account_label = _account_label(cfg)
    deterministic = deterministic_audit if isinstance(deterministic_audit, Mapping) else {}

    prompt = f"""You are reviewing a draft {account_label} reply before it is queued or posted.

This is a quality and safety review, not an evasion task. Use only visible
content quality signals. Do not claim to know X's hidden spam prompt or
production thresholds.

Public X-algorithm evidence available:
- SpamEapiLowFollowerClassifier renders the whole thread with ThreadRenderer.render(post).
- Reply ranking also renders thread context before scoring.
- The exact spam prompt, production thresholds, and enforcement escalation rules are not public.

Judge whether this reply is likely too generic, weakly thread-contextual,
unsupported, promotional, or trading-advice-like.
Also judge whether a skeptical human would read it as automated engagement:
too polished, abstract, consultant-shaped, or forcing {account_label}'s niche
onto a post that does not support that angle.

Source lane: {source}
Target author: @{target}

OP / thread text:
{target_text}

Draft reply:
{reply_text}

Deterministic local audit:
{json.dumps(deterministic, ensure_ascii=False)[:1800]}

Return strict JSON only:
{{
  "reply_risk_class": "auto_ok | approval_only | needs_rewrite | block",
  "score": 0,
  "thread_context_score": 0.0,
  "generic_template_score": 0.0,
  "unsupported_claim_flags": [],
  "direct_market_advice_flags": [],
  "trading_language_flags": [],
  "reasons": [],
  "rewrite_instruction": ""
}}

Risk-class rules:
- auto_ok: specific to the OP, useful, non-promotional, no unsupported authority claims, no direct trading advice.
- approval_only: mostly okay but marginal, contains non-advisory trading/market terms, or needs human taste check.
- needs_rewrite: generic, template-shaped, weakly grounded in the OP, forced-niche, or sounds like automated engagement.
- block: fabricated authority claim, direct financial/trading advice, spammy promotion, or unsafe claim.
"""
    try:
        import generate as _generate

        raw = _generate._call_claude([{"role": "user", "content": prompt}], max_tokens=700)
        data = _extract_json_object(raw)
        if not data:
            return {
                "enabled": True,
                "reply_risk_class": "approval_only",
                "score": 50,
                "reasons": ["ai_judge_returned_unparseable_json"],
                "raw_excerpt": str(raw or "")[:240],
            }
        return _normalize_ai_judge(data)
    except Exception as e:
        return {
            "enabled": True,
            "reply_risk_class": "approval_only",
            "score": 50,
            "reasons": [f"ai_judge_failed:{e}"],
        }


def _has_hard_deterministic_block(audit: Mapping[str, Any]) -> bool:
    if audit.get("unsupported_claim_flags") or audit.get("direct_market_advice_flags"):
        return True
    repeated = int(audit.get("same_structure_count_today", 0) or 0)
    max_repeated = int(audit.get("max_repeated_structure_per_day", 2) or 2)
    return repeated >= max_repeated


def _combine_audits(
    deterministic: dict[str, Any],
    ai_judge: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not ai_judge.get("enabled"):
        return deterministic

    qcfg = _quality_cfg(cfg)
    can_override_soft = bool(qcfg.get("ai_judge_can_override_soft_rules", True))
    hard_det = _has_hard_deterministic_block(deterministic)
    det_rank = _risk_rank(deterministic.get("reply_risk_class"))
    ai_rank = _risk_rank(ai_judge.get("reply_risk_class"))

    if hard_det or not can_override_soft:
        final_rank = max(det_rank, ai_rank)
    else:
        final_rank = ai_rank

    combined = {**deterministic}
    combined["deterministic_reply_risk_class"] = deterministic.get("reply_risk_class", "")
    combined["deterministic_score"] = deterministic.get("score", 0)
    combined["ai_reply_judge"] = dict(ai_judge)
    combined["reply_risk_class"] = _risk_from_rank(final_rank)
    combined["score"] = min(
        int(deterministic.get("score", 100) or 100),
        int(ai_judge.get("score", 100) or 100),
    )

    ai_reasons = [str(r) for r in (ai_judge.get("reasons") or []) if str(r).strip()]
    if ai_reasons:
        combined["reasons"] = list(combined.get("reasons") or []) + [
            "ai_judge:" + r for r in ai_reasons[:4]
        ]
    if ai_judge.get("rewrite_instruction"):
        combined["ai_rewrite_instruction"] = str(ai_judge.get("rewrite_instruction") or "")
    return combined


def apply_audit(
    entry: dict[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    recent_entries: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    deterministic = audit_reply(entry, cfg, recent_entries=recent_entries)
    ai_judge = ai_judge_reply(entry, cfg, deterministic_audit=deterministic)
    audit = _combine_audits(deterministic, ai_judge, cfg)
    entry["btcmind_reply_audit"] = audit
    entry["reply_risk_class"] = audit["reply_risk_class"]
    entry["reply_quality_score"] = audit["score"]
    entry["thread_context_score"] = audit["thread_context_score"]
    entry["generic_template_score"] = audit["generic_template_score"]
    entry["reply_structure_signature"] = audit["reply_structure_signature"]
    return audit


def rewrite_feedback(audit: Mapping[str, Any]) -> str:
    reasons = list(audit.get("reasons") or [])[:4]
    anchors = list(audit.get("thread_anchor_terms") or [])[:6]
    bits = []
    if reasons:
        bits.append("Previous draft failed local BTCMind reply audit: " + "; ".join(map(str, reasons)))
    if audit.get("ai_rewrite_instruction"):
        bits.append("AI judge rewrite instruction: " + str(audit.get("ai_rewrite_instruction") or ""))
    if anchors:
        bits.append("Use the actual thread anchors if relevant: " + ", ".join(map(str, anchors)))
    bits.append("Rewrite with a concrete noun/mechanism from the OP and avoid template phrasing.")
    return " ".join(bits)


def generation_rules(cfg: Mapping[str, Any] | None = None) -> str:
    qcfg = _quality_cfg(cfg)
    if not bool(qcfg.get("enabled", True)):
        return ""
    account_label = _account_label(cfg)
    root = _cfg(cfg)
    max_chars = int(qcfg.get("max_reply_chars") or root.get("generation", {}).get("max_reply_chars") or 120)
    max_words = int(qcfg.get("max_reply_words", 22) or 22)
    return f"""{account_label} reply quality rules:
- Keep keyword-engage replies short: one line, usually 4-16 words, hard max {max_words} words and {max_chars} characters.
- Sound like a person in the thread, not a content strategist. Fragments are okay. Mild uncertainty is okay. Do not over-explain.
- Vary the reply shape across runs: use a narrow question, a small caveat, a plain observation, a quick agreement-with-specifics, or a missing-risk note. Do not always use the same contrast/question format.
- Ground the reply in a specific noun, mechanism, cohort, number, or claim from the OP. If it could fit under 20 unrelated crypto posts, return an empty reply.
- Avoid repeated template shapes: "hidden constraint", "real signal", "real bottleneck", "real issue", "this only works if", "curious whether", generic "not X but Y" reframes, and "not a comms problem, that's a permissions problem" style contrasts unless the OP supplies a concrete X/Y.
- Do not force the account niche onto broad news, cybersecurity, politics, or drama. If the OP does not explicitly discuss agents, routing, permissions, context, token cost, or model behavior, do not introduce those as the diagnosis.
- Do not claim "we built", "we tracked", "we saw", "our data shows", or similar authority unless the OP or account fact context supports it.
- Do not give direct trading advice, price targets, entries, longs/shorts, pump/dump claims, or liquidation calls. Market-structure analysis is okay only when clearly non-advisory.
- Prefer one precise mechanism or one sharp operator question over brand promotion."""


def warning_line(entry: Mapping[str, Any]) -> str:
    audit = entry.get("btcmind_reply_audit")
    if not isinstance(audit, Mapping):
        return ""
    risk = str(audit.get("reply_risk_class") or "")
    if risk in {"", "auto_ok"}:
        return ""
    reasons = [str(r) for r in (audit.get("reasons") or []) if r]
    detail = " · ".join(reasons[:3]) if reasons else "local BTCMind reply audit"
    return f"*Reply audit: {risk}* - {detail}"
