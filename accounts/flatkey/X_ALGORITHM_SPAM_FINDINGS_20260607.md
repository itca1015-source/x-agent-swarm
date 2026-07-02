# X Algorithm Reply-Spam Findings - 2026-06-07

## Purpose

Reference note for Flatkey suspension triage. This captures what the May 15,
2026 public `xai-org/x-algorithm` release shows about reply spam, reply
ranking, visibility filtering, and suspension-evasion risk.

This is not an evasion guide. Use it for appeal context and for rebuilding
Flatkey automation in a compliant, human-reviewed, low-risk way.

## Primary Public Source

- Repo: `https://github.com/xai-org/x-algorithm`
- README says the May 15, 2026 release added a `grox/` content-understanding
  pipeline with classifiers, embedders, and task execution for workloads such
  as spam detection, post-category classification, and PTOS policy enforcement.
- README also says Home Mixer hydrates user context including IP, mutual follow
  graphs, impression/served history, and other features. That is feed-ranking
  context, not full account-enforcement disclosure.

Important limitation: the repo is not X's full enforcement stack. It does not
publish exact suspension thresholds, device/IP linkage rules, bot/session
fingerprinting, browser-automation detection, production dispatcher config, or
the exact Grok spam prompt.

## Reply-Spam Pipeline

Public plan:

```text
TaskSpamFilter
-> TaskRateLimitReplySpamAnnotationWithPost
-> TaskMediaHydration
-> TaskSpamDetection
-> TaskWriteReplySpamManhattan
-> TaskPublishKafka
```

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/plans/plan_spam_comment.py`

### 1. `TaskSpamFilter`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_filters.py`

What it does:

- Runs only on posts with `post.ancestors`, so this spam path is about replies
  or thread comments, not ordinary top-level posts.
- Skips if the post has no user.
- Skips system accounts.
- Skips same-user replies where the reply author is the same as the immediate
  parent author.
- Skips replies where the reply author is the same as the root author.
- Reads follower count for the immediate parent author and root author.
- If either follower count is above `FOLLOWER_COUNT_THRESHOLD_FOR_SPAM_DETECTION`,
  the spam path is skipped with reason `reply_ranking_target`.

Important limitation: `FOLLOWER_COUNT_THRESHOLD_FOR_SPAM_DETECTION` is blank in
the public repo. The structure is visible, but the production threshold is not.

Flatkey relevance:

- Flatkey's risky path was third-party replies into other people's
  conversations. That is exactly the object shape this filter is built around.
- The public code does not use the word "unsolicited"; that is our operational
  interpretation because Flatkey replied to strangers found by keyword search.

### 2. `TaskRateLimitReplySpamAnnotationWithPost`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_rate_limit.py`

What it does:

- Keeps a short TTL cache keyed by post ID.
- Allows the reply-spam annotation work once per post ID inside that TTL.
- Stops repeated internal processing of the same reply.

Important distinction:

- This is not X's user-facing posting limit.
- It does not say "how many replies per day is safe."
- It is pipeline dedupe / backpressure, not behavioral enforcement.

### 3. `TaskMediaHydration`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_media.py`

What it does:

- Hydrates media attached to the post/thread before classification.
- The spam classifier uses a Grok/VLM sampler, so the pipeline is designed to
  support more than bare text if media exists.

Flatkey relevance:

- Most Flatkey risk was reply text, but if target/context had media, the public
  pipeline can include richer thread context before classification.

### 4. `TaskSpamDetection`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_spam_detection.py`

What it does:

- Calls `SpamEapiLowFollowerClassifier().classify(post)`.
- Appends classifier results into `ctx.content_categories`.
- Checks for positive `ContentCategoryType.SPAM_COMMENT`.
- Buckets the immediate parent/root follower counts:

```text
lte_100
lte_500
lte_1000
gt_1000
```

- If Grok returns spam and the bucket is not `gt_1000`, logs "Reply Spam Found
  for lower than 1000 follower bucket."
- Emits positive/negative spam-comment metrics with the follower bucket as a
  reason.

Flatkey relevance:

- This is direct evidence that public X code has a reply-spam classifier with
  special handling/observability for lower-follower reply contexts.
- It does not prove the suspension threshold.

### 5. `SpamEapiLowFollowerClassifier`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/classifiers/content/spam.py`

What it does:

- Builds a Grok/VLM classifier for category `ContentCategoryType.SPAM_COMMENT`.
- Uses model `ModelName.VLM_PRIMARY` with very low temperature.
- Builds a conversation with:
  - system prompt: `SpamSystemLowFollower().render()`
  - human content: `ThreadRenderer.render(post, role=Role.HUMAN, separator="\n\n")`
- Samples from the model.
- Parses JSON into `SpamSampleResult`.
- If `decision == "spam"`, returns:

```text
category = SPAM_COMMENT
positive = true
score = 1.0
summary = reason from model output
```

Important limitation:

- `SpamSystemLowFollower` is imported from `grox.prompts.template`, but that
  prompt file is not present in the public repo tree. Therefore the exact spam
  rubric is not published.
- The classifier sees the rendered thread, but the public code does not reveal
  exactly how Grok weighs "AI style", genericness, relevance, repetition, or
  intent.

### 6. `TaskWriteReplySpamManhattan`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_pub.py`

What it does:

- Iterates content-category results.
- If a result category is `SPAM_COMMENT` and `result.positive` is true, calls:

```text
grokReplySpamActionWithLabels
```

- Logs whether labels were applied.
- Saves the spam reply annotation via:

```text
ReplySpamStratoLoader.save_spam_reply_annotation(...)
```

Flatkey relevance:

- This is the public bridge from "Grok classified this reply as spam" to
  "apply labels/actions and save an internal spam annotation."
- The repo does not reveal what labels mean operationally or when they escalate
  to account suspension.

### 7. `TaskPublishKafka`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_pub.py`

What it does:

- Publishes content-analysis output to Kafka.
- Downstream systems can consume the event for ranking, visibility filtering,
  analytics, or enforcement-adjacent workflows.

Important limitation:

- The repo does not publish all Kafka consumers or account-enforcement
  decisions.

## Reply-Ranking Pipeline

Public plan:

```text
TaskReplyRankingFilter
-> TaskRateLimitReplyRankingAnnotationWithPost
-> TaskMediaHydration
-> TaskRankReplies
-> TaskWriteReplyRankingManhattan
```

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/plans/plan_reply_ranking.py`

### `TaskReplyRankingFilter`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_filters.py`

What it does:

- Requires `post.ancestors`, so it also applies to replies.
- Skips missing users and malformed ancestor users.
- Reads parent/root follower counts.
- If both counts are at or below `FOLLOWER_COUNT_THRESHOLD_FOR_REPLY_RANKING`,
  skips with reason `low_blast_radius`.
- Skips self-replies and root-author replies.
- Marks the reply as eligible for reply ranking otherwise.

Important limitation:

- `FOLLOWER_COUNT_THRESHOLD_FOR_REPLY_RANKING` is blank in the public repo.
- The structure is visible, but production threshold is not.

### `TaskRankReplies`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_rank_replies.py`

What it logs before scoring:

```text
is_pasted
user_agent
composition_source
app_attestation_status
has_risky_user_safety_label
num_legit_blocks_received_last_24hrs
```

Then it calls `ReplyScorer().score(post)` and extends
`ctx.reply_ranking_results`.

Flatkey relevance:

- This is direct evidence that public reply-ranking code observes paste,
  user-agent, composition-source, app-attestation, risky-user-label, and recent
  legitimate-block signals.
- It does not expose final browser-automation detection logic.
- It does not say CDP/Chrome automation is detected by name.

### `TaskWriteReplyRankingManhattan`

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/tasks/task_pub.py`

What it does:

- Saves the reply ranking score and reasoning.
- If reply score is exactly `0.0`, calls `grokReplySpamActionWithLabels`.

Flatkey relevance:

- Public code shows two possible label/action bridges:
  - positive `SPAM_COMMENT`
  - reply-ranking score `0.0`

## Eligibility Injection And "Does Every Reply Get Scanned?"

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/schedules/types.py`

`TaskEligibility` includes:

```text
SPAM_COMMENT
REPLY_RANKING
```

Source:
`https://github.com/xai-org/x-algorithm/blob/main/grox/generators/stream_generator.py`

`PostStreamTaskGenerator` injects both eligibilities into realtime unified post
tasks:

```python
ELIGIBILITIES_TO_INJECT = {
    TaskEligibility.SPAM_COMMENT,
    TaskEligibility.REPLY_RANKING,
}
```

Interpretation:

- This is stronger than "only a small special stream can request spam scans."
  The public default post stream can inject both spam-comment and reply-ranking
  eligibility.
- However, it still does not prove every single reply is fully scanned in
  production, because:
  - dispatcher production config is not published;
  - Kafka topic coverage is not proven from the repo alone;
  - max QPS / sampling / routing config is not fully visible;
  - `TaskSpamFilter` and `TaskReplyRankingFilter` still reject many posts;
  - the follower thresholds are redacted/blank.

Careful answer:

> The public code shows broad eligibility injection for spam-comment and
> reply-ranking work on the realtime unified post stream. It does not prove
> that every reply completes Grok spam classification in production.

## Visibility Filtering

Source:
`https://github.com/xai-org/x-algorithm/blob/main/home-mixer/candidate_hydrators/vf_candidate_hydrator.rs`

What it does:

- Calls a `VisibilityFilteringClient` for candidate tweet IDs.
- Checks primary candidate IDs, ancestors, quoted tweets, and retweeted tweets.
- Uses safety levels `TimelineHome` for in-network and
  `TimelineHomeRecommendations` for out-of-network.
- Stores `visibility_reason` and whether ancillary posts should be dropped.

Source:
`https://github.com/xai-org/x-algorithm/blob/main/home-mixer/filters/vf_filter.rs`

What it does:

- Removes candidates if `visibility_reason` indicates a safety result with
  `Action::Drop`.
- Removes candidates for other filtered reasons.

Flatkey relevance:

- Public code shows that safety/spam visibility decisions can remove content
  from feeds.
- VF internals are not included, so exact spam-to-visibility mapping is not
  visible.

## Mapping Prior Claims To Source Evidence

### "The replies were unsolicited"

Public repo wording: not stated.

Evidence:

- Spam and reply-ranking paths target replies into another account's thread.
- Self-replies/root-author replies are skipped.

Our interpretation:

- Flatkey's keyword-sourced replies were unsolicited in ordinary language
  because recipients did not opt in and often had not engaged with Flatkey.

### "Generated from keyword search"

Public repo wording: not stated.

Evidence:

- This comes from Flatkey's local automation and Multica run history, not X
  public code.

Our interpretation:

- X likely sees resulting reply/thread/content/session signals, not our local
  keyword-search intent unless it is inferable from behavior.

### "Posted automatically"

Public repo wording: not stated as `is_automated`.

Evidence:

- Reply-ranking logs `is_pasted`, `user_agent`, `composition_source`, and
  `app_attestation_status`.

Our interpretation:

- These are plausible automation/composition observability points, but the
  exact automation enforcement logic is not published.

### "Likely shared similar AI-written style"

Public repo wording: exact criterion not published.

Evidence:

- Grok/VLM classifier receives the rendered thread and emits a spam/non-spam
  decision.
- The prompt `SpamSystemLowFollower` is not public.

Our interpretation:

- Grok could judge genericness/relevance/style from the thread, but the repo
  does not expose the exact rubric.

### "May have been pasted through browser automation"

Public repo wording: no CDP/browser-automation rule is published.

Evidence:

- Reply-ranking logs `is_pasted`, `user_agent`, `composition_source`,
  `app_attestation_status`.

Our interpretation:

- Same-machine/browser automation could create unusual or repetitive composition
  metadata, but the public repo does not publish device/session enforcement.

## Flatkey Operational Evidence

Local Multica run history showed `Flatkey X Keyword Engage` was configured with
direct auto-posting from `@mguozhen03`.

Visible examples:

- `2026-06-07T13:00Z`: run auto-posted and verified 2 replies.
- `2026-06-07T14:00Z`: run auto-posted and verified 4 replies.
- Earlier visible hourly runs also auto-posted replies.
- One run reported a daily total of `30/30`.

Why this matters:

- X's public code is centered on reply spam and reply ranking.
- Flatkey's public behavior fed many automated third-party replies into that
  exact surface area.
- The International Cyber Digest comment may have accelerated reports/manual
  review, but the automated hourly reply pattern itself was high risk.

See also:
`accounts/flatkey/SUSPENSION_TRIAGE_20260607.md`

## New Account / Same Computer Risk

Policy basis:

- X's platform manipulation / spam policy prohibits suspension evasion,
  including creating replacement accounts, imitating a suspended account, or
  repurposing accounts to evade enforcement.
- X's privacy policy says it collects signals such as IP address, browser type,
  operating system, device/app identifiers, cookies/X-generated identifiers,
  and account-associated identifiers. It also says identifiers used to create a
  suspended account may be retained to prevent repeat offenders.

Practical conclusion:

- A new account with similar name, bio, topic, behavior, and operator signals
  can be flagged as suspension evasion.
- Same computer/browser/profile/IP can contribute to linkage.
- Do not create a near-clone replacement while the original account is
  suspended. Appeal first and keep Flatkey automation frozen.

## What Is Not Published

The public repo does not publish:

- Exact suspension thresholds.
- Exact Grok spam prompt text for `SpamSystemLowFollower`.
- Device fingerprinting or same-computer account-linkage rules.
- IP/proxy scoring.
- Browser/CDP automation detection logic.
- Complete production dispatcher/task-generator config.
- Kafka consumers that turn spam annotations into account-level enforcement.
- Human review policy for suspension appeals.

## Compliance Implications For Flatkey

Do not re-enable any Flatkey auto-posting until:

- the suspended account is restored;
- all pending stale approvals are discarded or regenerated;
- replies are human-written or human-approved;
- no keyword-search auto-replies are allowed;
- inbound engagement is separated from outbound stranger replies;
- volume is dramatically reduced;
- automation is draft-only by default;
- original account appeal is resolved before any new-account activity.
