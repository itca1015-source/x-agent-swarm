#!/usr/bin/env python3
"""Flatkey Home / For You timeline scout.

Scrapes the logged-in Flatkey X Home / For You timeline, preserves media
metadata and screenshots for media-heavy posts, then routes aligned posts into
the existing Flatkey Telegram approval queue as quote or plain-repost
candidates.

This runner does not publish directly.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
STATE_DIR = os.path.join(ROOT_DIR, "state")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")
HOME_SEEN_PATH = os.path.join(STATE_DIR, "flatkey_home_timeline_seen.json")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "flatkey_home_timeline_scout")
MEDIA_DIR = os.path.join(STATE_DIR, "flatkey_home_timeline_media")
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "flatkey", "engage_config.json")
PLAYBOOK_PATH = os.path.join(ROOT_DIR, "accounts", "flatkey", "playbook.md")
POLICY_PATH = os.path.join(ROOT_DIR, "accounts", "flatkey", "autonomous_content_policy.json")

sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
import generate as _generate
import telegram as _tg
from lock import chrome_lock, file_lock


HOME_EXTRACT_JS = r"""
(function() {
    function compact(el) {
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
    function metric(el, testid) {
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
    function statusFromAnchor(a) {
        if (!a || !a.href) return null;
        var m = a.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
        if (!m) return null;
        return {author: m[1], id: m[2], url: 'https://x.com/' + m[1] + '/status/' + m[2]};
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
    var out = [];
    Array.from(document.querySelectorAll('article[data-testid="tweet"]')).forEach(function(el, idx) {
        try {
            var all = compact(el);
            if (!all || /\bPromoted\b/i.test(all)) return;
            var timeEl = el.querySelector('time');
            var main = statusFromAnchor(timeEl ? timeEl.closest('a[href*="/status/"]') : null);
            var statusLinks = Array.from(el.querySelectorAll('a[href*="/status/"]'))
                .map(statusFromAnchor)
                .filter(Boolean);
            if (!main && statusLinks.length) main = statusLinks[0];
            if (!main || !main.id) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var tweetText = textEl ? compact(textEl) : '';

            var imageNodes = Array.from(el.querySelectorAll('[data-testid="tweetPhoto"] img, a[href*="/photo/"] img, img[src*="pbs.twimg.com/media"]'));
            var images = unique(imageNodes.map(function(img) {
                var src = img.currentSrc || img.src || '';
                if (!src || /profile_images/.test(src)) return '';
                return JSON.stringify({
                    src: src,
                    alt: img.getAttribute('alt') || '',
                    aria: img.getAttribute('aria-label') || '',
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0
                });
            }).filter(Boolean)).map(function(x) {
                try { return JSON.parse(x); } catch(e) { return {}; }
            });

            var videoNodes = Array.from(el.querySelectorAll('[data-testid="videoPlayer"], video'));
            var videos = unique(videoNodes.map(function(v) {
                var node = v.tagName && v.tagName.toLowerCase() === 'video' ? v : v.querySelector('video');
                return JSON.stringify({
                    src: node ? (node.currentSrc || node.src || '') : '',
                    poster: node ? (node.getAttribute('poster') || '') : '',
                    aria: v.getAttribute('aria-label') || '',
                    text: visibleText(v).slice(0, 500)
                });
            })).map(function(x) {
                try { return JSON.parse(x); } catch(e) { return {}; }
            });

            var cardTexts = unique(Array.from(el.querySelectorAll('[data-testid="card.wrapper"], a[data-testid="card.wrapper"]'))
                .map(function(c) { return visibleText(c).slice(0, 700); })
                .filter(Boolean));
            var hrefs = unique(Array.from(el.querySelectorAll('a[href]')).map(normalizedHref));
            var statusIds = unique(statusLinks.map(function(x) { return x.id; }));
            var otherStatus = statusLinks.some(function(x) {
                return x.id !== main.id || x.author.toLowerCase() !== main.author.toLowerCase();
            });

            var rect = el.getBoundingClientRect();
            out.push({
                dom_index: idx,
                id: main.id,
                author: main.author,
                url: main.url,
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                text: tweetText,
                all_text: all.slice(0, 5000),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                likes_raw: metric(el, 'like'),
                bookmarks_raw: metric(el, 'bookmark'),
                views_raw: analyticsMetric(el),
                has_image: images.length > 0,
                has_video: videos.length > 0,
                has_card: cardTexts.length > 0,
                has_gif: !!el.querySelector('[data-testid="gif"], [aria-label*="GIF" i]'),
                images: images.slice(0, 8),
                videos: videos.slice(0, 4),
                card_texts: cardTexts.slice(0, 4),
                hrefs: hrefs.slice(0, 80),
                status_ids_in_card: statusIds.slice(0, 8),
                is_quote_context: otherStatus,
                is_repost_context: /\breposted\b/i.test(all),
                viewport_top: Math.round(rect.top),
                viewport_height: Math.round(rect.height)
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


SIGNAL_TERMS = {
    "model_or_tool_update": [
        "openai", "anthropic", "claude", "gemini", "gpt", "grok", "llama",
        "qwen", "deepseek", "model release", "new model", "reasoning model",
        "api", "sdk", "cursor", "windsurf", "replit", "bolt", "v0", "devin",
        "codex", "claude code", "openclaw",
    ],
    "coding_agent_workflow": [
        "coding agent", "agentic coding", "claude code", "cursor", "windsurf",
        "devin", "bolt.new", "replit agent", "v0", "pull request", "repo",
        "debug", "terminal", "tool call", "tool calls",
    ],
    "agent_infra": [
        "ai agent", "agents", "agent framework", "langchain", "llamaindex",
        "mcp", "browser use", "computer use", "workflow", "orchestration",
        "eval", "evals", "tracing", "observability", "memory", "context",
        "runtime", "sandbox",
    ],
    "cost_routing": [
        "token cost", "llm cost", "api cost", "pricing", "credits", "rate limit",
        "rate-limit", "context window", "prompt caching", "cache", "router",
        "routing", "fallback", "latency", "batch", "cost per", "spend",
    ],
    "local_autonomy": [
        "local agent", "local runtime", "local-first", "desktop agent",
        "on device", "on-device", "self-host", "self host", "daemon",
        "background agent", "autopilot",
    ],
    "trust_security": [
        "permission", "permissions", "oauth", "sandbox", "secrets", "credentials",
        "prompt injection", "supply chain", "security", "audit", "policy",
        "guardrail", "approval", "human in the loop", "human-in-the-loop",
    ],
    "agent_payments": [
        "x402", "micropayment", "micropayments", "stablecoin", "usdc",
        "wallet", "agent wallet", "payment", "payments", "pay per use",
        "usage-based", "metering", "billing",
    ],
    "builder_case_study": [
        "built", "launched", "demo", "benchmark", "experiment", "case study",
        "we shipped", "open sourced", "github", "usage", "logs",
    ],
}

APPROVED_SOURCE_TERMS = {
    "openrouter", "claudeai", "anthropicai", "openai", "cursor_ai", "windsurf_ai",
    "replit", "boltdotnew", "vercel", "langchainai", "llama_index",
    "artificialanlys", "openclaw", "github", "cloudflaredev",
}

BLOCKED_SOURCE_PATTERNS = [
    (r"\b(airdrop|staking|governance|tokenomics|presale|memecoin|meme coin)\b", "crypto_token_promo"),
    (r"\b(buy|sell|long|short|entry|exit|take profit|stop loss)\b.{0,80}\b(btc|eth|\$[a-z]{2,10})\b", "trading_advice"),
    (r"\b(politics|election|war|celebrity|sports|dating)\b", "off_topic"),
    (r"\b(this is huge|game changer|future of ai|insane|mind blowing)\b", "generic_hype"),
]

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


def _slug_ts() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _compact(text: str, n: int = 260) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def _parse_count(raw: str) -> int:
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


def _parse_dt(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _daily_key(name: str) -> str:
    return f"_daily:{name}:{datetime.now():%Y-%m-%d}"


def _daily_count(state: dict, name: str) -> int:
    try:
        return int(state.get(_daily_key(name), 0) or 0)
    except Exception:
        return 0


def _increment_daily(state: dict, name: str):
    state[_daily_key(name)] = _daily_count(state, name) + 1


def _media_text(row: dict) -> str:
    parts: list[str] = []
    for img in row.get("images") or []:
        alt = _compact(str(img.get("alt") or img.get("aria") or ""), 240)
        if alt:
            parts.append(f"image alt: {alt}")
    for vid in row.get("videos") or []:
        vtxt = _compact(" ".join(str(vid.get(k) or "") for k in ("aria", "text")), 240)
        if vtxt:
            parts.append(f"video metadata: {vtxt}")
        if vid.get("poster"):
            parts.append("video has poster frame")
    for card in row.get("card_texts") or []:
        if card:
            parts.append(f"card: {_compact(card, 300)}")
    vision = row.get("media_vision") or {}
    if vision.get("summary"):
        parts.append(f"media vision: {_compact(vision.get('summary') or '', 500)}")
    if vision.get("details"):
        parts.append(f"media details: {_compact(vision.get('details') or '', 500)}")
    return "\n".join(parts)


def _analysis_text(row: dict) -> str:
    parts = [
        str(row.get("text") or ""),
        _media_text(row),
        str(row.get("all_text") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()[:5000]


def _display_source_text(row: dict) -> str:
    text = str(row.get("text") or "").strip()
    media = _media_text(row)
    if media:
        text = (text + "\n\n" if text else "") + media
    if not text:
        text = str(row.get("all_text") or "").strip()
    return text[:1400]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _signals(row: dict, policy: dict) -> set[str]:
    hay = _norm(_analysis_text(row))
    found: set[str] = set()
    for label, terms in SIGNAL_TERMS.items():
        if any(term in hay for term in terms):
            found.add(label)
    author = str(row.get("author") or "").strip().lstrip("@").lower()
    policy_sources = set()
    for values in (policy.get("approved_source_clusters") or {}).values():
        policy_sources.update(str(x).lstrip("@").lower() for x in (values or []))
    if author in policy_sources or author in APPROVED_SOURCE_TERMS:
        found.add("approved_source")
    if row.get("has_video"):
        found.add("has_video")
    if row.get("has_image"):
        found.add("has_image")
    return found


def _source_block_reason(row: dict, policy: dict) -> str:
    hay = _norm(_analysis_text(row))
    for phrase in policy.get("hard_blocks") or []:
        p = _norm(str(phrase))
        if p and p in hay:
            return f"hard_block:{p}"
    for pattern, reason in BLOCKED_SOURCE_PATTERNS:
        if re.search(pattern, hay, flags=re.I | re.S):
            return reason
    return ""


def _flatkey_claim_violation(text: str) -> str:
    hay = _norm(text)
    blocked_patterns = [
        (r"\bflatkey\b.*\b(live|launched|available|shipping|supports?|integrates?|built|runs|routes?)\b", "product_capability_claim"),
        (r"\b(flatkey|we)\b.*\b(save[sd]?|saving|cut|reduce[sd]?)\b.*\b\d+[%$]?", "savings_claim"),
        (r"\b(flatkey|we)\b.*\b(api key|credits?|waitlist|access|try it|get started|sign up)\b", "access_or_cta_claim"),
        (r"\b(customer|user|usage|served|requests?|api calls?)\b.*\b(flatkey|we)\b", "customer_or_usage_claim"),
        (r"\b(staking|governance|airdrop|tokenomics|token utility)\b", "crypto_tokenomics"),
        (r"\bthis is huge\b|\bgame changer\b|\bfuture of ai\b", "generic_hype"),
        (r"#\w+", "hashtag"),
    ]
    for pattern, reason in blocked_patterns:
        if re.search(pattern, hay):
            return reason
    if re.search(r"(\$\s*\d+|\b\d+(?:\.\d+)?\s*(?:x|%|k|m|b|tokens?|credits?|calls?|requests?|users?|customers?|prs?)\b)", hay):
        return "unsupported_numeric_claim"
    return ""


def _quote_quality_issues(text: str) -> list[str]:
    """Reject quotes that are safe but unlikely to earn attention."""
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
        "without ", "instead", "shift", "changes", "becomes", "stops being",
        "moves", "moved", "turns", "breaks", "leaks", "burns", "hides",
        "reveals", "cheap ", "expensive", "most ", "real ",
    ]):
        issues.append("no_tension_or_contrast")
    if re.search(r"\b(need|needs|should|must)\b.{0,60}\b(before|when|if|without)\b", hay) and jargon_hits >= 5:
        issues.append("sounds_like_internal_policy_note")
    return issues


def _flatkey_score(row: dict, policy: dict) -> int:
    sigs = _signals(row, policy)
    score = len(sigs)
    for label in ("agent_infra", "coding_agent_workflow", "cost_routing", "local_autonomy", "trust_security", "agent_payments"):
        if label in sigs:
            score += 2
    if "approved_source" in sigs:
        score += 3
    if row.get("has_video"):
        score += 2
    if row.get("has_image"):
        score += 1
    engagement = int(row.get("likes") or 0) + 2 * int(row.get("reposts") or 0) + 3 * int(row.get("replies") or 0)
    if engagement:
        score += min(5, int(math.log10(max(1, engagement))) + 1)
    return score


def _media_dependent(row: dict) -> bool:
    text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
    return bool(row.get("has_video") or (row.get("has_image") and len(text) < 110))


def _classify(row: dict, policy: dict) -> dict:
    sigs = _signals(row, policy)
    blocked = _source_block_reason(row, policy)
    if blocked:
        return {"action": "skip", "reason": blocked, "signals": sorted(sigs)}
    if not sigs - {"has_image", "has_video"}:
        return {"action": "skip", "reason": "no_flatkey_ai_agent_infra_signal", "signals": sorted(sigs)}
    if _flatkey_claim_violation(str(row.get("text") or "")):
        return {"action": "skip", "reason": "source_or_context_hits_flatkey_safety_block", "signals": sorted(sigs)}
    if _media_dependent(row):
        return {"action": "quote_candidate", "reason": "media-heavy source needs Flatkey operator lens and review", "signals": sorted(sigs)}
    if "approved_source" in sigs and ({"model_or_tool_update", "cost_routing", "agent_infra"} & sigs):
        return {"action": "plain_repost_candidate", "reason": "credible source carries standalone AI agent/tooling signal", "signals": sorted(sigs)}
    if {"cost_routing", "coding_agent_workflow", "builder_case_study"} & sigs:
        return {"action": "quote_candidate", "reason": "can add practical agent-cost or workflow lens", "signals": sorted(sigs)}
    if {"local_autonomy", "trust_security", "agent_payments", "agent_infra"} & sigs:
        return {"action": "quote_candidate", "reason": "fits Flatkey category narrative beyond product updates", "signals": sorted(sigs)}
    return {"action": "skip", "reason": "adjacent_but_too_broad", "signals": sorted(sigs)}


def _enrich_row(row: dict, policy: dict) -> dict:
    row["likes"] = _parse_count(row.get("likes_raw", ""))
    row["reposts"] = _parse_count(row.get("reposts_raw", ""))
    row["replies"] = _parse_count(row.get("replies_raw", ""))
    row["bookmarks"] = _parse_count(row.get("bookmarks_raw", ""))
    row["views"] = _parse_count(row.get("views_raw", ""))
    dt = _parse_dt(row.get("iso", ""))
    row["age_minutes"] = int((datetime.now(timezone.utc) - dt).total_seconds() / 60) if dt else 999999
    row["media_text"] = _media_text(row)
    row["analysis_text"] = _analysis_text(row)
    row["flatkey_score"] = _flatkey_score(row, policy)
    row["deterministic_classification"] = _classify(row, policy)
    return row


def _extract_home(ws, policy: dict) -> list[dict]:
    raw = _chrome.eval_js(ws, HOME_EXTRACT_JS)
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    return [_enrich_row(r, policy) for r in rows]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "")[:80]


def _capture_article_screenshot(ws, row: dict, stamp: str) -> str:
    tid = str(row.get("id") or "")
    if not tid:
        return ""
    js = f"""
    (function() {{
        var tid = {json.dumps(tid)};
        var articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
        for (var i = 0; i < articles.length; i++) {{
            var el = articles[i];
            var links = Array.from(el.querySelectorAll('a[href*="/status/"]'));
            var matched = links.some(function(a) {{ return (a.href || '').indexOf('/status/' + tid) >= 0; }});
            if (!matched) continue;
            var r = el.getBoundingClientRect();
            if (r.width < 160 || r.height < 80 || r.bottom < 0 || r.top > window.innerHeight) return '';
            return JSON.stringify({{
                x: Math.max(0, window.scrollX + r.left),
                y: Math.max(0, window.scrollY + r.top),
                width: Math.min(r.width, window.innerWidth),
                height: Math.min(r.height, 900)
            }});
        }}
        return '';
    }})()
    """
    raw = _chrome.eval_js(ws, js)
    if not raw:
        return ""
    try:
        rect = json.loads(raw)
    except Exception:
        return ""
    if not rect.get("width") or not rect.get("height"):
        return ""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, f"{stamp}_{_safe_id(tid)}.png")
    try:
        res = _chrome._send(ws, "Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {
                "x": float(rect["x"]),
                "y": float(rect["y"]),
                "width": float(rect["width"]),
                "height": float(rect["height"]),
                "scale": 1,
            },
        }, msg_id=88)
        data = ((res.get("result") or {}).get("data") or "")
        if not data:
            return ""
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path
    except Exception as e:
        _log(f"media screenshot failed id={tid}: {e}")
        return ""


def scrape_home(
    port: int,
    policy: dict,
    target_posts: int,
    max_scrolls: int,
    wait: float,
    self_handle: str,
    max_media_screenshots: int,
    viewport_width: int,
    viewport_height: int,
) -> list[dict]:
    records: dict[str, dict] = {}
    screenshot_count = 0
    stamp = _slug_ts()
    with chrome_lock(port, timeout=7200, on_wait=lambda s: _log(str(s))):
        ws = _chrome.connect(port, timeout=60)
        try:
            _chrome.navigate(ws, "https://x.com/home", wait=5.0)
            _chrome.set_viewport(ws, viewport_width, viewport_height)
            time.sleep(1.0)
            _chrome.eval_js(ws, """
                (function(){
                    var tabs = Array.from(document.querySelectorAll('[role="tab"], a[role="tab"]'));
                    var t = tabs.find(function(x){ return /For you/i.test(x.innerText || x.textContent || ''); });
                    if (t) t.click();
                    return t ? 'clicked' : '';
                })()
            """)
            time.sleep(1.5)
            stagnant = 0
            for i in range(max_scrolls + 1):
                before = len(records)
                for row in _extract_home(ws, policy):
                    handle = str(row.get("author") or "").lstrip("@")
                    if not handle or handle.lower() == self_handle.lower():
                        continue
                    key = str(row.get("id") or "")
                    if not key:
                        continue
                    existing = records.get(key)
                    if existing:
                        existing["last_seen_scroll"] = i
                        continue
                    if (row.get("has_video") or row.get("has_image")) and screenshot_count < max_media_screenshots:
                        shot = _capture_article_screenshot(ws, row, stamp)
                        if shot:
                            row["media_screenshot_path"] = shot
                            screenshot_count += 1
                    row["first_seen_scroll"] = i
                    row["last_seen_scroll"] = i
                    records[key] = row
                new = len(records) - before
                _log(f"home scroll={i} posts={len(records)} new={new} media_screenshots={screenshot_count}")
                if len(records) >= target_posts:
                    break
                stagnant = stagnant + 1 if new == 0 else 0
                if stagnant >= 18:
                    _log("home scrape stopped after 18 stagnant scrolls")
                    break
                if stagnant and stagnant % 3 == 0:
                    meta = _chrome.eval_js(ws, """
                        (function(){
                            var se = document.scrollingElement || document.documentElement;
                            var before = window.scrollY || se.scrollTop || 0;
                            window.scrollTo(0, se.scrollHeight);
                            return JSON.stringify({before: before, after: window.scrollY || se.scrollTop || 0, height: se.scrollHeight});
                        })()
                    """)
                    _log(f"bottom-scroll recovery stagnant={stagnant} meta={meta}")
                    time.sleep(max(wait * 3, 6.0))
                else:
                    _chrome.eval_js(ws, """
                        (function(){
                            var se = document.scrollingElement || document.documentElement;
                            var before = window.scrollY || se.scrollTop || 0;
                            window.scrollBy(0, Math.floor(window.innerHeight * 0.72));
                            var after = window.scrollY || se.scrollTop || 0;
                            if (after + window.innerHeight >= se.scrollHeight - 250) window.scrollTo(0, se.scrollHeight);
                            return JSON.stringify({before: before, after: window.scrollY || se.scrollTop || 0, height: se.scrollHeight});
                        })()
                    """)
                    time.sleep(wait)
        finally:
            try:
                ws.close()
            except Exception:
                pass
    rows = list(records.values())
    rows.sort(key=lambda r: (int(r.get("first_seen_scroll") or 0), int(r.get("dom_index") or 0)))
    return rows[:target_posts]


def _json_from_model(raw: str, default: Any) -> Any:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return default
    return default


def _message_text(msg) -> str:
    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _vision_summary(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode("ascii")
        prompt = """Read this screenshot of an X post. Focus on visible images, video frames, diagrams, code, charts, and UI details.

Return STRICT JSON only:
{
  "summary": "one concise summary of the visible media/post",
  "details": "specific readable facts, code, chart labels, product names, or UI states visible in the media",
  "depends_on_media": true,
  "confidence": "high | medium | low"
}

If the media/post is unreadable, say so and use low confidence. Do not invent hidden video/audio content."""
        msg = _generate._client_get().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = _message_text(msg)
        data = _json_from_model(raw, {})
        return {
            "summary": str(data.get("summary") or "")[:700],
            "details": str(data.get("details") or "")[:900],
            "depends_on_media": bool(data.get("depends_on_media", True)),
            "confidence": str(data.get("confidence") or "low").lower()[:20],
        }
    except Exception as e:
        return {"summary": "", "details": f"vision_failed:{e}", "depends_on_media": True, "confidence": "low"}


def _shortlist_for_ai(rows: list[dict], max_items: int) -> list[dict]:
    def pri(row: dict) -> tuple:
        action = (row.get("deterministic_classification") or {}).get("action")
        action_score = {"plain_repost_candidate": 4, "quote_candidate": 3, "skip": 0}.get(action, 1)
        engagement = int(row.get("likes") or 0) + 2 * int(row.get("reposts") or 0) + 3 * int(row.get("replies") or 0)
        return (
            action_score,
            int(row.get("flatkey_score") or 0),
            1 if row.get("has_video") else 0,
            1 if row.get("has_image") else 0,
            engagement,
        )
    candidates = [
        r for r in rows
        if (r.get("deterministic_classification") or {}).get("action") != "skip"
        or int(r.get("flatkey_score") or 0) >= 5
    ]
    candidates.sort(key=pri, reverse=True)
    return candidates[:max_items]


def enrich_media_with_vision(rows: list[dict], max_summaries: int):
    if max_summaries <= 0:
        return
    media_rows = [r for r in _shortlist_for_ai(rows, max(max_summaries * 3, max_summaries)) if r.get("media_screenshot_path")]
    done = 0
    for row in media_rows:
        if done >= max_summaries:
            break
        path = row.get("media_screenshot_path")
        if not path or not os.path.exists(path):
            continue
        row["media_vision"] = _vision_summary(path)
        row["media_text"] = _media_text(row)
        row["analysis_text"] = _analysis_text(row)
        row["flatkey_score"] = _flatkey_score(row, _load_json(POLICY_PATH, {}))
        row["deterministic_classification"] = _classify(row, _load_json(POLICY_PATH, {}))
        done += 1
        _log(f"vision summarized media id={row.get('id')} confidence={(row.get('media_vision') or {}).get('confidence')}")


def ai_review(rows: list[dict], max_items: int, policy: dict) -> dict[str, dict]:
    shortlist = _shortlist_for_ai(rows, max_items)
    if not shortlist:
        return {}
    items = []
    for r in shortlist:
        cls = r.get("deterministic_classification") or {}
        items.append({
            "id": r.get("id"),
            "author": r.get("author"),
            "url": r.get("url"),
            "metrics": {
                "likes": r.get("likes", 0),
                "reposts": r.get("reposts", 0),
                "replies": r.get("replies", 0),
                "views": r.get("views", 0),
                "age_minutes": r.get("age_minutes", 0),
            },
            "media": {
                "has_image": bool(r.get("has_image")),
                "has_video": bool(r.get("has_video")),
                "image_count": len(r.get("images") or []),
                "video_count": len(r.get("videos") or []),
                "media_text": _compact(r.get("media_text") or "", 700),
                "vision": r.get("media_vision") or {},
            },
            "deterministic": {
                "action": cls.get("action"),
                "reason": cls.get("reason"),
                "signals": cls.get("signals") or [],
                "score": r.get("flatkey_score"),
            },
            "text": _compact(r.get("analysis_text") or r.get("all_text") or "", 1100),
        })

    playbook = _load_text(PLAYBOOK_PATH)
    prompt = f"""You are reviewing X Home / For You posts for the Flatkey account.

Flatkey's strategic center:
- Flatkey should grow without relying on product updates.
- The account should own the category conversation around AI agents becoming real operators: model/tool shifts, coding-agent workflows, context/cost/routing, local runtimes, security/permissions, agent payments, and practical automation.
- Product mentions are optional and usually unnecessary. Do not infer Flatkey capabilities.

Flatkey playbook:
{playbook[:3200]}

Current policy:
{json.dumps(policy, ensure_ascii=False)[:2600]}

Task:
- Decide which posts are appropriate for Flatkey to QUOTE or PLAIN-REPOST.
- Quote candidates need a real Flatkey lens: agent workflow economics, routing/context/cost, local autonomy, security/permissions, agent payments, or operator-grade AI infrastructure.
- Plain repost is only for credible source posts that are valuable without added context and safe as-is.
- Optimize for account growth: prioritize posts with source authority, visible engagement, a live debate, a strong visual/video anchor, or a non-obvious builder lesson that a new AI-builder follower would remember.
- A quote candidate must support a sharp public POV, not just an accurate internal note. Prefer "the default take is wrong because..." or "this shifts the bottleneck from X to Y" style angles.
- Favor media-rich posts when the visible media summary contains real signal. If the video/image details are unreadable, do not plain-repost; quote only if there is enough visible text for a defensible angle.
- Skip generic AI hype, memes, drama, price/trading calls, token shills, shallow ChatGPT tips, low-engagement polls, and direct competitor dunking.
- Skip posts where the best Flatkey quote would be a generic "agents need verification/security/routing" line.
- Never claim Flatkey is live, supports a provider/tool, saved money, has users, has credits, or has access available.

Return STRICT JSON only:
{{
  "decisions": [
    {{
      "id": "post id",
      "action": "skip | quote_candidate | plain_repost_candidate",
      "reason": "short reason including why it can attract AI-builder attention",
      "quote_angle": "specific public POV if quote_candidate, else empty",
      "risk_flags": ["optional short flags"]
    }}
  ]
}}

Posts:
{json.dumps(items, ensure_ascii=False, indent=2)}
"""
    def call_review_model(review_prompt: str, max_tokens: int = 4500) -> dict:
        msg = _generate._client_get().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": review_prompt}],
        )
        return _json_from_model(_message_text(msg), {"decisions": []})

    data = call_review_model(prompt)
    if not data.get("decisions") and items:
        fallback_items = items[:24]
        fallback_prompt = f"""You are selecting posts for the Flatkey X account.

Flatkey is centered on AI agent infrastructure and operator workflows: coding agents, tool use, MCP, context/cost/routing, runtimes, permissions/security, and agent payments. Product mentions are not required.

From the posts below, choose the best posts Flatkey can quote or plain-repost. Return at least 3 candidates if at least 3 are defensible. Use plain_repost_candidate only when the post is credible and useful as-is; otherwise use quote_candidate. Optimize for views/follows: source authority, engagement, live debate, media/video anchor, and a non-obvious builder lesson. Skip shallow polls, memes, generic hype, low-engagement productivity content, and unreadable media.

Return STRICT JSON only:
{{
  "decisions": [
    {{
      "id": "post id",
      "action": "skip | quote_candidate | plain_repost_candidate",
      "reason": "short reason",
      "quote_angle": "short Flatkey angle if quote_candidate, else empty",
      "risk_flags": ["optional short flags"]
    }}
  ]
}}

Posts:
{json.dumps(fallback_items, ensure_ascii=False, indent=2)}
"""
        data = call_review_model(fallback_prompt, max_tokens=3000)
    decisions = {}
    for d in data.get("decisions") or []:
        pid = str(d.get("id") or "")
        action = str(d.get("action") or "skip")
        if pid and action in {"skip", "quote_candidate", "plain_repost_candidate"}:
            decisions[pid] = {
                "action": action,
                "reason": str(d.get("reason") or "")[:500],
                "quote_angle": str(d.get("quote_angle") or "")[:350],
                "risk_flags": [str(x)[:140] for x in (d.get("risk_flags") or [])[:6]],
            }
    return decisions


def generate_flatkey_quote(row: dict, ai_decision: dict, max_chars: int) -> dict:
    playbook = _load_text(PLAYBOOK_PATH)
    source = _display_source_text(row)
    quote_limit = min(max_chars, 240)
    prompt = f"""You operate the Flatkey X account.

Flatkey playbook:
{playbook[:3400]}

Write three candidate quote-tweet comments for the source post below, then choose the strongest one.

Required angle from review:
{ai_decision.get('quote_angle') or ai_decision.get('reason') or 'Add a practical AI-agent infrastructure lens.'}

Growth-quality rules:
- The quote must make a strong public point that can earn views/follows from AI builders.
- Lead with tension or contrast: "not X, Y", "the bottleneck moved from X to Y", "once X happens, Y breaks", or a similarly sharp mechanism.
- Make one point only. No list of three things. No internal architecture memo tone.
- Prefer concrete words from the source post over generic words like "agent workflows", "operator-grade", or "infrastructure problem".
- Max {quote_limit} characters. Best range: 100-210 characters.
- No hashtags, emojis, URLs, or @-mentions.
- Do not mention Flatkey.
- Do not claim product capabilities, launch, integrations, users, access, benchmarks, savings, or private data.
- Do not summarize the source. Add a non-obvious mechanism, caveat, workflow implication, or cost/routing/security/payment lens.
- Do not end with a question unless the source itself is asking a builder question.
- Avoid weak openings/phrases: "Agent workflows need...", "Worth tracking:", "The hard part is...", "Routing becomes...", "Persistent memory flips...".
- If the visible source text/media is not enough to support a quote, return an empty reply.

Source post by @{row.get('author')}:
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
    data = _json_from_model(_message_text(msg), {})
    candidates = []
    for item in data.get("variants") or []:
        txt = str((item or {}).get("reply") or "").strip().strip('"')
        if txt:
            candidates.append(txt)
    chosen = str(data.get("reply") or "").strip().strip('"')
    if chosen:
        candidates.insert(0, chosen)
    reply = ""
    block_reason = ""
    for cand in candidates:
        if len(cand) > quote_limit:
            cand = cand[:quote_limit].rsplit(" ", 1)[0].rstrip() + "..."
        violation = _flatkey_claim_violation(cand)
        issues = _quote_quality_issues(cand)
        if not violation and not issues:
            reply = cand
            break
        block_reason = violation or ",".join(issues[:3])
    if not reply:
        data["reply_angle"] = f"blocked_quality:{block_reason or 'no_passing_variant'}"
    return {
        "op_summary": str(data.get("op_summary") or "")[:240],
        "reply_angle": str(data.get("reply_angle") or ai_decision.get("quote_angle") or "")[:240],
        "reply": reply,
    }


def _entry_base(cfg: dict, row: dict, kind: str) -> dict:
    return {
        "id": f"flatkey_home_{kind}_{int(time.time())}_{str(row.get('id') or 'x')[-8:]}",
        "account": cfg.get("hunter_handle", "flatkey"),
        "telegram_label": "Flatkey",
        "kind": kind,
        "source": "flatkey_home_timeline_scout",
        "source_keyword": "home_for_you",
        "target": str(row.get("author") or "").lstrip("@"),
        "target_url": row.get("url", ""),
        "target_text": _display_source_text(row),
        "source_text": _display_source_text(row),
        "post_likes": row.get("likes", 0),
        "post_replies": row.get("replies", 0),
        "post_reposts": row.get("reposts", 0),
        "post_age_min": row.get("age_minutes", 0),
        "media": {
            "has_image": bool(row.get("has_image")),
            "has_video": bool(row.get("has_video")),
            "images": row.get("images") or [],
            "videos": row.get("videos") or [],
            "card_texts": row.get("card_texts") or [],
            "screenshot_path": row.get("media_screenshot_path", ""),
            "vision": row.get("media_vision") or {},
        },
        "deterministic_classification": row.get("deterministic_classification") or {},
        "ai_decision": row.get("ai_decision") or {},
        "queued_at": _ts(),
        "telegram_message_id": 0,
        "needs_human_approval": True,
    }


def _queue_or_update(entry: dict):
    with file_lock("reply_queue", timeout=60, on_wait=lambda s: _log(str(s))):
        cur = _load_json(QUEUE_PATH, [])
        for i, existing in enumerate(cur):
            if existing.get("id") == entry.get("id"):
                cur[i] = entry
                _save_json(QUEUE_PATH, cur)
                return
        cur.append(entry)
        _save_json(QUEUE_PATH, cur)


def _telegram_credentials(cfg: dict) -> tuple[str, str]:
    tg_cfg = cfg.get("telegram") or {}
    token = os.environ.get(str(tg_cfg.get("bot_token_env") or "TELEGRAM_BOT_TOKEN_FLATKEY"), "")
    chat_id = os.environ.get(str(tg_cfg.get("chat_id_env") or "TELEGRAM_CHAT_ID_FLATKEY"), "")
    return token, chat_id


def _send_card(cfg: dict, entry: dict) -> int:
    token, chat_id = _telegram_credentials(cfg)
    if not token or not chat_id:
        _log("  Telegram card skipped: configured env vars are not set")
        return 0
    msg_id = _tg.send_reply_card(entry, bot_token=token, chat_id=chat_id)
    if msg_id:
        _log(f"  Telegram card sent: message_id={msg_id}")
    else:
        _log("  Telegram card returned no message_id")
    return msg_id


def _append_seen_action(seen: dict, row_id: str, action: str):
    seen[row_id] = {"action": action, "at": datetime.now().isoformat()}


def _recent_seen(seen: dict, row_id: str, dedup_hours: int) -> bool:
    val = seen.get(row_id)
    if not val:
        return False
    when = val.get("at") if isinstance(val, dict) else val
    try:
        dt = datetime.fromisoformat(str(when))
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < dedup_hours * 3600


def route_candidates(cfg: dict, rows: list[dict], args, report: dict):
    hcfg = cfg.get("home_timeline_scout") or {}
    max_chars = int((cfg.get("generation") or {}).get("max_post_chars") or 280)
    send_to_tg = bool(hcfg.get("send_to_telegram", True))
    dedup_h = int(hcfg.get("dedup_window_hours") or 72)
    seen = _load_json(HOME_SEEN_PATH, {})

    quote_daily_cap = int(hcfg.get("daily_quote_draft_cap") or 0)
    max_quote_run = int(args.max_quote_drafts or hcfg.get("max_quote_drafts_per_run") or 2)
    if quote_daily_cap:
        max_quote_run = min(max_quote_run, max(0, quote_daily_cap - _daily_count(seen, "quote_drafts")))

    repost_daily_cap = int(hcfg.get("daily_repost_cap") or 3)
    max_reposts = int(args.max_reposts or hcfg.get("max_reposts_per_run") or 2)
    if repost_daily_cap:
        max_reposts = min(max_reposts, max(0, repost_daily_cap - _daily_count(seen, "repost_candidates")))

    quoted = 0
    reposts = 0
    quote_candidates = []
    repost_candidates = []
    has_ai_decisions = any(bool(r.get("ai_decision")) for r in rows)
    min_quote_likes = int(hcfg.get("min_quote_likes") or 40)
    min_quote_reposts = int(hcfg.get("min_quote_reposts") or 5)

    ordered = sorted(
        rows,
        key=lambda r: (
            {"plain_repost_candidate": 3, "quote_candidate": 2, "skip": 0}.get((r.get("ai_decision") or {}).get("action"), 0),
            int(r.get("flatkey_score") or 0),
            int(r.get("likes") or 0) + 2 * int(r.get("reposts") or 0) + 3 * int(r.get("replies") or 0),
        ),
        reverse=True,
    )

    for row in ordered:
        row_id = str(row.get("id") or "")
        if not row_id or _recent_seen(seen, row_id, dedup_h):
            continue
        ai_action = (row.get("ai_decision") or {}).get("action") or "skip"
        det_action = (row.get("deterministic_classification") or {}).get("action") or "skip"
        signals = set((row.get("deterministic_classification") or {}).get("signals") or [])
        low_pull = (
            int(row.get("likes") or 0) < min_quote_likes
            and int(row.get("reposts") or 0) < min_quote_reposts
            and "approved_source" not in signals
        )

        if has_ai_decisions:
            if ai_action == "plain_repost_candidate" and det_action == "plain_repost_candidate" and not _media_dependent(row):
                repost_candidates.append(row)
            elif ai_action == "quote_candidate" and not low_pull:
                quote_candidates.append(row)
        else:
            if det_action == "plain_repost_candidate" and not _media_dependent(row):
                repost_candidates.append(row)
            elif det_action == "quote_candidate" and not low_pull:
                quote_candidates.append(row)

    for row in repost_candidates:
        if reposts >= max_reposts or not args.queue_reposts or args.dry_run:
            break
        entry = _entry_base(cfg, row, "repost")
        entry.update({
            "reply_text": "",
            "op_summary": "Source already carries a Flatkey-relevant AI-agent infrastructure signal.",
            "reply_angle": "Plain repost after approval.",
            "status": "pending",
        })
        _queue_or_update(entry)
        if send_to_tg:
            msg_id = _send_card(cfg, entry)
            if msg_id:
                entry["telegram_message_id"] = msg_id
                _queue_or_update(entry)
        reposts += 1
        _increment_daily(seen, "repost_candidates")
        _append_seen_action(seen, str(row.get("id")), "queued_repost")
        report["reposts_queued"].append(entry)

    for row in quote_candidates:
        if quoted >= max_quote_run or not args.queue_quotes or args.dry_run:
            break
        draft = generate_flatkey_quote(row, row.get("ai_decision") or {}, max_chars=max_chars)
        qt = (draft.get("reply") or "").strip()
        if not qt or len(qt) < 24:
            _append_seen_action(seen, str(row.get("id")), "skip_empty_quote_draft")
            continue
        entry = _entry_base(cfg, row, "quote")
        entry.update({
            "reply_text": qt,
            "op_summary": draft.get("op_summary", ""),
            "reply_angle": draft.get("reply_angle", ""),
            "status": "pending",
        })
        _queue_or_update(entry)
        if send_to_tg:
            msg_id = _send_card(cfg, entry)
            if msg_id:
                entry["telegram_message_id"] = msg_id
                _queue_or_update(entry)
        quoted += 1
        _increment_daily(seen, "quote_drafts")
        _append_seen_action(seen, str(row.get("id")), "queued_quote")
        report["quotes_queued"].append(entry)

    _save_json(HOME_SEEN_PATH, seen)
    report["candidate_counts"] = {
        "quote_candidates": len(quote_candidates),
        "repost_candidates": len(repost_candidates),
        "quote_slots_remaining_after_run": max_quote_run - quoted,
        "repost_slots_remaining_after_run": max_reposts - reposts,
    }


def write_report(rows: list[dict], report: dict, json_path: str, md_path: str):
    payload = {
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "scraped_posts": len(rows),
            "with_images": sum(1 for r in rows if r.get("has_image")),
            "with_videos": sum(1 for r in rows if r.get("has_video")),
            "media_screenshots": sum(1 for r in rows if r.get("media_screenshot_path")),
            "media_vision_summaries": sum(1 for r in rows if r.get("media_vision")),
            "ai_reviewed": sum(1 for r in rows if r.get("ai_decision")),
            "deterministic_actions": {},
            "ai_actions": {},
            "quotes_queued": len(report.get("quotes_queued") or []),
            "reposts_queued": len(report.get("reposts_queued") or []),
        },
        "candidate_counts": report.get("candidate_counts") or {},
        "quotes_queued": report.get("quotes_queued") or [],
        "reposts_queued": report.get("reposts_queued") or [],
        "rows": rows,
    }
    for r in rows:
        a = (r.get("deterministic_classification") or {}).get("action") or "unknown"
        payload["summary"]["deterministic_actions"][a] = payload["summary"]["deterministic_actions"].get(a, 0) + 1
        ai = (r.get("ai_decision") or {}).get("action")
        if ai:
            payload["summary"]["ai_actions"][ai] = payload["summary"]["ai_actions"].get(ai, 0) + 1
    _save_json(json_path, payload)
    _save_json(os.path.join(STATE_DIR, "flatkey_home_timeline_scout_latest.json"), payload)

    lines = [
        "# Flatkey Home / For You Scout",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Summary",
        "",
        f"- scraped_posts: {len(rows)}",
        f"- with_images: {payload['summary']['with_images']}",
        f"- with_videos: {payload['summary']['with_videos']}",
        f"- media_screenshots: {payload['summary']['media_screenshots']}",
        f"- media_vision_summaries: {payload['summary']['media_vision_summaries']}",
        f"- ai_reviewed: {payload['summary']['ai_reviewed']}",
        f"- deterministic_actions: {payload['summary']['deterministic_actions']}",
        f"- ai_actions: {payload['summary']['ai_actions']}",
        f"- quotes_queued: {payload['summary']['quotes_queued']}",
        f"- reposts_queued: {payload['summary']['reposts_queued']}",
        "",
        "## AI Quote Candidates",
        "",
    ]
    ai_quotes = [r for r in rows if (r.get("ai_decision") or {}).get("action") == "quote_candidate"]
    if not ai_quotes:
        lines.append("- none")
    for r in ai_quotes[:12]:
        d = r.get("ai_decision") or {}
        lines.append(f"- @{r.get('author')} ({r.get('likes', 0)} likes, video={bool(r.get('has_video'))}, image={bool(r.get('has_image'))}): {r.get('url')}")
        lines.append(f"  - reason: {d.get('reason', '')}")
        if d.get("quote_angle"):
            lines.append(f"  - angle: {d.get('quote_angle')}")
        if r.get("media_vision"):
            lines.append(f"  - media: {_compact((r.get('media_vision') or {}).get('summary') or '', 260)}")
        lines.append(f"  - text: {_compact(r.get('analysis_text') or '', 260)}")
    lines.extend(["", "## AI Plain Repost Candidates", ""])
    ai_reposts = [r for r in rows if (r.get("ai_decision") or {}).get("action") == "plain_repost_candidate"]
    if not ai_reposts:
        lines.append("- none")
    for r in ai_reposts[:12]:
        d = r.get("ai_decision") or {}
        lines.append(f"- @{r.get('author')} ({r.get('likes', 0)} likes): {r.get('url')}")
        lines.append(f"  - reason: {d.get('reason', '')}")
        lines.append(f"  - deterministic: {(r.get('deterministic_classification') or {}).get('action')} / {(r.get('deterministic_classification') or {}).get('reason')}")
        lines.append(f"  - text: {_compact(r.get('analysis_text') or '', 260)}")
    lines.extend(["", "## Files", "", f"- JSON: `{json_path}`"])
    with open(md_path, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--target-posts", type=int, default=0)
    ap.add_argument("--max-scrolls", type=int, default=0)
    ap.add_argument("--wait", type=float, default=0.0)
    ap.add_argument("--max-ai-candidates", type=int, default=0)
    ap.add_argument("--max-media-screenshots", type=int, default=0)
    ap.add_argument("--max-media-vision", type=int, default=0)
    ap.add_argument("--max-quote-drafts", type=int, default=0)
    ap.add_argument("--max-reposts", type=int, default=0)
    ap.add_argument("--queue-quotes", action="store_true")
    ap.add_argument("--queue-reposts", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-vision", action="store_true")
    args = ap.parse_args()

    cfg = _load_json(args.config, {})
    if not cfg:
        raise SystemExit(f"config not found or invalid: {args.config}")
    hcfg = cfg.get("home_timeline_scout") or {}
    if not hcfg.get("enabled", True):
        _log("home_timeline_scout disabled in config; exit")
        return 0
    policy = _load_json(POLICY_PATH, {})
    port = int(cfg.get("hunter_port", 10006))
    if not _chrome.ping(port):
        raise SystemExit(f"Chrome debug port {port} unavailable")

    target_posts = int(args.target_posts or hcfg.get("target_posts") or 200)
    max_scrolls = int(args.max_scrolls or hcfg.get("max_scrolls") or 140)
    max_ai = int(args.max_ai_candidates or hcfg.get("max_ai_candidates") or 45)
    max_media_shots = int(args.max_media_screenshots or hcfg.get("max_media_screenshots") or 40)
    max_media_vision = int(args.max_media_vision or hcfg.get("max_media_vision") or 18)
    wait = float(args.wait or hcfg.get("wait_seconds") or 2.0)
    viewport_width = int(hcfg.get("viewport_width") or 1200)
    viewport_height = int(hcfg.get("viewport_height") or 1000)
    handle = str(cfg.get("hunter_handle") or "flatkey").lstrip("@")

    _log(f"flatkey home scout - target_posts={target_posts}, max_scrolls={max_scrolls}, wait={wait}, viewport={viewport_width}x{viewport_height}, ai={not args.no_ai}, vision={not args.no_vision}, queue_quotes={args.queue_quotes}, queue_reposts={args.queue_reposts}, dry_run={args.dry_run}")
    rows = scrape_home(port, policy, target_posts, max_scrolls, wait, handle, max_media_shots, viewport_width, viewport_height)
    _log(f"scraped home posts: {len(rows)}")

    if not args.no_vision:
        enrich_media_with_vision(rows, max_media_vision)
    else:
        _log("media vision disabled")

    if not args.no_ai:
        try:
            decisions = ai_review(rows, max_ai, policy)
            for r in rows:
                if str(r.get("id")) in decisions:
                    r["ai_decision"] = decisions[str(r.get("id"))]
            _log(f"ai reviewed candidates: {len(decisions)}")
        except Exception as e:
            _log(f"AI review failed: {e}")
    else:
        _log("AI review disabled")

    report = {"quotes_queued": [], "reposts_queued": []}
    route_candidates(cfg, rows, args, report)

    stamp = _slug_ts()
    json_path = os.path.join(STATE_DIR, f"flatkey_home_timeline_scout_{stamp}.json")
    md_path = os.path.join(STATE_DIR, f"flatkey_home_timeline_scout_{stamp}.md")
    write_report(rows, report, json_path, md_path)
    _log(f"wrote JSON report: {json_path}")
    _log(f"wrote markdown report: {md_path}")
    _log(f"done - scraped={len(rows)}, quotes_queued={len(report['quotes_queued'])}, reposts_queued={len(report['reposts_queued'])}")
    print(json.dumps({
        "scraped_posts": len(rows),
        "quotes_queued": len(report["quotes_queued"]),
        "reposts_queued": len(report["reposts_queued"]),
        "json_report": json_path,
        "md_report": md_path,
    }, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
