#!/usr/bin/env python3
"""Read-only Flatkey keyword collector.

Searches X with the existing Flatkey keyword-engage config, applies the same
basic filters, and appends accepted tweet links to an HTML watchlist. This
script never queues drafts and never performs public X actions.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import chrome as _chrome
from lock import chrome_lock


DEFAULT_CONFIG = ROOT / "accounts" / "flatkey" / "engage_config.json"
DEFAULT_STATE = ROOT / "state" / "flatkey_keyword_links.json"
DEFAULT_HTML = ROOT / "state" / "flatkey_keyword_links.html"


SEARCH_RESULTS_JS = r"""
(function() {
    function outerArticles() {
        return Array.prototype.slice.call(document.querySelectorAll('article[data-testid="tweet"]'))
            .filter(function(el) {
                return !(el.parentElement && el.parentElement.closest('article[data-testid="tweet"]'));
            });
    }
    function sameArticleNodes(el, selector) {
        return Array.prototype.slice.call(el.querySelectorAll(selector))
            .filter(function(node) {
                return node.closest('article[data-testid="tweet"]') === el;
            });
    }
    function firstSameArticleNode(el, selector) {
        var nodes = sameArticleNodes(el, selector);
        return nodes.length ? nodes[0] : null;
    }
    function articleStatusLink(el, timeEl) {
        var link = timeEl ? timeEl.closest('a[href*="/status/"]') : null;
        if (link && link.closest('article[data-testid="tweet"]') === el) return link;
        return firstSameArticleNode(el, 'a[href*="/status/"]');
    }
    function metric(el, testid) {
        var node = firstSameArticleNode(el, '[data-testid="' + testid + '"]');
        return node ? ((node.innerText || node.getAttribute('aria-label') || '').replace(/[^0-9KMB.,]/g, '')) : '';
    }
    var arts = outerArticles();
    var out = [];
    arts.forEach(function(el) {
        var head = (el.innerText || '').slice(0, 300);
        if (/reposted/i.test(head)) return;
        if (/Replying to/i.test(head)) return;
        if (/Promoted/i.test(head)) return;

        var textEl = firstSameArticleNode(el, '[data-testid="tweetText"]');
        var text = textEl ? textEl.innerText.trim() : '';
        if (!text) return;

        var timeEl = firstSameArticleNode(el, 'time');
        var link = articleStatusLink(el, timeEl);
        if (!link || !link.href) return;

        var m = link.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
        if (!m) return;

        out.push({
            author: m[1],
            id: m[2],
            url: 'https://x.com/' + m[1] + '/status/' + m[2],
            text: text.slice(0, 900),
            repliesTxt: metric(el, 'reply'),
            likesTxt: metric(el, 'like'),
            repostsTxt: metric(el, 'retweet'),
            datetime: timeEl ? (timeEl.getAttribute('datetime') || '') : ''
        });
    });
    return JSON.stringify(out);
})()
"""


PROFILE_FOLLOWERS_JS = r"""
(function() {
    function parseCount(s) {
        s = (s || '').replace(/,/g, '').trim();
        var mult = 1;
        if (/[Kk]$/.test(s)) { mult = 1000; s = s.slice(0, -1); }
        else if (/[Mm]$/.test(s)) { mult = 1000000; s = s.slice(0, -1); }
        else if (/[Bb]$/.test(s)) { mult = 1000000000; s = s.slice(0, -1); }
        var n = parseFloat(s);
        return isNaN(n) ? 0 : Math.round(n * mult);
    }
    var links = document.querySelectorAll('a[href$="/followers"], a[href$="/verified_followers"], a[href*="/followers"]');
    for (var i = 0; i < links.length; i++) {
        var txt = links[i].innerText || links[i].textContent || '';
        var parent = links[i].parentElement ? links[i].parentElement.innerText : '';
        var blob = (txt + ' ' + parent).replace(/\s+/g, ' ');
        var m = blob.match(/([0-9][0-9,.]*[KMBkmb]?)\s*Followers?/i);
        if (m) {
            return JSON.stringify({followers: parseCount(m[1]), followers_raw: m[1]});
        }
    }
    return JSON.stringify({followers: 0, followers_raw: ''});
})()
"""


def now_local() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_utc_compact() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_count(value: str) -> int:
    if not value:
        return 0
    raw = str(value).strip().replace(",", "")
    mult = 1
    if raw and raw[-1] in "Kk":
        raw = raw[:-1]
        mult = 1_000
    elif raw and raw[-1] in "Mm":
        raw = raw[:-1]
        mult = 1_000_000
    elif raw and raw[-1] in "Bb":
        raw = raw[:-1]
        mult = 1_000_000_000
    try:
        return int(float(raw or "0") * mult)
    except ValueError:
        return 0


def as_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def age_minutes_from_datetime(value: str) -> int:
    if not value:
        return 9999
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(0, int((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 60))
    except Exception:
        return 9999


def compact_to_datetime(value: str):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def looks_english(text: str) -> bool:
    raw = re.sub(r"https?://\S+", " ", text or "")
    raw = re.sub(r"[@#][A-Za-z0-9_]+", " ", raw)
    letters = [ch for ch in raw if ch.isalpha()]
    if len(letters) < 3:
        return False
    ascii_letters = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
    if ascii_letters / max(1, len(letters)) < 0.82:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z']+", raw.lower())
    if len(words) < 3:
        return ascii_letters / max(1, len(letters)) >= 0.95
    markers = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
        "for", "from", "how", "if", "in", "is", "it", "not", "of", "on",
        "or", "that", "the", "this", "to", "was", "we", "what", "when",
        "where", "which", "who", "why", "will", "with", "you", "your",
    }
    domain = {
        "agent", "agents", "ai", "api", "builder", "builders", "code",
        "coding", "context", "cost", "credits", "llm", "model", "models",
        "prompt", "routing", "token", "tokens", "workflow", "workflows",
    }
    unique = set(words)
    return bool(unique & markers) or len(unique & domain) >= 2


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def current_page_login_state(port: int) -> dict:
    ws = _chrome.connect(port)
    try:
        raw = _chrome.eval_js(ws, r"""
            JSON.stringify({
                signed_in: !!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')
                    || !!document.querySelector('[data-testid="AppTabBar_Profile_Link"]'),
                login_path: /\/(login|i\/flow\/login|signup)/.test(location.pathname),
                title: document.title || '',
                url: location.href || ''
            })
        """)
        return json.loads(raw) if raw else {}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def fetch_keyword_candidates(port: int, keyword: str, limit: int, wait: float) -> list[dict]:
    q = urllib.parse.quote_plus(keyword)
    ws = _chrome.connect(port)
    try:
        with chrome_lock(port, on_wait=lambda msg: print(msg, flush=True)):
            _chrome.navigate(ws, f"https://x.com/search?q={q}&src=typed_query&f=live", wait=wait)
            time.sleep(1.2)
            _chrome.eval_js(ws, "window.scrollBy(0, 700)")
            time.sleep(1.0)
            raw = _chrome.eval_js(ws, SEARCH_RESULTS_JS)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    for row in rows:
        row["replies"] = parse_count(row.get("repliesTxt", ""))
        row["likes"] = parse_count(row.get("likesTxt", ""))
        row["reposts"] = parse_count(row.get("repostsTxt", ""))
        row["age_minutes"] = age_minutes_from_datetime(row.get("datetime", ""))
    return rows[:limit]


def fetch_author_followers(port: int, handle: str, wait: float) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle or ""):
        return {"followers": 0, "followers_raw": "", "error": "invalid handle"}
    ws = _chrome.connect(port)
    try:
        with chrome_lock(port, on_wait=lambda msg: print(msg, flush=True)):
            _chrome.navigate(ws, f"https://x.com/{handle}", wait=max(wait, 4.0))
            time.sleep(1.0)
            raw = _chrome.eval_js(ws, PROFILE_FOLLOWERS_JS)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    return {
        "followers": int(data.get("followers") or 0),
        "followers_raw": str(data.get("followers_raw") or ""),
    }


def cached_author_followers(port: int, handle: str, state: dict, wait: float, run_at: str, ttl_hours: int) -> dict:
    cache = state.setdefault("author_followers", {})
    key = (handle or "").lower()
    cached = cache.get(key) or {}
    fetched = compact_to_datetime(cached.get("fetched_at"))
    if fetched and dt.datetime.now(dt.timezone.utc) - fetched < dt.timedelta(hours=ttl_hours):
        return cached

    try:
        data = fetch_author_followers(port, handle, wait)
    except Exception as exc:
        data = {"followers": 0, "followers_raw": "", "error": str(exc)}
    data.update({"handle": handle, "fetched_at": run_at})
    cache[key] = data
    return data


def filter_reason(row: dict, keyword: str, cfg: dict) -> str:
    target_handles = {str(t).lower() for t in cfg.get("target_accounts", [])}
    blocked = {str(t).lower() for t in cfg.get("blocked_handles", [])}
    base = cfg.get("filters", {})
    kw_filters = cfg.get("keyword_engage", {}).get("filters", {})

    author = str(row.get("author") or "")
    if not author:
        return "missing author"
    if author.lower() in blocked:
        return "blocked handle"
    if kw_filters.get("skip_targets") and author.lower() in target_handles:
        return "target account"
    age_minutes = as_int(row.get("age_minutes"), 9999)
    max_age_minutes = as_int(kw_filters.get("max_post_age_minutes"), 60)
    if age_minutes >= max_age_minutes:
        return f"too old ({age_minutes}m)"
    likes = as_int(row.get("likes"), 0)
    if likes < as_int(kw_filters.get("min_post_likes"), 0):
        return f"too few likes ({likes})"
    if len(str(row.get("text") or "").strip()) < int(base.get("min_post_length", 30)):
        return "too short"
    replies = as_int(row.get("replies"), 0)
    if replies > as_int(base.get("max_existing_replies"), 100):
        return f"too many replies ({replies})"
    if (kw_filters.get("require_english") or kw_filters.get("skip_non_english")) and not looks_english(row.get("text", "")):
        return "non-English"
    return ""


def rotated_keywords(state: dict, cfg: dict) -> tuple[list[str], object]:
    kw_cfg = cfg.get("keyword_engage", {})
    groups = kw_cfg.get("keyword_groups") or {}
    pattern = [str(g).strip() for g in kw_cfg.get("rotation_pattern", []) if str(g).strip()]
    if groups and pattern:
        indices = state.setdefault("keyword_group_indices", {})
        selected = []
        for group_name in pattern:
            group_keywords = [
                str(k).strip()
                for k in groups.get(group_name, [])
                if str(k).strip()
            ]
            if not group_keywords:
                continue
            idx = int(indices.get(group_name, 0) or 0) % len(group_keywords)
            selected.append(group_keywords[idx])
            indices[group_name] = (idx + 1) % len(group_keywords)
        return selected, indices

    keywords = [str(k).strip() for k in kw_cfg.get("keywords", []) if str(k).strip()]
    if not keywords:
        return [], 0
    per = max(1, int(kw_cfg.get("keywords_per_sweep", 8) or 8))
    start = int(state.get("keyword_index", 0) or 0) % len(keywords)
    selected = [keywords[(start + i) % len(keywords)] for i in range(min(per, len(keywords)))]
    return selected, (start + len(selected)) % len(keywords)


def esc(value) -> str:
    return html.escape(str(value or ""))


def render_html(path: Path, state: dict, metadata: dict) -> None:
    rows = list(state.get("rows", []))
    rows.sort(key=lambda r: r.get("collected_at", ""), reverse=True)
    status = metadata.get("status") or state.get("last_status") or "unknown"
    generated = now_local()
    label = str(metadata.get("label") or "Flatkey")
    storage_prefix = str(metadata.get("storage_prefix") or "flatkeyKeywordLinks")
    config_label = str(metadata.get("config_label") or metadata.get("config_path") or "accounts/flatkey/engage_config.json")
    label_js = json.dumps(label)
    storage_prefix_js = json.dumps(storage_prefix)
    tag_prefix_js = json.dumps(re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-") or "keyword-links")

    cards = []
    for row in rows:
        url = esc(row.get("url"))
        text = esc(row.get("text"))
        cards.append(f"""
        <article class="card">
          <div class="meta">
            <span class="kw">{esc(row.get("keyword"))}</span>
            <span>@{esc(row.get("author"))}</span>
            <span>{esc(row.get("author_followers", ""))} followers</span>
            <span>{esc(row.get("age_minutes"))}m old</span>
            <span>{esc(row.get("likes"))} likes</span>
            <span>{esc(row.get("replies"))} replies</span>
            <span>{esc(row.get("collected_at"))}</span>
          </div>
          <a class="url" href="{url}" target="_blank">{url}</a>
          <p>{text}</p>
        </article>
        """)
    body = "\n".join(cards) if cards else "<p>No accepted links yet.</p>"
    notification_script = r"""
  <script>
    (function() {
      var label = __LABEL__;
      var storagePrefix = __STORAGE_PREFIX__;
      var tagPrefix = __TAG_PREFIX__;
      var seenKey = storagePrefix + "SeenIds";
      var enabledKey = storagePrefix + "NotificationsEnabled";
      var button;
      var note;

      function rowsFrom(data) {
        return Array.isArray(data && data.rows) ? data.rows : [];
      }

      function loadSeen() {
        try {
          return new Set(JSON.parse(localStorage.getItem(seenKey) || "[]"));
        } catch (e) {
          return new Set();
        }
      }

      function saveSeen(seen) {
        localStorage.setItem(seenKey, JSON.stringify(Array.from(seen).slice(-500)));
      }

      function notificationsEnabled() {
        return localStorage.getItem(enabledKey) === "1"
          && "Notification" in window
          && Notification.permission === "granted";
      }

      function updateUi() {
        if (!button || !note) return;
        if (!("Notification" in window)) {
          button.disabled = true;
          note.textContent = "Browser notifications unavailable.";
          return;
        }
        if (Notification.permission === "granted") {
          button.textContent = "Notifications enabled";
          note.textContent = "Background tab alerts are on.";
        } else if (Notification.permission === "denied") {
          button.textContent = "Notifications blocked";
          note.textContent = "Enable them in browser site settings.";
        } else {
          button.textContent = "Enable notifications";
          note.textContent = "Alerts fire for newly qualified posts while this tab is open.";
        }
      }

      function notify(row) {
        if (!notificationsEnabled()) return;
        var title = "New " + label + " target: @" + (row.author || "unknown");
        var text = ((row.keyword || "") + " | " + (row.text || "")).slice(0, 160);
        var n = new Notification(title, {
          body: text,
          tag: tagPrefix + "-" + (row.id || row.url || Date.now())
        });
        n.onclick = function() {
          window.open(row.url || "https://x.com", "_blank");
        };
      }

      async function poll() {
        try {
          var res = await fetch("/links.json?ts=" + Date.now(), {cache: "no-store"});
          if (!res.ok) return;
          var data = await res.json();
          var rows = rowsFrom(data);
          var seen = loadSeen();
          if (!localStorage.getItem(seenKey)) {
            rows.forEach(function(row) {
              if (row.id || row.url) seen.add(String(row.id || row.url));
            });
            saveSeen(seen);
            return;
          }
          rows.slice().reverse().forEach(function(row) {
            var id = String(row.id || row.url || "");
            if (!id || seen.has(id)) return;
            seen.add(id);
            notify(row);
          });
          saveSeen(seen);
        } catch (e) {}
      }

      window.addEventListener("DOMContentLoaded", function() {
        button = document.getElementById("notifyButton");
        note = document.getElementById("notifyStatus");
        if (button) {
          button.addEventListener("click", function() {
            if (!("Notification" in window)) return updateUi();
            Notification.requestPermission().then(function(permission) {
              localStorage.setItem(enabledKey, permission === "granted" ? "1" : "0");
              updateUi();
              poll();
            });
          });
        }
        updateUi();
        poll();
        setInterval(poll, 60000);
      });
    })();
  </script>
""".replace("__LABEL__", label_js).replace("__STORAGE_PREFIX__", storage_prefix_js).replace("__TAG_PREFIX__", tag_prefix_js)

    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="60">
  <title>{esc(label)} Keyword Links</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 28px; background: #f6f7f8; color: #171717; }}
    header {{ margin-bottom: 20px; }}
    h1 {{ margin: 0 0 8px; font-size: 26px; }}
    .sub {{ color: #555; line-height: 1.45; }}
    code {{ background: #eceff3; padding: 2px 5px; border-radius: 4px; }}
    .summary {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }}
    .pill {{ background: white; border: 1px solid #d7dce2; border-radius: 999px; padding: 6px 10px; font-size: 13px; }}
    .notify {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    .notify button {{ border: 1px solid #111; background: #111; color: white; border-radius: 6px; padding: 7px 11px; font: inherit; cursor: pointer; }}
    .notify span {{ color: #606a75; font-size: 13px; }}
    .card {{ background: white; border: 1px solid #dfe4ea; border-radius: 8px; padding: 14px 16px; margin: 12px 0; }}
    .meta {{ display: flex; gap: 10px; flex-wrap: wrap; color: #606a75; font-size: 13px; margin-bottom: 8px; }}
    .kw {{ color: #111; font-weight: 700; }}
    .url {{ display: inline-block; margin-bottom: 8px; color: #0757c2; }}
    p {{ white-space: pre-wrap; margin: 0; line-height: 1.45; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(label)} Keyword Links</h1>
    <div class="sub">
      Generated {esc(generated)}. Auto-refreshes every 60s.
      Read-only collector: no draft queue, no Telegram, no likes/reposts/replies.
    </div>
    <div class="summary">
      <span class="pill">status: {esc(status)}</span>
      <span class="pill">links: {len(rows)}</span>
      <span class="pill">last run: {esc(metadata.get("run_at") or state.get("last_run_at"))}</span>
      <span class="pill">config: <code>{esc(config_label)}</code></span>
    </div>
    <div class="notify">
      <button id="notifyButton" type="button">Enable notifications</button>
      <span id="notifyStatus"></span>
    </div>
  </header>
  {body}
{notification_script}
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--html", default=str(DEFAULT_HTML))
    parser.add_argument("--label", default="Flatkey")
    parser.add_argument("--storage-prefix", default="flatkeyKeywordLinks")
    parser.add_argument("--wait", type=float, default=4.0)
    parser.add_argument("--max-new", type=int, default=80)
    args = parser.parse_args()

    cfg_path = Path(args.config)
    state_path = Path(args.state)
    html_path = Path(args.html)
    cfg = load_json(cfg_path, {})
    state = load_json(state_path, {"rows": [], "seen_ids": {}, "runs": []})
    run_at = now_utc_compact()
    metadata = {
        "run_at": run_at,
        "label": args.label,
        "storage_prefix": args.storage_prefix,
        "config_path": str(cfg_path),
        "config_label": os.path.relpath(cfg_path, ROOT) if cfg_path.is_absolute() else str(cfg_path),
    }

    if not _chrome.ping(args.port):
        metadata["status"] = f"chrome port {args.port} unavailable"
        state["last_status"] = metadata["status"]
        render_html(html_path, state, metadata)
        save_json(state_path, state)
        print(metadata["status"], flush=True)
        return 0

    login_state = current_page_login_state(args.port)
    if not login_state.get("signed_in"):
        metadata["status"] = f"waiting for login on port {args.port}"
        metadata["current_url"] = login_state.get("url", "")
        state["last_status"] = metadata["status"]
        state["last_run_at"] = run_at
        render_html(html_path, state, metadata)
        save_json(state_path, state)
        print(metadata["status"], flush=True)
        return 0

    selected, next_index = rotated_keywords(state, cfg)
    kw_cfg = cfg.get("keyword_engage", {})
    kw_filters = kw_cfg.get("filters", {})
    per_kw = max(1, int(kw_cfg.get("results_per_keyword", 8) or 8))
    require_english = kw_filters.get("require_english") or kw_filters.get("skip_non_english")
    min_author_followers = int(kw_filters.get("min_author_followers", 0) or 0)
    follower_cache_hours = int(kw_filters.get("author_followers_cache_hours", 24) or 24)

    seen_ids = state.setdefault("seen_ids", {})
    rows = state.setdefault("rows", [])
    added = 0
    inspected = 0
    filtered = {}

    for keyword in selected:
        search_keyword = f"{keyword} lang:en" if require_english and "lang:" not in keyword else keyword
        try:
            candidates = fetch_keyword_candidates(args.port, search_keyword, per_kw, args.wait)
        except Exception as exc:
            filtered[f"search error: {keyword}"] = str(exc)
            continue
        for candidate in candidates:
            inspected += 1
            reason = filter_reason(candidate, keyword, cfg)
            if reason:
                filtered[reason] = filtered.get(reason, 0) + 1
                continue
            cid = str(candidate.get("id") or candidate.get("url") or "")
            if not cid or cid in seen_ids:
                continue
            author_followers = cached_author_followers(
                args.port,
                candidate.get("author", ""),
                state,
                args.wait,
                run_at,
                follower_cache_hours,
            )
            follower_count = int(author_followers.get("followers") or 0)
            if min_author_followers and follower_count <= min_author_followers:
                reason = f"too few author followers ({follower_count})"
                filtered[reason] = filtered.get(reason, 0) + 1
                continue
            row = {
                "id": cid,
                "url": candidate.get("url", ""),
                "author": candidate.get("author", ""),
                "keyword": keyword,
                "text": candidate.get("text", ""),
                "likes": candidate.get("likes", 0),
                "replies": candidate.get("replies", 0),
                "reposts": candidate.get("reposts", 0),
                "age_minutes": candidate.get("age_minutes", 9999),
                "author_followers": follower_count,
                "collected_at": run_at,
            }
            rows.append(row)
            seen_ids[cid] = {"url": row["url"], "keyword": keyword, "first_seen": run_at}
            added += 1
            if added >= args.max_new:
                break
        if added >= args.max_new:
            break

    if isinstance(next_index, dict):
        state["keyword_group_indices"] = next_index
    else:
        state["keyword_index"] = next_index
    state["last_run_at"] = run_at
    state["last_status"] = "ok"
    state.setdefault("runs", []).append({
        "run_at": run_at,
        "keywords": selected,
        "inspected": inspected,
        "added": added,
        "filtered": filtered,
    })
    state["runs"] = state["runs"][-200:]

    metadata.update({
        "status": "ok",
        "inspected": inspected,
        "added": added,
    })
    save_json(state_path, state)
    render_html(html_path, state, metadata)
    print(f"ok: inspected={inspected} added={added} html={html_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
