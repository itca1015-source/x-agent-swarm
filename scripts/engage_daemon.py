"""
Engage daemon — Hunter replies to new posts from target accounts using few-shot
patterns harvested from those accounts' winning replies.

Workflow per cycle:
  1. Pick next target (round-robin)
  2. Visit their profile, grab their most recent original post
  3. Skip if seen, too old, too many replies, or a thread-continuation
  4. Skip if Hunter already replied today (per-target cap) or hit daily total cap
  5. Generate a reply via generate.generate_engaged_reply() with few-shot examples
  6. Append to state/reply_queue.json with status=pending → review_queue.py posts

Usage:
    python3 engage_daemon.py                 # run as daemon, default cadence
    python3 engage_daemon.py --once          # one cycle then exit (test mode)
    python3 engage_daemon.py --dry-run       # don't generate, just log what would happen
    python3 engage_daemon.py --target X      # only check target X
"""
import argparse
import json
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, date

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR    = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import env;   env.load()
import chrome  as _chrome
import login   as _login
import fetch   as _fetch
import engage  as _engage
import generate as _generate
import btcmind_autonomy as _btcmind
import btcmind_publish_policy as _btcmind_publish
import btcmind_reply_quality as _btcmind_quality


def _btcmind_publish_decision(cfg, *, action, lane, live_requested):
    return _btcmind_publish.decide_public_action(
        cfg,
        action=action,
        lane=lane,
        live_requested=live_requested,
    )


def _is_btcmind_cfg(cfg):
    handle = str(cfg.get("hunter_handle") or cfg.get("x_handle") or "").strip().lower()
    comment = str(cfg.get("_comment") or "").lower()
    return handle in {"btcmind101", "hunter_solvea"} or "btcmind" in comment


def _reply_quality_enabled(cfg):
    qcfg = cfg.get("reply_quality_audit")
    return isinstance(qcfg, dict) and bool(qcfg.get("enabled", False))


def _reply_quality_label(cfg):
    qcfg = cfg.get("reply_quality_audit")
    if isinstance(qcfg, dict) and qcfg.get("account_label"):
        return str(qcfg.get("account_label")).strip()
    if _is_btcmind_cfg(cfg):
        return "BTCMind"
    return str(cfg.get("telegram_label") or cfg.get("hunter_handle") or "Reply").strip()


def _btcmind_generation_kwargs(cfg):
    return {
        "library_path": LIBRARY_PATH,
        "archetypes": cfg.get("archetypes", {}),
        "hunter_handle": cfg["hunter_handle"],
        "display_handle": _x_handle(cfg),
        "examples_per_prompt": cfg["generation"]["examples_per_prompt"],
        "max_reply_chars": cfg["generation"]["max_reply_chars"],
        "extra_quality_rules": _btcmind_quality.generation_rules(cfg) if _reply_quality_enabled(cfg) else "",
    }


def _generate_btcmind_reply_with_audit(target_handle, target_post_text, cfg, queue):
    qcfg = cfg.get("reply_quality_audit", {}) if isinstance(cfg.get("reply_quality_audit"), dict) else {}
    max_rewrites = int(qcfg.get("max_rewrite_attempts", 1) or 0)
    feedback = ""
    last_g = {}
    last_audit = {}

    for attempt in range(max_rewrites + 1):
        g = _generate.generate_engaged_reply(
            target_handle=target_handle,
            target_post_text=target_post_text,
            quality_feedback=feedback,
            **_btcmind_generation_kwargs(cfg),
        )
        reply_text = str(g.get("reply") or "").strip()
        last_g = g
        if not reply_text or len(reply_text) < 10:
            return g, {}, "empty/short generation"
        if _chrome.text_repeats_itself(reply_text):
            return g, {}, "generated reply repeats itself"
        if not _reply_quality_enabled(cfg):
            return g, {}, ""

        audit_entry = {
            "target": target_handle,
            "target_text": target_post_text,
            "reply_text": reply_text,
        }
        last_audit = _btcmind_quality.apply_audit(audit_entry, cfg, recent_entries=queue)
        risk_class = str(last_audit.get("reply_risk_class") or "")
        if risk_class not in {"needs_rewrite", "block"}:
            g["btcmind_reply_audit"] = last_audit
            return g, last_audit, ""
        if attempt < max_rewrites:
            feedback = _btcmind_quality.rewrite_feedback(last_audit)
            _log(f"    {_reply_quality_label(cfg)} reply audit requested rewrite: {', '.join(last_audit.get('reasons', [])[:3])}")
            continue

    if last_audit:
        last_g["btcmind_reply_audit"] = last_audit
        why = " | ".join((last_audit.get("reasons") or [])[:3]) or f"local {_reply_quality_label(cfg)} reply audit"
        return last_g, last_audit, f"{_reply_quality_label(cfg)} reply quality {last_audit.get('reply_risk_class')}: {why}"
    return last_g, {}, f"{_reply_quality_label(cfg)} reply quality failed"
from lock import chrome_lock, file_lock

# Bound by main() — used by Chrome-touching helpers to acquire the file lock
# that serializes us against telegram_bridge driving the same Chrome instance.
_HUNTER_PORT = 0

# Active CDP WebSocket. Bound by main(); may be re-bound by _recover_frozen_tab()
# when the page renderer hangs and we have to swap to a fresh tab. cycle() reads
# this at the top of each iteration so a swap takes effect on the next cycle.
_WS = None
_TELEGRAM_ENABLED = True

# Per-account Telegram credentials — set from config in main()
_TG_BOT_TOKEN = ""
_TG_CHAT_ID   = ""

DEFAULT_CONFIG  = os.path.join(ROOT_DIR, "engage_config.json")
STATE_DIR       = os.path.join(ROOT_DIR, "state")
LIBRARY_PATH    = os.path.join(STATE_DIR, "winning_replies.json")
QUEUE_PATH      = os.path.join(STATE_DIR, "reply_queue.json")
# Set per-account in main() from config hunter_handle
LOG_DIR         = os.path.join(ROOT_DIR, "logs", "engage")
SEEN_PATH       = os.path.join(STATE_DIR, "engage_seen.json")

_stop = False


# ── logging ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


# ── state ─────────────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _append_queue_locked(entry: dict, in_mem_queue: list) -> bool:
    """Atomically append `entry` to the on-disk queue, then update the local
    in-memory copy. Holds file_lock('reply_queue') over the load-modify-save
    so concurrent writers (buildlog_drafts, quote_scout, telegram_bridge)
    can't lose updates. Fixes incident 2026-05-16 00:30 — buildlog draft
    erased by daemon's stale-snapshot save."""
    with file_lock("reply_queue", on_wait=_log):
        disk = _load_json(QUEUE_PATH, [])
        eid = str(entry.get("id", ""))
        if eid and any(str(x.get("id", "")) == eid for x in disk):
            return False
        disk.append(entry)
        _save_json(QUEUE_PATH, disk)
    in_mem_queue.append(entry)
    return True


def _reserve_queue_entry_locked(entry: dict, in_mem_queue: list) -> bool:
    """Reserve an entry ID on disk before any irreversible side effect.

    For auto-post mode, this prevents concurrent sweeps from posting multiple
    live replies to the same target tweet and only discovering the duplicate
    later when queue append happens.
    """
    with file_lock("reply_queue", on_wait=_log):
        disk = _load_json(QUEUE_PATH, [])
        eid = str(entry.get("id", ""))
        if eid and any(str(x.get("id", "")) == eid for x in disk):
            return False
        disk.append(entry)
        _save_json(QUEUE_PATH, disk)
    in_mem_queue.append(entry)
    return True


def _update_queue_entry_locked(entry_id: str, updates: dict):
    """Atomically apply `updates` to the entry with id == entry_id on disk.
    Returns True if found+updated. Used for setting telegram_message_id post-hoc."""
    with file_lock("reply_queue", on_wait=_log):
        disk = _load_json(QUEUE_PATH, [])
        for i, e in enumerate(disk):
            if e.get("id") == entry_id:
                disk[i].update(updates)
                _save_json(QUEUE_PATH, disk)
                return True
    return False


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_count(s: str) -> int:
    if not s: return 0
    s = s.strip().replace(",", "")
    mult = 1
    if s and s[-1] in "Kk":   mult, s = 1_000, s[:-1]
    elif s and s[-1] in "Mm": mult, s = 1_000_000, s[:-1]
    elif s and s[-1] in "Bb": mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


# Parse "1h", "12m", "3d" relative timestamps that X shows on tweet cards.
def _parse_relative_age_minutes(rel: str) -> int:
    if not rel:
        return 9999
    m = re.match(r"(\d+)\s*([smhdw])", rel.strip(), re.I)
    if not m:
        return 9999  # absolute date string (e.g. "Feb 25") = old, skip
    n = int(m.group(1))
    unit = m.group(2).lower()
    return {"s": 0, "m": n, "h": n * 60, "d": n * 1440, "w": n * 10080}.get(unit, 9999)


def _age_minutes_from_datetime(dt_str: str) -> int:
    if not dt_str:
        return 9999
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds() / 60))
    except Exception:
        return 9999


def _ensure_chrome(port: int, handle: str, profile_dir: str = "") -> bool:
    if _chrome.ping(port):
        try:
            _login.ensure_page_tab(port)
        except Exception as e:
            _log(f"  could not ensure Chrome page tab on port {port}: {e}")
            return False
        return any(t.get("type") == "page" for t in _chrome.list_tabs(port))
    _log(f"chrome on port {port} ({handle}) down — relaunching")
    profile_dir = profile_dir or os.path.join(ROOT_DIR, "chrome-profiles", handle)
    if not os.path.exists(profile_dir):
        _log(f"  no profile dir at {profile_dir}")
        return False
    try:
        _login.launch_chrome(port, profile_dir)
        _login.ensure_page_tab(port)
        return _chrome.ping(port)
    except Exception as e:
        _log(f"  relaunch failed: {e}")
        return False


# ── login detector ────────────────────────────────────────────────────────────

LOGOUT_ALERT_COOLDOWN_SEC = 3600  # don't re-spam logout alert more than once an hour

def _x_handle(cfg: dict) -> str:
    return str(cfg.get("x_handle") or cfg.get("hunter_x_handle") or cfg.get("hunter_handle", "")).lstrip("@")


def _check_login(ws, expected_handle: str = "") -> bool:
    """Return True if Hunter is logged in to X, False if logged out.
    Safe to call frequently — does one navigate to x.com/home.
    Locked: prevents bridge-driven navigations from racing the home-page check
    and producing false "logged out" readings (incident 2026-05-15 16:47)."""
    try:
        with chrome_lock(_HUNTER_PORT, on_wait=_log):
            _chrome.navigate(ws, "https://x.com/home", wait=4.0)
            raw = _chrome.eval_js(ws, r"""
                JSON.stringify({
                    in:  !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')
                      || !!document.querySelector('[data-testid="AppTabBar_Profile_Link"]'),
                    profile: document.querySelector('[data-testid="AppTabBar_Profile_Link"]')?.getAttribute('href') || '',
                    login_redirect: /\/(login|i\/flow\/login)/.test(location.pathname),
                    login_btn: !!document.querySelector('a[href="/login"]')
                            || !!document.querySelector('[data-testid="loginButton"]')
                })
            """)
        d = json.loads(raw) if raw else {}
        # Treat as logged in only if we see the side-nav and we're NOT on a login redirect
        logged_in = bool(d.get("in")) and not d.get("login_redirect") and not d.get("login_btn")
        expected = expected_handle.lstrip("@").lower()
        if logged_in and expected:
            actual = str(d.get("profile") or "").strip("/").lower()
            if actual and actual != expected:
                _log(f"  logged in as @{actual}, expected @{expected}")
                return False
        return logged_in
    except Exception as e:
        _log(f"  login check error: {e}")
        return True  # assume OK on transient errors; will recheck next cycle


def _recover_frozen_tab() -> bool:
    """When _check_login reports "logged out" but the tab title still looks
    logged-in (e.g. "Home / X" or "(N) Home / X"), the page renderer is hung,
    not the session. Close the dead tab and open a fresh one — the session
    cookie lives in the profile dir, so login persists across the swap.

    Returns True if a tab swap actually happened. Caller should treat this cycle
    as a skip; the next cycle will pick up the new _WS and resume cleanly.

    Uses Chrome's HTTP /json endpoints (not WS) because they keep working when
    the page renderer is unresponsive. Incident 2026-05-15 22:00 — tab froze on
    x.com/home for 2.5h and the daemon kept yelling "logged out" until manual
    recovery."""
    global _WS
    try:
        tabs = _chrome.list_tabs(_HUNTER_PORT)
    except Exception as e:
        _log(f"  recovery: list_tabs failed: {e}")
        return False
    pages = [t for t in tabs if t.get("type") == "page"]
    # Find a tab whose title looks logged-in. Logged-in home is "Home / X" or
    # "(N) <name> on X: ..." (a tweet preview in the home feed). Logged-out
    # titles are "Log in to X", "Log in / X", or "X. It's what's happening".
    LOGGED_OUT_MARKERS = ("log in", "sign up", "it's what's happening")
    frozen = None
    for t in pages:
        title = (t.get("title") or "")
        low = title.lower()
        if any(m in low for m in LOGGED_OUT_MARKERS):
            continue
        if "home / x" in low or " on x" in low:
            frozen = t
            break
    if not frozen:
        return False
    old_id = frozen.get("id", "")
    _log(f"  recovery: tab title='{frozen.get('title','')[:60]}' looks signed-in but WS frozen; swapping")
    import urllib.request as _ur, urllib.parse as _up
    try:
        url = (f"http://localhost:{_HUNTER_PORT}/json/new?"
               + _up.quote("https://x.com/home", safe=":/"))
        with _ur.urlopen(_ur.Request(url, method="PUT"), timeout=10) as r:
            new = json.load(r)
    except Exception as e:
        _log(f"  recovery: new-tab failed: {e}")
        return False
    if not new.get("id"):
        _log("  recovery: new-tab response missing id")
        return False
    time.sleep(3.0)  # let new tab navigate + render
    if old_id:
        try:
            with _ur.urlopen(f"http://localhost:{_HUNTER_PORT}/json/close/{old_id}",
                             timeout=5):
                pass
        except Exception as e:
            _log(f"  recovery: close old tab failed (non-fatal): {e}")
    # Rebind module WS to the fresh tab so the next cycle uses it.
    try:
        if _WS is not None:
            try: _WS.close()
            except Exception: pass
    except Exception:
        pass
    try:
        _WS = _chrome.connect(_HUNTER_PORT)
        _log("  recovery: reconnected WS to fresh tab")
        return True
    except Exception as e:
        _log(f"  recovery: WS reconnect failed: {e}")
        return False


def _check_login_and_alert(ws, seen: dict, expected_handle: str = "") -> bool:
    """Wrap _check_login with state-transition Telegram alerts (throttled).
    On 'logged out', first attempt frozen-tab recovery before alerting — if the
    tab title still looks logged-in, the session is fine and only the renderer
    needs swapping."""
    is_in = _check_login(ws, expected_handle=expected_handle)
    prev = seen.get("_login_state", "unknown")

    if is_in:
        if prev == "out":
            if not _TELEGRAM_ENABLED:
                seen["_login_state"] = "in"
                return True
            try:
                import telegram as _tg
                _tg.send_text("✅ *Hunter is logged back in.* Daemon resumed.")
                _log("login restored — sent recovery alert")
            except Exception as e:
                _log(f"  recovery alert send failed: {e}")
        seen["_login_state"] = "in"
        return True

    # Before alerting "logged out", check whether this is actually a frozen tab.
    if _recover_frozen_tab():
        # Don't flip _login_state to "out" — recovery means the session was
        # always fine. Next cycle will run normally on the fresh _WS.
        _log("  recovery: tab swapped; next cycle will resume on fresh tab")
        return False

    # Real logout
    seen["_login_state"] = "out"
    last_alert = float(seen.get("_last_logout_alert", 0) or 0)
    if prev != "out" or time.time() - last_alert >= LOGOUT_ALERT_COOLDOWN_SEC:
        if not _TELEGRAM_ENABLED:
            return False
        try:
            import telegram as _tg
            _tg.send_text(
                "⚠️ *Hunter is logged out of X.*\n\n"
                "Open Hunter's Chrome window on the iMac (port 10000) and sign in at https://x.com .\n"
                "The daemon will auto-resume on the next cycle once login is restored."
            )
            seen["_last_logout_alert"] = time.time()
            _log("logged-out alert sent to telegram")
        except Exception as e:
            _log(f"  logout alert send failed: {e}")
    return False


# ── JS for fetching newest post from a target profile ─────────────────────────

LATEST_POST_JS = r"""
(function() {
    var articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (var i = 0; i < articles.length; i++) {
        var el = articles[i];
        var head = (el.innerText || '').slice(0, 300);
        if (/Pinned/i.test(head)) continue;
        if (/reposted/i.test(head)) continue;
        if (/Replying to/i.test(head)) continue;

        var textEl = el.querySelector('[data-testid="tweetText"]');
        var text   = textEl ? textEl.innerText.trim() : '';
        if (!text) continue;
        var urlEl = el.querySelector('a[href*="/status/"]');
        var url   = urlEl ? urlEl.href : '';
        if (!url) continue;
        var id    = (url.match(/status[/](\d+)/) || [])[1] || '';

        var replyEl = el.querySelector('[data-testid="reply"]');
        var replyTxt = replyEl ? replyEl.innerText.replace(/[^0-9KMB.,]/g,'') : '';

        var timeEl = el.querySelector('time');
        var datetime = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
        return JSON.stringify({id:id, url:url, text:text.slice(0,800), repliesTxt:replyTxt, datetime:datetime});
    }
    return '';
})()
"""


def fetch_latest_post(ws, handle: str) -> dict:
    with chrome_lock(_HUNTER_PORT, on_wait=_log):
        _chrome.navigate(ws, f"https://x.com/{handle}", wait=3.5)
        _chrome.eval_js(ws, "window.scrollTo(0, 300)")  # ensure first non-pinned card visible
        time.sleep(1.5)
        raw = _chrome.eval_js(ws, LATEST_POST_JS)
        # While we're on the profile page, opportunistically grab the follower
        # count — cached for the Telegram card render so the operator sees who
        # they're about to reply to without an extra Chrome navigation later.
        try:
            f_raw = _chrome.eval_js(ws, FOLLOWERS_JS)
            if f_raw and f_raw.isdigit():
                import author_info as _ai
                _ai.set_followers(handle, int(f_raw))
        except Exception:
            pass
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        d["replies"] = _parse_count(d.get("repliesTxt", ""))
        d["age_minutes"] = _age_minutes_from_datetime(d.get("datetime", ""))
        return d
    except Exception:
        return {}


# Follower-count extractor. Reads the link in the profile header that points
# to /<handle>/verified_followers (or /followers as fallback). Returns the
# integer follower count as a string, or '' on miss. We parse here instead
# of via _parse_count because X uses aria-labels with "Followers" suffix and
# the raw text uses commas + abbreviations (K/M).
FOLLOWERS_JS = r"""
(function(){
    function num(s){
        if (!s) return 0;
        s = String(s).replace(/[, ]/g,'');
        var m = s.match(/([\d.]+)\s*([KkMm]?)/);
        if (!m) return 0;
        var n = parseFloat(m[1]);
        if (!n) return 0;
        if (/k/i.test(m[2])) n *= 1000;
        if (/m/i.test(m[2])) n *= 1000000;
        return Math.round(n);
    }
    // Prefer the verified_followers link (post-2023 X UI), fall back to /followers
    var sels = ['a[href$="/verified_followers"]', 'a[href$="/followers"]'];
    for (var i = 0; i < sels.length; i++) {
        var els = document.querySelectorAll(sels[i]);
        for (var j = 0; j < els.length; j++) {
            var el = els[j];
            // First <span> child typically has the raw count.
            var spans = el.querySelectorAll('span');
            for (var k = 0; k < spans.length; k++) {
                var t = (spans[k].innerText || '').trim();
                if (/^[\d.,]+\s*[KkMm]?$/.test(t)) {
                    var n = num(t);
                    if (n > 0) return String(n);
                }
            }
        }
    }
    return '';
})()
"""


# ── JS for extracting search-results tweets ───────────────────────────────────

SEARCH_RESULTS_JS = r"""
(function() {
    var arts = document.querySelectorAll('article[data-testid="tweet"]');
    var out = [];
    arts.forEach(function(el) {
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

        out.push({id:id, url:url, text:text.slice(0,800), author:author,
                  repliesTxt:replyTxt, likesTxt:likeTxt, datetime:datetime});
    });
    return JSON.stringify(out);
})()
"""


def fetch_keyword_candidates(ws, keyword: str, limit: int) -> list:
    """Search X for `keyword` on the Latest tab and return parsed candidates."""
    import urllib.parse
    q = urllib.parse.quote_plus(keyword)
    with chrome_lock(_HUNTER_PORT, on_wait=_log):
        _chrome.navigate(ws, f"https://x.com/search?q={q}&src=typed_query&f=live", wait=4.0)
        time.sleep(1.5)
        _chrome.eval_js(ws, "window.scrollBy(0, 600)")
        time.sleep(1.5)
        raw = _chrome.eval_js(ws, SEARCH_RESULTS_JS)
    if not raw:
        return []
    try:
        candidates = json.loads(raw)
    except Exception:
        return []
    for c in candidates:
        c["replies"]     = _parse_count(c.get("repliesTxt", ""))
        c["likes"]       = _parse_count(c.get("likesTxt", ""))
        c["age_minutes"] = _age_minutes_from_datetime(c.get("datetime", ""))
    return candidates[:limit]


# ── filters & caps ────────────────────────────────────────────────────────────

API_BALANCE_ALERT_COOLDOWN_SEC = 2 * 3600   # don't re-alert within 2h


def _maybe_notify_api_balance(e: Exception, seen: dict):
    """Fire a Telegram alert when generation errors out due to Anthropic
    API credit exhaustion. Cooldown via seen['_last_api_balance_alert']
    (persisted, so it survives daemon restarts)."""
    msg = str(e).lower()
    if "credit balance is too low" not in msg and "credit balance" not in msg:
        return
    if not _TELEGRAM_ENABLED:
        return
    last_alert = float(seen.get("_last_api_balance_alert", 0) or 0)
    if time.time() - last_alert < API_BALANCE_ALERT_COOLDOWN_SEC:
        return
    try:
        import telegram as _tg
        _tg.send_text(
            "🛑 *Anthropic API credit exhausted.*\n\n"
            "Reply generation is failing — no candidates will be queued until "
            "credits are topped up at https://console.anthropic.com/settings/billing\n\n"
            "_Daemon keeps sweeping; will auto-resume when credits return._"
        )
        seen["_last_api_balance_alert"] = time.time()
        _log("api-balance alert sent to telegram")
    except Exception as send_e:
        _log(f"  api-balance alert send failed: {send_e}")


def _maybe_notify_cap_hit(daily: dict, today: str, cfg: dict):
    """Fire one Telegram alert per day when daily total reaches the reply cap.
    The `_cap_alerted` flag lives inside the per-day `daily` dict, so day
    rollover naturally clears it."""
    cap = cfg["daily_caps"]["total_replies"]
    if daily["total"] < cap or daily.get("_cap_alerted"):
        return
    if not _TELEGRAM_ENABLED:
        return
    try:
        import telegram as _tg
        _tg.send_text(
            f"🛑 *Hunter daily reply cap hit* — {daily['total']}/{cap} for {today}.\n"
            f"Engage daemon will skip all candidates until tomorrow."
        )
        daily["_cap_alerted"] = True
        _log(f"daily-cap alert sent to telegram ({daily['total']}/{cap})")
    except Exception as e:
        _log(f"  cap alert send failed: {e}")


def should_skip(post: dict, target: str, filters: dict, caps_state: dict, cfg: dict) -> str:
    """Return empty string to proceed, or a reason string to skip."""
    if not post.get("id"):
        return "no post id"
    if post.get("age_minutes", 9999) > filters["max_post_age_minutes"]:
        return f"too old ({post['age_minutes']}m)"
    if post.get("replies", 0) > filters["max_existing_replies"]:
        return f"too crowded ({post['replies']} replies)"
    if filters.get("skip_link_only") and len(post.get("text", "").strip()) < filters.get("min_post_length", 30):
        return "too short / link-only"

    today = date.today().isoformat()
    daily = caps_state.setdefault(today, {"total": 0, "per_target": {}})
    if daily["total"] >= cfg["daily_caps"]["total_replies"]:
        return f"daily total cap ({daily['total']})"
    if daily["per_target"].get(target, 0) >= cfg["daily_caps"]["per_target"]:
        return f"per-target cap (@{target})"

    return ""


def _looks_english(text: str) -> bool:
    """Conservative language gate for keyword replies.

    X `lang:en` search does most of the work. This catches obvious non-English
    posts that still appear in search cards before any generation/autopost.
    """
    raw = re.sub(r"https?://\S+", " ", text or "")
    raw = re.sub(r"[@#][A-Za-z0-9_]+", " ", raw)
    letters = [ch for ch in raw if ch.isalpha()]
    if len(letters) < 3:
        return False
    ascii_letters = sum(1 for ch in letters if ("a" <= ch.lower() <= "z"))
    if ascii_letters / max(1, len(letters)) < 0.82:
        return False

    words = re.findall(r"[A-Za-z][A-Za-z']+", raw.lower())
    if len(words) < 3:
        return ascii_letters / max(1, len(letters)) >= 0.95

    english_markers = {
        "a", "an", "and", "are", "as", "at", "be", "because", "but", "by",
        "can", "for", "from", "how", "if", "in", "is", "it", "just", "not",
        "of", "on", "or", "that", "the", "this", "to", "was", "we", "what",
        "when", "where", "which", "who", "why", "will", "with", "you", "your",
    }
    domain_markers = {
        "agent", "agents", "ai", "automation", "builder", "builders", "code",
        "coding", "customer", "customers", "ecommerce", "gtm", "model",
        "models", "sales", "startup", "support", "workflow", "workflows",
    }
    unique = set(words)
    return bool(unique & english_markers) or len(unique & domain_markers) >= 2


def _quality_gate_reason(entry: dict, cfg: dict) -> str:
    """Return a skip reason if the scored entry fails the configured quality gate."""
    qcfg = cfg.get("quality_gate", {})
    if not qcfg.get("enabled", False):
        return ""
    blocked = {str(x).strip().lower() for x in qcfg.get("block_labels", ["low"])}
    label = str(entry.get("predicted_engagement", "")).strip().lower()
    conf = int(entry.get("prediction_confidence", 0) or 0)
    min_conf = int(qcfg.get("min_confidence", 1) or 1)
    if label in blocked and conf >= min_conf:
        reasons = entry.get("prediction_reasons", []) or []
        why = " · ".join(reasons[:2]) if reasons else "scorer predicted low engagement"
        return f"quality gate: {label} ({why})"
    return ""


def _rollback_daily_reply(daily: dict, target: str):
    daily["total"] = max(0, int(daily.get("total", 0) or 0) - 1)
    per_target = daily.setdefault("per_target", {})
    cur = int(per_target.get(target, 0) or 0)
    if cur <= 1:
        per_target.pop(target, None)
    else:
        per_target[target] = cur - 1


def _browser_public_actions_enabled(cfg: dict) -> bool:
    return bool(cfg.get("browser_public_actions_enabled", True))


def _btcmind_autonomy_block(kind: str, entry: dict, cfg: dict, require_autonomy: bool = True) -> str:
    decision = _btcmind.check_entry(kind, entry, cfg, require_autonomy=require_autonomy)
    if decision.get("policy_applied"):
        entry["safety_verdict"] = "approve" if decision.get("ok") else "block"
        entry["safety_reason"] = decision.get("reason", "")
        entry["safety_issues"] = decision.get("issues", [])
        entry["safety_policy"] = "btcmind_autonomy"
    if not decision.get("ok"):
        return str(decision.get("reason") or "BTCMind autonomy safety block")
    return ""


# ── core cycle ────────────────────────────────────────────────────────────────

def _stride_for_misses(misses: int) -> int:
    """How many cycles to skip before next check, given consecutive misses.
    misses 0-3: every cycle. 4-7: every 2nd. 8-15: every 4th. 16+: every 8th."""
    if misses < 4:  return 1
    if misses < 8:  return 2
    if misses < 16: return 4
    return 8


def _process_target(ws, target: str, cfg: dict, seen: dict, queue: list,
                    caps_state: dict, dry_run: bool):
    blocked = {str(x).lower() for x in cfg.get("blocked_handles", [])}
    if target.lower() in blocked:
        _log(f"  → @{target} [skip: blocked_handle]")
        return

    # Active-target prioritization: skip targets that have been dormant for
    # N consecutive cycles, so we don't waste Chrome time hitting sleepy
    # profiles every cycle. State is per-target in seen["target_strides"].
    cycle_count = seen.get("_cycle_count", 0)
    strides = seen.setdefault("target_strides", {})
    ts = strides.get(target, {"misses": 0, "next_check_cycle": 0})
    disable_dormant_skip = bool(cfg.get("polling", {}).get("disable_dormant_skip", False))
    if (not disable_dormant_skip) and cycle_count < ts.get("next_check_cycle", 0):
        stride = _stride_for_misses(ts.get("misses", 0))
        _log(f"  → @{target} [skip: dormant — next check in {ts['next_check_cycle'] - cycle_count} cycle(s), stride={stride}]")
        return

    _log(f"  → @{target}")
    try:
        post = fetch_latest_post(ws, target)
    except Exception as e:
        _log(f"    fetch error: {e}")
        return
    if not post:
        _log(f"    no post extractable")
        # Treat as a miss for stride purposes
        ts["misses"] = ts.get("misses", 0) + 1
        ts["next_check_cycle"] = cycle_count + _stride_for_misses(ts["misses"])
        strides[target] = ts
        return

    pid = post["id"]
    seen_set = seen.setdefault("post_ids", {})
    if pid in seen_set:
        _log(f"    already seen {pid}")
        ts["misses"] = ts.get("misses", 0) + 1
        ts["next_check_cycle"] = cycle_count + _stride_for_misses(ts["misses"])
        strides[target] = ts
        return
    seen_set[pid] = {"target": target, "first_seen": _ts(), "url": post["url"]}
    # New post seen — reset stride, check again next cycle
    strides[target] = {"misses": 0, "next_check_cycle": cycle_count + 1}

    if _is_btcmind_cfg(cfg):
        like_publish_decision = _btcmind_publish_decision(
            cfg,
            action="like",
            lane="target",
            live_requested=bool(cfg.get("auto_like")) and not dry_run,
        )
        like_can_publish = like_publish_decision["action"] == "browser_publish"
    else:
        like_can_publish = _browser_public_actions_enabled(cfg)
    if cfg.get("auto_like") and like_can_publish and not dry_run:
        try:
            r = _engage.like_tweet(_HUNTER_PORT, post["url"])
            _log(f"    like → {'ok' if r.get('ok') else r.get('error','fail')}")
        except Exception as e:
            _log(f"    like error (non-fatal): {e}")

    skip = should_skip(post, target, cfg["filters"], caps_state, cfg)
    if skip:
        _log(f"    skip: {skip}")
        return

    _log(f"    candidate: {post['url']}  age={post['age_minutes']}m  replies={post['replies']}")
    if dry_run:
        _log(f"    [dry-run] would generate + queue")
        return

    try:
        g, reply_audit, quality_block = _generate_btcmind_reply_with_audit(
            target,
            post["text"],
            cfg,
            queue,
        )
    except Exception as e:
        _log(f"    generation error: {e}")
        _maybe_notify_api_balance(e, seen)
        return
    reply_text = g.get("reply", "")
    if quality_block:
        _log(f"    skip: {quality_block}")
        return

    today = date.today().isoformat()
    daily = caps_state.setdefault(today, {"total": 0, "per_target": {}})
    daily["total"] += 1
    daily["per_target"][target] = daily["per_target"].get(target, 0) + 1

    entry = {
        "id":           pid,
        "account":      cfg.get("hunter_handle", ""),
        "target":       target,
        "target_url":   post["url"],
        "target_text":  post["text"],
        "reply_text":   reply_text,
        "op_summary":   g.get("op_summary", ""),
        "reply_angle":  g.get("reply_angle", ""),
        "source":       "target",
        "status":       "pending",
        "queued_at":    _ts(),
        "post_age_min": post["age_minutes"],
        "post_replies": post["replies"],
    }
    if reply_audit:
        entry["btcmind_reply_audit"] = reply_audit
        entry["reply_risk_class"] = reply_audit.get("reply_risk_class", "")
        entry["reply_quality_score"] = reply_audit.get("score", 0)
        entry["thread_context_score"] = reply_audit.get("thread_context_score", 0)
        entry["generic_template_score"] = reply_audit.get("generic_template_score", 0)
        entry["reply_structure_signature"] = reply_audit.get("reply_structure_signature", "")

    try:
        import reply_scorer as _rs
        _rs.score_entry(entry)
    except Exception as _e:
        _log(f"    scorer error (non-fatal): {_e}")
    gate_skip = _quality_gate_reason(entry, cfg)
    if gate_skip:
        _rollback_daily_reply(daily, target)
        _log(f"    skip: {gate_skip}")
        return

    approval_mode = str(cfg.get("approval_mode", "telegram")).strip().lower()
    if _reply_quality_enabled(cfg) and approval_mode == "auto_post" and entry.get("reply_risk_class") != "auto_ok":
        _log(f"    {_reply_quality_label(cfg)} reply audit requires review: {entry.get('reply_risk_class')}")
        approval_mode = "telegram"
    if approval_mode == "auto_post" and not _browser_public_actions_enabled(cfg):
        _log("    browser public actions disabled; queueing instead of auto-posting")
        approval_mode = "telegram"
    safety_block = _btcmind_autonomy_block("reply", entry, cfg, require_autonomy=(approval_mode == "auto_post"))
    if safety_block:
        _rollback_daily_reply(daily, target)
        entry["status"] = "blocked_safety"
        _append_queue_locked(entry, queue)
        _log(f"    skip: BTCMind autonomy safety gate: {safety_block}")
        return
    if approval_mode == "auto_post":
        entry["status"] = "posting"
        reserved = _reserve_queue_entry_locked(entry, queue)
        if not reserved:
            _log(f"    duplicate queue entry skipped: {entry.get('id')}")
            _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
            _maybe_notify_cap_hit(daily, today, cfg)
            return
        try:
            res = _engage.reply_tweet(
                _HUNTER_PORT,
                post["url"],
                reply_text,
                dry_run=False,
                self_handle=_x_handle(cfg),
            )
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            entry["status"] = "posted"
            entry["posted_at"] = _ts()
            if res.get("reply_url"):
                entry["reply_url_actual"] = res["reply_url"]
            _update_queue_entry_locked(entry["id"], {
                "status": entry["status"],
                "posted_at": entry["posted_at"],
                "reply_url_actual": entry.get("reply_url_actual", ""),
            })
            _log(f"    auto-posted ({len(reply_text)} chars): \"{reply_text[:80]}...\"")
        else:
            entry["status"] = "post_failed"
            entry["error"] = res.get("error", "")
            _update_queue_entry_locked(entry["id"], {
                "status": entry["status"],
                "error": entry["error"],
            })
            _log(f"    auto-post FAILED: {entry['error']}")
        _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
        _maybe_notify_cap_hit(daily, today, cfg)
        return

    appended = _append_queue_locked(entry, queue)
    if not appended:
        _log(f"    duplicate queue entry skipped: {entry.get('id')}")
        _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
        _maybe_notify_cap_hit(daily, today, cfg)
        return

    # Push to Telegram for mobile approval (if configured)
    try:
        import telegram as _tg
        msg_id = _tg.send_reply_card(entry, bot_token=_TG_BOT_TOKEN, chat_id=_TG_CHAT_ID)
        if msg_id:
            entry["telegram_message_id"] = msg_id
            _update_queue_entry_locked(entry["id"], {"telegram_message_id": msg_id})
    except Exception as e:
        _log(f"    telegram notify failed (non-fatal): {e}")

    _log(f"    queued + notified ({len(reply_text)} chars): \"{reply_text[:80]}...\"")
    _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
    _maybe_notify_cap_hit(daily, today, cfg)


def _process_keyword_candidate(ws, candidate: dict, kw: str, cfg: dict,
                                seen: dict, queue: list, caps_state: dict,
                                dry_run: bool) -> bool:
    """Process one keyword-discovered post. Returns True if queued."""
    target_handles = {t.lower() for t in cfg.get("target_accounts", [])}
    kw_filters     = cfg["keyword_engage"]["filters"]
    base_filters   = cfg["filters"]

    author = candidate["author"]
    if not author:
        return False
    blocked = {str(x).lower() for x in cfg.get("blocked_handles", [])}
    if author.lower() in blocked:
        _log(f"    skip @{author}: blocked_handle")
        return False
    if kw_filters.get("skip_targets") and author.lower() in target_handles:
        _log(f"    skip @{author}: is target"); return False
    if candidate["age_minutes"] > kw_filters.get("max_post_age_minutes", 60):
        _log(f"    skip @{author}: too old ({candidate['age_minutes']}m)"); return False
    if candidate["likes"] < kw_filters.get("min_post_likes", 3):
        _log(f"    skip @{author}: too few likes ({candidate['likes']})"); return False
    if len(candidate["text"].strip()) < base_filters.get("min_post_length", 30):
        _log(f"    skip @{author}: too short"); return False
    if (kw_filters.get("require_english") or kw_filters.get("skip_non_english")) and not _looks_english(candidate.get("text", "")):
        _log(f"    skip @{author}: non-English original post"); return False
    if candidate["replies"] > base_filters.get("max_existing_replies", 100):
        _log(f"    skip @{author}: too many replies ({candidate['replies']})"); return False

    pid = candidate["id"]
    seen_set = seen.setdefault("post_ids", {})
    if pid in seen_set:
        return False
    seen_set[pid] = {"author": author, "source": "keyword",
                     "kw": kw, "first_seen": _ts(), "url": candidate["url"]}

    # Reuse should_skip just for daily-cap enforcement (post fields it cares
    # about are already validated above).
    post_for_skip = {
        "id": pid, "age_minutes": candidate["age_minutes"],
        "replies": candidate["replies"], "text": candidate["text"],
    }
    skip_filters = {**base_filters, "max_post_age_minutes": kw_filters.get(
        "max_post_age_minutes",
        base_filters.get("max_post_age_minutes", 60),
    )}
    skip = should_skip(post_for_skip, author, skip_filters, caps_state, cfg)
    if skip:
        _log(f"    @{author}: {skip}")
        return False

    _log(f"    candidate @{author}: {candidate['url']}  age={candidate['age_minutes']}m  "
         f"likes={candidate['likes']}  replies={candidate['replies']}")
    if dry_run:
        _log(f"    [dry-run] would generate")
        return False

    try:
        g, reply_audit, quality_block = _generate_btcmind_reply_with_audit(
            author,
            candidate["text"],
            cfg,
            queue,
        )
    except Exception as e:
        _log(f"    generation error: {e}")
        _maybe_notify_api_balance(e, seen)
        return False
    reply_text = g.get("reply", "")
    if quality_block:
        _log(f"    skip: {quality_block}")
        return False

    today = date.today().isoformat()
    daily = caps_state.setdefault(today, {"total": 0, "per_target": {}})
    daily["total"] += 1
    daily["per_target"][author] = daily["per_target"].get(author, 0) + 1

    entry = {
        "id":           pid,
        "account":      cfg.get("hunter_handle", ""),
        "target":       author,
        "target_url":   candidate["url"],
        "target_text":  candidate["text"],
        "reply_text":   reply_text,
        "op_summary":   g.get("op_summary", ""),
        "reply_angle":  g.get("reply_angle", ""),
        "source":       "keyword",
        "source_keyword": kw,
        "status":       "pending",
        "queued_at":    _ts(),
        "post_age_min": candidate["age_minutes"],
        "post_replies": candidate["replies"],
        "post_likes":   candidate["likes"],
    }
    if reply_audit:
        entry["btcmind_reply_audit"] = reply_audit
        entry["reply_risk_class"] = reply_audit.get("reply_risk_class", "")
        entry["reply_quality_score"] = reply_audit.get("score", 0)
        entry["thread_context_score"] = reply_audit.get("thread_context_score", 0)
        entry["generic_template_score"] = reply_audit.get("generic_template_score", 0)
        entry["reply_structure_signature"] = reply_audit.get("reply_structure_signature", "")
    try:
        import reply_scorer as _rs
        _rs.score_entry(entry)
    except Exception as _e:
        _log(f"    scorer error (non-fatal): {_e}")
    gate_skip = _quality_gate_reason(entry, cfg)
    if gate_skip:
        _rollback_daily_reply(daily, author)
        _log(f"    skip: {gate_skip}")
        return False

    approval_mode = str(cfg.get("approval_mode", "telegram")).strip().lower()
    if _reply_quality_enabled(cfg) and approval_mode == "auto_post" and entry.get("reply_risk_class") != "auto_ok":
        _log(f"    {_reply_quality_label(cfg)} reply audit requires review: {entry.get('reply_risk_class')}")
        approval_mode = "telegram"
    if approval_mode == "auto_post" and not _browser_public_actions_enabled(cfg):
        _log("    browser public actions disabled; queueing instead of auto-posting")
        approval_mode = "telegram"
    safety_block = _btcmind_autonomy_block("reply", entry, cfg, require_autonomy=(approval_mode == "auto_post"))
    if safety_block:
        _rollback_daily_reply(daily, author)
        entry["status"] = "blocked_safety"
        _append_queue_locked(entry, queue)
        _log(f"    skip: BTCMind autonomy safety gate: {safety_block}")
        return False
    if approval_mode == "auto_post":
        entry["status"] = "posting"
        reserved = _reserve_queue_entry_locked(entry, queue)
        if not reserved:
            _log(f"    duplicate queue entry skipped: {entry.get('id')}")
            _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
            _maybe_notify_cap_hit(daily, today, cfg)
            return False
        try:
            res = _engage.reply_tweet(
                _HUNTER_PORT,
                candidate["url"],
                reply_text,
                dry_run=False,
                self_handle=_x_handle(cfg),
            )
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            entry["status"] = "posted"
            entry["posted_at"] = _ts()
            if res.get("reply_url"):
                entry["reply_url_actual"] = res["reply_url"]
            _update_queue_entry_locked(entry["id"], {
                "status": entry["status"],
                "posted_at": entry["posted_at"],
                "reply_url_actual": entry.get("reply_url_actual", ""),
            })
            _log(f"    auto-posted ({len(reply_text)} chars)")
        else:
            entry["status"] = "post_failed"
            entry["error"] = res.get("error", "")
            _update_queue_entry_locked(entry["id"], {
                "status": entry["status"],
                "error": entry["error"],
            })
            _log(f"    auto-post FAILED: {entry['error']}")
        _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
        _maybe_notify_cap_hit(daily, today, cfg)
        return bool(res.get("ok"))

    # Keyword candidates come from search results — search cards don't show
    # the author's follower count, so the Telegram card would be missing the
    # ` · 12K followers` line that target-source replies have. Populate the
    # cache by visiting the author's profile once. Cached author_info means
    # this happens at most once per author per session.
    try:
        import author_info as _ai
        if author and _ai.followers(author) is None:
            _chrome.navigate(ws, f"https://x.com/{author}", wait=2.5)
            f_raw = _chrome.eval_js(ws, FOLLOWERS_JS)
            if f_raw and f_raw.isdigit():
                _ai.set_followers(author, int(f_raw))
                _log(f"    cached follower count for @{author}: {f_raw}")
    except Exception as _e:
        _log(f"    follower lookup failed (non-fatal): {_e}")

    appended = _append_queue_locked(entry, queue)
    if not appended:
        _log(f"    duplicate queue entry skipped: {entry.get('id')}")
        _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
        _maybe_notify_cap_hit(daily, today, cfg)
        return False

    try:
        import telegram as _tg
        msg_id = _tg.send_reply_card(entry, bot_token=_TG_BOT_TOKEN, chat_id=_TG_CHAT_ID)
        if msg_id:
            entry["telegram_message_id"] = msg_id
            _update_queue_entry_locked(entry["id"], {"telegram_message_id": msg_id})
    except Exception as e:
        _log(f"    telegram notify failed: {e}")

    _log(f"    queued + notified ({len(reply_text)} chars)")
    _log(f"    daily total {daily['total']}/{cfg['daily_caps']['total_replies']}")
    _maybe_notify_cap_hit(daily, today, cfg)
    return True


def _keyword_sweep(ws, cfg: dict, seen: dict, queue: list,
                    caps_state: dict, dry_run: bool):
    """Pick `keywords_per_sweep` keywords (rotating), search each, process top results."""
    kw_cfg = cfg.get("keyword_engage", {})
    if not kw_cfg.get("enabled"):
        return
    all_kws  = kw_cfg.get("keywords", [])
    if not all_kws:
        return
    per_sweep = kw_cfg.get("keywords_per_sweep", 3)
    per_kw    = kw_cfg.get("results_per_keyword", 5)
    require_english = bool((kw_cfg.get("filters") or {}).get("require_english") or (kw_cfg.get("filters") or {}).get("skip_non_english"))
    delay_lo, delay_hi = cfg["polling"]["per_target_delay_sec_range"]

    rot = seen.setdefault("_kw_rotation", {"idx": 0})
    keywords = [all_kws[(rot["idx"] + i) % len(all_kws)] for i in range(per_sweep)]
    rot["idx"] = (rot["idx"] + per_sweep) % len(all_kws)

    _log(f"keyword sweep — {len(keywords)} keyword(s): {keywords}")
    for i, kw in enumerate(keywords):
        if _stop:
            return
        _log(f"  ⌕ {kw}")
        try:
            search_kw = kw
            if require_english and "lang:" not in kw.lower():
                search_kw = f"{kw} lang:en"
            candidates = fetch_keyword_candidates(ws, search_kw, per_kw)
        except Exception as e:
            _log(f"    search error: {e}")
            continue
        if not candidates:
            _log(f"    no results")
            continue
        _log(f"    {len(candidates)} candidates")
        # Only queue one reply per keyword search — avoids spamming on a
        # popular term and lets the next sweep pick up the next.
        queued = False
        for c in candidates:
            if _stop:
                return
            if _process_keyword_candidate(ws, c, kw, cfg, seen, queue, caps_state, dry_run):
                queued = True
                break
        if not queued:
            _log(f"    all {len(candidates)} candidates filtered out")
        if i < len(keywords) - 1:
            time.sleep(random.uniform(delay_lo, delay_hi))


def cycle(ws, cfg: dict, seen: dict, queue: list, caps_state: dict,
          targets: list, dry_run: bool):
    """One full cycle: login check → target sweep → keyword sweep."""
    # Always re-bind to the module WS — _recover_frozen_tab() may have swapped
    # it between cycles.
    if _WS is not None:
        ws = _WS
    if not _check_login_and_alert(ws, seen, expected_handle=_x_handle(cfg)):
        _log("hunter logged out — skipping sweep")
        _save_json(SEEN_PATH, {**seen, "_caps": caps_state})
        return

    seen["_cycle_count"] = seen.get("_cycle_count", 0) + 1
    _log(f"target sweep — {len(targets)} targets (cycle #{seen['_cycle_count']})")
    delay_lo, delay_hi = cfg["polling"]["per_target_delay_sec_range"]
    for i, target in enumerate(targets):
        if _stop:
            return
        _process_target(ws, target, cfg, seen, queue, caps_state, dry_run)
        _save_json(SEEN_PATH, {**seen, "_caps": caps_state})
        if i < len(targets) - 1:
            time.sleep(random.uniform(delay_lo, delay_hi))
    _log("target sweep done")

    if not _stop:
        try:
            _keyword_sweep(ws, cfg, seen, queue, caps_state, dry_run)
        except Exception as e:
            _log(f"keyword sweep error: {e}")
        _save_json(SEEN_PATH, {**seen, "_caps": caps_state})
        _log("keyword sweep done")


def _handle_sigterm(signum, frame):
    global _stop
    _stop = True


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once",   action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target", default=None, help="only check this target this run")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    targets = [args.target] if args.target else cfg["target_accounts"]
    port    = cfg["hunter_port"]
    handle  = cfg["hunter_handle"]
    x_handle = _x_handle(cfg)
    profile_cfg = str(cfg.get("chrome_profile_dir") or cfg.get("chrome_profile") or "").strip()
    profile_dir = ""
    if profile_cfg:
        profile_dir = profile_cfg if os.path.isabs(profile_cfg) else os.path.join(ROOT_DIR, "chrome-profiles", profile_cfg)

    global _HUNTER_PORT, _WS, _TG_BOT_TOKEN, _TG_CHAT_ID, LOG_DIR, SEEN_PATH, _TELEGRAM_ENABLED
    _HUNTER_PORT = port

    # Per-account state and log paths — avoids collision when multiple accounts run
    LOG_DIR   = os.path.join(ROOT_DIR, "logs", f"engage_{handle.lower()}")
    SEEN_PATH = os.path.join(STATE_DIR, f"engage_seen_{handle.lower()}.json")

    approval_mode = str(cfg.get("approval_mode", "telegram")).strip().lower()
    tg_cfg = cfg.get("telegram", {})
    if tg_cfg:
        _TG_BOT_TOKEN = os.environ.get(tg_cfg.get("bot_token_env", ""), "")
        _TG_CHAT_ID   = os.environ.get(tg_cfg.get("chat_id_env", ""), "")
    # In direct-post mode, hard-disable all Telegram sends (cards + alerts).
    if approval_mode == "auto_post" and _browser_public_actions_enabled(cfg):
        _TELEGRAM_ENABLED = False
        _TG_BOT_TOKEN = ""
        _TG_CHAT_ID = ""

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT,  _handle_sigterm)

    _log(f"engage daemon starting — {len(targets)} targets, account={handle}, x=@{x_handle}, dry_run={args.dry_run}")

    if not _ensure_chrome(port, handle, profile_dir):
        _log("hunter chrome unavailable — abort")
        sys.exit(1)

    ws = _chrome.connect(port)
    _WS = ws  # share with _recover_frozen_tab(); rebound there on tab swap
    # NOTE: previously called _chrome.set_viewport(ws, 1280, 1600) here for
    # virtualized-rendering, but it makes the window unusable when the user
    # needs to manually interact (e.g. log in after X kicks Hunter out).
    # bring_to_front in chrome.navigate() handles the visibility issue alone.

    seen   = _load_json(SEEN_PATH, {})
    caps_state = seen.pop("_caps", {}) if isinstance(seen.get("_caps"), dict) else {}
    queue  = _load_json(QUEUE_PATH, [])

    poll_lo, poll_hi = cfg["polling"]["sweep_interval_sec_range"]

    try:
        while not _stop:
            try:
                cycle(ws, cfg, seen, queue, caps_state, targets, args.dry_run)
            except Exception as e:
                _log(f"cycle error: {e}")
            if args.once or _stop:
                break
            wait = random.randint(poll_lo, poll_hi)
            _log(f"next cycle in {wait}s")
            end = time.time() + wait
            while not _stop and time.time() < end:
                time.sleep(min(2, end - time.time()))
    finally:
        ws.close()
        _save_json(SEEN_PATH, {**seen, "_caps": caps_state})
        _log("engage daemon stopped")


if __name__ == "__main__":
    main()
