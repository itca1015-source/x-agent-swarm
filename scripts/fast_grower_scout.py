"""
Fast Grower Scout

Find X accounts that appear recently created but already have meaningful
followers and engagement. This is for reverse-engineering current playbooks,
not for reply-target automation.

Outputs:
  state/fast_grower_accounts.csv
  state/fast_grower_accounts.md

Usage:
  python3 scripts/fast_grower_scout.py --limit 10 --max-authors 60
"""
import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome as _chrome
import env
import fetch as _fetch
from lock import chrome_lock

env.load()

DEFAULT_CONFIG = os.path.join(ROOT_DIR, "fast_grower_scout_config.json")
STATE_DIR = os.path.join(ROOT_DIR, "state")
LOG_DIR = os.path.join(ROOT_DIR, "logs", "fast_grower_scout")

JOINED_RE = re.compile(
    r"Joined\s+([A-Za-z]+)\s+(\d{4})|Joined\s+(\d{4})",
    re.IGNORECASE,
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")


def _state_path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def _parse_count(s: str) -> int:
    if not s:
        return 0
    s = s.strip().replace(",", "")
    mult = 1
    if s[-1:] in ("K", "k"):
        mult, s = 1_000, s[:-1]
    elif s[-1:] in ("M", "m"):
        mult, s = 1_000_000, s[:-1]
    elif s[-1:] in ("B", "b"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def _extract_handle(author_text: str, url: str) -> str:
    m = re.search(r"x\.com/([A-Za-z0-9_]+)/status/", url or "")
    if m:
        return m.group(1)
    m = re.search(r"@([A-Za-z0-9_]+)", author_text or "")
    return m.group(1) if m else ""


def _extract_joined(text: str) -> tuple[str, int]:
    m = JOINED_RE.search(text or "")
    if not m:
        return "", 0
    if m.group(3):
        return m.group(3), int(m.group(3))
    label = f"{m.group(1)} {m.group(2)}"
    return label, int(m.group(2))


def _cluster_to_account(cluster: str) -> str:
    if "crypto" in cluster or "btc" in cluster:
        return "btcmind"
    if "tool" in cluster or "coding" in cluster or "agent" in cluster:
        return "Hunter's X"
    return "vocai"


def collect_authors(config: dict, limit_per_kw: int) -> dict:
    port = int(config["chrome_port"])
    authors = {}
    for cluster, keywords in config["keyword_clusters"].items():
        for keyword in keywords:
            for mode in ("top", "live"):
                _log(f"search [{cluster}/{mode}] {keyword}")
                try:
                    tweets = _fetch.search(port, keyword, mode=mode, limit=limit_per_kw)
                except Exception as e:
                    _log(f"  search error: {e}")
                    continue
                for tweet in tweets:
                    handle = _extract_handle(tweet.get("author", ""), tweet.get("url", ""))
                    if not handle:
                        continue
                    entry = authors.setdefault(handle, {
                        "handle": handle,
                        "clusters": set(),
                        "sample_urls": [],
                        "discovery_likes": 0,
                    })
                    entry["clusters"].add(cluster)
                    entry["discovery_likes"] = max(
                        entry["discovery_likes"],
                        int(tweet.get("likes") or 0),
                    )
                    if tweet.get("url") and len(entry["sample_urls"]) < 3:
                        entry["sample_urls"].append(tweet["url"])
                time.sleep(random.uniform(2.5, 4.5))

    for raw in config.get("seed_accounts", []):
        handle = raw.lstrip("@")
        entry = authors.setdefault(handle, {
            "handle": handle,
            "clusters": set(),
            "sample_urls": [],
            "discovery_likes": 0,
        })
        entry["clusters"].add("seed")
    return authors


PROFILE_JS = r"""
(function() {
    function parseCount(s) {
        if (!s) return 0;
        s = String(s).replace(/,/g, '').trim();
        var mult = 1;
        if (/[Kk]$/.test(s)) { mult = 1000; s = s.slice(0, -1); }
        else if (/[Mm]$/.test(s)) { mult = 1000000; s = s.slice(0, -1); }
        else if (/[Bb]$/.test(s)) { mult = 1000000000; s = s.slice(0, -1); }
        var n = parseFloat(s);
        return isNaN(n) ? 0 : Math.round(n * mult);
    }
    function metric(testid) {
        var el = document.querySelector('[data-testid="' + testid + '"]');
        if (!el) return 0;
        var txt = (el.innerText || el.textContent || '').replace(/[^0-9KMB.,]/g, '');
        return parseCount(txt);
    }

    var body = document.body ? document.body.innerText : '';
    var followers = 0;
    var links = document.querySelectorAll('a[href$="/followers"], a[href$="/verified_followers"], a[href*="/followers"]');
    for (var i = 0; i < links.length; i++) {
        var txt = (links[i].innerText || links[i].textContent || '');
        var parent = links[i].parentElement ? links[i].parentElement.innerText : '';
        var m = (txt + ' ' + parent).replace(/\s+/g, ' ').match(/([0-9][0-9,.]*[KMB]?)\s*Followers?/i);
        if (m) { followers = parseCount(m[1]); break; }
    }

    var nameEl = document.querySelector('[data-testid="UserName"]');
    var name = nameEl ? (nameEl.innerText || '').replace(/\s+/g, ' ').trim() : '';
    var bioEl = document.querySelector('[data-testid="UserDescription"]');
    var bio = bioEl ? (bioEl.innerText || '').replace(/\s+/g, ' ').trim() : '';

    var tweets = [];
    document.querySelectorAll('article[data-testid="tweet"]').forEach(function(el) {
        var textEl = el.querySelector('[data-testid="tweetText"]');
        var text = textEl ? textEl.innerText.trim() : '';
        var urlEl = el.querySelector('a[href*="/status/"]');
        var url = urlEl ? urlEl.href : '';
        if (!url) return;
        tweets.push({
            url: url,
            text: text.slice(0, 240),
            replies: metricFrom(el, 'reply'),
            likes: metricFrom(el, 'like'),
            retweets: metricFrom(el, 'retweet')
        });
    });

    function metricFrom(root, testid) {
        var el = root.querySelector('[data-testid="' + testid + '"]');
        if (!el) return 0;
        var txt = (el.innerText || el.textContent || '').replace(/[^0-9KMB.,]/g, '');
        return parseCount(txt);
    }

    return JSON.stringify({body: body, followers: followers, name: name, bio: bio, tweets: tweets.slice(0, 10)});
})()
"""


def fetch_profile(port: int, handle: str) -> dict:
    ws = _chrome.connect(port)
    try:
        with chrome_lock(port, on_wait=_log):
            _chrome.navigate(ws, f"https://x.com/{handle}", wait=3.5)
            _chrome.scroll_down(ws, px=1400)
            time.sleep(1.5)
            raw = _chrome.eval_js(ws, PROFILE_JS)
    finally:
        ws.close()

    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    joined_label, joined_year = _extract_joined(data.get("body", ""))
    tweets = data.get("tweets") or []
    avg_likes = sum(int(t.get("likes") or 0) for t in tweets) / len(tweets) if tweets else 0
    avg_replies = sum(int(t.get("replies") or 0) for t in tweets) / len(tweets) if tweets else 0
    avg_retweets = sum(int(t.get("retweets") or 0) for t in tweets) / len(tweets) if tweets else 0
    return {
        "handle": handle,
        "name": data.get("name", ""),
        "bio": data.get("bio", ""),
        "followers": int(data.get("followers") or 0),
        "joined": joined_label,
        "joined_year": joined_year,
        "tweets_fetched": len(tweets),
        "avg_likes": avg_likes,
        "avg_replies": avg_replies,
        "avg_retweets": avg_retweets,
        "sample_post": tweets[0]["url"] if tweets else "",
    }


def score(row: dict, config: dict) -> int:
    followers = int(row["followers"])
    likes = float(row["avg_likes"])
    replies = float(row["avg_replies"])
    joined_year = int(row["joined_year"] or 0)
    fmin = int(config["follower_min"])
    fmax = int(config["follower_max"])
    joined_min = int(config["joined_year_min"])

    size = 5 if fmin <= followers <= fmax else 2 if followers < fmin else 3
    recent = 5 if joined_year >= joined_min else 2 if joined_year else 1
    engagement_rate = likes / followers if followers else 0
    engagement = 5 if engagement_rate >= 0.01 else 4 if engagement_rate >= 0.005 else 3 if likes >= 50 else 2 if likes > 0 else 1
    reply = 5 if replies >= 20 else 4 if replies >= 10 else 3 if replies >= 3 else 2 if replies > 0 else 1
    cadence = 5 if int(row["tweets_fetched"]) >= 6 else 3 if int(row["tweets_fetched"]) >= 3 else 1
    return size * 4 + recent * 5 + engagement * 4 + reply * 2 + cadence


def write_outputs(rows: list[dict], csv_path: str, md_path: str):
    fields = [
        "score", "account_fit", "handle", "profile_url", "followers", "joined",
        "avg_likes", "avg_replies", "avg_retweets", "tweets_fetched",
        "clusters", "discovery_likes", "sample_post", "bio",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    with open(md_path, "w") as f:
        f.write(f"# Fast-Growing X Accounts Scout\n\nGenerated: {datetime.now():%Y-%m-%d %H:%M}\n\n")
        for account in ("btcmind", "Hunter's X", "vocai"):
            f.write(f"## {account}\n\n")
            group = [r for r in rows if r["account_fit"] == account][:12]
            if not group:
                f.write("No strong candidates found in this run.\n\n")
                continue
            for r in group:
                f.write(
                    f"- @{r['handle']} ({_fmt_count(r['followers'])}, joined {r['joined'] or 'unknown'}, "
                    f"score {r['score']}) - avg likes {r['avg_likes']:.1f}, avg replies {r['avg_replies']:.1f}. "
                    f"{r['profile_url']}\n"
                )
            f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-authors", type=int, default=80)
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)
    port = int(config["chrome_port"])
    if not _chrome.ping(port):
        raise SystemExit(f"Chrome debug port {port} unavailable")

    _log(f"config: {args.config}")
    authors = collect_authors(config, args.limit)
    ordered_authors = sorted(
        authors.items(),
        key=lambda item: (
            int(item[1].get("discovery_likes") or 0),
            len(item[1].get("clusters") or []),
        ),
        reverse=True,
    )
    serializable = {
        h: {**v, "clusters": sorted(v["clusters"])}
        for h, v in ordered_authors
    }
    authors_path = _state_path("fast_grower_authors.json")
    with open(authors_path, "w") as f:
        json.dump(serializable, f, indent=2)
    _log(f"collected {len(serializable)} unique authors")

    rows = []
    fmin = int(config["follower_min"])
    fmax = int(config["follower_max"])
    max_followers = int(config["max_profile_followers"])
    joined_min = int(config["joined_year_min"])
    for i, (handle, discovery) in enumerate(list(serializable.items())[: args.max_authors], 1):
        _log(f"profile [{i}/{min(len(serializable), args.max_authors)}] @{handle}")
        try:
            row = fetch_profile(port, handle)
        except Exception as e:
            _log(f"  profile error: {e}")
            continue
        row["clusters"] = ", ".join(discovery["clusters"])
        row["account_fit"] = _cluster_to_account(row["clusters"])
        row["discovery_likes"] = int(discovery.get("discovery_likes") or 0)
        row["profile_url"] = f"https://x.com/{handle}"

        if row["followers"] <= 0 or row["followers"] > max_followers:
            _log(f"  drop followers={row['followers']}")
            continue
        if row["followers"] < fmin or row["followers"] > fmax:
            _log(f"  keep but out of target band followers={row['followers']}")
        if row["joined_year"] and row["joined_year"] < joined_min:
            _log(f"  old join date {row['joined']}; keeping as lower priority")
        row["score"] = score(row, config)
        rows.append(row)
        time.sleep(random.uniform(2.5, 4.5))

    rows.sort(key=lambda r: (r["score"], r["avg_likes"], r["followers"]), reverse=True)
    csv_path = _state_path("fast_grower_accounts.csv")
    md_path = _state_path("fast_grower_accounts.md")
    write_outputs(rows, csv_path, md_path)
    _log(f"done - {len(rows)} rows -> {csv_path} and {md_path}")
    for r in rows[:15]:
        _log(f"  @{r['handle']:<20} {_fmt_count(r['followers']):>6} joined={r['joined'] or '?':<12} score={r['score']}")


if __name__ == "__main__":
    main()
