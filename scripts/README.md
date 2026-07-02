# Scripts

The scripts in this folder support the X agent workflow:

- `engage.py` and `engage_daemon.py`: engagement queue generation and background execution
- `quote_scout.py`, `repost_agent.py`, and `inbound_engage.py`: candidate discovery and inbound engagement workflows
- `btcmind_home_timeline_scout.py`, `btcmind_repost_scout.py`, and `btcmind_signal_poster.py`: BTCMind-specific discovery and drafting utilities
- `flatkey_home_timeline_scout.py`, `flatkey_keyword_link_collector.py`, and `flatkey_autonomous_execute.py`: Flatkey-specific scouting and drafting utilities
- `reply_scorer.py` and `analytics_feedback_loop.py`: evaluation and learning loops
- `telegram_bridge.py` and `telegram.py`: human approval workflow helpers

Live credentials, runtime state, browser profiles, logs, and deployment wrappers are intentionally excluded from this portfolio version.
