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

## Writing style and tone

Before filling `article.json`, rewrite the source into a scroll-native X
Article, not a polished memo.

- Put the best idea in the first 3-5 paragraphs. Do not bury the real hook
  behind background, model names, benchmarks, or setup.
- Open with something concrete: a weird detail, a personal reaction, a number,
  a tool people already know, or a small scene. Avoid abstract openers like
  "X is not just a story about Y" unless the reader already cares about X and Y.
- Write for someone taking a short break from work. Short beats win. Use one
  idea per paragraph. Let paragraphs breathe.
- Prefer plain, slightly conversational lines over essay transitions. Good
  examples: "Small trick. Big implication." "Now add price." "But not
  everywhere." Use this sparingly, but let the piece sound human.
- Make the title curiosity-first and low-jargon. It should work for readers who
  do not already know every model or company in the story.
- State the big idea directly in the article and in the companion X thread. Do
  not only tease the article.
- Keep caveats, but make them readable. They should add credibility without
  sounding like a legal disclaimer.
- Cut AI-sounding filler: "this is a signal", "it is worth noting", "in
  conclusion", "this demonstrates", and over-explained thesis paragraphs.
- If the article has a tool/model/product split, a shocking cost, a personal
  pain point, or a concrete workflow, lead with that instead of the category
  label.

### High-performing X article pattern

When the user asks to adapt an article for X, model the piece on high-reach X
articles that feel like a smart friend explaining the hidden mechanism behind a
trend.

Tone:
- Confident, plain-spoken, and slightly provocative.
- Make the reader feel: "this is the obvious thing everyone is missing."
- Avoid academic framing, corporate polish, and excessive hedging.
- Use direct language and concrete nouns. No fluffy metaphors unless the source
  already earns them.
- Balance hype with a sober warning so the piece feels trustworthy.

Cadence:
- Mix short punchy lines with longer explanatory paragraphs.
- Use clean transition phrases when useful: "Here is the part people miss.",
  "This is where it gets expensive.", "Now the trap.", "The mistake is simple."
- Prefer binaries that clarify the shift: prompt vs loop, answer vs action,
  manual vs automatic, tool vs system.
- Let examples do work. A concrete template, workflow, or checklist is better
  than another abstract paragraph.

Structure:
1. Big tension: name a behavior readers recognize and imply it is slower,
   weaker, or outdated than they think.
2. Simple reframe: introduce the better way in one clean sentence.
3. Promise: tell readers what they will understand by the end.
4. Social proof: use one or two credible references, examples, numbers, or
   public moments to make the idea feel current.
5. Definition: explain the concept in the simplest contrast possible.
6. Mechanism: break the idea into a memorable cycle, checklist, stack, or parts
   list.
7. Critical insight: name the part most people miss.
8. Decision filter: explain when to use the idea and when not to.
9. Concrete example: give a copyable spec, prompt, workflow, or mini-template.
10. Advanced version: show how serious users or teams apply it at scale.
11. Warning: explain the cost, failure mode, or trap.
12. Build order: give the practical sequence for doing it safely.
13. Everyday version: translate the big idea into normal work or life use cases.
14. Start here: give easy examples readers can try immediately.
15. Final reframe: close with a memorable shift in who does the work.

Use this arc flexibly. The important shape is:
problem -> reframe -> mechanism -> proof -> caution -> examples -> action.

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
