# X Agent Swarm

This project is the operating system for running an adaptable and scalable X platform across multiple company products.

The company goal is to create many products, potentially ten or more, and build an agent swarm that can market any product on X without being tightly coupled to one brand, audience, or offer.

## Current Product Portfolio

### VOC.ai

VOC.ai is an Amazon review and customer insight product with roughly 400,000 users.

### Solvea.cx

Solvea.cx is an AI receptionist product serving hundreds of ToB users.

### Flatkey

Flatkey is a token product. For now, it can be interpreted as a product that sells tokens or makes token usage cheaper, similar in spirit to OpenRouter.

### BTCMind

BTCMind is a crypto product.

## Current Execution Focus

As of May 31, 2026, the near-term focus is BTCMind first.

Because the system is still in the experimentation stage, do not try to write the reusable agent template for later products upfront. Instead, as the crypto agents are built, keep a running decision log that captures every meaningful product, agent, content, targeting, scheduling, and workflow decision and the reason behind it.

Examples:

- Chose 4 personas because X.
- Post at these times because Y.
- Prioritized these crypto audiences because Z.

That running decision log is the seed of the future reusable agent template.

## Mission

Build agents that operate X as a scalable growth and marketing system for the entire product portfolio.

The agent swarm should be able to:

- Understand different products, audiences, and market narratives.
- Generate and test content strategies for each product.
- Discover relevant conversations, creators, prospects, and communities.
- Engage in ways that are useful, contextual, and aligned with each brand.
- Reuse common infrastructure while allowing each product to have its own positioning, tone, and funnel.
- Scale from one product to many without requiring a full rebuild.

## Design Principles

- Product-agnostic core: agents should use shared systems for discovery, posting, engagement, analytics, and safety.
- Product-specific configuration: each product should define its own audience, positioning, offers, keywords, competitors, and voice.
- Human-controllable automation: agents should support review, override, and approval workflows where needed.
- Measurable growth: every agent action should connect back to signals such as impressions, replies, leads, signups, trials, demos, or revenue.
- Fast experimentation: the system should make it easy to test hooks, narratives, target audiences, and engagement strategies.
- Safety and reputation: agents should avoid spam, low-quality replies, brand confusion, and risky claims.

## Agent Swarm Goals

The swarm should eventually include agents for:

- Market and trend discovery
- Audience and account scouting
- Competitor tracking
- Content ideation
- Post drafting
- Reply and engagement drafting
- Review and approval
- Posting and scheduling
- Lead capture and routing
- Performance analysis
- Playbook generation

## Long-Term Direction

The goal is not only to automate posting. The goal is to create a reusable go-to-market machine for X that can launch, learn, and scale across VOC.ai, Solvea.cx, Flatkey, BTCMind, and future products.
