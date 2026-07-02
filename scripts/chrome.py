"""Chrome CDP helpers for x-agent. Direct WebSocket, no external CLI dependency."""
import json
import time
import urllib.request
import websocket


def _composer_selector_js() -> str:
    return """
        var el = null;
        var dialogs = document.querySelectorAll('[role="dialog"]');
        for (var i = dialogs.length - 1; i >= 0; i--) {
            el = dialogs[i].querySelector('[data-testid="tweetTextarea_0"]')
              || dialogs[i].querySelector('[contenteditable="true"]');
            if (el) break;
        }
        if (!el) el = document.querySelector('[data-testid="tweetTextarea_0"]');
        if (!el) {
            var ctes = document.querySelectorAll('[contenteditable="true"]');
            el = ctes[ctes.length - 1];
        }
    """


def normalize_composer_text(text: str) -> str:
    """Normalize X composer text for exact intended-vs-actual comparisons."""
    return (
        (text or "")
        .replace("\u00a0", " ")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def composer_text(ws: websocket.WebSocket) -> str:
    js = """
    (function() {
    """ + _composer_selector_js() + """
        if (!el) return '';
        return el.innerText || el.textContent || '';
    })()
    """
    return eval_js(ws, js)


def clear_composer(ws: websocket.WebSocket) -> str:
    js = """
    (function() {
    """ + _composer_selector_js() + """
        if (!el) return 'no editor';
        el.focus();
        try {
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
        } catch(e) {}
        return 'ok';
    })()
    """
    return eval_js(ws, js)


def composer_has_duplicate(actual: str, expected: str) -> bool:
    actual_n = normalize_composer_text(actual)
    expected_n = normalize_composer_text(expected)
    if not actual_n or not expected_n or actual_n == expected_n:
        return False
    compact_actual = "".join(actual_n.split())
    compact_expected = "".join(expected_n.split())
    head_len = min(50, max(20, len(expected_n) // 4))
    head = expected_n[:head_len]
    second_head = actual_n.find(head, max(1, len(expected_n) // 2))
    compact_head = "".join(head.split())
    compact_second_head = compact_actual.find(compact_head, max(1, len(compact_expected) // 2))
    return (
        actual_n.count(expected_n) >= 2
        or compact_actual == compact_expected + compact_expected
        or (
            actual_n.startswith(head)
            and second_head >= 0
            and len(actual_n) > len(expected_n) * 1.35
        )
        or (
            compact_actual.startswith(compact_head)
            and compact_second_head >= 0
            and len(compact_actual) > len(compact_expected) * 1.35
        )
    )


def composer_submit_enabled(ws: websocket.WebSocket) -> bool:
    """Return true when X has enabled the submit button for the active composer."""
    raw = eval_js(ws, """
        (function() {
            var dialogs = document.querySelectorAll('[role="dialog"]');
            var btn = null;
            for (var i = dialogs.length - 1; i >= 0; i--) {
                btn = dialogs[i].querySelector('[data-testid="tweetButton"]')
                   || dialogs[i].querySelector('[data-testid="tweetButtonInline"]');
                if (btn) break;
            }
            if (!btn) {
                btn = document.querySelector('[data-testid="tweetButton"]')
                   || document.querySelector('[data-testid="tweetButtonInline"]');
            }
            return !!(btn && btn.getAttribute('aria-disabled') !== 'true' && !btn.disabled);
        })()
    """)
    return raw == "True" or raw == "true"


def text_repeats_itself(text: str) -> bool:
    """Detect a draft that already contains its own opening/content twice."""
    text_n = normalize_composer_text(text)
    if len(text_n) < 40:
        return False
    compact = "".join(text_n.split())
    if len(compact) < 40:
        return False
    half = len(compact) // 2
    if compact[:half] == compact[half:]:
        return True
    head_len = min(60, max(25, len(text_n) // 4))
    head = text_n[:head_len]
    return text_n.find(head, head_len) >= 0


def connect(port: int, timeout: int = 20) -> websocket.WebSocket:
    """Connect to the first page tab on the given Chrome debug port."""
    tabs = list_tabs(port)
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError(f"No page tabs on port {port}")
    ws_url = pages[0]["webSocketDebuggerUrl"]
    # Chrome requires a recognised Origin or rejects with 403.
    # "devtools://devtools" is accepted by all Chrome versions regardless of
    # --remote-allow-origins settings.
    return websocket.create_connection(
        ws_url, timeout=timeout,
        header=["Origin: devtools://devtools"],
    )


def list_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://localhost:{port}/json", timeout=5) as r:
        return json.loads(r.read())


def ping(port: int) -> bool:
    try:
        list_tabs(port)
        return True
    except Exception:
        return False


def _send(ws: websocket.WebSocket, method: str, params: dict, msg_id: int = 1) -> dict:
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    deadline = time.time() + 120
    while True:
        try:
            msg = json.loads(ws.recv())
        except Exception as e:
            if "timed out" in str(e).lower() and time.time() < deadline:
                continue
            raise
        if msg.get("id") == msg_id:
            return msg


def handle_js_dialog(ws: websocket.WebSocket, accept: bool = True) -> bool:
    """Accept a native JS dialog such as X's beforeunload 'Leave site?' prompt."""
    msg_id = 12 if accept else 13
    payload = {
        "id": msg_id,
        "method": "Page.handleJavaScriptDialog",
        "params": {"accept": accept},
    }
    old_timeout = None
    try:
        try:
            old_timeout = ws.gettimeout()
            ws.settimeout(2)
        except Exception:
            old_timeout = None
        ws.send(json.dumps(payload))
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                return False
            if msg.get("id") == msg_id:
                return not msg.get("error")
        return False
    except Exception:
        return False
    finally:
        if old_timeout is not None:
            try:
                ws.settimeout(old_timeout)
            except Exception:
                pass


def dismiss_leave_site_dialog(ws: websocket.WebSocket) -> bool:
    """Click X's in-page "Leave" / "Discard" confirmation if it is visible."""
    raw = eval_js(ws, """
        (function() {
            function visible(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                var s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            }
            var dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
            for (var i = dialogs.length - 1; i >= 0; i--) {
                var d = dialogs[i];
                var text = (d.innerText || d.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!/leave|discard|unsaved|site/i.test(text)) continue;
                var buttons = Array.from(d.querySelectorAll('button,[role="button"]')).filter(visible);
                for (var j = buttons.length - 1; j >= 0; j--) {
                    var b = buttons[j];
                    var label = (b.innerText || b.textContent || b.getAttribute('aria-label') || '').trim();
                    if (/^(leave|discard)$/i.test(label) || /leave|discard/i.test(label)) {
                        b.click();
                        return 'clicked:' + label;
                    }
                }
            }
            return '';
        })()
    """)
    return bool(raw and raw.startswith("clicked:"))


def navigate(ws: websocket.WebSocket, url: str, wait: float = 4.0):
    handle_js_dialog(ws, accept=True)
    dismiss_leave_site_dialog(ws)
    # Bring tab to front first — X (and other sites) pause content loading on
    # tabs with document.hidden=true. A user switching to another window can
    # silently re-hide our tab between navigations.
    try:
        _send(ws, "Page.bringToFront", {}, msg_id=11)
    except Exception:
        pass
    try:
        _send(ws, "Page.navigate", {"url": url}, msg_id=10)
    except Exception:
        handle_js_dialog(ws, accept=True)
        dismiss_leave_site_dialog(ws)
        # Some Chrome profile states never ack Page.navigate but still navigate.
        # Keep going and let downstream DOM checks determine success/failure.
        pass
    handle_js_dialog(ws, accept=True)
    dismiss_leave_site_dialog(ws)
    time.sleep(wait)


def set_viewport(ws: websocket.WebSocket, width: int = 1280, height: int = 1600):
    """Force a viewport size via CDP. Needed for X thread pages, which use
    virtualized rendering — with a small window, reply cards below the visible
    area never enter the DOM. Persists until cleared or tab closes."""
    _send(ws, "Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
    }, msg_id=40)


def bring_to_front(ws: websocket.WebSocket):
    """Activate the CDP-controlled tab so document.visibilityState='visible'.
    X (and other sites) pause content loading on hidden tabs via the Page
    Visibility API — without this, thread reply lists never populate."""
    _send(ws, "Page.bringToFront", {}, msg_id=41)


def eval_js(ws: websocket.WebSocket, expression: str, await_promise: bool = False) -> str:
    """Run JS and return the result as a string. Returns '' on error."""
    params = {"expression": expression, "returnByValue": True}
    if await_promise:
        params["awaitPromise"] = True
    r = _send(ws, "Runtime.evaluate", params, msg_id=20)
    result = r.get("result", {}).get("result", {})
    # Handles string, number, boolean, null
    value = result.get("value")
    if value is None:
        return ""
    return str(value)


def get_tweets(ws: websocket.WebSocket) -> list[dict]:
    """
    Extract tweet cards from current page via JS.
    Returns list of {id, text, url, likes, author}.
    """
    js = """
    (function() {
        var articles = document.querySelectorAll('article[data-testid="tweet"]');
        var results = [];
        articles.forEach(function(el) {
            try {
                var textEl = el.querySelector('[data-testid="tweetText"]');
                var text = textEl ? textEl.innerText.trim() : '';
                var links = el.querySelectorAll("a[href*='/status/']");
                var url = '';
                var id = '';
                links.forEach(function(a) {
                    var m = a.href.match(/status[/]([0-9]+)/);
                    if (m && !url) { url = a.href; id = m[1]; }
                });
                var likesEl = el.querySelector('[data-testid="like"] span');
                var likes = likesEl ? parseInt(likesEl.innerText.replace(/[^0-9]/g,'')) || 0 : 0;
                var authorEl = el.querySelector('[data-testid="User-Name"] span');
                var author = authorEl ? authorEl.innerText.trim() : '';
                if (id && text) results.push({id: id, text: text, url: url, likes: likes, author: author});
            } catch(e) {}
        });
        return JSON.stringify(results);
    })()
    """
    raw = eval_js(ws, js)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def paste_text(ws: websocket.WebSocket, text: str) -> str:
    """
    Insert text into the focused contenteditable element.

    Three strategies, in order of preference:
      1. CDP Input.insertText — protocol-level "type this text" command.
         Generates real beforeinput/input events that Draft.js (X's editor)
         picks up. Highest reliability. Required because X's Tweet submit
         button is gated on React state, not DOM content; execCommand alone
         can leave React thinking the box is empty even when text is visible.
      2. document.execCommand('insertText') — legacy JS path. Works in some
         Chrome versions/contexts; we keep as fallback.
      3. ClipboardEvent — last resort; often inserts visible text without
         updating React state at all.

    Prefers the composer inside the topmost [role="dialog"] (reply modal) when
    present, so we never type into a background composer by accident.
    Returns 'ok' (CDP), 'ok' (execCommand), or 'ok-fallback' (Clipboard).
    """
    dismiss_leave_site_dialog(ws)
    # Strategy 1: CDP Input.insertText. Requires the right element to be
    # focused first — clear the box, then let the page take input.
    safe = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    focus_and_clear = f"""
    (function() {{
        var el = null;
        var dialogs = document.querySelectorAll('[role="dialog"]');
        for (var i = dialogs.length - 1; i >= 0; i--) {{
            el = dialogs[i].querySelector('[data-testid="tweetTextarea_0"]')
              || dialogs[i].querySelector('[contenteditable="true"]');
            if (el) break;
        }}
        if (!el) el = document.querySelector('[data-testid="tweetTextarea_0"]');
        if (!el) {{
            var ctes = document.querySelectorAll('[contenteditable="true"]');
            el = ctes[ctes.length - 1];
        }}
        if (!el) return 'no editor';
        el.focus();
        try {{
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
        }} catch(e) {{}}
        return 'focused';
    }})()
    """
    focus_res = eval_js(ws, focus_and_clear)
    if focus_res != "focused":
        return focus_res or "no editor"

    # Fire Input.insertText through CDP — the protocol-level equivalent of
    # the user typing. Generates beforeinput → input events that Draft.js
    # listens for, which is what enables the Tweet submit button.
    r = _send(ws, "Input.insertText", {"text": text}, msg_id=50)
    if not r.get("error"):
        return "ok"

    # Strategy 2 + 3 fallback: original JS path.
    js = f"""
    (function() {{
        var el = null;
        // Prefer the composer inside the most-recently-opened dialog
        var dialogs = document.querySelectorAll('[role="dialog"]');
        for (var i = dialogs.length - 1; i >= 0; i--) {{
            el = dialogs[i].querySelector('[data-testid="tweetTextarea_0"]')
              || dialogs[i].querySelector('[contenteditable="true"]');
            if (el) break;
        }}
        if (!el) el = document.querySelector('[data-testid="tweetTextarea_0"]');
        if (!el) {{
            var ctes = document.querySelectorAll('[contenteditable="true"]');
            el = ctes[ctes.length - 1];
        }}
        if (!el) return 'no editor';
        el.focus();

        // Clear any existing content first — inline reply composers can carry
        // stale text from a prior interaction, which would cause our paste to
        // append (producing "nice videonice video") and leave submit disabled.
        try {{
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
        }} catch(e) {{}}

        // execCommand fires React's input handler → composer state updates → submit enables
        var ok = false;
        try {{ ok = document.execCommand('insertText', false, `{safe}`); }} catch(e) {{}}
        if (ok) {{
            try {{
                el.dispatchEvent(new InputEvent('input',
                    {{bubbles: true, inputType: 'insertFromPaste', data: `{safe}`}}));
            }} catch(e) {{}}
            return 'ok';
        }}

        // Fallback: ClipboardEvent (legacy path)
        var dt = new DataTransfer();
        dt.setData('text/plain', `{safe}`);
        var evt = new ClipboardEvent('paste',
            {{clipboardData: dt, bubbles: true, cancelable: true}});
        el.dispatchEvent(evt);
        return 'ok-fallback';
    }})()
    """
    return eval_js(ws, js)


def type_text(ws: websocket.WebSocket, text: str, char_delay: float = 0.0) -> str:
    """Type `text` into the currently focused element via per-character
    Input.dispatchKeyEvent. Use this instead of paste_text when the editor
    silently drops Input.insertText (observed on X's QT compose modal —
    even with focus emulation enabled, the composer ends up empty after
    paste:ok). dispatchKeyEvent fires real keydown/keyup events that
    Draft.js cannot drop.

    Caller is responsible for focusing the right element BEFORE calling.
    Returns 'ok' on success, or an error string."""
    import time as _time
    try:
        for ch in text:
            _send(ws, "Input.dispatchKeyEvent",
                  {"type": "keyDown", "text": ch, "unmodifiedText": ch}, msg_id=100)
            _send(ws, "Input.dispatchKeyEvent",
                  {"type": "keyUp", "text": ch, "unmodifiedText": ch}, msg_id=101)
            if char_delay > 0:
                _time.sleep(char_delay)
        return "ok"
    except Exception as e:
        return f"err:{e}"


def click_testid(ws: websocket.WebSocket, testid: str) -> str:
    """Click the first element matching data-testid. Returns 'ok' or 'not found'."""
    js = f"""
    (function() {{
        var el = document.querySelector('[data-testid="{testid}"]');
        if (!el) return 'not found';
        el.click();
        return 'ok';
    }})()
    """
    return eval_js(ws, js)


def scroll_down(ws: websocket.WebSocket, px: int = 800):
    eval_js(ws, f"window.scrollBy(0, {px})")
    time.sleep(1.5)


def upload_media(ws: websocket.WebSocket, file_paths: list[str]) -> str:
    """
    Attach files (image or video) to the currently-open X compose dialog
    by setting the value of its <input type="file"> via CDP DOM.setFileInputFiles.
    Returns 'ok' or an error message.
    """
    # Get the root document node id
    doc = _send(ws, "DOM.getDocument", {"depth": -1}, msg_id=31)
    root = doc.get("result", {}).get("root", {}).get("nodeId")
    if not root:
        return "no document root"

    # Find the file input. X's compose has data-testid="fileInput".
    q = _send(
        ws, "DOM.querySelector",
        {"nodeId": root, "selector": 'input[data-testid="fileInput"]'},
        msg_id=32,
    )
    node_id = q.get("result", {}).get("nodeId")
    if not node_id:
        q = _send(
            ws, "DOM.querySelector",
            {"nodeId": root, "selector": 'input[type="file"]'},
            msg_id=33,
        )
        node_id = q.get("result", {}).get("nodeId")
    if not node_id:
        return "file input not found"

    r = _send(
        ws, "DOM.setFileInputFiles",
        {"nodeId": node_id, "files": file_paths},
        msg_id=34,
    )
    if r.get("error"):
        return f"setFileInputFiles error: {r['error']}"
    return "ok"
