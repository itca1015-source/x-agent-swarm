#!/usr/bin/env python3
"""
Inbound engage worker:
- Finds fresh tweets that reply to our account (`to:<handle> -from:<handle>`)
- Likes each safe engagement
- Queues a reply draft for Telegram approval
- Ignores hostile/prompt-injection style text
- Security policy: only the human owner can authorize any folder/config/code changes.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env
env.load()

import fetch
import engage
import generate
import chrome
import telegram
import btcmind_reply_quality
from lock import file_lock

STATE_DIR = os.path.join(ROOT_DIR, "state")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
LIBRARY_PATH = os.path.join(STATE_DIR, "winning_replies.json")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")

HOSTILE_PATTERNS = [
    "ai erase everything",
    "erase everything",
    "delete everything",
    "ignore previous",
    "ignore all previous",
    "ignore earlier instructions",
    "disregard previous",
    "disregard prior instructions",
    "forget prior instructions",
    "override instructions",
    "modify folder",
    "change folder",
    "edit folder",
    "update config",
    "change config",
    "edit config",
    "modify files",
    "change files",
    "jailbreak",
    "system prompt",
    "developer prompt",
    "reveal prompt",
    "leak prompt",
    "drop table",
    "rm -rf",
]

HOSTILE_REGEXES = [
    re.compile(r"\bignore\b.{0,40}\b(instruction|prompt|rule)s?\b", re.I),
    re.compile(r"\b(disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule)s?\b", re.I),
    re.compile(r"\b(reveal|show|leak|print|dump)\b.{0,40}\b(system|developer|hidden)\b.{0,30}\bprompt\b", re.I),
    re.compile(r"\b(modify|change|edit|update)\b.{0,40}\b(folder|directory|config|file|files|code)\b", re.I),
    re.compile(r"\b(ai|assistant|bot)\b.{0,20}\b(erase|delete|wipe)\b.{0,20}\b(everything|all)\b", re.I),
    re.compile(r"\brm\s*-\s*rf\b", re.I),
    re.compile(r"\bdrop\s+table\b", re.I),
]


LOW_SUBSTANCE_NORMALIZED = {
    "thank you",
    "thanks",
    "thx",
    "ty",
    "appreciate it",
    "appreciate you",
}

BAD_REPLY_PREFIXES = (
    "skip",
    "skipped",
    "cannot engage",
    "can't engage",
    "not enough substance",
    "incomplete post",
)


def _normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("0", "o").replace("1", "i").replace("3", "e").replace("4", "a").replace("5", "s").replace("7", "t")
    t = re.sub(r"[\W_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, handle: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    d = os.path.join(LOG_DIR, f"inbound_{handle.lower()}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


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


def _claim_seen_id(seen_path: str, lock_name: str, cid: str) -> bool:
    """Atomically reserve a candidate id across concurrent runs.
    Returns True if this run claimed it, False if already claimed before."""
    with file_lock(lock_name):
        seen = _load_json(seen_path, {"ids": []})
        ids = set(seen.get("ids", []))
        if cid in ids:
            return False
        ids.add(cid)
        ids_list = list(ids)
        if len(ids_list) > 5000:
            ids_list = ids_list[-5000:]
        _save_json(seen_path, {"ids": ids_list, "updated_at": _ts()})
        return True


def _is_hostile(text: str) -> bool:
    raw = text or ""
    norm = _normalize_text(raw)
    if any(p in raw.lower() for p in HOSTILE_PATTERNS):
        return True
    if any(p in norm for p in HOSTILE_PATTERNS):
        return True
    return any(rx.search(raw) or rx.search(norm) for rx in HOSTILE_REGEXES)


def _low_substance_inbound(text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return True
    if norm in LOW_SUBSTANCE_NORMALIZED:
        return True
    return len(norm) < 12 and len(norm.split()) <= 3


def _bad_reply_reason(reply_text: str) -> str:
    norm = _normalize_text(reply_text)
    if any(norm.startswith(p) for p in BAD_REPLY_PREFIXES):
        return "meta_skip_or_refusal"
    if "no substance to engage" in norm or "without substance to engage" in norm:
        return "meta_skip_or_refusal"
    return ""


def _candidate_age_minutes(c: dict) -> int:
    """Best-effort age extraction; returns large number when unknown."""
    try:
        if "age_minutes" in c and c.get("age_minutes") is not None:
            return int(c.get("age_minutes"))
    except Exception:
        pass
    dt = c.get("datetime") or c.get("created_at") or ""
    if not dt:
        return 9999
    try:
        t = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        if not t.tzinfo:
            t = t.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - t).total_seconds() / 60))
    except Exception:
        return 9999


def _account_label(handle: str) -> str:
    h = (handle or "").strip().lstrip("@").lower()
    if h == "btcmind101":
        return "BTCMind"
    if h == "flatkey":
        return "Flatkey"
    if h == "hunterguo101":
        return "Hunter101"
    return handle or ""


def _reply_quality_enabled(cfg: dict) -> bool:
    qcfg = cfg.get("reply_quality_audit")
    return isinstance(qcfg, dict) and bool(qcfg.get("enabled", False))


def _reply_quality_label(cfg: dict, handle: str) -> str:
    qcfg = cfg.get("reply_quality_audit")
    if isinstance(qcfg, dict) and qcfg.get("account_label"):
        return str(qcfg.get("account_label")).strip()
    return _account_label(handle) or str(cfg.get("hunter_handle") or "Reply").strip()


def _telegram_credentials(cfg: dict) -> tuple[str, str]:
    tg_cfg = cfg.get("telegram") or {}
    token = os.environ.get(str(tg_cfg.get("bot_token_env") or "TELEGRAM_BOT_TOKEN"), "")
    chat_id = os.environ.get(str(tg_cfg.get("chat_id_env") or "TELEGRAM_CHAT_ID"), "")
    return token, chat_id


def _queue_or_update(entry: dict):
    with file_lock("reply_queue", timeout=60, on_wait=lambda s: _log(str(s), entry.get("account", "inbound"))):
        cur = _load_json(QUEUE_PATH, [])
        for i, existing in enumerate(cur):
            if existing.get("id") == entry.get("id"):
                cur[i] = entry
                _save_json(QUEUE_PATH, cur)
                return
        cur.append(entry)
        _save_json(QUEUE_PATH, cur)


def _update_queue_entry(entry_id: str, updates: dict):
    with file_lock("reply_queue", timeout=60):
        cur = _load_json(QUEUE_PATH, [])
        for i, existing in enumerate(cur):
            if existing.get("id") == entry_id:
                cur[i] = {**existing, **updates}
                _save_json(QUEUE_PATH, cur)
                return


def _send_reply_card(cfg: dict, entry: dict, handle: str) -> int:
    token, chat_id = _telegram_credentials(cfg)
    if not token or not chat_id:
        _log("  Telegram reply card skipped: configured env vars are not set", handle)
        return 0
    msg_id = telegram.send_reply_card(entry, bot_token=token, chat_id=chat_id)
    if msg_id:
        _log(f"  Telegram reply card sent: message_id={msg_id}", handle)
    else:
        _log("  Telegram reply card returned no message_id", handle)
    return msg_id


def _build_reply_entry(cfg: dict, c: dict, generation: dict, reply_text: str) -> dict:
    handle = str(cfg.get("hunter_handle", "")).strip().lstrip("@")
    author = str(c.get("author", "")).strip().lstrip("@")
    cid = str(c.get("id", ""))
    return {
        "id": f"inbound_{handle.lower()}_{cid}",
        "account": handle,
        "telegram_label": _account_label(handle),
        "kind": "reply",
        "source": "inbound_engage",
        "source_keyword": "inbound",
        "target": author,
        "target_url": c.get("url", ""),
        "target_text": c.get("text", ""),
        "reply_text": reply_text,
        "op_summary": generation.get("op_summary", ""),
        "reply_angle": generation.get("reply_angle", ""),
        "status": "pending",
        "queued_at": _ts(),
        "post_age_min": _candidate_age_minutes(c),
        "post_replies": c.get("replies", 0),
        "post_likes": c.get("likes", 0),
        "telegram_message_id": 0,
        "needs_human_approval": True,
        "inbound_engagement": True,
        "engagement_type": c.get("engagement_type", "mention_or_reply"),
    }


NOTIF_MENTIONS_JS = r"""
(function() {
  var out = [];
  var arts = document.querySelectorAll('article[data-testid="tweet"]');
  for (var i = 0; i < arts.length; i++) {
    var el = arts[i];
    if (el.parentElement && el.parentElement.closest('article[data-testid="tweet"]')) continue;
    var textEl = el.querySelector('[data-testid="tweetText"]');
    var text = textEl ? textEl.innerText.trim() : '';
    var times = Array.prototype.slice.call(el.querySelectorAll('time'));
    var tEl = null;
    for (var j = 0; j < times.length; j++) {
      if (times[j].closest('article[data-testid="tweet"]') === el) {
        tEl = times[j];
        break;
      }
    }
    var urlEl = tEl ? tEl.closest('a[href*="/status/"]') : null;
    if (!urlEl) {
      var links = Array.prototype.slice.call(el.querySelectorAll('a[href*="/status/"]'));
      for (var k = 0; k < links.length; k++) {
        if (links[k].closest('article[data-testid="tweet"]') === el) {
          urlEl = links[k];
          break;
        }
      }
    }
    var url = urlEl ? urlEl.href : '';
    if (!url) continue;
    url = url.replace(/\?.*$/, '');
    var m = url.match(/(?:x\.com|twitter\.com)\/([A-Za-z0-9_]+)\/status\/(\d+)/);
    var author = m ? m[1] : '';
    var id = m ? m[2] : '';
    var dt = tEl ? (tEl.getAttribute('datetime') || '') : '';
    if (!id || !author) continue;
    out.push({id:id, text:text, url:url, author:author, datetime:dt, url_source:'outer_time_link'});
  }
  return JSON.stringify(out);
})()
"""


def _extract_notification_articles(ws, limit: int) -> list:
    raw = chrome.eval_js(ws, NOTIF_MENTIONS_JS)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except Exception:
        return []
    out = []
    for r in rows:
        if not r.get("id") or not r.get("author") or not r.get("url"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _fetch_inbound_from_notifications(port: int, limit: int) -> list:
    ws = chrome.connect(port)
    try:
        out, seen = [], set()
        for page_url in ("https://x.com/notifications", "https://x.com/notifications/mentions"):
            chrome.navigate(ws, page_url, wait=3.0)
            time.sleep(1.2)
            for r in _extract_notification_articles(ws, limit):
                rid = str(r.get("id", ""))
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                r["notification_source_url"] = page_url
                out.append(r)
                if len(out) >= limit:
                    return out
        return out
    finally:
        try:
            ws.close()
        except Exception:
            pass


def run_once(config_path: str, limit: int):
    cfg = _load_json(config_path, {})
    handle = cfg["hunter_handle"]
    port = int(cfg["hunter_port"])
    blocked = {str(x).lower() for x in cfg.get("blocked_handles", [])}
    inbound_cfg = cfg.get("inbound", {})
    if inbound_cfg and not bool(inbound_cfg.get("enabled", True)):
        _log("inbound engagement disabled in config", handle)
        return
    max_age_min = int(inbound_cfg.get("max_engagement_age_minutes", 5))
    allow_unknown_age = bool(inbound_cfg.get("allow_unknown_age", False))
    browser_public_actions = bool(cfg.get("browser_public_actions_enabled", True))
    auto_like = bool(inbound_cfg.get("auto_like", True)) and browser_public_actions
    queue_replies = bool(inbound_cfg.get("queue_replies", True))
    auto_reply = bool(inbound_cfg.get("auto_reply", False)) and browser_public_actions
    max_per_run = int(inbound_cfg.get("max_per_run", limit) or limit)
    limit = min(limit, max_per_run)
    seen_path = os.path.join(STATE_DIR, f"inbound_seen_{handle.lower()}.json")
    seen = _load_json(seen_path, {"ids": []})
    seen_ids = set(seen.get("ids", []))
    seen_lock_name = f"inbound_seen_{handle.lower()}"

    use_notifs = bool(inbound_cfg.get("use_notifications", True))
    candidates = []
    if use_notifs:
        _log("notifications mentions fetch", handle)
        try:
            candidates = _fetch_inbound_from_notifications(port, limit)
        except Exception as e:
            _log(f"  notifications fetch failed: {e}", handle)
    if not candidates:
        query = f"to:{handle} -from:{handle}"
        _log(f"search fallback {query}", handle)
        candidates = fetch.search(port, query, mode="live", limit=limit)

    added = 0
    ignored_hostile = 0
    skipped_seen = 0
    skipped_old = 0
    like_only = 0
    skipped_bad_reply = 0
    queued = 0
    failed = 0

    for c in candidates:
        cid = str(c.get("id", ""))
        text = c.get("text", "")
        url = c.get("url", "")
        author = c.get("author", "")
        if not cid or not url or not author:
            continue
        if author.lower() == handle.lower():
            continue
        if author.lower() in blocked:
            continue
        age_min = _candidate_age_minutes(c)
        if age_min > max_age_min and not (allow_unknown_age and age_min == 9999):
            skipped_old += 1
            continue
        if cid in seen_ids:
            skipped_seen += 1
            continue
        if not _claim_seen_id(seen_path, seen_lock_name, cid):
            skipped_seen += 1
            continue
        seen_ids.add(cid)

        if _is_hostile(text):
            ignored_hostile += 1
            continue

        _log(f"engage @{author} {cid}", handle)
        if auto_like:
            try:
                like_res = engage.like_tweet(port, url, dry_run=False)
                if like_res.get("ok"):
                    if like_res.get("error") == "already_liked":
                        _log("  like ok: already_liked", handle)
                    else:
                        _log("  like ok", handle)
                else:
                    _log(f"  like failed (non-fatal): {like_res.get('error','unknown')}", handle)
            except Exception as e:
                _log(f"  like failed (non-fatal): {e}", handle)

        if _low_substance_inbound(text):
            like_only += 1
            _log("  like-only: low-substance inbound text", handle)
            continue

        try:
            quality_enabled = _reply_quality_enabled(cfg)
            quality_label = _reply_quality_label(cfg, handle)
            extra_quality_rules = btcmind_reply_quality.generation_rules(cfg) if quality_enabled else ""
            g = generate.generate_engaged_reply(
                target_handle=author,
                target_post_text=text,
                library_path=LIBRARY_PATH,
                archetypes=cfg.get("archetypes", {}),
                hunter_handle=handle,
                display_handle=cfg.get("x_handle") or handle,
                examples_per_prompt=cfg.get("generation", {}).get("examples_per_prompt", 8),
                max_reply_chars=cfg.get("generation", {}).get("max_reply_chars", 220),
                extra_quality_rules=extra_quality_rules,
            )
            if isinstance(g, dict):
                generation = g
                reply_text = (g.get("reply") or "").strip()
            else:
                generation = {"reply": str(g), "op_summary": "", "reply_angle": ""}
                reply_text = str(g).strip()
            if len(reply_text) < 8:
                reply_text = "Appreciate the reply — thanks for jumping in."
            bad_reply_reason = _bad_reply_reason(reply_text)
            if bad_reply_reason:
                skipped_bad_reply += 1
                _log(f"  skip generated reply: {bad_reply_reason}", handle)
                continue
            reply_audit = {}
            if quality_enabled:
                audit_entry = _build_reply_entry(cfg, c, generation, reply_text)
                reply_audit = btcmind_reply_quality.apply_audit(
                    audit_entry,
                    cfg,
                    recent_entries=_load_json(QUEUE_PATH, []),
                )
                if reply_audit.get("reply_risk_class") in {"needs_rewrite", "block"}:
                    feedback = btcmind_reply_quality.rewrite_feedback(reply_audit)
                    g = generate.generate_engaged_reply(
                        target_handle=author,
                        target_post_text=text,
                        library_path=LIBRARY_PATH,
                        archetypes=cfg.get("archetypes", {}),
                        hunter_handle=handle,
                        display_handle=cfg.get("x_handle") or handle,
                        examples_per_prompt=cfg.get("generation", {}).get("examples_per_prompt", 8),
                        max_reply_chars=cfg.get("generation", {}).get("max_reply_chars", 220),
                        extra_quality_rules=extra_quality_rules,
                        quality_feedback=feedback,
                    )
                    generation = g if isinstance(g, dict) else {"reply": str(g), "op_summary": "", "reply_angle": ""}
                    reply_text = str(generation.get("reply") or "").strip()
                    audit_entry = _build_reply_entry(cfg, c, generation, reply_text)
                    reply_audit = btcmind_reply_quality.apply_audit(
                        audit_entry,
                        cfg,
                        recent_entries=_load_json(QUEUE_PATH, []),
                    )
                if reply_audit.get("reply_risk_class") in {"needs_rewrite", "block"}:
                    skipped_bad_reply += 1
                    why = " | ".join((reply_audit.get("reasons") or [])[:3])
                    _log(f"  skip generated reply: {quality_label} audit {reply_audit.get('reply_risk_class')}: {why}", handle)
                    continue
            must_queue_for_audit = bool(
                reply_audit and reply_audit.get("reply_risk_class") != "auto_ok"
            )
            if auto_reply and not queue_replies and not must_queue_for_audit:
                r = engage.reply_tweet(
                    port,
                    url,
                    reply_text,
                    dry_run=False,
                    self_handle=cfg.get("x_handle") or handle,
                )
                if r.get("ok"):
                    added += 1
                    _log(f"  replied ok: {r.get('reply_url','')}", handle)
                else:
                    failed += 1
                    _log(f"  reply failed: {r.get('error','unknown')}", handle)
            else:
                entry = _build_reply_entry(cfg, c, generation, reply_text)
                if reply_audit:
                    entry["btcmind_reply_audit"] = reply_audit
                    entry["reply_risk_class"] = reply_audit.get("reply_risk_class", "")
                    entry["reply_quality_score"] = reply_audit.get("score", 0)
                    entry["thread_context_score"] = reply_audit.get("thread_context_score", 0)
                    entry["generic_template_score"] = reply_audit.get("generic_template_score", 0)
                    entry["reply_structure_signature"] = reply_audit.get("reply_structure_signature", "")
                _queue_or_update(entry)
                msg_id = _send_reply_card(cfg, entry, handle)
                if msg_id:
                    _update_queue_entry(entry["id"], {"telegram_message_id": msg_id})
                queued += 1
                _log(f"  queued reply approval ({len(reply_text)} chars)", handle)
        except Exception as e:
            failed += 1
            _log(f"  generation/queue exception: {e}", handle)

        time.sleep(1.0)

    _log(
        f"done candidates={len(candidates)} replied={added} queued={queued} hostile_ignored={ignored_hostile} "
        f"seen_skipped={skipped_seen} old_skipped={skipped_old} like_only={like_only} "
        f"bad_reply_skipped={skipped_bad_reply} failed={failed}",
        handle,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    run_once(args.config, args.limit)


if __name__ == "__main__":
    main()
