"""Best-effort X analytics export test for a logged-in account profile.

Purpose:
  - do not restart or relaunch Chrome
  - only use an already-live debug port
  - attempt to open X analytics / content, export a CSV, and inspect it

This is intentionally conservative because it is meant to run alongside the
existing posting workflow without taking ownership of the browser session.
If the debug port is unavailable, the script exits cleanly with a skip report.
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from datetime import date, timedelta
from urllib.parse import urlencode

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome as _chrome
import analytics_feedback_loop as _feedback


LOG_DIR = os.path.join(ROOT_DIR, "logs", "analytics_export_test")


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{time.strftime('%Y-%m-%d')}.log"), "a") as f:
        f.write(line + "\n")


def _click_text(ws, texts) -> str:
    js = f"""
    (function() {{
      var wants = {json.dumps([t.lower() for t in texts])};
      var els = document.querySelectorAll('button, a, [role="button"], [role="menuitem"], span, div');
      function visible(el) {{
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }}
      for (var i = 0; i < els.length; i++) {{
        var el = els[i];
        if (!visible(el)) continue;
        var t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (!t) continue;
        for (var j = 0; j < wants.length; j++) {{
          if (t === wants[j] || t.indexOf(wants[j]) >= 0) {{
            el.click();
            return 'clicked:' + t.slice(0, 120);
          }}
        }}
      }}
      return 'not_found';
    }})()
    """
    return _chrome.eval_js(ws, js)


def _click_icon_semantic(ws, texts) -> str:
    js = f"""
    (function() {{
      var wants = {json.dumps([t.lower() for t in texts])};
      var els = document.querySelectorAll('button, a, [role="button"], [role="menuitem"]');
      function visible(el) {{
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }}
      function blob(el) {{
        return [
          el.innerText || '',
          el.textContent || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          el.getAttribute('data-testid') || '',
        ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
      }}
      for (var i = 0; i < els.length; i++) {{
        var el = els[i];
        if (!visible(el)) continue;
        var t = blob(el);
        if (!t) continue;
        for (var j = 0; j < wants.length; j++) {{
          if (t.indexOf(wants[j]) >= 0) {{
            el.click();
            return 'clicked:' + t.slice(0, 160);
          }}
        }}
      }}
      return 'not_found';
    }})()
    """
    return _chrome.eval_js(ws, js)


def _visible_click_labels(ws):
    js = r"""
    (function() {
      var els = document.querySelectorAll('button, a, [role="button"], [role="menuitem"]');
      function visible(el) {
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }
      var out = [];
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (!visible(el)) continue;
        var t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        if (!t) continue;
        if (out.indexOf(t) === -1) out.push(t);
      }
      return JSON.stringify(out.slice(0, 200));
    })()
    """
    raw = _chrome.eval_js(ws, js)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _visible_click_targets(ws):
    js = r"""
    (function() {
      var els = document.querySelectorAll('button, a, [role="button"], [role="menuitem"]');
      function visible(el) {
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }
      var out = [];
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (!visible(el)) continue;
        var rec = {
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(),
          aria_label: (el.getAttribute('aria-label') || '').trim(),
          title: (el.getAttribute('title') || '').trim(),
          testid: (el.getAttribute('data-testid') || '').trim(),
        };
        if (!(rec.text || rec.aria_label || rec.title || rec.testid)) continue;
        out.push(rec);
      }
      return JSON.stringify(out.slice(0, 200));
    })()
    """
    raw = _chrome.eval_js(ws, js)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _page_text(ws) -> str:
    return _chrome.eval_js(
        ws,
        "(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 4000)",
    )


def _page_title(ws) -> str:
    return _chrome.eval_js(ws, "document.title")


def _page_url(ws) -> str:
    return _chrome.eval_js(ws, "location.href")


def _set_download_dir(ws, downloads_dir: Path):
    try:
        _chrome._send(
            ws,
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(downloads_dir),
                "eventsEnabled": False,
            },
            msg_id=61,
        )
        return True
    except Exception as e:
        _log(f"download behavior setup failed: {e}")
        return False


def _latest_csv(downloads_dir: Path, since: float):
    csvs = [p for p in downloads_dir.glob("*.csv") if p.stat().st_mtime >= since]
    if not csvs:
        return None
    csvs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0]


def _recent_download_snapshot(downloads_dir: Path, limit: int = 20):
    files = []
    for p in downloads_dir.iterdir():
        try:
            if not p.is_file():
                continue
            st = p.stat()
            files.append((st.st_mtime, p))
        except Exception:
            continue
    files.sort(key=lambda x: x[0], reverse=True)
    out = []
    for mtime, p in files[:limit]:
        out.append({
            "name": p.name,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
            "size": p.stat().st_size,
        })
    return out


def _recent_csv_fallback(downloads_dir: Path, age_s: int = 1800):
    now = time.time()
    csvs = []
    for p in downloads_dir.glob("*.csv"):
        try:
            st = p.stat()
            if now - st.st_mtime <= age_s:
                csvs.append((st.st_mtime, p))
        except Exception:
            continue
    if not csvs:
        return None
    csvs.sort(key=lambda x: x[0], reverse=True)
    return csvs[0][1]


def _multica_summary(result: dict) -> str:
    lines = [f"status: {result.get('status', 'unknown')}"]
    if result.get("status") != "ok":
        if result.get("reason"):
            lines.append(f"reason: {result['reason']}")
        return "\n".join(lines)

    feedback = result.get("feedback") or {}
    if feedback:
        summary = (feedback.get("summary") or "").strip()
        if summary:
            lines.append(summary)
    return "\n".join(lines)


def _compact_result(result: dict) -> dict:
    out = {
        "status": result.get("status"),
        "multica_summary": result.get("multica_summary", ""),
    }
    if result.get("status") != "ok":
        if result.get("reason"):
            out["reason"] = result["reason"]
        return out

    out["rows"] = result.get("rows", 0)
    feedback = result.get("feedback") or {}
    if feedback:
        recs = feedback.get("recommendations", {})
        out["config_changed"] = feedback.get("config_changed", False)
        out["changes"] = feedback.get("apply_plan", {}).get("notes", [])
        out["performance_judgement"] = recs.get("performance_judgement", {})
        out["suggestions"] = recs.get("strategy_suggestions", [])
        out["responsive_target_candidates"] = recs.get("responsive_target_candidates", [])
        out["reply_taste_updates"] = recs.get("reply_taste_updates", [])
        out["algorithm_updates"] = recs.get("algorithm_updates", [])
    return out


def _inspect_csv(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = reader.fieldnames or []
    cols_l = [c.lower() for c in cols]
    wanted = {
        "impressions": any("impression" in c for c in cols_l),
        "detail_expands": any("detail" in c and "expand" in c for c in cols_l),
        "profile_visits": any("profile" in c and "visit" in c for c in cols_l),
        "follows": any("follow" in c for c in cols_l),
    }
    return {
        "path": str(path),
        "rows": len(rows),
        "columns": cols,
        "has_metrics": wanted,
    }


def _try_navigation(ws, handle: str):
    # Try content-first analytics routes with a rolling yesterday→today window.
    # Same-day exports can be incomplete while X is still processing today's data.
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    content_qs = urlencode({
        "type": "all",
        "sort": "date",
        "dir": "desc",
        "from": yesterday,
        "to": today,
    })
    urls = [
        f"https://x.com/i/account_analytics/content?{content_qs}",
        "https://x.com/i/account_analytics",
        "https://analytics.x.com",
        "https://studio.x.com",
        f"https://x.com/{handle}/analytics",
    ]
    for url in urls:
        _log(f"navigate → {url}")
        _chrome.navigate(ws, url, wait=5.0)
        current_url = _page_url(ws)
        title = _page_title(ws)
        body = _page_text(ws).lower()
        _log(f"landed → url={current_url} title={title}")
        if any(key in body for key in ("page not found", "something went wrong", "this page doesn’t exist")):
            continue
        if any(key in body for key in ("analytics", "creator", "content", "posts and replies", "impressions")):
            return {
                "ok": True,
                "requested_url": url,
                "current_url": current_url,
                "title": title,
                "body": body,
            }
    return {
        "ok": False,
        "requested_url": urls[-1],
        "current_url": _page_url(ws),
        "title": _page_title(ws),
        "body": _page_text(ws).lower(),
    }


def run(port: int, handle: str, downloads_dir: Path, timeout_s: int) -> dict:
    if not _chrome.ping(port):
        return {
            "status": "skipped",
            "reason": f"chrome debug port {port} is not live",
        }

    ws = _chrome.connect(port)
    try:
        _chrome.bring_to_front(ws)
        _chrome.set_viewport(ws, width=1440, height=1800)
        _set_download_dir(ws, downloads_dir)
        _log(f"downloads dir → {downloads_dir}")

        nav = _try_navigation(ws, handle)
        if not nav["ok"]:
            return {
                "status": "failed",
                "reason": "could not reach analytics/content surface",
                "requested_url": nav["requested_url"],
                "landing_url": nav["current_url"],
                "title": nav["title"],
                "body_head": nav["body"][:1000],
            }

        _log("attempt content navigation")
        _click_text(ws, ["content"])
        time.sleep(2.0)
        _click_text(ws, ["posts and replies", "posts & replies"])
        time.sleep(2.0)
        labels = _visible_click_labels(ws)
        targets = _visible_click_targets(ws)
        _log(f"visible click labels: {labels[:40]}")

        before = time.time()
        _log("attempt csv export")
        export_result = _click_icon_semantic(
            ws,
            [
                "download",
                "download csv",
                "export",
                "export data",
                "download data",
                "csv",
            ],
        )
        if export_result == "not_found":
            _log("attempt export via menu/overflow icon")
            menu_result = _click_icon_semantic(
                ws,
                [
                    "more",
                    "more options",
                    "overflow",
                    "actions",
                    "share",
                    "menu",
                ],
            )
            _log(f"menu click result: {menu_result}")
            time.sleep(1.5)
            export_result = _click_icon_semantic(
                ws,
                [
                    "download",
                    "download csv",
                    "export",
                    "export data",
                    "download data",
                    "csv",
                ],
            )
        _log(f"export click result: {export_result}")

        deadline = time.time() + timeout_s
        newest = None
        while time.time() < deadline:
            newest = _latest_csv(downloads_dir, before)
            if newest:
                break
            time.sleep(2.0)

        if not newest:
            newest = _recent_csv_fallback(downloads_dir, age_s=1800)
            if newest:
                _log(f"fallback recent csv found → {newest}")
        
        if not newest:
            return {
                "status": "failed",
                "reason": "no csv download detected",
                "requested_url": nav["requested_url"],
                "landing_url": nav["current_url"],
                "title": nav["title"],
                "export_click": export_result,
                "visible_labels": labels,
                "visible_targets": targets,
                "downloads_dir": str(downloads_dir),
                "recent_downloads": _recent_download_snapshot(downloads_dir),
                "body_head": _page_text(ws)[:1500],
            }

        info = _inspect_csv(newest)
        return {
            "status": "ok",
            "requested_url": nav["requested_url"],
            "landing_url": nav["current_url"],
            "title": nav["title"],
            "export_click": export_result,
            "visible_labels": labels,
            "visible_targets": targets,
            "downloads_dir": str(downloads_dir),
            **info,
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--handle", required=True)
    p.add_argument("--downloads-dir", default=str(Path.home() / "Downloads"))
    p.add_argument("--timeout", type=int, default=45)
    args = p.parse_args()

    result = run(
        port=args.port,
        handle=args.handle,
        downloads_dir=Path(args.downloads_dir),
        timeout_s=args.timeout,
    )
    if result.get("status") == "ok" and result.get("path"):
        try:
            result["feedback"] = _feedback.run(
                csv_path=Path(result["path"]),
                config_path=Path(ROOT_DIR) / "accounts" / args.handle / "engage_config.json",
                apply_config=True,
            )
        except Exception as e:
            result["feedback"] = {
                "status": "failed",
                "reason": f"feedback loop failed: {e}",
            }
    result["multica_summary"] = _multica_summary(result)
    print(json.dumps(_compact_result(result), indent=2))


if __name__ == "__main__":
    main()
