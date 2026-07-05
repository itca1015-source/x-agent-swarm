# x-agent-swarm

Multi-product X automation system for running brand-specific growth agents across several startup products.

This repo is a cleaned portfolio version of an internal automation workspace. It focuses on the reusable architecture: account playbooks, content strategy, discovery scripts, engagement drafting, approval workflows, analytics, and safety guardrails.

## What It Does

The system helps product teams operate X accounts with a repeatable human-in-the-loop workflow:

- Discover relevant conversations, creators, prospects, and market narratives
- Draft replies, quote posts, repost candidates, and original content ideas
- Apply product-specific voice, audience, and claim-safety rules
- Queue higher-risk actions for human approval before posting
- Track performance signals and improve future drafting

The goal is not blind auto-posting. The goal is controlled, measurable growth automation that keeps brand voice and reputation intact.

## Start Here: the Article Engine

[`article-engine/`](article-engine/) is the long-form content pipeline behind the founder account this system runs, and the best place to see the whole philosophy working:

- **Output:** 113 posts in the account's first 4 weeks, including 10 long-form X Articles, operated by one person
- **Quality loop:** drafts are scored by an LLM judge against [18 versioned posting rules](article-engine/POSTING_RULES.md), and every rule traces back to a dated entry in the [feedback log](article-engine/FEEDBACK_LOG.md)
- **Full projects included:** article source, infographic generators, build scripts, and per-project feedback

The operating principle across this repo: agents run the production line; human time goes to taste — what to test next and what to kill.

## Featured Product Workspaces

### Flatkey

AI infrastructure and LLM cost/routing content system.

- Finds conversations about AI agents, coding agents, model routing, token cost, permissions, and agent payments
- Drafts practical replies and quote posts for builder audiences
- Uses claim-safety rules to avoid unsupported product claims
- Routes substantive drafts through approval before public action

Key docs:

- `accounts/flatkey/playbook.md`
- `accounts/flatkey/AUTONOMOUS_CONTENT_RUNBOOK.md`
- `accounts/flatkey/autonomous_content_policy.json`

### BTCMind

Crypto intelligence X agent system.

- Scouts crypto and market-structure conversations
- Filters out price calls, token shilling, vague hype, and unsafe claims
- Drafts quote/repost candidates for human review
- Uses analytics and quality rubrics to judge whether posts match the account strategy

Key docs:

- `accounts/hunter_solvea/playbook.md`
- `accounts/hunter_solvea/analytics_judgement.md`
- `crypto/specs_for_agents/README.md`

### VOC.ai

Ecommerce and customer-review insight brand agent.

- Targets Amazon seller and ecommerce operator conversations
- Focuses on customer language, review patterns, returns, listing quality, and retention
- Keeps replies concise, useful, and grounded in buyer-signal insights

Key docs:

- `accounts/VOC_ai/playbook.md`

### SolveaCX

AI receptionist brand account for SMBs.

- Defines positioning for salons, dental clinics, and local service businesses
- Focuses on missed calls, staffing costs, after-hours coverage, appointment booking, and SMS follow-up
- Uses strict brand-voice rules for short, data-backed replies

Key docs:

- `accounts/SolveaCX/playbook.md`

## Architecture

The project separates shared automation from product-specific strategy.

- `accounts/` contains brand playbooks, safety policies, and evaluation rubrics
- `scripts/` contains discovery, drafting, scoring, queue, approval, and analytics utilities
- `article-engine/` contains the long-form article pipeline: posting rules, feedback log, template, and example projects
- `crypto/specs_for_agents/` contains agent spec sheets for a BTCMind-focused agent swarm
- `docs/` contains architecture notes and decision logs

Core agent layers:

- Listener / radar: discover relevant posts and market signals
- Connector / scout: identify accounts, audiences, and conversations
- Replier / quoter: draft context-specific replies and quote posts
- Brand safety gate: block unsupported claims, spammy behavior, and risky topics
- Scheduler / orchestrator: manage timing and workflow state
- Analytics / learning: review performance and update strategy

## Stack

- Python
- Browser automation
- JSON-based account configuration
- Telegram-style approval workflows
- Scheduling/orchestration scripts
- LLM-assisted drafting and evaluation
- Growth analytics and content-quality rubrics

## Safety Notes

This portfolio version intentionally excludes local runtime files, credentials, browser profiles, private state, logs, and operational handoff details. Public docs focus on architecture and product strategy, not live account credentials or deployment setup.
