"""Post original tweets via X.com browser CDP."""
import json
import time
import sys
import os
import urllib.parse
import urllib.request
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import websocket

from chrome import (
    clear_composer,
    composer_has_duplicate,
    composer_submit_enabled,
    composer_text,
    connect,
    eval_js,
    get_tweets,
    navigate,
    normalize_composer_text,
    paste_text,
    text_repeats_itself,
    click_testid,
    upload_media,
    _send,
)
from lock import chrome_lock


def post_tweet(port: int, text: str, handle: str = "", dry_run: bool = False,
               media_paths: Optional[list] = None,
               expected_media_size: tuple[int, int] = (0, 0)) -> dict:
    """
    Post an original tweet. Holds chrome_lock so we don't race with
    engage_daemon / telegram_bridge on the same Chrome instance.
    Returns {"ok": bool, "error": str | None, "url": str}.
    """
    media_paths = media_paths or []
    if dry_run:
        print(f"  [post] dry-run: {text[:80]}")
        return {"ok": True, "dry_run": True, "media_paths": media_paths}
    if text_repeats_itself(text):
        return {"ok": False, "error": "draft_text_repeats_before_compose", "url": ""}

    with chrome_lock(port):
        ws = connect(port)
        try:
            navigate(ws, "https://x.com/home", wait=4.0)

            # Spoof focus so Draft.js processes Input.insertText even when
            # Chrome isn't the frontmost macOS app. Without this, paste below
            # silently no-ops when Chrome is backgrounded and the submit
            # appears to succeed but no post lands (the bridge then marks the
            # entry "posted" even though X received nothing).
            try:
                _send(ws, "Emulation.setFocusEmulationEnabled",
                      {"enabled": True}, msg_id=43)
            except Exception as e:
                print(f"[post_tweet] focus emulation set: {e} (continuing)",
                      flush=True)

            # Focus the compose box — click the "What is happening?" area
            focused = eval_js(ws, """
                (function() {
                    var box = document.querySelector('[data-testid="tweetTextarea_0"]');
                    if (!box) box = document.querySelector('[contenteditable="true"]');
                    if (!box) return 'not found';
                    box.click();
                    box.focus();
                    return 'ok';
                })()
            """)
            if focused == "not found":
                return {"ok": False, "error": "compose_box_not_found"}
            time.sleep(1.0)

            # paste_text uses CDP Input.insertText (primary) which fires the
            # beforeinput/input events Draft.js needs to enable the submit
            # button. Accept both 'ok' and 'ok-fallback' as success.
            result = paste_text(ws, text)
            if result not in ("ok", "ok-fallback"):
                return {"ok": False, "error": f"paste_failed: {result}"}
            time.sleep(1.5)
            current = composer_text(ws)
            if normalize_composer_text(current) != normalize_composer_text(text):
                if composer_has_duplicate(current, text):
                    clear_composer(ws)
                    return {
                        "ok": False,
                        "error": "composer_duplicate_before_submit",
                        "composer": current[:500],
                    }
                if normalize_composer_text(current):
                    return {
                        "ok": False,
                        "error": "composer_text_mismatch_before_submit",
                        "composer": current[:500],
                    }
                if not composer_submit_enabled(ws):
                    return {
                        "ok": False,
                        "error": "composer_empty_and_submit_disabled",
                        "composer": current[:500],
                    }
            if composer_has_duplicate(current, text):
                clear_composer(ws)
                return {
                    "ok": False,
                    "error": "composer_duplicate_before_submit",
                    "composer": current[:500],
                }

            if media_paths:
                uploaded = upload_media(ws, media_paths)
                if uploaded != "ok":
                    clear_composer(ws)
                    return {"ok": False, "error": f"upload_failed:{uploaded}", "url": ""}
                if not _media_ready(ws, *expected_media_size):
                    return {
                        "ok": False,
                        "error": "media_preview_not_ready",
                        "composer": composer_text(ws),
                        "url": "",
                    }

            # Click the tweet submit button
            clicked = click_testid(ws, "tweetButtonInline")
            if clicked == "not found":
                clicked = click_testid(ws, "tweetButton")
            if clicked == "not found":
                return {"ok": False, "error": "submit_button_not_found"}
            # Give X time to finish the post and clear its dirty compose flag
            # before navigating away for verification. Navigating too early can
            # trigger Chrome's "Leave site?" prompt and stall the flow.
            time.sleep(12.0)

            # Verify: compose box should be empty after successful post
            remaining = eval_js(ws, """
                (function() {
                    var box = document.querySelector('[data-testid="tweetTextarea_0"]');
                    if (!box) box = document.querySelector('[contenteditable="true"]');
                    return box ? box.innerText.trim() : '';
                })()
            """)
            if remaining and len(remaining) > 10:
                return {"ok": False, "error": "compose_not_cleared_after_post"}

            # Navigate to profile page to grab the URL of the just-posted tweet
            tweet_url = ""
            if handle:
                try:
                    navigate(ws, f"https://x.com/{handle}", wait=3.0)
                    tweets = get_tweets(ws)
                    if tweets:
                        tweet_url = tweets[0].get("url", "")
                except Exception:
                    pass

            return {"ok": True, "error": None, "url": tweet_url}
        finally:
            ws.close()


def _json_get(port: int, path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://localhost:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _new_tab(port: int, url: str) -> dict:
    return _json_get(port, "/json/new?" + urllib.parse.quote(url, safe=":/?=&"), method="PUT")


def _close_tab(port: int, tab_id: str) -> None:
    try:
        _json_get(port, f"/json/close/{tab_id}")
    except Exception:
        pass


def _connect_tab(tab: dict) -> websocket.WebSocket:
    return websocket.create_connection(
        tab["webSocketDebuggerUrl"],
        timeout=60,
        header=["Origin: devtools://devtools"],
    )


def _wait_for_composer(ws, attempts: int = 30) -> bool:
    for _ in range(attempts):
        found = eval_js(ws, """
            (function() {
                var box = document.querySelector('[data-testid="tweetTextarea_0"]')
                       || document.querySelector('[contenteditable="true"]');
                if (!box) return '';
                box.click();
                box.focus();
                return 'ok';
            })()
        """)
        if found == "ok":
            return True
        time.sleep(0.5)
    return False


def _composer_text(ws) -> str:
    return eval_js(ws, """
        (function() {
            var box = document.querySelector('[data-testid="tweetTextarea_0"]')
                   || document.querySelector('[contenteditable="true"]');
            return box ? (box.innerText || box.textContent || '') : '';
        })()
    """)


def _media_ready(ws, expected_width: int = 0, expected_height: int = 0) -> bool:
    for _ in range(40):
        raw = eval_js(ws, f"""
            (function() {{
                var attachments = document.querySelectorAll('[data-testid="attachments"]').length;
                var blobs = Array.from(document.querySelectorAll('img[src^="blob:"]'))
                    .filter(function(img) {{
                        if ({expected_width} && img.naturalWidth !== {expected_width}) return false;
                        if ({expected_height} && img.naturalHeight !== {expected_height}) return false;
                        return true;
                    }}).length;
                return JSON.stringify({{attachments: attachments, blobs: blobs}});
            }})()
        """)
        try:
            state = json.loads(raw) if raw else {}
        except Exception:
            state = {}
        if int(state.get("attachments", 0) or 0) > 0 or int(state.get("blobs", 0) or 0) > 0:
            return True
        time.sleep(0.75)
    return False


def _click_enabled_submit(ws) -> str:
    return eval_js(ws, """
        (function() {
            var btns = Array.from(document.querySelectorAll(
                '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
            ));
            var enabled = btns.find(function(btn) {
                return btn.getAttribute('aria-disabled') !== 'true' && !btn.disabled;
            });
            if (enabled) {
                enabled.click();
                return 'clicked';
            }
            return btns.length ? 'disabled' : 'not found';
        })()
    """)


def post_tweet_fresh_tab(port: int, text: str, handle: str = "",
                         media_paths: Optional[list] = None,
                         required_substrings: Optional[list] = None,
                         expected_media_size: tuple[int, int] = (0, 0),
                         dry_run: bool = False) -> dict:
    """
    Post an original tweet from a fresh /compose/post tab.

    Use this for Premium-length posts and media posts. Opening a new tab avoids
    X's "Leave this website?" prompt from dirty timeline/search tabs.
    """
    media_paths = media_paths or []
    required_substrings = required_substrings or []
    if dry_run:
        return {"ok": True, "dry_run": True, "text": text, "media_paths": media_paths}
    if text_repeats_itself(text):
        return {"ok": False, "error": "draft_text_repeats_before_compose", "url": ""}

    with chrome_lock(port):
        tab = _new_tab(port, "https://x.com/compose/post")
        ws = _connect_tab(tab)
        close_tab = False
        try:
            try:
                _send(ws, "Page.bringToFront", {}, msg_id=41)
                _send(ws, "Emulation.setFocusEmulationEnabled", {"enabled": True}, msg_id=43)
            except Exception:
                pass

            if not _wait_for_composer(ws):
                return {"ok": False, "error": "compose_box_not_found"}

            result = paste_text(ws, text)
            if result not in ("ok", "ok-fallback"):
                return {"ok": False, "error": f"paste_failed:{result}"}
            time.sleep(1.5)

            current = _composer_text(ws)
            if normalize_composer_text(current) != normalize_composer_text(text):
                if composer_has_duplicate(current, text):
                    clear_composer(ws)
                    return {
                        "ok": False,
                        "error": "composer_duplicate_before_submit",
                        "composer": current[:500],
                    }
                if normalize_composer_text(current):
                    return {
                        "ok": False,
                        "error": "composer_text_mismatch_before_submit",
                        "composer": current[:500],
                    }
                if required_substrings:
                    return {
                        "ok": False,
                        "error": "text_not_confirmed",
                        "missing": required_substrings,
                        "composer": current,
                    }
                if not composer_submit_enabled(ws):
                    return {
                        "ok": False,
                        "error": "composer_empty_and_submit_disabled",
                        "composer": current[:500],
                    }
            if composer_has_duplicate(current, text):
                clear_composer(ws)
                return {
                    "ok": False,
                    "error": "composer_duplicate_before_submit",
                    "composer": current[:500],
                }
            missing = [s for s in required_substrings if s not in current]
            if missing:
                return {
                    "ok": False,
                    "error": "text_not_confirmed",
                    "missing": missing,
                    "composer": current,
                }

            if media_paths:
                uploaded = upload_media(ws, media_paths)
                if uploaded != "ok":
                    return {"ok": False, "error": f"upload_failed:{uploaded}"}
                if not _media_ready(ws, *expected_media_size):
                    return {
                        "ok": False,
                        "error": "media_preview_not_ready",
                        "composer": _composer_text(ws),
                    }

            clicked = _click_enabled_submit(ws)
            if clicked != "clicked":
                return {
                    "ok": False,
                    "error": f"submit_not_clicked:{clicked}",
                    "composer": _composer_text(ws),
                }
            # Wait before profile verification so X can clear the compose
            # beforeunload/dirty flag after accepting the post.
            time.sleep(12.0)

            if handle:
                navigate(ws, f"https://x.com/{handle}", wait=5.0)
                tweets = get_tweets(ws)
                latest = tweets[0] if tweets else {}
                latest_text = latest.get("text", "")
                ok = all(s in latest_text for s in required_substrings) if required_substrings else True
                close_tab = True
                return {"ok": ok, "url": latest.get("url", ""), "text": latest_text, "latest": latest}
            close_tab = True
            return {"ok": True, "url": "", "text": ""}
        finally:
            try:
                ws.close()
            finally:
                if close_tab:
                    _close_tab(port, tab.get("id", ""))
