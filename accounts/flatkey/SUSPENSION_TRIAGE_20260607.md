# Flatkey Suspension Triage - 2026-06-07

## Reported Trigger

User reported the Flatkey/X account was suspended after International Cyber
Digest commented that a reply looked AI-generated.

## Immediate Freeze

- Paused all three Flatkey Multica autopilots in workspace
  `401521b5-22d9-411b-82ac-db2f3a0e1cb8`.
- Disabled the schedule triggers for:
  - `Flatkey X Keyword Engage`
  - `Flatkey Home/For You AI Quote/Repost Scout`
  - `Flatkey Inbound Notifications Engage`
- Disabled and stopped the Flatkey Telegram bridge
  `com.solvea.telegram-bridge-flatkey`.
- Closed the Flatkey Chrome/CDP profile on port `10006`.
- Added `state/flatkey_suspended.lock`; the three Flatkey Multica wrappers exit
  with status `75` before launching Chrome or posting.
- Changed `accounts/flatkey/engage_config.json` so Flatkey is no longer in
  auto-post mode, reply caps are zero, and keyword/inbound/home automation is
  disabled.
- Marked 24 pending Flatkey approval queue entries as `suspended`; backup saved
  as `state/reply_queue.before_flatkey_suspension_20260607T1556Z.json`.

## Evidence From Multica Run History

`Flatkey X Keyword Engage` was active and configured with
`approval_mode=auto_post`. Recent run history showed direct replies from
`@mguozhen03`, including:

- `2026-06-07T14:00Z`: auto-posted and verified 4 replies.
- `2026-06-07T13:00Z`: auto-posted and verified 2 replies.
- Earlier visible hourly runs also auto-posted replies; one run reported a
  daily total of `30/30`.

This makes automated reply volume the most likely suspension vector. The
International Cyber Digest comment may have accelerated reports or review, but
the operational risk was the direct hourly auto-post configuration.

## Do Not Re-Enable Until

- X account access is restored.
- Appeal/review is complete.
- All Flatkey automation is redesigned as draft-only, human-reviewed, and much
  lower volume.
- Any old Telegram approval cards are ignored or explicitly regenerated.
