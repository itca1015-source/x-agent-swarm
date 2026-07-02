#!/usr/bin/env python3
import collections
import datetime
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "state" / "reply_queue.json"
OUTPUT_PATH = ROOT / "state" / "flatkey_reply_queue.html"


def esc(value) -> str:
    return html.escape(str(value or ""))


def render_row(row: dict) -> str:
    status = esc(row.get("status"))
    source = esc(row.get("source"))
    target = esc(row.get("target"))
    url = esc(row.get("target_url"))
    queued = esc(row.get("queued_at"))
    target_text = esc(row.get("target_text"))
    reply = esc(row.get("reply_text"))
    angle = esc(row.get("reply_angle"))
    reason = esc(row.get("rejection_reason"))
    link = f'<a href="{url}" target="_blank">open tweet</a>' if url else ""
    rejection = ""
    if reason:
        rejection = f"""
        <section>
          <h2>Rejection</h2>
          <p>{reason}</p>
        </section>
        """
    return f"""
    <article class="card {status}">
      <div class="meta">
        <span class="status">{status}</span>
        <span>{source}</span>
        <span>@{target}</span>
        <span>{queued}</span>
        <span>{link}</span>
      </div>
      <section>
        <h2>Target</h2>
        <p>{target_text}</p>
      </section>
      <section>
        <h2>Reply Draft</h2>
        <p class="reply">{reply}</p>
      </section>
      <section>
        <h2>Angle</h2>
        <p>{angle}</p>
      </section>
      {rejection}
    </article>
    """


def main() -> int:
    with QUEUE_PATH.open() as f:
        queue = json.load(f)
    rows = [row for row in queue if row.get("account") == "flatkey"]
    counts = collections.Counter(row.get("status", "") for row in rows)
    pills = "".join(
        f'<span class="pill">{esc(status)}: {count}</span>'
        for status, count in counts.items()
    )
    body = "\n".join(render_row(row) for row in rows)
    if not body:
        body = "<p>No Flatkey queue entries found.</p>"

    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Flatkey Reply Queue</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 32px;
      background: #f7f7f4;
      color: #1d1d1b;
    }}
    header {{ margin-bottom: 24px; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    h2 {{
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: #777;
      margin: 14px 0 6px;
    }}
    p {{ white-space: pre-wrap; line-height: 1.45; margin: 0; }}
    a {{ color: #0757c2; }}
    code {{ background: #eee; padding: 1px 4px; border-radius: 4px; }}
    .summary {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .pill {{
      border: 1px solid #ccc;
      background: white;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 13px;
    }}
    .card {{
      background: white;
      border: 1px solid #ddd;
      border-radius: 8px;
      padding: 18px;
      margin: 14px 0;
      box-shadow: 0 1px 2px rgba(0,0,0,.04);
    }}
    .meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: #666;
      font-size: 13px;
      margin-bottom: 12px;
    }}
    .status {{ font-weight: 700; color: #111; }}
    .reply {{ font-size: 17px; color: #111; }}
    .pending {{ border-left: 5px solid #1d8f4f; }}
    .posted {{ border-left: 5px solid #246bce; }}
    .rejected {{ border-left: 5px solid #c44; }}
  </style>
</head>
<body>
  <header>
    <h1>Flatkey Reply Queue</h1>
    <div>
      Generated {esc(generated)} from <code>state/reply_queue.json</code>.
      Showing only <code>account == flatkey</code>.
    </div>
    <div class="summary">
      <span class="pill">total: {len(rows)}</span>
      {pills}
    </div>
  </header>
  {body}
</body>
</html>
"""
    OUTPUT_PATH.write_text(doc, encoding="utf-8")
    print(OUTPUT_PATH)
    print(f"flatkey rows: {len(rows)}")
    print(f"statuses: {dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
