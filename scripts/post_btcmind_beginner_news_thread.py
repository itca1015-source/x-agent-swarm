#!/usr/bin/env python3
"""Post the BTCMind beginner crypto news thread.

This follows the existing media-thread posting pattern from
post_hunter_token_spend_thread.py: use the Home inline composer for the media
opener, then reply_tweet for each follow-up.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from chrome import (
    _send,
    click_testid,
    connect,
    eval_js,
    get_tweets,
    navigate,
    paste_text,
    upload_media,
)
from engage import reply_tweet
from lock import chrome_lock


PORT = 10007
HANDLE = "btcmind101"
IMAGE_PATH = os.path.join(ROOT_DIR, "state", "media", "btcmind_crypto_news_beginners_cover.png")
OUT_PATH = os.path.join(ROOT_DIR, "state", "btcmind_beginner_news_thread_posted_2026-06-08.json")

THREAD = [
    """Crypto news digest for beginners:

Main crypto stories from the last few days, translated into plain English.

Format:
News -> what it means -> why people care.""",
    """1. A big bank is watching a company that owns a lot of Bitcoin.

That company, Strategy, holds Bitcoin like a reserve asset.

Why it matters: companies still need cash for debt and dividends. Cash pressure can affect reserve decisions.""",
    """2. Some Bitcoin traders are buying protection.

Think of this like insurance.

They pay money in case Bitcoin falls.

Why it matters: it shows some traders are cautious enough to protect themselves.""",
    """3. A crypto bridge had a security problem.

A bridge moves crypto from one blockchain to another.

Why it matters: if a bridge has a bug, money or fake tokens can move in ways they should not.""",
    """4. Yuga Labs helped rescue valuable NFTs during an exploit.

An NFT is a digital collectible.

A whitehat rescue means defenders moved assets before attackers could.

Why it matters: crypto safety also depends on fast emergency response.""",
    """5. Nansen showed a trader making money on Hyperliquid.

Hyperliquid is a crypto trading platform.

Why it matters: some trading data is public. People can study wallet behavior instead of only listening to opinions.""",
    """6. An investigator raised concerns about a crypto exchange.

An exchange is an app where people buy, sell, or hold crypto.

Why it matters: if withdrawals fail, users ask a simple question: does the exchange have the funds?""",
]


def _read_composer(ws) -> str:
    return eval_js(ws, """
        (function() {
            var box = document.querySelector('[data-testid="tweetTextarea_0"]')
                   || document.querySelector('[contenteditable="true"]');
            return box ? (box.innerText || box.textContent || '') : '';
        })()
    """)


def _clear_composer(ws) -> None:
    eval_js(ws, """
        (function() {
            document.querySelectorAll('[aria-label="Remove media"]').forEach(function(btn) {
                try { btn.click(); } catch(e) {}
            });
            var box = document.querySelector('[data-testid="tweetTextarea_0"]')
                   || document.querySelector('[contenteditable="true"]');
            if (box) {
                box.click();
                box.focus();
                try {
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                } catch(e) {}
            }
            return 'ok';
        })()
    """)
    time.sleep(0.5)


def _media_ready(ws) -> bool:
    for _ in range(40):
        raw = eval_js(ws, """
            (function() {
                var attachments = document.querySelectorAll('[data-testid="attachments"]').length;
                var blobs = Array.from(document.querySelectorAll('img[src^="blob:"]'))
                    .filter(function(img) {
                        return img.naturalWidth === 1600 && img.naturalHeight === 900;
                    }).length;
                return JSON.stringify({attachments: attachments, blobs: blobs});
            })()
        """)
        try:
            state = json.loads(raw) if raw else {}
        except Exception:
            state = {}
        if int(state.get("attachments", 0) or 0) > 0 or int(state.get("blobs", 0) or 0) > 0:
            return True
        time.sleep(0.75)
    return False


def post_first_tweet() -> dict:
    if any(len(text) > 280 for text in THREAD):
        return {"ok": False, "error": "thread_part_over_280"}
    if not os.path.exists(IMAGE_PATH):
        return {"ok": False, "error": f"image_not_found:{IMAGE_PATH}"}

    with chrome_lock(PORT):
        ws = connect(PORT, timeout=60)
        try:
            navigate(ws, "https://x.com/home", wait=4.0)
            try:
                _send(ws, "Emulation.setFocusEmulationEnabled", {"enabled": True}, msg_id=43)
            except Exception:
                pass

            focused = eval_js(ws, """
                (function() {
                    var box = document.querySelector('[data-testid="tweetTextarea_0"]')
                           || document.querySelector('[contenteditable="true"]');
                    if (!box) return 'not found';
                    box.click();
                    box.focus();
                    return 'ok';
                })()
            """)
            if focused == "not found":
                return {"ok": False, "error": "compose_box_not_found"}

            _clear_composer(ws)
            result = paste_text(ws, THREAD[0])
            if result not in ("ok", "ok-fallback"):
                return {"ok": False, "error": f"paste_failed:{result}"}
            time.sleep(1.0)

            composer = _read_composer(ws)
            required = ["Crypto news digest", "translated into plain English"]
            if not all(fragment in composer for fragment in required):
                return {"ok": False, "error": f"text_not_confirmed:{composer!r}"}

            uploaded = upload_media(ws, [IMAGE_PATH])
            if uploaded != "ok":
                return {"ok": False, "error": f"upload_failed:{uploaded}"}
            if not _media_ready(ws):
                return {"ok": False, "error": "media_preview_not_ready"}

            clicked = click_testid(ws, "tweetButtonInline")
            if clicked == "not found":
                clicked = click_testid(ws, "tweetButton")
            if clicked == "not found":
                return {"ok": False, "error": "submit_button_not_found"}
            time.sleep(10.0)

            navigate(ws, f"https://x.com/{HANDLE}", wait=4.0)
            time.sleep(1.5)
            tweets = get_tweets(ws)
            latest = tweets[0] if tweets else {}
            latest_text = latest.get("text", "")
            if all(fragment in latest_text for fragment in required):
                return {"ok": True, "url": latest.get("url", ""), "text": latest_text}
            return {"ok": False, "error": "first_tweet_not_verified", "latest": latest}
        finally:
            ws.close()


def _save(result: dict) -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    result = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "port": PORT,
        "handle": HANDLE,
        "image_path": IMAGE_PATH,
        "posts": [],
    }
    _save(result)

    print(f"[thread] posting opener with image: {IMAGE_PATH}", flush=True)
    first = post_first_tweet()
    result["posts"].append({"index": 1, "text": THREAD[0], "result": first})
    _save(result)
    print(f"[thread] opener result: {first}", flush=True)

    if not first.get("ok") or not first.get("url"):
        result["error"] = "opener_failed_or_missing_url"
        _save(result)
        return 1

    prev_url = first["url"]
    for idx, text in enumerate(THREAD[1:], start=2):
        print(f"[thread] posting reply {idx}/7 to {prev_url}", flush=True)
        res = reply_tweet(PORT, prev_url, text, self_handle=HANDLE, verbose=True)
        result["posts"].append({"index": idx, "text": text, "reply_to": prev_url, "result": res})
        _save(result)
        print(f"[thread] reply {idx} result: {res}", flush=True)
        if not res.get("ok") or not res.get("reply_url"):
            result["error"] = f"reply_{idx}_failed_or_missing_url"
            _save(result)
            return 1
        prev_url = res["reply_url"]

    result["ok"] = True
    result["thread_url"] = first["url"]
    result["last_url"] = prev_url
    _save(result)
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
