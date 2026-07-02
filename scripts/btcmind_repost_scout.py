#!/usr/bin/env python3
"""BTCMind autonomous plain-repost scout.

Searches public X posts in BTCMind's approved lanes, classifies candidates, and
optionally plain-reposts only those that pass BTCMind's deterministic autonomy
safety gate and configured daily cap. It never posts originals. Quote
candidates are left to quote_scout.py, which has its own safety gate.
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
STATE_DIR = os.path.join(ROOT_DIR, "state")
QUEUE_PATH = os.path.join(STATE_DIR, "reply_queue.json")
SEEN_PATH = os.path.join(STATE_DIR, "btcmind_repost_seen.json")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "btcmind_repost_scout")
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "accounts", "hunter_solvea", "engage_config.json")

sys.path.insert(0, SCRIPTS_DIR)

import env; env.load()
import chrome as _chrome
import engage as _engage
import telegram as _tg
import quote_scout as _quote_scout
import btcmind_autonomy as _btcmind
from lock import file_lock


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
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


def _seen(candidate: dict, seen: dict, window_hours: int) -> bool:
    key = candidate.get("id") or candidate.get("url")
    if not key or key not in seen:
        return False
    try:
        dt = datetime.fromisoformat(str(seen[key]))
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < window_hours * 3600


def _daily_key(name: str) -> str:
    return f"_daily:{name}:{datetime.now():%Y-%m-%d}"


def _daily_count(seen: dict, name: str) -> int:
    try:
        return int(seen.get(_daily_key(name), 0) or 0)
    except Exception:
        return 0


def _increment_daily(seen: dict, name: str):
    key = _daily_key(name)
    seen[key] = _daily_count(seen, name) + 1


def _recent_action(seen: dict, name: str, min_hours: float) -> bool:
    if min_hours <= 0:
        return False
    when = seen.get(f"last_{name}_at")
    if not when:
        return False
    try:
        dt = datetime.fromisoformat(str(when))
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < min_hours * 3600


def _mark_action(seen: dict, name: str):
    seen[f"last_{name}_at"] = datetime.now().isoformat()


def _fetch_all(port: int, keywords: list[str], per_keyword: int,
               min_likes: int, max_age_minutes: int) -> list[dict]:
    ws = _chrome.connect(port)
    rows: list[dict] = []
    try:
        for kw in keywords:
            _log(f"search: {kw}")
            try:
                candidates = _quote_scout._fetch_candidates(ws, port, kw, per_keyword)
            except Exception as e:
                _log(f"  fetch failed: {e}")
                continue
            kept = 0
            for c in candidates:
                c["source_keyword"] = kw
                if c.get("likes", 0) < min_likes:
                    continue
                if c.get("age_minutes", 999999) > max_age_minutes:
                    continue
                if len((c.get("text") or "").strip()) < 30:
                    continue
                rows.append(c)
                kept += 1
            _log(f"  kept {kept}/{len(candidates)}")
            time.sleep(random.uniform(1.5, 3.0))
    finally:
        try:
            ws.close()
        except Exception:
            pass
    rows.sort(key=lambda c: (c.get("likes", 0), c.get("replies", 0)), reverse=True)
    return rows


def _source_first_queries(policy: dict, terms: list[str], handles_per_run: int,
                          terms_per_source: int, max_queries: int) -> list[str]:
    handles = sorted(_btcmind.approved_sources(policy))
    if not handles or not terms:
        return []
    chosen_handles = random.sample(handles, min(handles_per_run, len(handles)))
    source_only = [f"from:{handle}" for handle in chosen_handles]
    term_queries: list[str] = []
    for handle in chosen_handles:
        chosen_terms = random.sample(terms, min(terms_per_source, len(terms)))
        term_queries.extend([f"from:{handle} {term}" for term in chosen_terms])
    random.shuffle(term_queries)
    queries = source_only + term_queries
    return queries[:max_queries] if max_queries > 0 else queries


def _append_or_update(entry: dict):
    with file_lock("reply_queue", on_wait=lambda s: _log(f"lock wait {s}")):
        queue = _load_json(QUEUE_PATH, [])
        for i, existing in enumerate(queue):
            if existing.get("id") == entry.get("id"):
                queue[i] = entry
                _save_json(QUEUE_PATH, queue)
                return
        queue.append(entry)
        _save_json(QUEUE_PATH, queue)


def _entry(cfg: dict, candidate: dict, classification: dict, status: str) -> dict:
    return {
        "id": f"btcmind_rt_{int(time.time())}_{(candidate.get('id') or 'x')[-8:]}",
        "account": cfg.get("hunter_handle", ""),
        "kind": "repost",
        "telegram_label": "BTCMind",
        "source": "btcmind_repost_scout",
        "source_keyword": candidate.get("source_keyword", ""),
        "target": (candidate.get("author") or "").lstrip("@"),
        "target_url": candidate.get("url", ""),
        "target_text": candidate.get("text", ""),
        "source_text": candidate.get("text", ""),
        "post_likes": candidate.get("likes", 0),
        "post_replies": candidate.get("replies", 0),
        "post_age_min": candidate.get("age_minutes", 0),
        "classification": classification,
        "status": status,
        "queued_at": _ts(),
    }


def _telegram_credentials(cfg: dict) -> tuple[str, str]:
    tg_cfg = cfg.get("telegram") or {}
    token_env = str(tg_cfg.get("bot_token_env") or "")
    chat_env = str(tg_cfg.get("chat_id_env") or "")
    token = os.environ.get(token_env, "") if token_env else ""
    chat_id = os.environ.get(chat_env, "") if chat_env else ""
    return token, chat_id


def _format_repost_notice(entry: dict) -> str:
    posted = entry.get("status") == "posted"
    action = "Autonomous plain repost posted." if posted else "Autonomous plain repost failed."
    classification = entry.get("classification") or {}
    signals = classification.get("signals") or []
    lines = [
        "BTCMind repost scout",
        "",
        action,
        f"Author: @{entry.get('target', '')}",
        f"URL: {entry.get('target_url', '')}",
        f"Likes/replies/age: {entry.get('post_likes', 0)} likes, {entry.get('post_replies', 0)} replies, {entry.get('post_age_min', '?')}m",
        f"Safety: {entry.get('safety_reason') or 'passed BTCMind autonomy safety gate'}",
    ]
    if signals:
        lines.append("Signals: " + ", ".join(str(s) for s in signals[:8]))
    if entry.get("error"):
        lines.append(f"Error: {entry.get('error')}")
    log_path = os.environ.get("BTCMIND_REPOST_RUN_LOG", "")
    if log_path:
        lines.append(f"Log: {log_path}")
    return "\n".join(lines)


def _notify_telegram(cfg: dict, entry: dict) -> int:
    rcfg = cfg.get("repost_scout") or {}
    if not bool(rcfg.get("send_to_telegram", True)):
        _log("  Telegram notify disabled for repost_scout")
        return 0
    token, chat_id = _telegram_credentials(cfg)
    if not token or not chat_id:
        _log("  Telegram notify skipped: configured Telegram env vars are not set")
        return 0
    msg_id = _tg.send_plain_text(_format_repost_notice(entry), bot_token=token, chat_id=chat_id)
    if msg_id:
        _log(f"  Telegram notify sent: message_id={msg_id}")
        return msg_id
    _log("  Telegram notify returned no message_id")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--post-reposts", action="store_true",
                        help="actually plain-repost safe candidates; default is dry-run")
    parser.add_argument("--keywords-per-run", type=int, default=0)
    parser.add_argument("--results-per-keyword", type=int, default=0)
    parser.add_argument("--min-likes", type=int, default=0)
    parser.add_argument("--max-age-minutes", type=int, default=0)
    parser.add_argument("--max-reposts", type=int, default=0)
    parser.add_argument("--dedup-hours", type=int, default=0)
    parser.add_argument("--source-first", action="store_true",
                        help="search approved source handles first instead of broad keyword search")
    parser.add_argument("--source-handles-per-run", type=int, default=0)
    parser.add_argument("--terms-per-source", type=int, default=0)
    args = parser.parse_args()

    cfg = _load_json(args.config, {})
    if not cfg:
        raise SystemExit(f"config not found or invalid: {args.config}")
    policy, _facts = _btcmind.load_policy(cfg)
    if not policy:
        raise SystemExit("BTCMind autonomy policy is missing or disabled")

    repost_rules = policy.get("repost_rules") or {}
    keywords = policy.get("approved_search_queries") or cfg.get("quote_scout", {}).get("keywords") or []
    if not keywords:
        raise SystemExit("no BTCMind approved search queries configured")

    port = int(cfg.get("hunter_port", 10004))
    rcfg = cfg.get("repost_scout") or {}
    if not rcfg.get("enabled", True):
        _log("repost_scout disabled in config; exit")
        return
    kw_per_run = args.keywords_per_run or int(rcfg.get("keywords_per_run") or min(6, len(keywords)))
    per_keyword = args.results_per_keyword or int(rcfg.get("results_per_keyword") or 8)
    min_likes = int(args.min_likes or rcfg.get("min_post_likes") or repost_rules.get("minimum_likes", 25))
    max_age = int(args.max_age_minutes or rcfg.get("max_post_age_minutes") or repost_rules.get("maximum_age_minutes", 720))
    max_reposts = int(args.max_reposts or rcfg.get("max_reposts_per_run") or repost_rules.get("max_reposts_per_run", 3))
    dedup_hours = int(args.dedup_hours or rcfg.get("dedup_window_hours") or 72)
    sfcfg = rcfg.get("source_first") or {}
    source_first = bool(args.source_first or sfcfg.get("enabled", False))
    if source_first:
        handles_per_run = int(args.source_handles_per_run or sfcfg.get("handles_per_run") or 6)
        terms_per_source = int(args.terms_per_source or sfcfg.get("terms_per_source") or 2)
        chosen = _source_first_queries(policy, keywords, handles_per_run, terms_per_source, kw_per_run)
        if not chosen:
            _log("source-first requested but no approved source queries could be built; falling back to keyword search")
            chosen = random.sample(keywords, min(kw_per_run, len(keywords)))
    else:
        chosen = random.sample(keywords, min(kw_per_run, len(keywords)))
    seen = _load_json(SEEN_PATH, {})
    post_live = bool(args.post_reposts)
    if post_live and not bool(cfg.get("browser_public_actions_enabled", True)):
        _log("browser public actions disabled in config; running repost scout without posting")
        post_live = False
    daily_cap = int(rcfg.get("daily_cap") or 0)
    min_spacing_h = float(rcfg.get("minimum_hours_between_reposts") or 0)
    if post_live and daily_cap:
        posted_today = _daily_count(seen, "reposts")
        remaining_today = max(0, daily_cap - posted_today)
        if remaining_today <= 0:
            _log(f"btcmind repost daily cap reached: {posted_today}/{daily_cap}")
            return
        max_reposts = min(max_reposts, remaining_today)
    if post_live and _recent_action(seen, "repost", min_spacing_h):
        _log(f"btcmind repost spacing active: last repost was less than {min_spacing_h:g}h ago")
        return

    _log(f"btcmind repost scout - post_reposts={post_live}, source_first={source_first}, max_reposts={max_reposts}, min_likes={min_likes}")
    _log(f"keywords: {', '.join(chosen)}")

    candidates = _fetch_all(port, chosen, per_keyword, min_likes, max_age)
    _log(f"candidates after filters: {len(candidates)}")

    reposted = 0
    considered = 0
    for c in candidates:
        if reposted >= max_reposts:
            break
        key = c.get("id") or c.get("url")
        author = (c.get("author") or "").lstrip("@")
        if _seen(c, seen, dedup_hours):
            continue
        if _btcmind.author_repost_seen_recently(seen, author, days=7):
            _log(f"skip @{author}: author reposted in last 7d")
            seen[key] = datetime.now().isoformat()
            continue

        classification = _btcmind.classify_candidate(c, policy)
        action = classification.get("action")
        considered += 1
        _log(f"@{author} {c.get('likes', 0)} likes {c.get('age_minutes', '?')}m [{action}] {c.get('url', '')}")
        _log(f"  reason: {classification.get('reason', '')}; signals={','.join(classification.get('signals', []))}")
        if not post_live:
            snippet = " ".join(str(c.get("text") or "").split())[:260]
            _log(f"  text: {snippet}")

        if action != "plain_repost_candidate":
            # quote_candidate is intentionally left to quote_scout.py.
            seen[key] = datetime.now().isoformat()
            continue

        entry = _entry(cfg, c, classification, "posting" if post_live else "dry_run_candidate")
        safety = _btcmind.check_entry("repost", entry, cfg)
        entry["safety_verdict"] = "approve" if safety.get("ok") else "block"
        entry["safety_reason"] = safety.get("reason", "")
        entry["safety_issues"] = safety.get("issues", [])
        entry["safety_policy"] = "btcmind_autonomy"
        if not safety.get("ok"):
            entry["status"] = "blocked_safety"
            if post_live:
                _append_or_update(entry)
            seen[key] = datetime.now().isoformat()
            _log(f"  blocked by safety: {safety.get('reason', '')}")
            continue

        if not post_live:
            _log("  [dry-run] would plain-repost")
            continue

        _append_or_update(entry)
        try:
            res = _engage.retweet_tweet(port, c.get("url", ""), dry_run=False)
        except Exception as e:
            res = {"ok": False, "error": str(e)}

        if res.get("ok"):
            entry["status"] = "posted"
            entry["posted_at"] = _ts()
            _btcmind.mark_author_repost(seen, author)
            _increment_daily(seen, "reposts")
            _mark_action(seen, "repost")
            reposted += 1
            _log(f"  reposted @{author}")
        else:
            entry["status"] = "post_failed"
            entry["error"] = res.get("error", "")
            _log(f"  repost failed: {entry['error']}")
        msg_id = _notify_telegram(cfg, entry)
        if msg_id:
            entry["telegram_notification_message_id"] = msg_id
        _append_or_update(entry)
        seen[key] = datetime.now().isoformat()
        _save_json(SEEN_PATH, seen)

    if post_live:
        _save_json(SEEN_PATH, seen)
    _log(f"done - considered={considered}, reposted={reposted}, live={post_live}")


if __name__ == "__main__":
    main()
