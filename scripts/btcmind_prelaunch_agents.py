#!/usr/bin/env python3
"""Executable BTCMind prelaunch agents.

Design source is limited to crypto/specs_for_agents. The runner implements the
safe prelaunch subset:

- 06 listener/radar: emit evidence-backed signals.
- 07 connector/scout: rank target accounts and recommend graph actions.
- 03 replier: draft conversation replies only, never auto-post.
- 09 brand-safety gate: approve/edit/block queued drafts.
- 10 analytics/learning: report low-risk learning from local state.
"""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
import telegram as _tg
import btcmind_reply_quality as _reply_quality
from lock import chrome_lock, file_lock


DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "engage_config.json")
SPECS_DIR = os.path.join(ROOT_DIR, "crypto", "specs_for_agents")
STATE_DIR = os.path.join(ROOT_DIR, "state")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")
RADAR_PATH = os.path.join(STATE_DIR, "btcmind_radar_signals.json")
GRAPH_PATH = os.path.join(STATE_DIR, "btcmind_target_graph.json")
SAFETY_PATH = os.path.join(STATE_DIR, "btcmind_safety_reviews.json")
ANALYTICS_PATH = os.path.join(STATE_DIR, "btcmind_analytics_report.json")
SEEN_PATH = os.path.join(STATE_DIR, "btcmind_prelaunch_seen.json")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "btcmind_prelaunch_agents")

QUIET = False

LISTENER_QUERIES = [
    '"BTC" "funding rate"',
    '"Bitcoin" "open interest"',
    '"BTC" liquidations',
    '"Bitcoin ETF" flows',
    '"crypto AI" "agent"',
    '"OKX" "BTC"',
    '"BTCMind"',
]

CONNECTOR_QUERIES = [
    '"BTC" "funding rate" trader',
    '"open interest" "BTC" trader',
    '"crypto AI" builder',
    '"AI agent" crypto',
    '"onchain analytics" "BTC"',
    '"OKX" trader',
    '"Bitcoin" derivatives',
]

REPLIER_QUERIES = [
    '"BTC" "funding rate"',
    '"Bitcoin" "open interest"',
    '"BTC" liquidations',
    '"crypto AI" "agent"',
    '"wallet UX" crypto',
    '"OKX" "BTC"',
]

BLOCK_PATTERNS = [
    r"\bguaranteed\b",
    r"\bguarantee\b",
    r"\bcan't lose\b",
    r"\bcannot lose\b",
    r"\beasy money\b",
    r"\bwill pump\b",
    r"\bmoon\b",
    r"\b100x\b",
    r"\bfinancial advice\b",
    r"\bbuy now\b",
    r"\bsell now\b",
    r"\bsignal(s)? group\b",
]

UNREADY_PRODUCT_PATTERNS = [
    r"\bnow live\b",
    r"\bdelegate\b",
    r"\bdelegated execution\b",
    r"\broutes? .* OKX\b",
    r"\busers? (caught|called)\b",
    r"\bBTCMind (called|caught|flagged)\b",
    r"\bour agents (called|caught|flagged)\b",
    r"\bconfidence score\b",
]

SEARCH_RESULTS_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el) {
        try {
            var head = (el.innerText || '').slice(0, 300);
            if (/reposted/i.test(head)) return;
            if (/Replying to/i.test(head)) return;
            if (/Promoted/i.test(head)) return;

            var textEl = el.querySelector('[data-testid="tweetText"]');
            var text = textEl ? textEl.innerText.trim() : '';
            if (!text) return;
            var urlEl = el.querySelector('a[href*="/status/"]');
            var url = urlEl ? urlEl.href : '';
            if (!url) return;
            var idMatch = url.match(/status[/](\d+)/);
            var id = idMatch ? idMatch[1] : '';
            var authorMatch = url.match(/x\.com\/([A-Za-z0-9_]+)\/status\//);
            var author = authorMatch ? authorMatch[1] : '';

            var replyEl = el.querySelector('[data-testid="reply"]');
            var replyTxt = replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g,'') : '';
            var likeEl = el.querySelector('[data-testid="like"]');
            var likeTxt = likeEl ? likeEl.innerText.replace(/[^0-9KMB.,]/g,'') : '';

            var timeEl = el.querySelector('time');
            var datetime = timeEl ? (timeEl.getAttribute('datetime') || '') : '';

            out.push({id:id, url:url, text:text.slice(0,900), author:author,
                      repliesTxt:replyTxt, likesTxt:likeTxt, datetime:datetime});
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(msg: str):
    if not QUIET:
        print(f"[{_ts()}] {msg}", flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(f"[{_ts()}] {msg}\n")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


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


def _spec_text(*names: str) -> str:
    chunks = []
    for name in ("README.md", *names):
        path = os.path.join(SPECS_DIR, name)
        txt = _load_text(path)
        if txt:
            chunks.append(f"--- {name} ---\n{txt}")
    return "\n\n".join(chunks)


def _strip_json(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.startswith("json"):
            s = s[4:].strip()
    return s


def _parse_json(raw: str, fallback):
    try:
        return json.loads(_strip_json(raw))
    except Exception:
        return fallback


def _model(cfg: dict) -> str:
    return ((cfg.get("generation") or {}).get("model") or "claude-haiku-4-5-20251001")


def _call_json(cfg: dict, prompt: str, fallback, max_tokens: int = 1400):
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=_model(cfg),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(msg.content[0].text, fallback)
    except Exception as e:
        _log(f"LLM unavailable, using fallback: {e}")
        return fallback


def _parse_count(s: str) -> int:
    if not s:
        return 0
    s = str(s).strip().upper().replace(",", "")
    m = re.match(r"^([0-9.]+)\s*([KMB])?$", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}[m.group(2) or ""])


def _age_minutes(dt_str: str) -> int:
    if not dt_str:
        return 999999
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() / 60))
    except Exception:
        return 999999


def _numbers(text: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z0-9_])[$+]?\d[\d,.]*(?:\.\d+)?%?[KMBkmb]?", text or "")


def _stable_id(*parts: str) -> str:
    joined = "\n".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _ensure_chrome(port: int):
    if not _chrome.ping(port):
        raise RuntimeError(f"Chrome port {port} is not live")
    return _chrome.connect(port)


def _fetch_search(ws, port: int, query: str, limit: int) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    with chrome_lock(port, on_wait=lambda s: _log(str(s))):
        _chrome.navigate(ws, f"https://x.com/search?q={q}&src=typed_query&f=live", wait=4.0)
        time.sleep(1.2)
        _chrome.eval_js(ws, "window.scrollBy(0, 700)")
        time.sleep(1.2)
        raw = _chrome.eval_js(ws, SEARCH_RESULTS_JS)
    rows = _parse_json(raw, [])
    out = []
    for row in rows[:limit]:
        row["likes"] = _parse_count(row.get("likesTxt", ""))
        row["replies"] = _parse_count(row.get("repliesTxt", ""))
        row["age_minutes"] = _age_minutes(row.get("datetime", ""))
        row["query"] = query
        out.append(row)
    return out


def _fetch_many(cfg: dict, queries: list[str], per_query: int) -> list[dict]:
    port = int(cfg.get("hunter_port", 10004))
    ws = _ensure_chrome(port)
    rows = []
    try:
        for query in queries:
            _log(f"search: {query}")
            try:
                rows.extend(_fetch_search(ws, port, query, per_query))
            except Exception as e:
                _log(f"  search failed: {e}")
            time.sleep(random.uniform(1.5, 3.0))
    finally:
        try:
            ws.close()
        except Exception:
            pass
    seen_urls = set()
    deduped = []
    for row in rows:
        url = row.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(row)
    return deduped


def _query_arg(value: str, defaults: list[str]) -> list[str]:
    if not value:
        return defaults
    return [x.strip() for x in value.split(",") if x.strip()]


def _print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _merge_records(path: str, records: list[dict], key: str, max_records: int = 1000):
    existing = _load_json(path, [])
    if isinstance(existing, dict):
        existing = existing.get("items") or existing.get("signals") or []
    by_key = {str(x.get(key, "")): x for x in existing if x.get(key)}
    for rec in records:
        rec_key = str(rec.get(key, ""))
        if rec_key:
            by_key[rec_key] = rec
    merged = list(by_key.values())[-max_records:]
    _save_json(path, merged)
    return merged


def _listener_fallback(candidates: list[dict], max_signals: int) -> list[dict]:
    out = []
    for c in candidates:
        text = c.get("text", "")
        low = text.lower()
        nums = _numbers(text)
        typ = ""
        route = "human"
        urgency = 2
        if "btcmind" in low and any(x in low for x in ("fake", "scam", "airdrop", "impersonat")):
            typ, route, urgency = "threat", "human", 5
        elif "btcmind" in low:
            typ, route, urgency = "brand_mention", "reposter", 3
        elif any(k in low for k in ("funding", "open interest", "liquidation", "etf flow", "flows")) and nums:
            typ, route, urgency = "market_event", "poster", 4
        elif any(k in low for k in ("crypto ai", "ai agent", "btc", "bitcoin", "okx")):
            typ, route, urgency = "conversation", "replier", 2
        if not typ:
            continue
        out.append({
            "id": _stable_id(c.get("url", ""), text),
            "run_at": _utc_iso(),
            "type": typ,
            "summary": re.sub(r"\s+", " ", text).strip()[:220],
            "evidence": {
                "numbers": "; ".join(nums + [f"{c.get('likes', 0)} likes", f"{c.get('replies', 0)} replies"]),
                "links": [c.get("url", "")],
            },
            "ticker": "$BTC" if any(x in low for x in ("btc", "bitcoin")) else "",
            "route_to": route,
            "author": "@" + c.get("author", "") if c.get("author") else "",
            "author_tier": "unknown",
            "urgency": urgency,
            "age_hours": round(c.get("age_minutes", 0) / 60.0, 2),
        })
        if len(out) >= max_signals:
            break
    return out


def run_listener(args, cfg: dict):
    queries = _query_arg(args.queries, LISTENER_QUERIES)
    candidates = _fetch_many(cfg, queries[: args.keyword_limit], args.results_per_query)
    candidates = [c for c in candidates if c.get("age_minutes", 999999) <= args.max_age_minutes]
    candidates.sort(key=lambda c: (c.get("likes", 0) + c.get("replies", 0) * 2), reverse=True)
    candidates = candidates[: args.max_candidates]
    fallback = _listener_fallback(candidates, args.max_signals)
    prompt = f"""{_spec_text("06-listener-radar.md")}

Prelaunch constraint: BTCMind product is not ready. Never recommend direct
posting because a product claim is available. Emit evidence-backed signals only.

Classify these X candidates into the exact JSON array schema from
06-listener-radar.md. Include only items with concrete evidence. Do not invent
numbers. Prefer route_to human or brand_safety for sensitive items.

Candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Return JSON array only."""
    signals = _call_json(cfg, prompt, fallback, max_tokens=1800)
    if not isinstance(signals, list):
        signals = fallback
    normalized = []
    for sig in signals[: args.max_signals]:
        if not isinstance(sig, dict):
            continue
        ev = sig.get("evidence") or {}
        links = ev.get("links") or []
        numbers = str(ev.get("numbers") or "")
        if not links and not numbers:
            continue
        sig["id"] = sig.get("id") or _stable_id(sig.get("summary", ""), json.dumps(links))
        sig["run_at"] = sig.get("run_at") or _utc_iso()
        normalized.append(sig)
    if not args.dry_run:
        _merge_records(RADAR_PATH, normalized, "id")
    _print_json(normalized)


def _connector_fallback(candidates: list[dict], max_targets: int) -> dict:
    by_author = {}
    for c in candidates:
        author = c.get("author", "")
        if not author:
            continue
        rec = by_author.setdefault(author.lower(), {
            "handle": "@" + author,
            "tier": 2,
            "relevance": 0.0,
            "engagement": 0.0,
            "audience_overlap": 0.0,
            "english": True,
            "recommended_action": "engage",
            "note": "",
            "_texts": [],
            "_likes": 0,
            "_replies": 0,
            "_queries": set(),
        })
        rec["_texts"].append(c.get("text", ""))
        rec["_likes"] += c.get("likes", 0)
        rec["_replies"] += c.get("replies", 0)
        rec["_queries"].add(c.get("query", ""))
    targets = []
    bad_terms = re.compile(r"\b(guaranteed|100x|signals group|pump|airdrop)\b", re.I)
    for rec in by_author.values():
        text = " ".join(rec.pop("_texts"))
        queries = " ".join(rec.pop("_queries"))
        likes = rec.pop("_likes")
        replies = rec.pop("_replies")
        low = (text + " " + queries).lower()
        if bad_terms.search(low):
            action = "exclude"
            note = "scam/guaranteed-return or token-shill language"
        elif "okx" in low or "exchange" in low:
            rec["tier"] = 1
            action = "engage"
            note = "partner or exchange ecosystem adjacency"
        elif any(k in low for k in ("funding", "open interest", "derivatives", "perp")):
            action = "recruit"
            note = "BTC/derivatives conversation fit"
        elif any(k in low for k in ("crypto ai", "ai agent", "onchain", "wallet")):
            action = "list"
            note = "audience overlap with BTCMind research-desk positioning"
        else:
            action = "engage"
            note = "crypto conversation candidate"
        rec["relevance"] = min(1.0, 0.45 + 0.08 * len(_numbers(text)) + (0.2 if action in ("recruit", "list") else 0))
        rec["engagement"] = min(1.0, (likes + replies * 2) / 250.0)
        rec["audience_overlap"] = min(1.0, 0.35 + (0.3 if action in ("recruit", "list") else 0.1))
        rec["recommended_action"] = action
        rec["note"] = note
        targets.append(rec)
    targets.sort(key=lambda r: (r["recommended_action"] == "exclude", -(r["relevance"] + r["engagement"] + r["audience_overlap"])))
    targets = targets[:max_targets]
    graph_actions = []
    for t in targets:
        if t["recommended_action"] == "recruit":
            graph_actions.append(f"add {t['handle']} to BTC derivatives KOL candidates")
        elif t["recommended_action"] == "list":
            graph_actions.append(f"add {t['handle']} to crypto AI/onchain watch list")
        elif t["recommended_action"] == "engage":
            graph_actions.append(f"monitor {t['handle']} for useful conversations")
    return {"targets": targets, "graph_actions_today": graph_actions[:10]}


def run_connector(args, cfg: dict):
    queries = _query_arg(args.queries, CONNECTOR_QUERIES)
    candidates = _fetch_many(cfg, queries[: args.keyword_limit], args.results_per_query)
    candidates = [c for c in candidates if c.get("age_minutes", 999999) <= args.max_age_minutes]
    fallback = _connector_fallback(candidates, args.max_targets)
    prompt = f"""{_spec_text("07-connector-scout.md")}

Prelaunch constraint: BTCMind product is not ready. Do not post, reply, quote,
repost, DM, or claim product performance. You may recommend graph actions only.

Rank these X candidates into the exact JSON object schema from
07-connector-scout.md. Exclude bots/scams/guaranteed-return accounts. Favor
mid-tier BTC/derivatives traders, crypto AI builders, onchain analysts, and OKX
ecosystem accounts.

Candidates:
{json.dumps(candidates[:120], ensure_ascii=False, indent=2)}

Return JSON object only."""
    result = _call_json(cfg, prompt, fallback, max_tokens=1800)
    if not isinstance(result, dict) or not isinstance(result.get("targets"), list):
        result = fallback
    result["run_at"] = _utc_iso()
    if not args.dry_run:
        existing = _load_json(GRAPH_PATH, {"targets": [], "runs": []})
        by_handle = {str(t.get("handle", "")).lower(): t for t in existing.get("targets", [])}
        for t in result.get("targets", []):
            h = str(t.get("handle", "")).lower()
            if h:
                by_handle[h] = t
        existing["targets"] = list(by_handle.values())[-500:]
        existing.setdefault("runs", []).append(result)
        existing["runs"] = existing["runs"][-100:]
        _save_json(GRAPH_PATH, existing)
    _print_json({k: v for k, v in result.items() if k != "run_at"})


def _is_seen(seen: dict, key: str, window_hours: int) -> bool:
    when = seen.get(key)
    if not when:
        return False
    try:
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        return age < window_hours
    except Exception:
        return False


def _deterministic_safety(text: str, provenance: str = ""):
    failed = []
    for i, pat in enumerate(BLOCK_PATTERNS, start=1):
        if re.search(pat, text or "", flags=re.I):
            failed.append(1)
            break
    for pat in UNREADY_PRODUCT_PATTERNS:
        if re.search(pat, text or "", flags=re.I):
            failed.append(6)
            break
    if re.search(r"\b(to|at|above|below)\s+\$?\d[\d,.]*[Kk]?\b", text or "", flags=re.I):
        failed.append(2)
    draft_nums = set(_numbers(text))
    prov_nums = set(_numbers(provenance))
    unsourced = [n for n in draft_nums if n not in prov_nums]
    if unsourced:
        failed.append(3)
    if failed:
        return {
            "verdict": "block",
            "edited_text": None,
            "failed_checks": sorted(set(failed)),
            "reason": "deterministic prelaunch safety block",
            "escalate_to_human": 3 in failed,
        }
    return None


def _generate_reply_json(cfg: dict, candidate: dict, quality_feedback: str = "") -> dict:
    fallback = {
        "reply_text": "",
        "should_reply": False,
        "needs_safety_review": False,
        "rationale": "fallback skipped because LLM was unavailable",
    }
    prompt = f"""{_spec_text("03-replier.md", "09-brand-safety-gate.md")}

Prelaunch constraint: BTCMind product is not ready. Conversation mode only.
Do not claim product performance, execution, screenshots, confidence scores,
calls, or live BTCMind output. Do not pitch BTCMind unless directly invited.

Local BTCMind reply quality rules:
{_reply_quality.generation_rules(cfg)}
{("Local audit feedback to fix: " + quality_feedback) if quality_feedback else ""}

Target tweet:
{json.dumps(candidate, ensure_ascii=False, indent=2)}

Write one short helpful reply under the exact JSON schema from 03-replier.md.
If this is bait, low signal, unsafe, or not relevant, set should_reply=false and
reply_text="". Return JSON only."""
    data = _call_json(cfg, prompt, fallback, max_tokens=700)
    if not isinstance(data, dict):
        return fallback
    return {
        "reply_text": str(data.get("reply_text") or "").strip(),
        "should_reply": bool(data.get("should_reply")),
        "needs_safety_review": bool(data.get("needs_safety_review")),
        "rationale": str(data.get("rationale") or "").strip()[:300],
    }


def _queue_reply(entry: dict, cfg: dict, send_telegram: bool):
    with file_lock("reply_queue", on_wait=lambda s: _log(str(s))):
        cur = _load_json(QUEUE_PATH, [])
        if any(x.get("id") == entry.get("id") for x in cur):
            return False
        cur.append(entry)
        _save_json(QUEUE_PATH, cur)

    if send_telegram:
        try:
            tg_cfg = cfg.get("telegram") or {}
            token = os.environ.get(tg_cfg.get("bot_token_env", ""), "")
            chat_id = os.environ.get(tg_cfg.get("chat_id_env", ""), "")
            msg_id = _tg.send_reply_card(entry, bot_token=token, chat_id=chat_id)
            if msg_id:
                with file_lock("reply_queue", on_wait=lambda s: _log(str(s))):
                    cur = _load_json(QUEUE_PATH, [])
                    for i, row in enumerate(cur):
                        if row.get("id") == entry.get("id"):
                            cur[i]["telegram_message_id"] = msg_id
                            _save_json(QUEUE_PATH, cur)
                            break
        except Exception as e:
            _log(f"telegram notify failed: {e}")
    return True


def _build_prelaunch_reply_entry(c: dict, draft: dict, text: str) -> dict:
    return {
        "id": f"btcmind_pre_{int(time.time())}_{_stable_id(c.get('url', ''), text)}",
        "kind": "reply",
        "source": "btcmind_prelaunch_replier",
        "source_keyword": c.get("query", ""),
        "target": c.get("author", ""),
        "target_url": c.get("url", ""),
        "target_text": c.get("text", ""),
        "reply_text": text,
        "op_summary": re.sub(r"\s+", " ", c.get("text", "")).strip()[:160],
        "reply_angle": draft.get("rationale", ""),
        "status": "pending",
        "queued_at": _ts(),
        "post_age_min": c.get("age_minutes", 0),
        "post_replies": c.get("replies", 0),
        "post_likes": c.get("likes", 0),
        "needs_safety_review": bool(draft.get("needs_safety_review")),
        "prelaunch": True,
    }


def run_replier(args, cfg: dict):
    queries = _query_arg(args.queries, REPLIER_QUERIES)
    candidates = _fetch_many(cfg, queries[: args.keyword_limit], args.results_per_query)
    target_set = {str(x).lower() for x in cfg.get("target_accounts", [])}
    filtered = []
    for c in candidates:
        if c.get("age_minutes", 999999) > args.max_age_minutes:
            continue
        if c.get("likes", 0) < args.min_likes:
            continue
        if c.get("replies", 0) > args.max_replies:
            continue
        if len((c.get("text") or "").strip()) < 35:
            continue
        if c.get("author", "").lower() in target_set and args.skip_targets:
            continue
        filtered.append(c)
    filtered.sort(key=lambda c: (c.get("likes", 0) + c.get("replies", 0) * 2), reverse=True)

    seen = _load_json(SEEN_PATH, {})
    drafted = []
    for c in filtered:
        if len(drafted) >= args.max_drafts:
            break
        key = f"reply:{c.get('id') or c.get('url')}"
        if _is_seen(seen, key, args.dedup_hours):
            continue
        draft = _generate_reply_json(cfg, c)
        if not draft.get("should_reply") or not draft.get("reply_text"):
            seen[key] = _utc_iso()
            continue
        text = draft["reply_text"]
        if len(text) > args.max_chars:
            text = text[: args.max_chars].rsplit(" ", 1)[0]
        safety = _deterministic_safety(text, c.get("text", ""))
        if safety:
            seen[key] = _utc_iso()
            drafted.append({
                "target": "@" + c.get("author", ""),
                "target_url": c.get("url", ""),
                "reply_text": text,
                "blocked_by_safety": safety,
            })
            continue
        entry = _build_prelaunch_reply_entry(c, draft, text)
        audit = _reply_quality.apply_audit(entry, cfg, recent_entries=_load_json(QUEUE_PATH, []))
        if audit.get("reply_risk_class") in {"needs_rewrite", "block"}:
            draft = _generate_reply_json(cfg, c, _reply_quality.rewrite_feedback(audit))
            if not draft.get("should_reply") or not draft.get("reply_text"):
                seen[key] = _utc_iso()
                continue
            text = draft["reply_text"]
            if len(text) > args.max_chars:
                text = text[: args.max_chars].rsplit(" ", 1)[0]
            safety = _deterministic_safety(text, c.get("text", ""))
            if safety:
                seen[key] = _utc_iso()
                drafted.append({
                    "target": "@" + c.get("author", ""),
                    "target_url": c.get("url", ""),
                    "reply_text": text,
                    "blocked_by_safety": safety,
                })
                continue
            entry = _build_prelaunch_reply_entry(c, draft, text)
            audit = _reply_quality.apply_audit(entry, cfg, recent_entries=_load_json(QUEUE_PATH, []))
        if audit.get("reply_risk_class") in {"needs_rewrite", "block"}:
            seen[key] = _utc_iso()
            drafted.append({
                "target": "@" + c.get("author", ""),
                "target_url": c.get("url", ""),
                "reply_text": text,
                "blocked_by_quality": audit,
            })
            continue
        if not args.dry_run:
            _queue_reply(entry, cfg, not args.no_telegram)
            seen[key] = _utc_iso()
            _save_json(SEEN_PATH, seen)
        drafted.append(entry)
    if not args.dry_run:
        _save_json(SEEN_PATH, seen)
    _print_json(drafted)


def _llm_safety(cfg: dict, entry: dict) -> dict:
    provenance = "\n".join([
        str(entry.get("target_text") or ""),
        str(entry.get("source_context") or ""),
        str(entry.get("op_summary") or ""),
    ])
    deterministic = _deterministic_safety(entry.get("reply_text", ""), provenance)
    if deterministic:
        return deterministic
    fallback = {
        "verdict": "approve",
        "edited_text": None,
        "failed_checks": [],
        "reason": "deterministic fallback found no blocking issue",
        "escalate_to_human": False,
    }
    prompt = f"""{_spec_text("09-brand-safety-gate.md")}

Prelaunch constraint: BTCMind product is not ready, so alpha-honesty is strict.
Review this queued draft. Return the exact JSON schema from
09-brand-safety-gate.md only.

Draft entry:
{json.dumps(entry, ensure_ascii=False, indent=2)}

Signal provenance:
{provenance}

Return JSON only."""
    data = _call_json(cfg, prompt, fallback, max_tokens=900)
    if not isinstance(data, dict):
        return fallback
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("approve", "edit", "block"):
        verdict = "block"
    return {
        "verdict": verdict,
        "edited_text": data.get("edited_text"),
        "failed_checks": data.get("failed_checks") if isinstance(data.get("failed_checks"), list) else [],
        "reason": str(data.get("reason") or "")[:500],
        "escalate_to_human": bool(data.get("escalate_to_human")),
    }


def run_safety(args, cfg: dict):
    queue = _load_json(QUEUE_PATH, [])
    reviews = _load_json(SAFETY_PATH, {})
    if not isinstance(reviews, dict):
        reviews = {}
    selected = []
    for entry in queue:
        source = str(entry.get("source", ""))
        if entry.get("status") != "pending":
            continue
        if args.only_btcmind and not (source.startswith("btcmind") or entry.get("prelaunch")):
            continue
        fingerprint = _stable_id(entry.get("id", ""), entry.get("reply_text", ""))
        if reviews.get(fingerprint) and not args.force:
            continue
        selected.append((entry, fingerprint))
        if len(selected) >= args.max_reviews:
            break

    outputs = []
    updates = {}
    for entry, fingerprint in selected:
        verdict = _llm_safety(cfg, entry)
        verdict["entry_id"] = entry.get("id", "")
        verdict["reviewed_at"] = _utc_iso()
        reviews[fingerprint] = verdict
        outputs.append(verdict)
        if not args.dry_run:
            updates[entry.get("id", "")] = verdict

    if updates:
        with file_lock("reply_queue", on_wait=lambda s: _log(str(s))):
            queue = _load_json(QUEUE_PATH, [])
            for i, entry in enumerate(queue):
                verdict = updates.get(entry.get("id", ""))
                if not verdict:
                    continue
                entry["safety_verdict"] = verdict["verdict"]
                entry["safety_reason"] = verdict.get("reason", "")
                entry["safety_reviewed_at"] = verdict.get("reviewed_at", "")
                if verdict["verdict"] == "block":
                    entry["status"] = "blocked_safety"
                elif verdict["verdict"] == "edit" and verdict.get("edited_text"):
                    entry["reply_text_original"] = entry.get("reply_text", "")
                    entry["reply_text"] = verdict["edited_text"]
                queue[i] = entry
            _save_json(QUEUE_PATH, queue)
    if not args.dry_run:
        _save_json(SAFETY_PATH, reviews)
    _print_json(outputs)


def _parse_local_ts(value: str):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def run_analytics(args, cfg: dict):
    queue = _load_json(QUEUE_PATH, [])
    btcmind = [q for q in queue if str(q.get("source", "")).startswith("btcmind") or q.get("prelaunch")]
    dates = [_parse_local_ts(q.get("queued_at", "")) for q in btcmind]
    dates = [d for d in dates if d]
    start = min(dates).date().isoformat() if dates else datetime.now().date().isoformat()
    end = datetime.now().date().isoformat()
    by_status = {}
    by_source = {}
    for q in btcmind:
        by_status[q.get("status", "unknown")] = by_status.get(q.get("status", "unknown"), 0) + 1
        by_source[q.get("source", "unknown")] = by_source.get(q.get("source", "unknown"), 0) + 1
    radar = _load_json(RADAR_PATH, [])
    graph = _load_json(GRAPH_PATH, {"targets": []})

    winners = []
    losers = []
    recommendations = []
    experiments = [{
        "name": "prelaunch draft-only conversation presence",
        "status": "running",
        "result": f"{len(btcmind)} BTCMind queue items; public-performance sample is not mature yet",
    }]
    posted = by_status.get("posted", 0)
    if posted < args.min_sample:
        recommendations.append({
            "agent": "replier",
            "change": "Keep conversation-mode drafts human-reviewed until posted sample reaches a useful size.",
            "confidence": "low",
        })
    if len(radar) == 0:
        recommendations.append({
            "agent": "poster",
            "change": "No evidence-backed Listener signals yet; do not activate original posting.",
            "confidence": "high",
        })
    else:
        winners.append({
            "pattern": "evidence-backed Listener signals available",
            "metric": "signal_count",
            "lift": str(len(radar)),
            "n": len(radar),
        })
    pending = by_status.get("pending", 0)
    if pending > 10:
        losers.append({
            "pattern": "pending human-review backlog",
            "metric": "pending_queue",
            "n": pending,
        })
        recommendations.append({
            "agent": "scheduler",
            "change": "Throttle replier draft creation until pending review backlog is cleared.",
            "confidence": "med",
        })
    if len(graph.get("targets", [])) > 0:
        winners.append({
            "pattern": "target graph accumulating prelaunch candidates",
            "metric": "target_count",
            "lift": str(len(graph.get("targets", []))),
            "n": len(graph.get("targets", [])),
        })

    report = {
        "period": f"{start}..{end}",
        "winners": winners,
        "losers": losers,
        "recommendations": recommendations,
        "experiments": experiments,
        "prelaunch_counts": {
            "queue_items": len(btcmind),
            "by_status": by_status,
            "by_source": by_source,
            "radar_signals": len(radar) if isinstance(radar, list) else 0,
            "target_graph_size": len(graph.get("targets", [])) if isinstance(graph, dict) else 0,
        },
    }
    if not args.dry_run:
        _save_json(ANALYTICS_PATH, report)
    _print_json(report)


def build_parser():
    p = argparse.ArgumentParser(description="Run BTCMind prelaunch agents from specs_for_agents.")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--json-only", action="store_true", help="print only final JSON on stdout")
    sub = p.add_subparsers(dest="cmd", required=True)

    common_search = argparse.ArgumentParser(add_help=False)
    common_search.add_argument("--queries", default="", help="comma-separated queries; defaults are spec-derived")
    common_search.add_argument("--keyword-limit", type=int, default=4)
    common_search.add_argument("--results-per-query", type=int, default=8)
    common_search.add_argument("--max-age-minutes", type=int, default=1440)
    common_search.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("listener", parents=[common_search])
    s.add_argument("--max-candidates", type=int, default=40)
    s.add_argument("--max-signals", type=int, default=12)
    s.set_defaults(func=run_listener)

    s = sub.add_parser("connector", parents=[common_search])
    s.add_argument("--max-targets", type=int, default=25)
    s.set_defaults(func=run_connector)

    s = sub.add_parser("replier-drafts", parents=[common_search])
    s.add_argument("--max-drafts", type=int, default=3)
    s.add_argument("--min-likes", type=int, default=10)
    s.add_argument("--max-replies", type=int, default=180)
    s.add_argument("--max-chars", type=int, default=200)
    s.add_argument("--dedup-hours", type=int, default=72)
    s.add_argument("--skip-targets", action="store_true", default=True)
    s.add_argument("--no-telegram", action="store_true")
    s.set_defaults(func=run_replier)

    s = sub.add_parser("safety")
    s.add_argument("--max-reviews", type=int, default=20)
    s.add_argument("--only-btcmind", action="store_true", default=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=run_safety)

    s = sub.add_parser("analytics")
    s.add_argument("--min-sample", type=int, default=10)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=run_analytics)
    return p


def main():
    global QUIET
    parser = build_parser()
    args = parser.parse_args()
    QUIET = bool(args.json_only)
    cfg = _load_json(args.config, {})
    if not cfg:
        raise SystemExit(f"config not found or invalid: {args.config}")
    args.func(args, cfg)


if __name__ == "__main__":
    main()
