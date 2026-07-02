"""Exercise X compose safety checks without submitting anything.

This opens a real reply composer, inserts intentionally duplicated text,
verifies the duplicate guard would block submit, then discards the draft.
"""
import argparse
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from chrome import (
    clear_composer,
    composer_has_duplicate,
    composer_text,
    connect,
    eval_js,
    handle_js_dialog,
    navigate,
    normalize_composer_text,
    paste_text,
)
from engage import discard_open_composer
from lock import chrome_lock


DEFAULT_TARGET = "https://x.com/GuoHunter95258/status/2059892211022758301"
BASE_TEXT = (
    "compose safety probe duplicate block."
)


def _click_reply_on_first_article(ws) -> str:
    return eval_js(ws, """
        (function() {
            var arts = document.querySelectorAll('article[data-testid="tweet"]');
            if (!arts.length) return 'no_articles';
            var btn = arts[0].querySelector('[data-testid="reply"]');
            if (!btn) return 'no_reply_button';
            btn.click();
            return 'clicked';
        })()
    """)


def _focus_composer(ws) -> str:
    for _ in range(10):
        res = eval_js(ws, """
            (function() {
                var box = document.querySelector('[data-testid="tweetTextarea_0"]');
                if (!box) {
                    var boxes = document.querySelectorAll('[contenteditable="true"]');
                    box = boxes[boxes.length - 1];
                }
                if (!box) return 'no_box';
                box.click();
                box.focus();
                return 'focused';
            })()
        """)
        if res == "focused":
            return res
        time.sleep(0.5)
    return res


def _set_composer_text_for_probe(ws, text: str) -> str:
    safe = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    return eval_js(ws, f"""
        (function() {{
            var box = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (!box) {{
                var boxes = document.querySelectorAll('[contenteditable="true"]');
                box = boxes[boxes.length - 1];
            }}
            if (!box) return 'no_box';
            box.focus();
            box.innerText = `{safe}`;
            try {{
                box.dispatchEvent(new InputEvent('input', {{
                    bubbles: true,
                    inputType: 'insertText',
                    data: `{safe}`
                }}));
            }} catch(e) {{
                box.dispatchEvent(new Event('input', {{bubbles: true}}));
            }}
            return 'ok';
        }})()
    """)


def run_probe(port: int, target_url: str, mode: str) -> dict:
    intended = BASE_TEXT
    inserted = intended + " " + intended if mode == "duplicate" else intended
    with chrome_lock(port):
        ws = connect(port, timeout=30)
        try:
            handle_js_dialog(ws, accept=True)
            discard_open_composer(ws)
            navigate(ws, target_url, wait=5.0)
            clicked = _click_reply_on_first_article(ws)
            if clicked != "clicked":
                return {"ok": False, "stage": "open_reply", "error": clicked}
            time.sleep(2.0)
            focused = _focus_composer(ws)
            if focused != "focused":
                discard_open_composer(ws)
                return {"ok": False, "stage": "focus", "error": focused}
            clear_composer(ws)
            pasted = paste_text(ws, inserted)
            time.sleep(1.0)
            actual = composer_text(ws)
            if not actual.strip():
                clear_composer(ws)
                typed = _set_composer_text_for_probe(ws, inserted)
                time.sleep(1.0)
                actual = composer_text(ws)
            else:
                typed = ""
            duplicated = composer_has_duplicate(actual, intended)
            exact = normalize_composer_text(actual) == normalize_composer_text(intended)
            blocked = duplicated or not exact
            discard_open_composer(ws)
            handle_js_dialog(ws, accept=True)
            return {
                "ok": bool(blocked if mode == "duplicate" else exact),
                "stage": "checked",
                "mode": mode,
                "paste": pasted,
                "type_fallback": typed,
                "duplicate_detected": duplicated,
                "exact_match": exact,
                "would_block_submit": blocked,
                "composer_preview": actual[:240],
            }
        finally:
            try:
                ws.close()
            except Exception:
                pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--target-url", default=DEFAULT_TARGET)
    p.add_argument("--mode", choices=["duplicate", "clean"], default="duplicate")
    args = p.parse_args()
    res = run_probe(args.port, args.target_url, args.mode)
    print(res)
    if not res.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
