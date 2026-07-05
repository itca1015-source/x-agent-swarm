# Feedback Log

Use this file as durable memory for future X Article preparation and autoposting.

## 2026-06-17 - Loop Engineering Article

Project: `loop-engineering-x-post`

User feedback:
- The article text must be in English. Chinese drafts should always be translated into English before publishing.
- The first version had no image; future article drafts need visual assets.
- The article needed at least three images.
- Images should be informative, not decorative.
- The published X article had too much spacing between paragraphs.
- Future posts should avoid extra spacing between paragraphs and should not paste body text as many separate Draft.js paragraph blocks.

Resulting changes:
- Converted the article to English.
- Added three informative diagrams.
- Updated the X Article template so consecutive text blocks copy as compact soft line breaks.
- Added `POSTING_RULES.md` so English output, informative images, and compact spacing are default requirements.

## 2026-06-17 - Anthropic OPC Article

Project: `anthropic-opc-x-post`

User feedback:
- The article needed more images.
- Images should contain more of the explanatory text so the article feels less overwhelming.
- The first image set felt too business-like.
- The images should be more attractive.
- The final version with six richer editorial-style infographics looked good and was approved for posting.
- After posting, the compact paste had no visible spacing between paragraphs.
- The user asked to review the corrected edit before republishing.

Resulting changes:
- Increased the image count from three to six.
- Reworked the images into a warmer editorial infographic style.
- Put more explanatory text directly inside each image.
- Distributed images throughout the article so the reading flow is broken into smaller chunks.
- Published the final version to `@hunterguo101`.
- Rebuilt the X draft with balanced paragraph grouping and left it in Preview for approval before republishing.
- Updated the generator so future copies combine short related blocks, while keeping section headings, long blocks, and image positions as real breaks.

## 2026-06-29 - AI Cutoff Article

Project: `ai-cutoff-x-post`

User feedback:
- The article should have visible line breaks after every paragraph.
- Do not merge multiple source paragraphs into one X paragraph with soft line breaks; the published article makes those separate blocks look jammed together.

Resulting changes:
- Updated the canonical `x-article-template` generator so every `article.json` `p` block copies as its own X paragraph block.
- Kept soft `<br>` line breaks only for intentional line breaks inside a single `p`, such as compact lists or data blocks.
- Updated `POSTING_RULES.md` so preserving paragraph boundaries is the default for future X Articles.

## 2026-06-29 - GLM/Opus Article

Project: `glm52-opus-x-post`

User feedback:
- The strongest hook was not "GLM caught up with Opus"; it was that a rival
  model could run inside a tool/interface people already love.
- Move the shell/brain argument into the first few paragraphs.
- The first line should not depend on the reader already caring about model
  rankings.
- The draft sounded too much like AI writing and was hard to read for people
  casually browsing X during a work break.
- Study a high-performing X Article tone: concrete pain/detail first, short
  beats, direct stakes, less abstract memo language.

Resulting changes:
- Retitled the article to `The Model Is Becoming a Replaceable Part`.
- Rewrote the opening around the concrete Claude Code/GLM split.
- Converted the article into shorter, more conversational paragraphs.
- Added a direct companion X thread with the big idea in the first post.
- Updated the canonical template and posting rules with tone, hook, title, and
  anti-AI-voice guidance.

## Standing Lessons

- Default language for published X Articles is English.
- For long analytical posts, prefer about six informative images rather than the bare minimum of three.
- Images should carry real explanatory load: frameworks, comparisons, key quotes, steps, or summaries.
- For this Anthropic AI-native organization article, the approved image direction is dark editorial graphics with black backgrounds, muted dusty pink-red accents, Impact/DIN-style display typography, and normal-color Dario photography where used.
- Before pasting or posting to X, verify the active account is `hunterguo101`.
- Before publishing on X, verify the body has no visible image markers or failed camera placeholder glyphs. If X cannot insert images cleanly, remove the image slots and publish clean text only.
- X Article images can be inserted from the in-app browser by pasting each PNG as a binary clipboard image. Do not use HTML `img` paste for X Articles; it can create placeholder artifacts instead of uploaded media.
- Avoid a stiff corporate-deck look. Prefer attractive editorial cards, warm backgrounds, clear hierarchy, and readable text.
- Preserve paragraph boundaries in X: one `article.json` `p` block should become one X paragraph block. Avoid extra empty paragraphs, and do not merge separate paragraphs into one soft-break block.
- Put the strongest hook in the first 3-5 paragraphs, especially when the real
  story is a concrete workflow, tool, price, or ownership shift.
- Write X Articles for distracted readers: short beats, concrete scenes,
  curiosity-first titles, and plain human transitions.
- Avoid AI-sounding thesis language and abstract setup when a sharper concrete
  detail is available.
