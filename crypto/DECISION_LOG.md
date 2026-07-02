# BTCMind X Agent Swarm — Decision Log

A running record of the questions asked while building the crypto agents. Seed of the future reusable agent template.

---

## 2026-05-31

- Read the README to understand the project.
- Explored the product (https://btcmind.ai/): what the industry is, who the competitors are, and who the upstream/downstream businesses are.
- Found the X accounts of competitors.
- Decided to target English users and pick a successful competitor account to emulate → chose **Nansen (@nansen_ai, ~355K followers)** as the X-management template (grew on research credibility, not token hype; tagline nearly identical to BTCMind's).
- Reverse-engineered the autopilot system needed to emulate an X account (Poster/Quoter/Replier/Reposter + intelligence/orchestration/amplification agents).
- Scraped & analyzed 593 of Nansen's posts across all 4 categories → full playbook saved to `nansen_playbook.md` (content mix, engagement benchmarks, signature "smart-money data drop" format, weekday/07–12 UTC cadence, interaction graph, ~5–6 company-owned account cluster).
- Wrote per-autopilot spec sheets (system prompt + rules + I/O schema + few-shot examples + guardrails) for the execution agents → `x-agents/` (10 agents + README with shared brand context).
