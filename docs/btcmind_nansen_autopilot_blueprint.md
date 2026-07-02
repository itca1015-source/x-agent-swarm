# BTCMind Nansen-Derived Autopilot Blueprint

Date: 2026-06-01

## Purpose

BTCMind should emulate Nansen's operating mechanics, not its exact voice or
token-call behavior.

The target system is a proof-distribution loop:

1. Product or market signal appears.
2. BTCMind turns it into a compact data post.
3. A continuation reply adds evidence, link, caveat, or what to watch.
4. Users, partners, or analysts post proof that BTCMind surfaced something.
5. BTCMind reposts or quotes the best proof.
6. Analytics feeds the next run.

Nansen's account works because the action layer is fed by a strong evidence and
community layer. A repost autopilot without proof supply will be weak.

## Research Basis

Fresh Nansen sample saved at:

- `state/nansen_reverse_engineering_500.json`

Deduped records:

- Originals: 181
- Reposts: 66
- Quotes: 109
- Replies/thread-continuations: 132
- Community mentions: 105

## Autopilot Set

### 1. Signal Source

Job: Convert BTCMind product output into structured posting inputs.

Inputs:

- Market or wallet signal
- Bull thesis
- Bear thesis
- Confidence or disagreement among BTCMind analysts
- Evidence lines
- Risk notes
- Optional source URL

Output schema:

```json
{
  "id": "stable_unique_id",
  "type": "wallet_flow | debate_result | market_structure | operator_watch",
  "summary": "one sentence describing the signal",
  "evidence": ["specific evidence line", "specific evidence line"],
  "bull_case": "optional",
  "bear_case": "optional",
  "operator_watch": "what builders/traders/operators should watch next",
  "risk_notes": "what would make this signal wrong or overfit",
  "source_url": "optional"
}
```

### 2. Poster

Job: Publish or queue original BTCMind posts from Signal Source.

Nansen-derived templates to adapt:

- Wallet/flow alert: entity + amount/change + time window + implication.
- Divergence: price/news says one thing, wallets/flows say another.
- Product/integration proof: feature is live + what it lets users see/do.
- Market-structure thread: big market + hidden constraint + next test.
- Campaign/community post: clear user action + reward/proof mechanism.

BTCMind rule: originals should feel like an AI crypto research desk surfacing
the machinery under the headline.

Default route: queue for human review. Do not auto-publish originals until the
template performance and safety gates are proven.

### 3. Thread/CTA Continuation

Job: Add follow-up replies to BTCMind originals.

Nansen uses replies mostly as continuations, not casual chatter. Common shapes:

- "Track this here"
- "The broader picture"
- "What to watch next"
- "Full report"
- "2/ ..." evidence steps

BTCMind adaptation:

- Add the bear side after a bullish signal.
- Add the condition that would invalidate the signal.
- Link to a BTCMind artifact when available.
- Ask one operator-grade question.

### 4. Reposter

Job: Amplify external proof, not generic praise.

Repost candidates:

- User posts a BTCMind screenshot.
- User says BTCMind caught a move or framed a debate well.
- Partner announces BTCMind integration or use case.
- Analyst uses BTCMind output to support a market read.

Reject candidates:

- Empty compliments.
- Giveaway spam.
- Pure price-target claims.
- Posts that imply guaranteed returns.

### 5. Quoter

Job: Add BTCMind's data layer to strong external posts.

Quote only when BTCMind can add one of:

- Missing mechanism.
- Counter-signal.
- Wallet/flow context.
- Operator implication.
- Risk or measurement problem.

Do not quote with generic agreement.

### 6. Replier

Job: Join high-signal conversations under target accounts.

Reply shapes:

- "The hidden constraint is [mechanism]."
- "The useful split is [cohort A] vs [cohort B]."
- "This only matters if [condition]. Otherwise [failure mode]."
- "What would change my mind is [specific signal]."

Replies should usually be one or two sentences.

### 7. Listener/Radar

Job: Feed the action autopilots.

Watch:

- Mentions of BTCMind and screenshots.
- BTC, stablecoin, custody, wallet UX, market-structure, security, and crypto AI narratives.
- Target ecosystem accounts.
- Product-adjacent launch/news events.
- Fast-moving posts with high reply velocity.

### 8. Audience Scout

Job: Maintain interaction lists.

Starting communities from Nansen analysis:

- Onchain analysts and wallet-flow accounts.
- Hyperliquid/data traders.
- AI-agent crypto builders.
- Base, Virtuals, OKX, Solana, Injective, Mantle-style ecosystem accounts.
- BTC/stablecoin/custody/security operators.

### 9. UGC Engine

Job: Create the supply that Reposter amplifies.

Mechanics:

- Encourage users to post their BTCMind debate/result screenshots.
- Give them a precise prompt: "post the signal, what BTCMind saw, and what happened next."
- Track and repost the best examples.
- Build a weekly "signals users caught with BTCMind" habit.

### 10. Scheduler

Job: Sequence actions safely.

Starting schedule from Nansen timing:

- 2am PT: data original.
- 5am PT: continuation reply or CTA.
- 11am-1pm PT: quote/reply into US crypto conversation.
- 5pm-8pm PT: community proof repost or second original.

### 11. Safety Gate

Block:

- Price targets.
- Return guarantees.
- "will pump", "easy money", "100x", "can't lose".
- Financial advice.
- Unverified claims about named people or funds.
- Shilling thin-liquidity tokens.
- Posts that only restate news without a mechanism.

Require:

- Evidence or uncertainty.
- A next signal to watch.
- No hashtags, emojis, or empty hype for BTCMind.

### 12. Analytics Learner

Job: Update template weights.

Track by category:

- Hook type.
- Template.
- Post time.
- Media/link usage.
- Engagement after 1h, 6h, 24h.
- Repost/quote/reply downstream effects.
- Whether the post generated UGC or inbound.

## Initial Build Order

1. Signal Source + Poster.
2. UGC/Community Radar.
3. Quote Scout tuned to BTCMind templates.
4. Continuation Reply generator.
5. Reposter rules.
6. Analytics feedback loop.

## First Implementation Decision

The first autopilot should only create reviewable original-post drafts. BTCMind
does not yet have enough account-specific evidence to auto-publish originals.
The action surface should be:

```bash
python3 scripts/btcmind_signal_poster.py \
  --config accounts/hunter_solvea/engage_config.json \
  --signals-file state/btcmind_signals.json \
  --max-drafts 1
```

For one-off manual testing:

```bash
python3 scripts/btcmind_signal_poster.py \
  --config accounts/hunter_solvea/engage_config.json \
  --signal "Stablecoin payment volume is rising, but the operator constraint is failed settlement handling, not checkout UX." \
  --dry-run
```
