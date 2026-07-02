"""
Review CLI for the engage reply queue.

Walks pending replies one at a time:
  [y] approve + post immediately
  [n] reject (mark and skip)
  [e] edit the reply text (opens $EDITOR or inline prompt)
  [r] regenerate from scratch
  [s] skip for now (stays pending for next review session)
  [o] open the OP tweet URL in default browser
  [q] quit

Usage:
    python3 review_queue.py
    python3 review_queue.py --config accounts/flatkey/engage_config.json --account flatkey
    python3 review_queue.py --auto-post   # skip prompts, post everything pending (DANGEROUS)
    python3 review_queue.py --status pending|approved|rejected|posted   # filter
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR    = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env;   env.load()
import engage  as _engage
import generate as _generate
import post as _post
import btcmind_autonomy as _btcmind

CONFIG_PATH = os.path.join(ROOT_DIR, "engage_config.json")
STATE_DIR   = os.path.join(ROOT_DIR, "state")
QUEUE_PATH  = os.path.join(STATE_DIR, "reply_queue.json")
LIBRARY     = os.path.join(STATE_DIR, "winning_replies.json")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _edit_text(text: str) -> str:
    """Open $EDITOR for multiline edit. Fall back to single-line input."""
    editor = os.environ.get("EDITOR", "")
    if editor:
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as f:
            f.write(text); tmp = f.name
        try:
            subprocess.call([editor, tmp])
            with open(tmp) as f:
                return f.read().rstrip("\n")
        finally:
            os.unlink(tmp)
    new = input("New text (single line, Enter to keep): ").strip()
    return new or text


def _color(s: str, c: str) -> str:
    codes = {"r": 31, "g": 32, "y": 33, "b": 34, "m": 35, "c": 36, "w": 37}
    return f"\033[{codes.get(c, 37)}m{s}\033[0m"


def _auto_post_block(q: dict, cfg: dict) -> str:
    account = str(q.get("account") or cfg.get("hunter_handle") or "").lower()
    source = str(q.get("source") or "").lower()
    is_btcmind = account in {"btcmind101", "hunter_solvea"} or source.startswith("btcmind")
    if is_btcmind and not bool(cfg.get("x_public_posting_enabled", False)):
        return "BTCMind X public posting is disabled"
    decision = _btcmind.check_entry(q.get("kind", "reply"), q, cfg)
    if decision.get("policy_applied"):
        q["safety_verdict"] = "approve" if decision.get("ok") else "block"
        q["safety_reason"] = decision.get("reason", "")
        q["safety_issues"] = decision.get("issues", [])
        q["safety_policy"] = "btcmind_autonomy"
    if not decision.get("ok"):
        return str(decision.get("reason") or "BTCMind autonomy safety block")
    try:
        import btcmind_reply_quality as _bq
        if q.get("kind", "reply") == "reply" and is_btcmind:
            audit = _bq.apply_audit(q, cfg)
            if audit.get("reply_risk_class") in {"needs_rewrite", "block"}:
                why = " | ".join((audit.get("reasons") or [])[:3])
                return f"BTCMind reply quality {audit.get('reply_risk_class')}: {why}"
    except Exception as e:
        return f"BTCMind reply quality audit failed: {e}"
    return ""


def review(queue: list, cfg: dict, auto_post: bool, status_filter: str, account_filter: str = "") -> list:
    pending = [
        (i, q) for i, q in enumerate(queue)
        if q.get("status") == status_filter
        and (not account_filter or q.get("account") == account_filter)
    ]
    if not pending:
        suffix = f" for @{account_filter}" if account_filter else ""
        print(f"no {status_filter} entries{suffix} in queue (total entries: {len(queue)})")
        return queue

    suffix = f" for @{account_filter}" if account_filter else ""
    print(_color(f"{len(pending)} {status_filter} replies{suffix}", "c"))
    quit_now = False
    for k, (idx, q) in enumerate(pending, 1):
        if quit_now: break
        print()
        print(_color("─" * 70, "w"))
        target = q.get("target") or q.get("news_source") or "original"
        target_text = q.get("target_text") or q.get("source_context") or ""
        target_url = q.get("target_url") or q.get("news_url") or ""
        print(f"[{k}/{len(pending)}] @{target}  kind={q.get('kind','reply')}  "
              f"age={q.get('post_age_min','?')}m  replies={q.get('post_replies','?')}  "
              f"queued {q.get('queued_at','?')}")
        print(_color(f"OP: {target_text[:400]}", "y"))
        print(_color(f"URL: {target_url}", "b"))
        print(_color(f"REPLY ({len(q['reply_text'])} chars):", "g"))
        print(q["reply_text"])

        if auto_post:
            action = "y"
        else:
            action = input("\n[y/n/e/r/s/o/q] > ").strip().lower() or "s"

        if action == "y":
            kind = q.get("kind", "reply")
            safety_block = _auto_post_block(q, cfg)
            if safety_block:
                q["status"] = "blocked_safety"
                q["safety_reviewed_at"] = _ts()
                print(_color(f"  blocked by BTCMind safety: {safety_block}", "r"))
                queue[idx] = q
                _save(QUEUE_PATH, queue)
                continue
            if kind == "original":
                res = _post.post_tweet(cfg["hunter_port"], q["reply_text"],
                                       handle=cfg.get("hunter_handle", ""),
                                       dry_run=False)
            elif kind == "quote":
                res = _engage.quote_tweet(cfg["hunter_port"], q["target_url"],
                                          q["reply_text"], dry_run=False,
                                          verify_handle=cfg.get("x_handle") or cfg.get("hunter_handle"))
            else:
                res = _engage.reply_tweet(cfg["hunter_port"], q["target_url"],
                                          q["reply_text"], dry_run=False)
            if res.get("ok"):
                q["status"] = "posted"
                q["posted_at"] = _ts()
                if res.get("url"):
                    q["reply_url_actual"] = res["url"]
                if res.get("reply_url"):
                    q["reply_url_actual"] = res["reply_url"]
                print(_color("  posted ✓", "g"))
            else:
                q["status"] = "post_failed"
                q["error"] = res.get("error", "")
                print(_color(f"  post FAILED: {q['error']}", "r"))
        elif action == "n":
            q["status"] = "rejected"
            q["rejected_at"] = _ts()
            print(_color("  rejected", "r"))
        elif action == "e":
            q["reply_text"] = _edit_text(q["reply_text"])
            print(_color(f"  edited ({len(q['reply_text'])} chars). Press y to post, n to reject.", "y"))
            action2 = input("  [y/n/s] > ").strip().lower() or "s"
            if action2 == "y":
                kind = q.get("kind", "reply")
                safety_block = _auto_post_block(q, cfg)
                if safety_block:
                    q["status"] = "blocked_safety"
                    q["safety_reviewed_at"] = _ts()
                    print(_color(f"  blocked by BTCMind safety: {safety_block}", "r"))
                    queue[idx] = q
                    _save(QUEUE_PATH, queue)
                    continue
                if kind == "original":
                    res = _post.post_tweet(cfg["hunter_port"], q["reply_text"],
                                           handle=cfg.get("hunter_handle", ""),
                                           dry_run=False)
                elif kind == "quote":
                    res = _engage.quote_tweet(cfg["hunter_port"], q["target_url"],
                                              q["reply_text"], dry_run=False,
                                              verify_handle=cfg.get("x_handle") or cfg.get("hunter_handle"))
                else:
                    res = _engage.reply_tweet(cfg["hunter_port"], q["target_url"],
                                              q["reply_text"], dry_run=False)
                q["status"] = "posted" if res.get("ok") else "post_failed"
                if res.get("ok"):
                    q["posted_at"] = _ts(); print(_color("  posted ✓", "g"))
                else:
                    q["error"] = res.get("error", ""); print(_color(f"  FAILED: {q['error']}", "r"))
            elif action2 == "n":
                q["status"] = "rejected"; q["rejected_at"] = _ts(); print(_color("  rejected", "r"))
        elif action == "r":
            if q.get("kind", "reply") != "reply":
                print(_color("  regenerate is only wired for reply entries; edit instead.", "y"))
                queue[idx] = q
                _save(QUEUE_PATH, queue)
                continue
            print("  regenerating...")
            try:
                extra_quality_rules = ""
                quality_feedback = ""
                btcmind_quality = None
                try:
                    import btcmind_reply_quality as _bq
                    account = str(q.get("account") or cfg.get("hunter_handle") or "").lower()
                    source = str(q.get("source") or "").lower()
                    if account in {"btcmind101", "hunter_solvea"} or source.startswith("btcmind"):
                        btcmind_quality = _bq
                        extra_quality_rules = _bq.generation_rules(cfg)
                        if q.get("btcmind_reply_audit"):
                            quality_feedback = _bq.rewrite_feedback(q.get("btcmind_reply_audit") or {})
                except Exception:
                    pass
                g = _generate.generate_engaged_reply(
                    target_handle=q["target"],
                    target_post_text=q["target_text"],
                    library_path=LIBRARY,
                    archetypes=cfg.get("archetypes", {}),
                    hunter_handle=cfg["hunter_handle"],
                    examples_per_prompt=cfg["generation"]["examples_per_prompt"],
                    max_reply_chars=cfg["generation"]["max_reply_chars"],
                    extra_quality_rules=extra_quality_rules,
                    quality_feedback=quality_feedback,
                )
                if isinstance(g, dict):
                    new = g.get("reply", "")
                    q["op_summary"] = g.get("op_summary", "")
                    q["reply_angle"] = g.get("reply_angle", "")
                else:
                    new = str(g)
                q["reply_text"] = new
                if btcmind_quality and new:
                    audit = btcmind_quality.apply_audit(q, cfg)
                    q["btcmind_reply_audit"] = audit
                    q["reply_risk_class"] = audit.get("reply_risk_class", "")
                    q["reply_quality_score"] = audit.get("score", 0)
                print(_color(f"  regenerated ({len(new)} chars):", "g")); print(new)
                print("  Press y to post, n to reject, s to keep pending.")
                action2 = input("  [y/n/s] > ").strip().lower() or "s"
                if action2 == "y":
                    res = _engage.reply_tweet(cfg["hunter_port"], q["target_url"], q["reply_text"], dry_run=False)
                    q["status"] = "posted" if res.get("ok") else "post_failed"
                    if res.get("ok"):
                        q["posted_at"] = _ts(); print(_color("  posted ✓", "g"))
                elif action2 == "n":
                    q["status"] = "rejected"; q["rejected_at"] = _ts()
            except Exception as e:
                print(_color(f"  regen failed: {e}", "r"))
        elif action == "o":
            if target_url:
                subprocess.call(["open", target_url])
            else:
                print(_color("  no URL on this entry", "y"))
            # re-prompt for action
            action2 = input("  [y/n/e/r/s] > ").strip().lower() or "s"
            if action2 in ("y", "n", "e", "r"):
                # crude re-dispatch by mutating action; easier to just loop k again
                queue[idx] = q
                _save(QUEUE_PATH, queue)
                continue
        elif action == "s":
            pass
        elif action == "q":
            quit_now = True

        queue[idx] = q
        _save(QUEUE_PATH, queue)

    return queue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_PATH,
                        help="engage config to use for posting/reposting")
    parser.add_argument("--account", default="",
                        help="only review queue entries for this account handle")
    parser.add_argument("--auto-post", action="store_true",
                        help="DANGEROUS: post every pending reply with no review")
    parser.add_argument("--status", default="pending",
                        choices=["pending", "human_review", "approved", "rejected", "posted", "post_failed"])
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    account_filter = args.account.strip()
    queue = _load(QUEUE_PATH, [])
    if not queue:
        print(f"queue is empty: {QUEUE_PATH}")
        return

    review(queue, cfg, args.auto_post, args.status, account_filter)
    print(_color("\ndone.", "c"))


if __name__ == "__main__":
    main()
