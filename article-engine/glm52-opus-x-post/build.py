#!/usr/bin/env python3
"""Generate a paste-ready X Article page from article.json.

Usage:
    python3 build.py [article.json] [output_dir]

Defaults: article.json in this directory, output to this directory (index.html).
Put referenced images in assets/ before building.

The generated page's copy buttons emit clipboard payloads matched to how X's
article editor (Draft.js) actually handles pastes — verified June 2026:
  - body text is copied as flat sibling <p>/<br> HTML; each article.json p
    becomes its own X paragraph block
  - any wrapper like <article> can collapse paragraphs into one block on paste
  - images are NEVER carried in pasted HTML (X turns <img> into a 📷 emoji);
    each image gets its own button that canvas-converts it to PNG and writes
    an image/png ClipboardItem, pasted at a [ IMAGE n HERE ] marker line
"""
import html
import json
import re
import sys
from pathlib import Path

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__PAGE_TITLE__</title>
  <style>
    :root {
      --bg: #f4f5f7;
      --ink: #111827;
      --muted: #5b6575;
      --line: #d9dee7;
      --card: #ffffff;
      --accent: #0f766e;
      --good: #166534;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    header {
      background: #111827;
      color: white;
      padding: 28px 22px 24px;
      border-bottom: 4px solid var(--accent);
    }
    .wrap { max-width: 1040px; margin: 0 auto; }
    h1 { margin: 0 0 8px; font-size: clamp(26px, 4vw, 40px); line-height: 1.08; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }
    .sub { color: #c8d1df; max-width: 760px; font-size: 15px; }
    main { padding: 24px 22px 54px; }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 18px; align-items: start; }
    section, aside {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .note {
      font-size: 13px;
      color: var(--muted);
      margin: 0 0 14px;
    }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0 16px; }
    button, .download {
      appearance: none;
      border: 1px solid var(--line);
      background: #f8fafc;
      color: var(--ink);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 13px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      min-height: 36px;
    }
    button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    .steps { counter-reset: step; margin: 0 0 18px; padding: 0; list-style: none; }
    .steps > li {
      counter-increment: step;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 14px 16px 14px 52px;
      margin-bottom: 10px;
      position: relative;
    }
    .steps > li::before {
      content: counter(step);
      position: absolute;
      left: 14px;
      top: 14px;
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: var(--accent);
      color: white;
      font-size: 14px;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .steps strong { display: block; margin-bottom: 4px; font-size: 14px; }
    .steps .hint { font-size: 13px; color: var(--muted); margin: 0 0 10px; }
    .image-step {
      display: flex;
      gap: 10px;
      align-items: center;
      border-top: 1px dashed var(--line);
      padding: 10px 0;
    }
    .image-step:first-of-type { border-top: 0; }
    .image-step img {
      width: 110px;
      aspect-ratio: 5 / 2;
      object-fit: cover;
      border: 1px solid #e5e7eb;
      border-radius: 4px;
      flex-shrink: 0;
    }
    .image-step .meta { flex: 1; min-width: 0; font-size: 13px; }
    .image-step .meta code {
      display: block;
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .headline-box {
      border: 1px solid #cdd5e0;
      background: #fbfcfe;
      border-radius: 8px;
      padding: 14px 16px;
      font-size: 22px;
      font-weight: 760;
      line-height: 1.18;
      margin-bottom: 14px;
    }
    .paste-article {
      border: 1px solid #cdd5e0;
      background: #fbfcfe;
      border-radius: 8px;
      padding: 16px;
      font-size: 15px;
      line-height: 1.45;
      user-select: text;
    }
    .paste-article p {
      margin: 0 0 12px;
      padding: 0;
    }
    .paste-article p:last-child { margin-bottom: 0; }
    .paste-article figure {
      margin: 14px 0;
      padding: 0;
    }
    .paste-article figcaption {
      font-size: 12px;
      color: var(--muted);
      padding: 4px 0 0;
    }
    .paste-article img {
      display: block;
      width: 100%;
      max-width: 1600px;
      height: auto;
      border: 0;
      border-radius: 0;
      background: #f8fafc;
    }
    .copybox {
      border: 1px solid #cdd5e0;
      background: #fbfcfe;
      border-radius: 8px;
      padding: 12px;
      font-size: 15px;
    }
    .status { font-size: 13px; color: var(--good); min-height: 18px; margin-top: 8px; }
    .image-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 14px;
      background: #fff;
    }
    .image-card img {
      display: block;
      width: 100%;
      height: auto;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      background: #f8fafc;
    }
    .image-card strong { display: block; font-size: 13px; margin: 9px 0 8px; }
    .image-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .tweet {
      border: 1px solid #d9dee7;
      border-radius: 8px;
      padding: 12px;
      margin: 10px 0;
      background: #fbfcfe;
    }
    .tweet p { margin: 0 0 10px; }
    .tweet p:last-child { margin-bottom: 0; }
    .label {
      display: inline-flex;
      font-size: 11px;
      font-weight: 700;
      color: #475569;
      background: #eef2f7;
      border-radius: 999px;
      padding: 3px 8px;
      margin-bottom: 8px;
    }
    @media (max-width: 860px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>__PAGE_TITLE__</h1>
      <div class="sub">Paste-ready X Article: clean paragraph text plus one-click image copying, matched to how the X article editor actually handles pastes.</div>
    </div>
  </header>
  <main>
    <div class="wrap grid">
      <section>
        <h2>Paste into X — 3 steps</h2>
        <p class="note">X's article editor strips images from pasted HTML and flattens wrapped markup, so text and images go in separately. Follow the steps in order.</p>
        <ol class="steps">
          <li>
            <strong>Title</strong>
            <p class="hint">Copy the headline, then paste it into the "Add a title" field on X.</p>
            <button class="primary" data-copy-headline>Copy Headline</button>
          </li>
          <li>
            <strong>Body text</strong>
            <p class="hint">Copy the article text, click into the X article body, and paste. Each article <em>p</em> block becomes its own X paragraph block. Newlines inside a single <em>p</em> stay as soft line breaks. Each image position is marked with a line like <em>[ IMAGE 1 HERE … ]</em>.</p>
            <button class="primary" data-copy-body>Copy Article Text (with image markers)</button>
          </li>
          <li>
            <strong>Images</strong>
            <p class="hint">For each image: click <em>Copy Image</em> below, then in X click at the <strong>end of the matching marker line</strong> and paste (⌘V). The image uploads inline right below the marker. Then triple-click the marker line and press ⌫ twice (once for the text, once for the leftover empty line).</p>
__IMAGE_STEPS__
          </li>
        </ol>
        <div class="status" id="copyStatus"></div>
        <h2 style="margin-top:24px;">Preview</h2>
        <div id="headlineText" class="headline-box">__HEADLINE__</div>
        <article id="xArticleBody" class="paste-article">
__BODY_BLOCKS__
        </article>
__THREAD_SECTION__
      </section>
      <aside>
        <h2>Image Assets</h2>
        <p class="note">Use the step-3 buttons for pasting into X. These are backup download links.</p>
__IMAGE_CARDS__
      </aside>
    </div>
  </main>
  <script>
    var headline = __HEADLINE_JS__;

    function setStatus(message) {
      var status = document.getElementById("copyStatus");
      status.textContent = message;
      setTimeout(function() { status.textContent = ""; }, 3200);
    }

    function escapeHtml(value) {
      return value.replace(/[&<>"']/g, function(char) {
        return {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;"
        }[char];
      });
    }

    function normalizePlainText(text) {
      return text
        .replace(/\\u00a0/g, " ")
        .replace(/[ \\t]+\\n/g, "\\n")
        .replace(/\\n[ \\t]+/g, "\\n")
        .replace(/\\n{3,}/g, "\\n\\n")
        .trim();
    }

    function bodyBlocks() {
      return Array.from(document.getElementById("xArticleBody").children);
    }

    function buildBodyHtml() {
      var html = [];
      bodyBlocks().forEach(function(el) {
        if (el.tagName === "P") {
          var plainText = normalizePlainText(el.innerText);
          if (plainText) {
            html.push("<p>" + plainText.split("\\n").map(escapeHtml).join("<br>") + "</p>");
          }
        }
        if (el.tagName === "FIGURE") {
          html.push("<p>" + escapeHtml("[ " + el.getAttribute("data-image-marker") + " ]") + "</p>");
        }
      });
      return html.join("");
    }

    function buildBodyPlainText() {
      var chunks = [];
      bodyBlocks().forEach(function(el) {
        if (el.tagName === "P") {
          var text = normalizePlainText(el.innerText);
          if (text) {
            chunks.push(text);
          }
        }
        if (el.tagName === "FIGURE") {
          chunks.push("[ " + el.getAttribute("data-image-marker") + " ]");
        }
      });
      return chunks.filter(Boolean).join("\\n\\n");
    }

    async function copyBody() {
      var plain = buildBodyPlainText();
      if (navigator.clipboard && window.ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([buildBodyHtml()], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" })
        })]);
        setStatus("Copied article text. Paste into the X article body, then add images (step 3).");
      } else {
        await navigator.clipboard.writeText(plain);
        setStatus("Copied article text (plain). Paste into the X article body.");
      }
    }

    function imagePngBlob(src) {
      return new Promise(function(resolve, reject) {
        var img = new Image();
        img.onload = function() {
          var canvas = document.createElement("canvas");
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          canvas.getContext("2d").drawImage(img, 0, 0);
          canvas.toBlob(function(blob) {
            if (blob) { resolve(blob); } else { reject(new Error("PNG conversion failed")); }
          }, "image/png");
        };
        img.onerror = function() { reject(new Error("Could not load " + src)); };
        img.src = src;
      });
    }

    // Clipboard image writes only accept image/png, and Safari requires the
    // promise form of ClipboardItem, so the blob promise is passed directly.
    async function copyImageToClipboard(src, label) {
      if (!navigator.clipboard || !window.ClipboardItem) {
        throw new Error("This browser cannot copy images. Use the download link instead.");
      }
      await navigator.clipboard.write([new ClipboardItem({ "image/png": imagePngBlob(src) })]);
      setStatus("Copied " + label + ". In X, click at the end of its marker line and paste (⌘V), then triple-click the marker line and press ⌫ twice.");
    }

    document.querySelector("[data-copy-body]").addEventListener("click", function() {
      copyBody().catch(function(error) {
        setStatus("Copy failed: " + error.message);
        console.error(error);
      });
    });

    document.querySelectorAll("[data-copy-image]").forEach(function(btn) {
      btn.addEventListener("click", function() {
        copyImageToClipboard(btn.getAttribute("data-copy-image"), btn.getAttribute("data-image-label")).catch(function(error) {
          setStatus("Image copy failed: " + error.message);
          console.error(error);
        });
      });
    });

    document.querySelector("[data-copy-headline]").addEventListener("click", async function() {
      await navigator.clipboard.writeText(headline);
      setStatus("Copied headline. Paste into the X title field.");
    });

    var threadBtn = document.querySelector("[data-copy-thread]");
    if (threadBtn) {
      threadBtn.addEventListener("click", async function() {
        await navigator.clipboard.writeText(document.getElementById("threadText").textContent.trim());
        setStatus("Copied thread.");
      });
    }

    document.querySelectorAll("[data-open]").forEach(function(btn) {
      btn.addEventListener("click", function() {
        window.open(btn.getAttribute("data-open"), "_blank");
      });
    });

    window.__xArticleCopy = {
      buildBodyHtml: buildBodyHtml,
      buildBodyPlainText: buildBodyPlainText
    };
  </script>
</body>
</html>
"""

THREAD_SECTION = """        <h2 style="margin-top:24px;">X Thread Draft</h2>
        <div class="toolbar">
          <button data-copy-thread>Copy Full Thread</button>
        </div>
        <div id="threadText" class="copybox" hidden>__THREAD_TEXT__</div>
__TWEETS__"""


def esc(text):
    return html.escape(text, quote=True)


def para_html(text):
    return "<br>".join(esc(line) for line in text.split("\n"))


def build(config):
    headline = config["headline"]
    page_title = config.get("page_title", headline)
    body = config["body"]

    images = [b for b in body if b["type"] == "image"]
    for n, img in enumerate(images, 1):
        img["n"] = n
        img["marker"] = "IMAGE %d HERE — %s" % (n, img["label"].lower())

    body_blocks = []
    for block in body:
        if block["type"] == "p":
            body_blocks.append("          <p>%s</p>" % para_html(block["text"]))
        elif block["type"] == "image":
            body_blocks.append(
                '          <figure data-image-marker="%s"><img src="%s" alt="%s">'
                "<figcaption>Image %d — pasted separately in step 3</figcaption></figure>"
                % (esc(block["marker"]), esc(block["src"]), esc(block.get("alt", block["label"])), block["n"])
            )
        else:
            raise ValueError("unknown body block type: %r" % block["type"])

    image_steps = []
    for img in images:
        image_steps.append(
            """            <div class="image-step">
              <img src="%s" alt="%s">
              <div class="meta">
                <strong>Image %d — %s</strong>
                <code>[ %s ]</code>
              </div>
              <button data-copy-image="%s" data-image-label="Image %d">Copy Image %d</button>
            </div>"""
            % (esc(img["src"]), esc(img.get("alt", img["label"])), img["n"], esc(img["label"]),
               esc(img["marker"]), esc(img["src"]), img["n"], img["n"])
        )

    image_cards = []
    for img in images:
        image_cards.append(
            """        <div class="image-card">
          <img src="%s" alt="%s">
          <strong>Image %d — %s</strong>
          <div class="image-actions">
            <a class="download" href="%s" download>Download</a>
            <button data-open="%s">Open</button>
          </div>
        </div>"""
            % (esc(img["src"]), esc(img.get("alt", img["label"])), img["n"], esc(img["label"]),
               esc(img["src"]), esc(img["src"]))
        )

    thread = config.get("thread") or []
    if thread:
        tweets = []
        for entry in thread:
            m = re.match(r"^(\d+/)\s*(.*)$", entry, re.S)
            label, text = (m.group(1), m.group(2)) if m else ("•", entry)
            tweets.append(
                '        <div class="tweet">\n          <span class="label">%s</span>\n          <p>%s</p>\n        </div>'
                % (esc(label), para_html(text))
            )
        thread_section = THREAD_SECTION.replace("__THREAD_TEXT__", esc("\n\n".join(thread)))
        thread_section = thread_section.replace("__TWEETS__", "\n".join(tweets))
    else:
        thread_section = ""

    page = TEMPLATE
    page = page.replace("__PAGE_TITLE__", esc(page_title))
    page = page.replace("__HEADLINE_JS__", json.dumps(headline, ensure_ascii=False))
    page = page.replace("__HEADLINE__", esc(headline))
    page = page.replace("__BODY_BLOCKS__", "\n".join(body_blocks))
    page = page.replace("__IMAGE_STEPS__", "\n".join(image_steps))
    page = page.replace("__IMAGE_CARDS__", "\n".join(image_cards))
    page = page.replace("__THREAD_SECTION__", thread_section)
    return page


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "article.json"
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else config_path.parent
    config = json.loads(config_path.read_text())

    missing = [b["src"] for b in config["body"] if b["type"] == "image" and not (out_dir / b["src"]).exists()]
    if missing:
        print("WARNING: missing image files (put them in place before deploying):")
        for src in missing:
            print("  " + str(out_dir / src))

    out_path = out_dir / "index.html"
    out_path.write_text(build(config))
    n_imgs = sum(1 for b in config["body"] if b["type"] == "image")
    n_paras = sum(1 for b in config["body"] if b["type"] == "p")
    print("Wrote %s (%d paragraphs, %d images, thread: %s)" % (
        out_path, n_paras, n_imgs, "yes" if config.get("thread") else "no"))


if __name__ == "__main__":
    main()
