# X Article Page Generator — instructions for an AI model

You are given a block of text (and optionally images) to publish as an X Article.
Your job: produce a Vercel-deployed page with copy buttons that paste cleanly
into X's article editor. Do NOT write the HTML yourself — fill in `article.json`
and run the build script. The template already encodes hard-won rules about
X's editor; do not deviate from it.

## Why this template exists (do not "improve" these away)

X's article editor is Draft.js. Verified behavior (June 2026):
- Pasted HTML keeps paragraphs ONLY with flat `<p>`/`<div>`/`<br>` markup.
  Any wrapper like `<article>` collapses everything into one block.
- Each `article.json` paragraph must paste as its own Draft.js paragraph block.
  Do not merge consecutive paragraphs with soft `<br>` line breaks; it makes
  separate paragraphs appear inside the same text block in the published article.
- Use `<br>` only for intentional line breaks inside a single logical paragraph,
  such as a short list, score table, or compact data block.
- Any `<img>` in pasted HTML becomes a literal 📷 emoji. Images can only be
  inserted by pasting an `image/png` clipboard item at the cursor.
- Therefore: body text is one copy-paste; each image is its own copy-paste at
  a `[ IMAGE n HERE — … ]` marker line which the user then deletes.

## Steps

1. Copy this whole directory to a new project folder, e.g.
   `cp -r ~/Desktop/x-article-template ~/Desktop/<project-name>`
   (or work in place if regenerating the same article).

2. Put images in `assets/` (JPEG or PNG, any size; landscape ~2.5:1 looks best
   in X). Delete sample images that you don't use.

3. Rewrite `article.json`:
   - `headline`: the X article title (also used as the page `<h1>`).
   - `page_title`: optional, defaults to headline.
   - `body`: ordered list of blocks:
     - `{"type": "p", "text": "..."}` — one logical text block. Use `\n`
       inside `text` only for tight internal line breaks (e.g. lists, score
       tables). If two ideas should appear as separate paragraphs in X, use two
       separate `p` blocks; the copy script preserves each `p` as its own X
       paragraph block.
     - `{"type": "image", "src": "assets/file.jpeg", "label": "Short name", "alt": "alt text"}`
       — place these exactly where the image belongs in the article flow.
       Images are auto-numbered in order.
   - `thread`: optional array of tweet strings ("1/ ...", "2/ ..."), for a
     companion X thread. Omit or set to [] if not needed.
   - Plain text only in `text` fields — no HTML, no markdown. Quotes are fine.

4. Build:
   ```bash
   cd <project folder> && python3 build.py
   ```
   This writes `index.html`. Fix any "missing image" warnings.

5. Sanity-check locally (optional but recommended):
   ```bash
   python3 -m http.server 8123   # then open http://localhost:8123
   ```
   Click each copy button; the status line under step 3 should confirm each copy.

6. Deploy to Vercel (ask the user for a token if you don't have one):
   ```bash
   export VERCEL_TOKEN=<token>
   npx -y vercel@latest link --yes --project <project-name> --token "$VERCEL_TOKEN"
   npx -y vercel@latest deploy --prod --yes --token "$VERCEL_TOKEN"
   ```
   New project names are created automatically on first link. Give the user
   the `https://<project-name>.vercel.app` URL.

## What the user does with the deployed page (already explained on the page)

1. Copy Headline → paste into X's title field.
2. Copy Article Text → paste into X's article body (each `p` becomes its own paragraph block).
3. For each image: Copy Image n → in X, click at the end of the matching
   `[ IMAGE n HERE — … ]` marker line → paste (⌘V) → triple-click the marker
   line → press ⌫ twice.

## Files

- `build.py` — generator; the page template (HTML/CSS/JS) is embedded in it.
- `article.json` — the article content; the sample is a real published example.
- `assets/` — image files referenced from `article.json`.
- `index.html` — generated output; never edit by hand, rerun `build.py`.
