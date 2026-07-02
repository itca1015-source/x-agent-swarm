from __future__ import annotations

from typing import Any, Mapping


OBSERVED_X_REPLY_RANKING_METADATA = (
    "is_pasted",
    "user_agent",
    "composition_source",
    "app_attestation_status",
)

DEFAULT_PUBLISH_CHANNEL = {
    "reply": "review_only",
    "quote": "review_only",
    "repost": "review_only",
    "like": "off",
    "default": "review_only",
}

DEFAULT_LANE_POLICY = {
    "keyword": "review_only",
    "target": "review_only",
    "inbound": "review_only",
    "home_quote": "review_only",
    "home_repost": "review_only",
    "repost_scout": "review_only",
}

BROWSER_PUBLISH_ACTION = "browser_publish"
REVIEW_ONLY_ACTION = "review_only"
BLOCKED_ACTION = "blocked"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _configured_channel(cfg: Mapping[str, Any], action: str) -> str:
    channels = {**DEFAULT_PUBLISH_CHANNEL, **_mapping(cfg.get("publish_channel"))}
    return str(channels.get(action) or channels.get("default") or "review_only")


def _configured_lane(cfg: Mapping[str, Any], lane: str | None) -> str | None:
    if not lane:
        return None
    lanes = {**DEFAULT_LANE_POLICY, **_mapping(cfg.get("lane_policy"))}
    value = lanes.get(lane)
    return str(value) if value else None


def _normalize_channel(channel: str | None) -> str:
    value = (channel or "review_only").strip().lower()
    aliases = {
        "review": "review_only",
        "human_review": "review_only",
        "manual": "review_only",
        "manual_review": "review_only",
        "reviewed_browser": "review_only",
        "browser_review": "review_only",
        "api": "review_only",
        "api_only": "review_only",
        "api_or_review": "review_only",
        "limited_auto_api_only": "review_only",
        "none": "off",
        "disabled": "off",
        "autonomous_browser": "browser",
    }
    return aliases.get(value, value)


def decide_public_action(
    cfg: Mapping[str, Any] | None,
    *,
    action: str,
    lane: str | None = None,
    live_requested: bool = False,
) -> dict[str, Any]:
    """Return the BTCMind publish decision without altering browser metadata.

    X's public ranking code logs browser/app composition metadata for replies.
    This helper does not try to spoof or hide those fields. It only makes the
    publication decision explicit: queue for review, block, or use browser
    publish when that lane is deliberately enabled.
    """

    cfg = _mapping(cfg)
    action = str(action or "default").strip().lower()
    lane = str(lane).strip().lower() if lane else None

    action_channel = _normalize_channel(_configured_channel(cfg, action))
    lane_channel = _normalize_channel(_configured_lane(cfg, lane))
    channel = lane_channel or action_channel

    browser_cfg = _mapping(cfg.get("browser"))
    legacy_browser_enabled = bool(cfg.get("browser_public_actions_enabled", True))
    browser_autonomous_publish = bool(
        browser_cfg.get("allow_autonomous_publish", legacy_browser_enabled)
    )

    reasons: list[str] = []
    if lane_channel:
        reasons.append(f"lane_policy:{lane}={lane_channel}")
    reasons.append(f"publish_channel:{action}={action_channel}")

    if channel == "off":
        decision_action = BLOCKED_ACTION
        reasons.append("public_action_disabled")
    elif channel == "browser":
        if live_requested and browser_autonomous_publish:
            decision_action = BROWSER_PUBLISH_ACTION
            reasons.append("browser_autonomous_publish_allowed")
        else:
            decision_action = REVIEW_ONLY_ACTION
            reasons.append("browser_publish_requires_review_or_config")
    else:
        decision_action = REVIEW_ONLY_ACTION
        reasons.append("human_review_required")

    return {
        "allowed": decision_action in {BROWSER_PUBLISH_ACTION, REVIEW_ONLY_ACTION},
        "action": decision_action,
        "action_type": action,
        "lane": lane,
        "channel": channel,
        "live_requested": bool(live_requested),
        "browser_autonomous_publish": browser_autonomous_publish,
        "observed_x_reply_ranking_metadata": list(OBSERVED_X_REPLY_RANKING_METADATA),
        "reasons": reasons,
    }


def should_browser_publish(
    cfg: Mapping[str, Any] | None,
    *,
    action: str,
    lane: str | None = None,
    live_requested: bool = False,
) -> bool:
    return (
        decide_public_action(cfg, action=action, lane=lane, live_requested=live_requested)[
            "action"
        ]
        == BROWSER_PUBLISH_ACTION
    )
