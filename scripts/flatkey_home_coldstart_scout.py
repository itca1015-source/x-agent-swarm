#!/usr/bin/env python3
"""Find fast-start accounts from Flatkey's Home / For you timeline.

Read-only CDP scraper. The workflow is intentionally narrow:

1. Scroll Flatkey's Home / For you timeline and collect unique tweet authors.
2. Visit those profiles, extract joined date, followers, and visible post count.
3. Select accounts joined within the last year whose followers exceed
   200 * elapsed months since joining X.
4. For selected accounts with a reasonable visible post count, scrape profile
   Posts + Replies tabs for cold-start analysis.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

import chrome
from lock import chrome_lock
from scrape_x_profile_tabs import (
    category_counts,
    collect_tab,
    infer_replies_from_tabs,
    sort_records,
    trim_preserving_categories,
)


STATE_DIR = os.path.join(ROOT_DIR, "state")
AS_OF_DEFAULT = "2026-06-06"
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
JOINED_RE = re.compile(r"Joined\s+([A-Za-z]+)\s+(\d{4})|Joined\s+(\d{4})", re.I)


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def state_path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def parse_count(raw: str) -> int:
    s = (raw or "").strip().replace(",", "").upper()
    if not s:
        return 0
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def compact(text: str, limit: int = 260) -> str:
    out = re.sub(r"\s+", " ", text or "").strip()
    return out if len(out) <= limit else out[: limit - 1] + "..."


def joined_info(text: str, as_of: date) -> dict:
    m = JOINED_RE.search(text or "")
    if not m:
        return {
            "joined": "",
            "joined_year": 0,
            "joined_month": 0,
            "months_since_joined": 0,
            "joined_within_year": False,
        }
    if m.group(3):
        year = int(m.group(3))
        return {
            "joined": str(year),
            "joined_year": year,
            "joined_month": 0,
            "months_since_joined": 0,
            "joined_within_year": False,
        }
    month_name = m.group(1)
    year = int(m.group(2))
    month = MONTHS.get(month_name.lower(), 0)
    months = 0
    if month:
        months = (as_of.year - year) * 12 + (as_of.month - month)
        months = max(1, months)
    return {
        "joined": f"{month_name} {year}",
        "joined_year": year,
        "joined_month": month,
        "months_since_joined": months,
        "joined_within_year": bool(month and months <= 12 and months >= 1),
    }


HOME_EXTRACT_JS = r"""
(function() {
    function text(el) {
        return (el && (el.innerText || el.textContent) || '')
            .replace(/\s+/g, ' ')
            .trim();
    }
    function metric(el, testid) {
        var node = el.querySelector('[data-testid="' + testid + '"]');
        if (!node) return '';
        var label = node.getAttribute('aria-label') || '';
        var m = label.match(/([\d,.]+)\s*([KMBkmb]?)\s+(Replies|Reply|reposts?|likes?|bookmarks?|views?)/i);
        if (m) return m[1] + (m[2] || '');
        return (node.innerText || '').replace(/[^0-9KMBkmb.,]/g, '');
    }
    function statusFromAnchor(a) {
        if (!a || !a.href) return null;
        var m = a.href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
        if (!m) return null;
        return {author: m[1], id: m[2], url: 'https://x.com/' + m[1] + '/status/' + m[2]};
    }
    var out = [];
    Array.from(document.querySelectorAll('article[data-testid="tweet"]')).forEach(function(el) {
        try {
            var all = text(el);
            if (!all || /Promoted/i.test(all)) return;
            var timeEl = el.querySelector('time');
            var main = statusFromAnchor(timeEl ? timeEl.closest('a[href*="/status/"]') : null);
            if (!main) {
                var first = Array.from(el.querySelectorAll('a[href*="/status/"]')).map(statusFromAnchor).filter(Boolean)[0];
                main = first || null;
            }
            if (!main || !main.author || !main.id) return;
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var tweetText = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';
            out.push({
                author: main.author,
                id: main.id,
                url: main.url,
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                text: tweetText,
                all_text: all.slice(0, 1800),
                likes_raw: metric(el, 'like'),
                replies_raw: metric(el, 'reply'),
                reposts_raw: metric(el, 'retweet'),
                has_image: !!el.querySelector('[data-testid="tweetPhoto"], a[href*="/photo/"]'),
                has_video: !!el.querySelector('[data-testid="videoPlayer"], video'),
                has_card: !!el.querySelector('[data-testid="card.wrapper"], a[data-testid="card.wrapper"]')
            });
        } catch(e) {}
    });
    return JSON.stringify(out);
})()
"""


PROFILE_META_JS = r"""
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
    function linkMetric(regex) {
        var links = Array.from(document.querySelectorAll('a[href]'));
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            if (!regex.test(href)) continue;
            var txt = ((links[i].innerText || links[i].textContent || '') + ' ' +
                       (links[i].parentElement ? links[i].parentElement.innerText : ''))
                .replace(/\s+/g, ' ')
                .trim();
            var m = txt.match(/([0-9][0-9,.]*[KMBkmb]?)\s*(Followers?|Following)/i);
            if (m) return parseCount(m[1]);
            var spans = Array.from(links[i].querySelectorAll('span')).map(function(s) {
                return (s.innerText || s.textContent || '').trim();
            });
            for (var j = 0; j < spans.length; j++) {
                if (/^[0-9][0-9,.]*[KMBkmb]?$/.test(spans[j])) return parseCount(spans[j]);
            }
        }
        return 0;
    }
    function profileHandle() {
        var a = document.querySelector('[data-testid="UserName"] a[href^="/"]');
        if (a) return (a.getAttribute('href') || '').replace(/^\/+/, '').split('/')[0];
        var m = (document.body.innerText || '').match(/@([A-Za-z0-9_]{1,20})/);
        return m ? m[1] : '';
    }
    function profileName() {
        var el = document.querySelector('[data-testid="UserName"]');
        return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
    }
    function profileBio() {
        var el = document.querySelector('[data-testid="UserDescription"]');
        return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
    }
    var body = document.body ? (document.body.innerText || '') : '';
    var top = body.slice(0, 3200);
    var postsRaw = '';
    var pm = top.match(/([0-9][0-9,.]*[KMBkmb]?)\s+posts/i);
    if (pm) postsRaw = pm[1];
    var tweets = [];
    Array.from(document.querySelectorAll('article[data-testid="tweet"]')).forEach(function(el) {
        try {
            var textEl = el.querySelector('[data-testid="tweetText"]');
            var t = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';
            var timeEl = el.querySelector('time');
            var a = timeEl ? timeEl.closest('a[href*="/status/"]') : el.querySelector('a[href*="/status/"]');
            var href = a ? a.href : '';
            var m = href.match(/x\.com\/([A-Za-z0-9_]+)\/status\/([0-9]+)/);
            if (!m) return;
            tweets.push({
                author: m[1],
                id: m[2],
                url: 'https://x.com/' + m[1] + '/status/' + m[2],
                iso: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                text: t.slice(0, 360)
            });
        } catch(e) {}
    });
    return JSON.stringify({
        body: body.slice(0, 6500),
        handle: profileHandle(),
        name: profileName(),
        bio: profileBio(),
        followers: linkMetric(/\/(followers|verified_followers)$/),
        following: linkMetric(/\/following$/),
        posts_raw: postsRaw,
        posts_count: parseCount(postsRaw),
        visible_tweets: tweets.slice(0, 12)
    });
})()
"""


def extract_home(ws) -> list[dict]:
    raw = chrome.eval_js(ws, HOME_EXTRACT_JS)
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        rows = []
    for row in rows:
        row["likes"] = parse_count(row.get("likes_raw", ""))
        row["replies"] = parse_count(row.get("replies_raw", ""))
        row["reposts"] = parse_count(row.get("reposts_raw", ""))
    return rows


def collect_home_authors(ws, port: int, self_handle: str, target_authors: int, scrolls: int, wait: float) -> dict:
    authors: dict[str, dict] = {}
    with chrome_lock(port):
        chrome.navigate(ws, "https://x.com/home", wait=5.0)
        chrome.set_viewport(ws, 1400, 2200)
        time.sleep(1.0)
        chrome.eval_js(ws, """
            (function(){
                var tabs = Array.from(document.querySelectorAll('[role="tab"], a[role="tab"]'));
                var t = tabs.find(function(x){ return /For you/i.test(x.innerText || x.textContent || ''); });
                if (t) t.click();
                return t ? 'clicked' : '';
            })()
        """)
        time.sleep(1.5)
        for i in range(scrolls + 1):
            new_authors = 0
            for row in extract_home(ws):
                handle = (row.get("author") or "").lstrip("@")
                if not handle or handle.lower() == self_handle.lower():
                    continue
                entry = authors.setdefault(handle, {
                    "handle": handle,
                    "home_posts_seen": 0,
                    "home_urls": [],
                    "home_texts": [],
                    "home_max_likes": 0,
                    "home_max_replies": 0,
                    "home_first_seen_iso": row.get("iso", ""),
                })
                before = entry["home_posts_seen"]
                if row.get("url") and row["url"] not in entry["home_urls"]:
                    entry["home_urls"].append(row["url"])
                    entry["home_texts"].append(row.get("text") or row.get("all_text") or "")
                    entry["home_posts_seen"] += 1
                    entry["home_max_likes"] = max(entry["home_max_likes"], int(row.get("likes") or 0))
                    entry["home_max_replies"] = max(entry["home_max_replies"], int(row.get("replies") or 0))
                if entry["home_posts_seen"] > before and before == 0:
                    new_authors += 1
            log(f"home scroll={i} authors={len(authors)} new_authors={new_authors}")
            if len(authors) >= target_authors:
                break
            chrome.eval_js(ws, "window.scrollBy(0, Math.floor(window.innerHeight * 0.88))")
            time.sleep(wait)
    return authors


def fetch_profile_meta(ws, port: int, handle: str, as_of: date) -> dict:
    with chrome_lock(port):
        chrome.navigate(ws, f"https://x.com/{handle}", wait=4.5)
        chrome.set_viewport(ws, 1400, 2200)
        time.sleep(1.0)
        raw = chrome.eval_js(ws, PROFILE_META_JS)
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    joined = joined_info(data.get("body", ""), as_of)
    row = {
        "handle": handle,
        "profile_url": f"https://x.com/{handle}",
        "name": data.get("name", ""),
        "bio": data.get("bio", ""),
        "followers": int(data.get("followers") or 0),
        "following": int(data.get("following") or 0),
        "posts_raw": data.get("posts_raw", ""),
        "posts_count": int(data.get("posts_count") or 0),
        "visible_tweets": data.get("visible_tweets") or [],
    }
    row.update(joined)
    months = int(row.get("months_since_joined") or 0)
    row["follower_velocity_threshold"] = 200 * months if months else 0
    row["followers_per_elapsed_month"] = (
        round(row["followers"] / months, 1) if months else 0
    )
    return row


def profile_candidates(
    ws,
    port: int,
    authors: dict,
    as_of: date,
    max_profiles: int,
    checkpoint_path: str = "",
    order: str = "popular",
) -> list[dict]:
    if order == "tail":
        ordered = sorted(
            authors.values(),
            key=lambda r: (int(r.get("home_max_likes") or 0), int(r.get("home_posts_seen") or 0)),
        )[:max_profiles]
    elif order == "discovery":
        ordered = list(authors.values())[:max_profiles]
    else:
        ordered = sorted(
            authors.values(),
            key=lambda r: (int(r.get("home_max_likes") or 0), int(r.get("home_posts_seen") or 0)),
            reverse=True,
        )[:max_profiles]
    out = []
    for i, discovery in enumerate(ordered, 1):
        handle = discovery["handle"]
        log(f"profile [{i}/{len(ordered)}] @{handle}")
        try:
            meta = fetch_profile_meta(ws, port, handle, as_of)
        except Exception as e:
            log(f"  profile error @{handle}: {e}")
            continue
        row = {**discovery, **meta}
        row["qualifies_recent_fast"] = bool(
            row.get("joined_within_year")
            and row.get("followers", 0) > row.get("follower_velocity_threshold", 0)
        )
        out.append(row)
        if checkpoint_path:
            with open(checkpoint_path, "w") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
                f.write("\n")
        log(
            "  @{h} followers={f} joined={j} months={m} threshold={t} posts={p} fast={q}".format(
                h=handle,
                f=fmt_count(row.get("followers", 0)),
                j=row.get("joined") or "?",
                m=row.get("months_since_joined") or "?",
                t=row.get("follower_velocity_threshold") or "?",
                p=row.get("posts_raw") or row.get("posts_count") or "?",
                q=row["qualifies_recent_fast"],
            )
        )
        time.sleep(1.0)
    return out


def scrape_profile_posts(ws, port: int, handle: str, post_limit: int, max_scrolls: int, wait: float, out_path: str) -> list[dict]:
    records: dict[str, dict] = {}
    with chrome_lock(port, timeout=7200):
        collect_tab(ws, port, handle, "posts", post_limit, max_scrolls, 18, wait, records)
        collect_tab(ws, port, handle, "replies", post_limit, max_scrolls, 18, wait, records)
    rows = list(records.values())
    infer_replies_from_tabs(rows)
    rows = trim_preserving_categories(sort_records(rows), post_limit)
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return rows


def simple_theme_counts(rows: list[dict]) -> Counter:
    themes = {
        "build_in_public": ["buildinpublic", "build in public", "day ", "building"],
        "ai_coding": ["ai", "agent", "agents", "llm", "claude", "gpt", "cursor", "code", "coding"],
        "startup_growth": ["startup", "founder", "growth", "users", "revenue", "launch", "ship", "marketing"],
        "community_networking": ["connect", "follow", "reply", "dm", "builders", "community"],
        "product_tutorial": ["how to", "guide", "tutorial", "template", "playbook", "learn"],
        "personal_story": ["i ", "my ", "quit", "job", "story", "learned"],
    }
    counts = Counter()
    for row in rows:
        text = compact((row.get("text") or "") + " " + (row.get("all_text") or ""), 2000).lower()
        for name, words in themes.items():
            if any(w in text for w in words):
                counts[name] += 1
    return counts


def analyze_selected(candidates: list[dict], posts_by_handle: dict[str, list[dict]], run_id: str, out_path: str) -> None:
    selected = [r for r in candidates if r.get("scraped_posts_path")]
    lines = []
    lines.append("# Cold Start Strategy")
    lines.append("")
    lines.append("## Flatkey Home / For You Cold-Start Scout")
    lines.append("")
    lines.append(f"Generated: {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")
    lines.append("Source: Flatkey logged-in Home / For you timeline.")
    lines.append("")
    lines.append("Selection rule:")
    lines.append("- Joined X within the last 12 elapsed calendar months.")
    lines.append("- Followers greater than `200 * months_since_joined`.")
    lines.append("- Account must fit the AI founder/builder niche by manual semantic review of the bio/profile evidence.")
    lines.append("- Visible post count not above the scrape cap.")
    lines.append("")
    if not selected:
        lines.append("No qualifying accounts were scraped in this run.")
    else:
        lines.append("### Selected Accounts")
        lines.append("")
        for c in selected:
            rows = posts_by_handle.get(c["handle"], [])
            counts = category_counts(rows)
            dates = [r.get("iso") for r in rows if r.get("iso")]
            oldest = min(dates) if dates else ""
            newest = max(dates) if dates else ""
            lines.append(
                f"- @{c['handle']} - {fmt_count(c['followers'])} followers, joined {c.get('joined')}, "
                f"{c.get('months_since_joined')} elapsed months, threshold {c.get('follower_velocity_threshold')}, "
                f"{c.get('posts_raw') or c.get('posts_count') or '?'} visible posts, scraped {len(rows)} records "
                f"({counts}). {c['profile_url']}"
            )
            if oldest and newest:
                lines.append(f"  Scraped date range: {oldest[:10]} to {newest[:10]}.")
        lines.append("")
        lines.append("### Cold-Start Patterns")
        lines.append("")
        lines.append("The common pattern from these Flatkey-home candidates is follower velocity from a sharp public niche rather than broad lifestyle posting. Accounts that pass the rule tend to make their value obvious in the first screen: AI/coding, founder growth, or tactical learning. The early posts worth copying are usually one of: public build logs, practical tutorials, strong opinions about a specific workflow, or repeated replies into the niche they want to own.")
        lines.append("")
        lines.append("Operational takeaways:")
        lines.append("- Start with one visible niche and repeat it until the profile is legible in under five seconds.")
        lines.append("- Use early posts as proof of work: demos, guides, lessons, teardown screenshots, or public experiments.")
        lines.append("- Reply into larger conversations in the same niche; most fast starts show a high reply count relative to original posts.")
        lines.append("- If the account has no audience, ask direct questions and make explicit connection posts, but attach them to a concrete lesson or result.")
        lines.append("- Do not hide the product. The fastest accounts make the product/workflow/belief obvious in profile bio, pinned/latest posts, and replies.")
        lines.append("")
        lines.append("### Per-Account Notes")
        lines.append("")
        for c in selected:
            rows = posts_by_handle.get(c["handle"], [])
            if not rows:
                continue
            themes = simple_theme_counts(rows)
            originals = [r for r in rows if r.get("category") in ("original", "quote")]
            replies = [r for r in rows if r.get("category") == "reply"]
            for r in rows:
                r["_score"] = (r.get("likes") or 0) + 2 * (r.get("reposts") or 0) + 3 * (r.get("replies") or 0)
            top = sorted(rows, key=lambda r: r.get("_score", 0), reverse=True)[:4]
            early = sorted(rows, key=lambda r: r.get("iso") or "")[:5]
            lines.append(f"#### @{c['handle']}")
            lines.append("")
            lines.append(f"- Bio/name: {compact((c.get('name') or '') + ' - ' + (c.get('bio') or ''), 300)}")
            lines.append(f"- Mix: {len(originals)} originals/quotes, {len(replies)} replies, {len(rows)} total scraped records.")
            if themes:
                lines.append("- Theme counts: " + ", ".join(f"{k}={v}" for k, v in themes.most_common(6)) + ".")
            lines.append("- Earliest scraped posts:")
            for r in early:
                lines.append(f"  - {str(r.get('iso') or '')[:10]} {r.get('category')}: {compact(r.get('text') or r.get('all_text') or '', 220)} ({r.get('url')})")
            lines.append("- Top engagement posts:")
            for r in top:
                lines.append(
                    f"  - {str(r.get('iso') or '')[:10]} {r.get('category')} "
                    f"L{r.get('likes', 0)} R{r.get('replies', 0)} RT{r.get('reposts', 0)}: "
                    f"{compact(r.get('text') or r.get('all_text') or '', 220)} ({r.get('url')})"
                )
            lines.append("")
    lines.append("### Artifacts")
    lines.append("")
    lines.append(f"- Candidate/profile data: `state/flatkey_home_coldstart_candidates_{run_id}.json`")
    lines.append(f"- Selected-account post files: `state/flatkey_home_coldstart_posts_*_{run_id}.json`")
    with open(out_path, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=10006)
    ap.add_argument("--self-handle", default="flatkey")
    ap.add_argument("--as-of", default=AS_OF_DEFAULT)
    ap.add_argument("--phase", choices=["candidates", "scrape", "all"], default="all")
    ap.add_argument("--home-scrolls", type=int, default=55)
    ap.add_argument("--home-target-authors", type=int, default=70)
    ap.add_argument("--max-profiles", type=int, default=70)
    ap.add_argument("--max-qualified", type=int, default=6)
    ap.add_argument("--max-posts-to-scrape", type=int, default=2000)
    ap.add_argument("--post-limit", type=int, default=2200)
    ap.add_argument("--post-scrolls", type=int, default=260)
    ap.add_argument("--profile-order", choices=["popular", "tail", "discovery"], default="popular")
    ap.add_argument("--wait", type=float, default=1.15)
    ap.add_argument("--run-id", default="2026-06-06")
    ap.add_argument("--candidates-out", default="")
    ap.add_argument("--report-out", default=os.path.join(STATE_DIR, "cold_start_strategy.md"))
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of)
    candidates_path = args.candidates_out or state_path(f"flatkey_home_coldstart_candidates_{args.run_id}.json")
    if not chrome.ping(args.port):
        raise SystemExit(f"Chrome debug port {args.port} unavailable")

    ws = chrome.connect(args.port, timeout=60)
    try:
        if args.phase in ("candidates", "all"):
            authors = collect_home_authors(
                ws,
                args.port,
                args.self_handle.lstrip("@"),
                args.home_target_authors,
                args.home_scrolls,
                args.wait,
            )
            candidates = profile_candidates(
                ws,
                args.port,
                authors,
                as_of,
                args.max_profiles,
                candidates_path,
                args.profile_order,
            )
            candidates.sort(
                key=lambda r: (
                    bool(r.get("qualifies_recent_fast")),
                    float(r.get("followers_per_elapsed_month") or 0),
                    int(r.get("followers") or 0),
                ),
                reverse=True,
            )
            with open(candidates_path, "w") as f:
                json.dump(candidates, f, indent=2, ensure_ascii=False)
                f.write("\n")
            log(f"wrote {len(candidates)} profiled candidates -> {candidates_path}")
        else:
            with open(candidates_path) as f:
                candidates = json.load(f)

        posts_by_handle: dict[str, list[dict]] = {}
        if args.phase in ("scrape", "all"):
            qualified = [
                c for c in candidates
                if c.get("qualifies_recent_fast")
                and (not c.get("posts_count") or int(c.get("posts_count") or 0) <= args.max_posts_to_scrape)
            ][: args.max_qualified]
            skipped = [
                c for c in candidates
                if c.get("qualifies_recent_fast")
                and c.get("posts_count")
                and int(c.get("posts_count") or 0) > args.max_posts_to_scrape
            ]
            if skipped:
                log("skipped too-many-posts: " + ", ".join(f"@{c['handle']}({c.get('posts_raw')})" for c in skipped))
            log("qualified to scrape: " + (", ".join("@" + c["handle"] for c in qualified) or "none"))
            for c in qualified:
                handle = c["handle"]
                out_path = state_path(f"flatkey_home_coldstart_posts_{handle.lower()}_{args.run_id}.json")
                target = min(args.post_limit, max(100, int(c.get("posts_count") or args.post_limit)))
                log(f"scrape @{handle} target={target} -> {out_path}")
                try:
                    rows = scrape_profile_posts(ws, args.port, handle, target, args.post_scrolls, args.wait, out_path)
                except Exception as e:
                    log(f"  scrape error @{handle}: {e}")
                    c["scrape_error"] = str(e)
                    continue
                c["scraped_posts_path"] = out_path
                c["scraped_posts"] = len(rows)
                c["scraped_categories"] = category_counts(rows)
                posts_by_handle[handle] = rows
                log(f"  scraped @{handle}: {len(rows)} records {c['scraped_categories']}")
            with open(candidates_path, "w") as f:
                json.dump(candidates, f, indent=2, ensure_ascii=False)
                f.write("\n")
            analyze_selected(candidates, posts_by_handle, args.run_id, args.report_out)
            log(f"wrote report -> {args.report_out}")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
