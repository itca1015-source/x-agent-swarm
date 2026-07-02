"""BTCMind autonomous action policy and safety checks.

This module is intentionally deterministic. It is used immediately before live
reply, quote, and repost actions so the generator cannot bypass the approved
BTCMind autonomy policy.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)

DEFAULT_POLICY = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "autonomous_content_policy.json")
DEFAULT_FACTS = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "btcmind_fact_sheet.json")

BTCMIND_HANDLES = {"btcmind101", "hunter_solvea"}


def _load_json(path: str, default: Any) -> Any:
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _abs(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(ROOT_DIR, path)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _numbers(text: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z0-9_])[$+]?\d[\d,.]*(?:\.\d+)?%?[KMBkmb]?", text or "")


def _number_is_high_claim(token: str) -> bool:
    token = str(token or "").strip()
    if not token:
        return False
    if any(ch in token for ch in "$%,"):
        return True
    if re.search(r"[KMBkmb]$", token):
        return True
    try:
        return float(token.replace(",", "")) >= 10
    except ValueError:
        return True


def policy_paths(cfg: dict) -> tuple[str, str]:
    block = cfg.get("autonomy_policy") or {}
    policy_path = _abs(str(block.get("policy_path") or ""))
    fact_path = _abs(str(block.get("fact_sheet_path") or ""))
    if not policy_path:
        policy_path = DEFAULT_POLICY
    if not fact_path:
        policy = _load_json(policy_path, {})
        fact_path = _abs(str(policy.get("fact_sheet_path") or "")) or DEFAULT_FACTS
    return policy_path, fact_path


def applies(cfg: dict) -> bool:
    block = cfg.get("autonomy_policy") or {}
    if block.get("enabled") is True:
        return True
    handle = str(cfg.get("hunter_handle") or cfg.get("x_handle") or "").lstrip("@").lower()
    return handle in BTCMIND_HANDLES and os.path.exists(DEFAULT_POLICY)


def load_policy(cfg: dict) -> tuple[dict, dict]:
    if not applies(cfg):
        return {}, {}
    policy_path, fact_path = policy_paths(cfg)
    return _load_json(policy_path, {}), _load_json(fact_path, {})


def approved_sources(policy: dict) -> set[str]:
    clusters = policy.get("approved_source_clusters") or {}
    handles: list[str] = []
    for values in clusters.values():
        handles.extend(values or [])
    return {str(h).strip().lstrip("@").lower() for h in handles if str(h).strip()}


def source_signals(text: str, author: str = "", policy: dict | None = None) -> set[str]:
    hay = _norm(text)
    found: set[str] = set()
    signal_terms = {
        "security": ["wallet drain", "phishing", "seed phrase", "scam", "impersonator", "security warning"],
        "stablecoin": ["stablecoin", "usdc", "payment", "payments", "settlement"],
        "wallet_ux": ["wallet ux", "wallet flow", "wallet drain", "custody", "bridge", "onboarding", "private key", "seed phrase"],
        "onchain": ["onchain", "on-chain", "wallet flow", "exchange reserves", "analytics"],
        "market_structure": ["funding", "open interest", "liquidation", "liquidations", "etf flow", "etf flows", "perp"],
        "bitcoin": ["btc", "bitcoin", "$btc"],
        "crypto_ai": ["crypto ai", "ai agent", "agentic", "agents"],
        "operator": ["compliance", "risk", "treasury", "exchange", "liquidity"],
    }
    for label, terms in signal_terms.items():
        if any(term in hay for term in terms):
            found.add(label)
    if policy and str(author).strip().lstrip("@").lower() in approved_sources(policy):
        found.add("approved_source")
    return found


def _allowed_fact_phrases(facts: dict) -> set[str]:
    phrases: set[str] = set()
    now = datetime.utcnow().date()
    for claim in facts.get("allowed_claims") or []:
        expires = claim.get("expires_at")
        if expires:
            try:
                if datetime.fromisoformat(str(expires)).date() < now:
                    continue
            except Exception:
                continue
        for phrase in claim.get("allowed_phrases") or []:
            if phrase:
                phrases.add(_norm(str(phrase)))
    return phrases


def _hard_block_issues(text: str, policy: dict) -> list[str]:
    hay = _norm(text)
    issues: list[str] = []
    for phrase in policy.get("hard_blocks") or []:
        p = _norm(str(phrase))
        if p and p in hay:
            issues.append(f"hard_block:{p}")
    for pattern in policy.get("blocked_regex") or []:
        try:
            if re.search(pattern, text or "", flags=re.I | re.S):
                issues.append(f"blocked_regex:{pattern}")
        except re.error:
            issues.append(f"invalid_policy_regex:{pattern}")
    return issues


def _product_claim_issues(text: str, policy: dict, facts: dict) -> list[str]:
    hay = _norm(text)
    if "btcmind" not in hay and "our agents" not in hay and "the agents" not in hay:
        return []
    allowed = _allowed_fact_phrases(facts)
    issues: list[str] = []
    blocked_claims = (policy.get("product_claim_state") or {}).get("blocked_product_claims") or []
    for claim in blocked_claims:
        c = _norm(str(claim))
        # The policy claims are human-readable; matching their important nouns
        # catches obvious unsafe drafts without pretending to parse legal text.
        key_terms = [w for w in re.findall(r"[a-z0-9]+", c) if len(w) >= 5 and w not in {"btcmind", "without"}]
        if key_terms and "btcmind" in hay and any(t in hay for t in key_terms):
            issues.append(f"blocked_product_claim:{claim}")
    if "btcmind" in hay:
        # Bare brand mentions are allowed only when not paired with capability,
        # performance, access, integration, or maturity words. Those phrases are
        # handled by blocked_regex and blocked_claims above.
        if allowed and any(p in hay for p in allowed):
            return issues
    return issues


def _market_advice_issues(text: str) -> list[str]:
    issues: list[str] = []
    advice_patterns = [
        r"\b(buy|sell|long|short|entry|exit|take profit|stop loss)\b.{0,50}\b(btc|bitcoin|\$btc)\b",
        r"\b(btc|bitcoin|\$btc)\b.{0,50}\b(buy|sell|long|short|entry|exit|take profit|stop loss)\b",
        r"\b(btc|bitcoin|\$btc)\b.{0,50}\b(to|above|below|hits?|will reach)\s+\$?\d",
        r"\bnot financial advice\b.*\b(buy|sell|long|short)\b",
    ]
    for pattern in advice_patterns:
        if re.search(pattern, text or "", flags=re.I | re.S):
            issues.append(f"market_advice:{pattern}")
    return issues


def _unsourced_number_issues(text: str, source_text: str, facts: dict) -> list[str]:
    source_nums = set(_numbers(source_text))
    for claim in facts.get("allowed_claims") or []:
        source_nums.update(_numbers(str(claim.get("claim") or "")))
        source_nums.update(_numbers(" ".join(claim.get("allowed_phrases") or [])))
    issues = []
    for n in _numbers(text):
        if n in source_nums:
            continue
        if not _number_is_high_claim(n):
                continue
        issues.append(f"unsourced_number:{n}")
    return issues


def _promotional_source_issues(text: str) -> list[str]:
    issues: list[str] = []
    promo_patterns = [
        r"\b(banger week|ecosystem growth|major updates?|core features?|all in one|sleek application)\b",
        r"\b(easy onboarding|less friction|better ux for everyday users?)\b",
        r"\b(ai-powered|agentic|autonomous ai agents?)\b.{0,100}\b(trad(e|ing)|futures|spot|strategy|24/7)\b",
        r"\b(deploy|launch)\b.{0,80}\b(autonomous ai agents?|trading agents?)\b",
    ]
    for pattern in promo_patterns:
        if re.search(pattern, text or "", flags=re.I | re.S):
            issues.append(f"promotional_source:{pattern}")
    return issues


def _product_update_source_issues(text: str) -> list[str]:
    issues: list[str] = []
    product_patterns = [
        r"\b(we['’]re excited|we just added|today we['’]re launching|we['’]re launching)\b",
        r"\b(data partnership|our research team wrote|check it out below|check out the full article|complete guide)\b",
        r"\b(trade where you research|now trade here too|how to use [A-Za-z0-9_ ]+)\b",
    ]
    for pattern in product_patterns:
        if re.search(pattern, text or "", flags=re.I | re.S):
            issues.append(f"product_update_source:{pattern}")
    return issues


def _trader_drama_source_issues(text: str) -> list[str]:
    issues: list[str] = []
    trading_patterns = [
        r"\b(dumping|dumped|preparing to dump|intent to sell|buying the dip)\b",
        r"\b(whales?|institutions?)\b.{0,80}\b(keep|continue|still|are)\b.{0,40}\b(buying|accumulating|withdrew|withdrawing)\b",
        r"\b(whale|perps?|futures|longs?|shorts?)\b.{0,100}\b(loss|profit|unrealized|funding fee|liquidat|entry|roi|margin)\b",
        r"\b(shorted|shorting|flipped long|long position|liquidation king|liquidation price|unrealized profit)\b",
        r"\b(wiped out|down another|down over|lost a total|sitting on over)\b.{0,80}\$?\d",
        r"\b(perps?|futures|spot)\b.{0,100}\b(profit|loss|liquidat|long|short|trade|trading)\b",
        r"\b(called|predicted)\b.{0,80}\$[A-Za-z][A-Za-z0-9_]*",
    ]
    for pattern in trading_patterns:
        if re.search(pattern, text or "", flags=re.I | re.S):
            issues.append(f"trader_drama_source:{pattern}")
    return issues


def _is_public_data_observation(text: str) -> bool:
    public_data_terms = [
        "market cap",
        "supply",
        "volume",
        "revenue",
        "transactions",
        "holders",
        "issuer",
        "issuers",
        "market share",
        "dashboard",
        "chart",
        "report",
        "brief",
        "coverage",
        "tokenized assets",
        "stablecoin",
        "on-chain analysis",
        "onchain analysis",
    ]
    hay = _norm(text)
    return any(term in hay for term in public_data_terms)


def _published_text_issues(kind: str, text: str) -> list[str]:
    issues: list[str] = []
    if kind in {"reply", "quote", "original"}:
        if re.search(r"@[A-Za-z0-9_]{1,20}", text or ""):
            issues.append("mention_not_allowed")
        if re.search(r"#\w+", text or ""):
            issues.append("hashtag_not_allowed")
    if re.search(r"\b(this is huge|must read|lfg|wagmi|send it)\b", text or "", flags=re.I):
        issues.append("generic_hype")
    return issues


def check_entry(kind: str, entry: dict, cfg: dict, source_text: str = "",
                require_autonomy: bool = True) -> dict:
    """Return a safety decision for one pending autonomous action.

    The return schema is:
      {"ok": bool, "reason": str, "issues": [str], "policy_applied": bool}
    """
    kind = str(kind or entry.get("kind") or "").strip().lower()
    if not applies(cfg):
        return {"ok": True, "reason": "no BTCMind autonomy policy applies", "issues": [], "policy_applied": False}

    policy, facts = load_policy(cfg)
    if not policy:
        return {"ok": False, "reason": "BTCMind autonomy policy missing", "issues": ["policy_missing"], "policy_applied": True}

    autonomy = policy.get("autonomy") or {}
    if kind == "original" and autonomy.get("original") == "human_approval_required" and require_autonomy:
        return {
            "ok": False,
            "reason": "original posts require human approval",
            "issues": ["original_requires_approval"],
            "policy_applied": True,
        }
    if kind in {"reply", "quote", "repost"} and autonomy.get(kind) != "autonomous_allowed" and require_autonomy:
        return {
            "ok": False,
            "reason": f"{kind} requires human approval",
            "issues": [f"{kind}_requires_human_approval"],
            "policy_applied": True,
        }

    publish_text = str(entry.get("reply_text") or entry.get("text") or "")
    target_text = source_text or str(entry.get("target_text") or entry.get("source_text") or "")
    if kind == "repost":
        # A plain repost has no new text, but the source claim becomes visible
        # through BTCMind. Treat the source text as the text under review.
        publish_text = target_text or publish_text

    issues: list[str] = []
    if not publish_text.strip():
        issues.append("empty_text")
    if kind == "reply" and len(publish_text) > int((policy.get("reply_rules") or {}).get("max_chars", 240)):
        issues.append("reply_too_long")
    if kind == "quote" and len(publish_text) > int((policy.get("quote_rules") or {}).get("max_chars", 280)):
        issues.append("quote_too_long")

    issues.extend(_published_text_issues(kind, publish_text))
    issues.extend(_hard_block_issues(publish_text, policy))
    issues.extend(_market_advice_issues(publish_text))
    issues.extend(_product_claim_issues(publish_text, policy, facts))

    if kind in {"reply", "quote", "original"}:
        issues.extend(_unsourced_number_issues(publish_text, target_text, facts))

    if kind == "quote":
        issues.extend(_hard_block_issues(target_text, policy))
        issues.extend(_product_claim_issues(target_text, policy, facts))
    if kind == "repost":
        issues.extend(_hard_block_issues(target_text, policy))
        issues.extend(_product_claim_issues(target_text, policy, facts))
        brand_win = re.search(r"\bbtcmind\b.{0,80}\b(called|caught|flagged|predicted|win|guarantee|10x|100x)\b", target_text or "", flags=re.I | re.S)
        if brand_win and not entry.get("verified_brand_claim"):
            issues.append("unverified_btcmind_win_or_hype_repost")

    # Deduplicate while preserving order.
    seen = set()
    deduped = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            deduped.append(issue)
    if deduped:
        return {
            "ok": False,
            "reason": "; ".join(deduped[:4]),
            "issues": deduped,
            "policy_applied": True,
        }
    return {"ok": True, "reason": "passed BTCMind autonomy safety gate", "issues": [], "policy_applied": True}


def classify_candidate(candidate: dict, policy: dict) -> dict:
    text = str(candidate.get("text") or "")
    author = str(candidate.get("author") or "")
    sigs = source_signals(text, author, policy)
    if not sigs:
        return {"action": "skip", "reason": "no_btcmind_public_signal", "signals": []}
    safety = check_entry("repost", {"target_text": text, "source_text": text}, {"autonomy_policy": {"enabled": True}}, text)
    # The line above uses the default policy path, which is what this helper is
    # for. If the source is unsafe to repost, it is unsafe as an autonomous quote
    # source too unless a human reviews it.
    if not safety.get("ok"):
        return {"action": "skip", "reason": safety.get("reason", "safety_block"), "signals": sorted(sigs)}
    promo_issues = _promotional_source_issues(text)
    if promo_issues and "approved_source" not in sigs:
        return {"action": "skip", "reason": "; ".join(promo_issues[:2]), "signals": sorted(sigs)}
    product_issues = _product_update_source_issues(text)
    if product_issues and "security" not in sigs:
        return {"action": "skip", "reason": "; ".join(product_issues[:2]), "signals": sorted(sigs)}
    trader_issues = _trader_drama_source_issues(text)
    if trader_issues and "security" not in sigs:
        return {"action": "skip", "reason": "; ".join(trader_issues[:2]), "signals": sorted(sigs)}

    if "approved_source" in sigs:
        if "security" in sigs:
            return {"action": "plain_repost_candidate", "reason": "approved source with public security signal", "signals": sorted(sigs)}
        if ({"stablecoin", "onchain"} & sigs) and _is_public_data_observation(text):
            return {"action": "plain_repost_candidate", "reason": "approved source with public data observation", "signals": sorted(sigs)}
        if {"market_structure", "stablecoin", "onchain", "wallet_ux", "crypto_ai", "operator"} & sigs:
            return {"action": "quote_candidate", "reason": "approved-source signal needs BTCMind context before amplification", "signals": sorted(sigs)}

    if "security" in sigs:
        return {"action": "plain_repost_candidate", "reason": "public security signal", "signals": sorted(sigs)}
    if "market_structure" in sigs:
        return {"action": "quote_candidate", "reason": "market-structure signal needs human-approved BTCMind context", "signals": sorted(sigs)}
    if {"stablecoin", "wallet_ux", "onchain", "crypto_ai", "operator"} & sigs:
        return {"action": "quote_candidate", "reason": "better as BTCMind operator/market-structure quote", "signals": sorted(sigs)}
    return {"action": "skip", "reason": "signal too weak for autonomous action", "signals": sorted(sigs)}


def author_repost_seen_recently(seen: dict, author: str, days: int = 7) -> bool:
    key = f"author_repost:{str(author).strip().lstrip('@').lower()}"
    when = seen.get(key)
    if not when:
        return False
    try:
        dt = datetime.fromisoformat(str(when))
    except Exception:
        return False
    return datetime.now() - dt < timedelta(days=days)


def mark_author_repost(seen: dict, author: str):
    key = f"author_repost:{str(author).strip().lstrip('@').lower()}"
    seen[key] = datetime.now().isoformat()
