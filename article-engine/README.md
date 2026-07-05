# Article Engine

The long-form content pipeline behind the founder X account this system operates. This is the part of the repo that produced the visible output: **113 posts in the account's first 4 weeks, including 10 long-form X Articles**, run by one person.

## How a post gets made

1. **Topic research** — an agent works a rotation of source material (product news, model releases, operator pain points) and proposes angles.
2. **Draft** — an LLM drafts the article in the account's voice from a few-shot voice file, structured as `article.json` blocks.
3. **Judge pass** — the draft is reviewed by an LLM judge (run in Claude Code) against [`POSTING_RULES.md`](POSTING_RULES.md): hook placement, opening concreteness, paragraph rhythm, anti-AI-voice rules, title style. Drafts that fail get rewritten before a human ever reads them.
4. **Images** — `generate_informative_images.py` in each project renders editorial infographic cards (frameworks, comparisons, key quotes) so images carry explanatory load instead of decoration.
5. **Build & publish** — `build.py` renders the article page; text is pasted into X preserving one paragraph block per `p` block, images inserted as media at their planned positions. A human approves before anything goes live.

## How it gets better

- [`POSTING_RULES.md`](POSTING_RULES.md) — 18 versioned rules. Every rule exists because something under- or over-performed.
- [`FEEDBACK_LOG.md`](FEEDBACK_LOG.md) — dated iteration log: what the feedback was, what changed in the system as a result. The rules file is the compiled output of this log.

The loop is the point: human taste decisions get codified into rules, the judge enforces the rules on the next draft, and human time stays on what to test next and what to kill.

## Example projects

- [`loop-engineering-x-post/`](loop-engineering-x-post/) — full project: article source, image generator, build script, and its own `FEEDBACK.md`.
- [`glm52-opus-x-post/`](glm52-opus-x-post/) — the iteration case study: the published title, hook, and paragraph rhythm all came out of judge/feedback cycles documented in the main log.
- [`x-article-template/`](x-article-template/) — the canonical template new projects are cloned from; `INSTRUCTIONS.md` is the agent-facing runbook.
