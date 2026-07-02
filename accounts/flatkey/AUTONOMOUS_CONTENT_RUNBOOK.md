# Flatkey Autonomous Content Runbook

Approved: 2026-06-06

## Operating Rule

Until builder input arrives, Flatkey operates as a category-native LLM cost and routing account.

Flatkey can educate, ask, repost, and quote public cost/routing signals, but it cannot claim product capabilities, launch status, integrations, pricing, savings, customer usage, credits, or access.

## Autonomous Lane

Flatkey can post without builder input when the post is about one of these categories:

1. LLM cost mental models
   - Cost per completed task.
   - Failure cost vs token price.
   - When cheap models become expensive.

2. Agent spend failure modes
   - Context bloat.
   - Retry loops.
   - Planning passes.
   - Tool-call chains.
   - Long chat histories.
   - Overusing frontier models.
   - Bad fallback behavior.

3. Routing heuristics
   - Route by task risk, failure cost, context size, latency tolerance, and verification burden.
   - Strong model for uncertain steps, cheaper model for bounded/formatting steps.
   - Measure workflow outcome, not isolated token price.

4. Coding-agent cost posts
   - Claude Code, Cursor, OpenClaw, Replit, Bolt, v0, Devin-style workflows.
   - Safe angle: where token spend, credits, rate limits, or context growth show up in the workflow.

5. Public model/pricing commentary
   - Only from public official or credible third-party sources.
   - The post must explain the cost/routing implication.

6. Builder audience discovery
   - Ask builders where spend leaks.
   - Ask what model stack they use.
   - Ask what workflow surprised them on cost.

7. Cost checklist/resource posts
   - Token leak checklist.
   - Cost-per-task worksheet.
   - Routing checklist.
   - Spend logging questions.

8. Public observation roundups
   - Summarize visible public builder pain.
   - Do not invent metrics unless counted from logged public samples.

## Repost Lane

Plain reposts are allowed when the source already carries the proof:

- Builder complaints about LLM spend, API credits, rate limits, or context windows.
- Official model launches, pricing changes, context-window changes, or service-tier changes.
- Credible model benchmarks and comparison data.
- Open-source tools around routing, logging, tracing, prompt caching, evals, or gateways.
- Real coding-agent workflow demos where cost, context, or routing is visibly relevant.
- Posts from approved adjacent infra/model/tool accounts.
- Builder posts showing actual token usage, API credits, monthly AI-tool spend, or rate-limit pain.

Quote only when Flatkey can add a specific cost/routing lens. The external post supplies proof or news; Flatkey adds the mechanism.

Good quote shapes:

- "The hidden cost here is not the first model call. It is the retries after the agent loses context."
- "This is where routing gets practical: use the expensive model for the uncertain step, not the whole workflow."
- "The benchmark I would want is cost per accepted PR, not cost per 1M tokens."

## Hard Blocks

Do not post or repost autonomously if it requires any of these claims:

- Flatkey is live.
- Flatkey saves a specific percent or dollar amount.
- Flatkey supports a provider, model, tool, API, or integration.
- Flatkey has credits, API keys, a waitlist, or access available.
- Flatkey ran a benchmark, unless the benchmark exists and can be attached.
- Flatkey has customers, users, usage volume, or product results.
- Flatkey has a roadmap, launch date, pricing, docs, or screenshots.
- Crypto-token utility, staking, governance, marketplace, airdrop, or tokenomics discourse.
- Direct competitor dunking.
- Generic AI hype unrelated to cost/routing.

## First 14-Day Cadence

Daily:

- 2 original post drafts.
- 5-10 plain repost candidates.
- 2-4 quote candidates.
- 15-30 replies under the existing Flatkey reply sprint.

Review gates:

- Original posts: review mode for the first week.
- Quote posts: review mode for the first week.
- Plain reposts from approved sources and public cost complaints can be autonomous after source rules are approved.
- Any mention of Flatkey as a working product needs human review.

## Weekly Mix

- 4 LLM cost/routing education posts.
- 3 builder question or pain-discovery posts.
- 2 public model/pricing commentary posts.
- 2 checklist/resource posts.
- 1 public pain roundup.
- 35-70 plain reposts from approved categories.
- 14-28 quote candidates with cost/routing comments.

## Approved Search Queries

- `"token cost"`
- `"LLM cost"`
- `"API credits"`
- `"Claude Code" tokens`
- `"Claude Code" limit`
- `"context window"`
- `"model routing"`
- `"LLM routing"`
- `"prompt caching"`
- `"tool calls" "LLM"`
- `"coding agent" "cost"`
- `"OpenRouter" pricing`
- `"OpenRouter" router`
- `"AI agent" "token cost"`

## Approved Source Clusters

Official/model/tool accounts:

- `OpenRouter`
- `claudeai`
- `cursor_ai`
- `openclaw`
- `Replit`
- `boltdotnew`
- `windsurf_ai`
- `vercel`

Infra/analysis accounts:

- `LiteLLM`
- `HeliconeAI`
- `PortkeyAI`
- `RequestyAI`
- `KeywordsAI`
- `notdiamond`
- `MartianRouter`
- `ArtificialAnlys`

## Daily Execution Checklist

1. Run or review the Flatkey reply sprint.
2. Collect repost candidates from approved source clusters and search queries.
3. Classify each candidate as plain repost, quote candidate, reply candidate, or skip.
4. Draft two originals from the autonomous lane.
5. Draft quote comments only when there is a specific cost/routing mechanism.
6. Block any product claim that needs builder input.
7. Log recurring pain terms and useful builder accounts for follow-up.

## Metrics

Track:

- Profile visits from AI builders.
- Replies from builders mentioning stack, spend, credits, limits, context, or routing.
- Follows from target AI-builder and AI-infra cluster.
- Quote/repost engagement.
- Recurring public pain terms.
- Whether other users tag Flatkey into cost/routing conversations.
