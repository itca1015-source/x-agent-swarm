#!/usr/bin/env python3
"""BTCMind Signal Source + Poster autopilot.

Reads structured BTCMind signals and creates reviewable original-post drafts in
the shared reply_queue.json flow. This script never auto-publishes.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import telegram as _tg
from lock import file_lock

DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "engage_config.json")
DEFAULT_SIGNALS = os.path.join(ROOT_DIR, "state", "btcmind_signals.json")
QUEUE_PATH = os.path.join(ROOT_DIR, "state", "reply_queue.json")
SEEN_PATH = os.path.join(ROOT_DIR, "state", "btcmind_signal_poster_seen.json")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "btcmind_signal_poster")
BLUEPRINT_PATH = os.path.join(ROOT_DIR, "docs", "btcmind_nansen_autopilot_blueprint.md")

BLOCK_PATTERNS = [
    r"\b100x\b",
    r"\bguaranteed\b",
    r"\bcan't lose\b",
    r"\bcannot lose\b",
    r"\beasy money\b",
    r"\bwill pump\b",
    r"\bprice target\b",
    r"\bfinancial advice\b",
    r"#\w+",
    r"@[A-Za-z0-9_]{1,20}",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _daily_key(handle: str, name: str) -> str:
    acct = str(handle or "default").strip().lstrip("@").lower() or "default"
    return f"_daily:{acct}:{name}:{datetime.now():%Y-%m-%d}"


def _daily_count(seen: dict, handle: str, name: str) -> int:
    try:
        return int(seen.get(_daily_key(handle, name), 0) or 0)
    except Exception:
        return 0


def _increment_daily(seen: dict, handle: str, name: str):
    key = _daily_key(handle, name)
    seen[key] = _daily_count(seen, handle, name) + 1


def _load_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _signal_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _normalize_signal(raw) -> dict:
    if isinstance(raw, str):
        return {
            "id": _signal_id(raw),
            "type": "operator_watch",
            "summary": raw.strip(),
            "evidence": [],
            "operator_watch": "",
            "risk_notes": "",
            "source_url": "",
        }
    if not isinstance(raw, dict):
        return {}
    summary = (raw.get("summary") or raw.get("signal") or raw.get("text") or "").strip()
    if not summary:
        return {}
    out = dict(raw)
    out["summary"] = summary
    out["id"] = str(out.get("id") or _signal_id(summary))
    out["type"] = str(out.get("type") or "operator_watch")
    evidence = out.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    out["evidence"] = [str(x).strip() for x in evidence if str(x).strip()]
    out["operator_watch"] = str(out.get("operator_watch") or out.get("watch") or "").strip()
    out["risk_notes"] = str(out.get("risk_notes") or out.get("risk") or "").strip()
    out["source_url"] = str(out.get("source_url") or out.get("url") or "").strip()
    return out


def _load_signals(path: str, inline_signal: str) -> list:
    rows = []
    if inline_signal.strip():
        rows.append(_normalize_signal(inline_signal))
    data = _load(path, [])
    if isinstance(data, dict):
        data = data.get("signals") or []
    for raw in data:
        rows.append(_normalize_signal(raw))
    return [r for r in rows if r]


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "."


def _template_post(signal: dict, max_chars: int) -> dict:
    summary = _clip(signal.get("summary", ""), 170)
    evidence = signal.get("evidence") or []
    watch = _clip(signal.get("operator_watch", ""), 90)
    risk = _clip(signal.get("risk_notes", ""), 80)
    stype = signal.get("type", "operator_watch")

    if stype == "wallet_flow":
        body = summary
        if evidence:
            body += f"\n\nSignal: {_clip(evidence[0], 90)}"
        if watch:
            body += f"\n\nWatch whether {watch}"
    elif stype == "debate_result":
        body = f"The useful part of this debate is not who sounds bullish. It is the constraint: {summary}"
        if watch:
            body += f"\n\nWatch {watch}"
    elif stype == "market_structure":
        body = summary
        if risk:
            body += f"\n\nThe risk is {risk}"
        if watch:
            body += f"\n\nThe next signal is {watch}"
    else:
        body = summary
        if evidence:
            body += f"\n\nEvidence: {_clip(evidence[0], 100)}"
        if watch:
            body += f"\n\nWatch {watch}"

    body = _clip(body, max_chars)
    return {
        "reply": body,
        "op_summary": summary,
        "reply_angle": f"BTCMind signal poster: {stype}",
    }


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return raw


def _generate_post(handle: str, signal: dict, max_chars: int, template_only: bool) -> dict:
    if template_only:
        return _template_post(signal, max_chars)

    import anthropic

    account_playbook = _load_text(os.path.join(ROOT_DIR, "accounts", handle, "playbook.md"))
    blueprint = _load_text(BLUEPRINT_PATH)[:6000]
    prompt = f"""You operate the BTCMind X account.

Account playbook:
{account_playbook}

Nansen-derived autopilot blueprint:
{blueprint}

Write one original BTCMind post from this structured signal.

Signal:
{json.dumps(signal, ensure_ascii=False, indent=2)}

Rules:
- Maximum {max_chars} characters.
- No hashtags.
- No emojis.
- No @ mentions.
- No financial advice.
- No price targets or return promises.
- Do not say "this is huge", "bullish", "must read", or "LFG".
- Show a mechanism, constraint, or next signal to watch.
- Sound like an AI crypto research desk, not a token promoter.

Return STRICT JSON only:
{{
  "reply": "the post text",
  "op_summary": "one-line source signal summary",
  "reply_angle": "one-line angle"
}}"""

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_json(msg.content[0].text)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"reply": raw, "op_summary": signal.get("summary", ""), "reply_angle": "BTCMind signal poster"}
    return {
        "reply": (data.get("reply") or "").strip(),
        "op_summary": (data.get("op_summary") or signal.get("summary") or "").strip(),
        "reply_angle": (data.get("reply_angle") or "BTCMind signal poster").strip(),
    }


def _safety_issues(text: str, max_chars: int) -> list:
    issues = []
    if len(text) > max_chars:
        issues.append(f"too_long:{len(text)}>{max_chars}")
    if len(text.strip()) < 30:
        issues.append("too_short")
    for pat in BLOCK_PATTERNS:
        if re.search(pat, text, flags=re.I):
            issues.append(f"blocked_pattern:{pat}")
    return issues


def _build_entry(handle: str, signal: dict, draft: dict) -> dict:
    ctx_parts = [
        f"type: {signal.get('type', '')}",
        f"summary: {signal.get('summary', '')}",
    ]
    if signal.get("evidence"):
        ctx_parts.append("evidence: " + " | ".join(signal["evidence"][:3]))
    if signal.get("operator_watch"):
        ctx_parts.append("watch: " + signal["operator_watch"])
    if signal.get("risk_notes"):
        ctx_parts.append("risk: " + signal["risk_notes"])
    if signal.get("source_url"):
        ctx_parts.append("source: " + signal["source_url"])

    return {
        "id": f"sig_{int(time.time())}_{signal['id']}",
        "account": handle,
        "kind": "original",
        "source": "btcmind_signal",
        "source_signal_id": signal["id"],
        "source_context": "\n".join(ctx_parts),
        "reply_text": draft["reply"],
        "op_summary": draft.get("op_summary", ""),
        "reply_angle": draft.get("reply_angle", ""),
        "status": "pending",
        "needs_human_approval": True,
        "safety_policy": "btcmind_originals_approval_only",
        "queued_at": _ts(),
        "telegram_message_id": 0,
    }


def _queue_entry(entry: dict, cfg: dict):
    try:
        import reply_scorer as _rs
        _rs.score_entry(entry)
    except Exception as e:
        _log(f"  scorer error (non-fatal): {e}")

    with file_lock("reply_queue", on_wait=_log):
        cur = _load(QUEUE_PATH, [])
        cur.append(entry)
        _save(QUEUE_PATH, cur)

    try:
        tg_cfg = cfg.get("telegram") or {}
        token = os.environ.get(tg_cfg.get("bot_token_env", ""), "")
        chat_id = os.environ.get(tg_cfg.get("chat_id_env", ""), "")
        if not token or not chat_id:
            _log("  Telegram send skipped: configured Telegram env vars are not set")
            msg_id = 0
        else:
            msg_id = _tg.send_reply_card(entry, bot_token=token, chat_id=chat_id)
        if msg_id:
            entry["telegram_message_id"] = msg_id
            with file_lock("reply_queue", on_wait=_log):
                cur = _load(QUEUE_PATH, [])
                for i, existing in enumerate(cur):
                    if existing.get("id") == entry["id"]:
                        cur[i] = entry
                        _save(QUEUE_PATH, cur)
                        break
        elif token and chat_id:
            _log("  Telegram send returned no message_id")
    except Exception as e:
        _log(f"  Telegram send failed: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--signals-file", default=DEFAULT_SIGNALS)
    parser.add_argument("--signal", default="", help="one-off signal summary")
    parser.add_argument("--max-drafts", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--template-only", action="store_true",
                        help="avoid LLM call and use local deterministic template")
    args = parser.parse_args()

    cfg = _load(args.config, {})
    handle = cfg.get("hunter_handle", "hunter_solvea")
    max_chars = int((cfg.get("generation") or {}).get("max_post_chars", 280))
    spcfg = cfg.get("signal_poster") or {}
    max_drafts = int(args.max_drafts or spcfg.get("max_drafts_per_run") or 1)

    signals = _load_signals(args.signals_file, args.signal)
    if not signals:
        _log(f"no signals found. Add {args.signals_file} or pass --signal")
        return

    seen = _load(SEEN_PATH, {})
    daily_cap = int(spcfg.get("daily_draft_cap") or 0)
    if daily_cap and not args.dry_run:
        drafted_today = _daily_count(seen, handle, "original_drafts")
        remaining_today = max(0, daily_cap - drafted_today)
        if remaining_today <= 0:
            _log(f"signal_poster daily draft cap reached for {handle}: {drafted_today}/{daily_cap}")
            return
        max_drafts = min(max_drafts, remaining_today)
    drafted = 0
    _log(f"signal_poster run - handle={handle}, signals={len(signals)}, max_drafts={max_drafts}")

    for signal in signals:
        if drafted >= max_drafts:
            break
        sid = signal["id"]
        if seen.get(sid):
            _log(f"  skip {sid}: already drafted at {seen[sid]}")
            continue
        try:
            draft = _generate_post(handle, signal, max_chars, args.template_only)
        except Exception as e:
            _log(f"  generate failed for {sid}: {e}")
            continue
        text = (draft.get("reply") or "").strip()
        issues = _safety_issues(text, max_chars)
        if issues:
            _log(f"  safety blocked {sid}: {issues} text={text[:160]!r}")
            continue
        entry = _build_entry(handle, signal, draft)
        if args.dry_run:
            _log(f"  [dry-run] {sid} -> {text}")
        else:
            _queue_entry(entry, cfg)
            seen[sid] = _ts()
            _increment_daily(seen, handle, "original_drafts")
            _save(SEEN_PATH, seen)
            _log(f"  queued {entry['id']}: {text[:100]}")
        drafted += 1

    _log(f"done - drafted={drafted}")


if __name__ == "__main__":
    main()
