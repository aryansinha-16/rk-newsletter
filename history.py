# -*- coding: utf-8 -*-
"""
GitHub-backed sent-story history for the RK newsletter.

Railway containers are ephemeral, so we can't keep history on local disk.
Instead we read/write a JSON file (`sent_history.json`) directly in the repo
via the GitHub Contents API. Each entry records a story we already emailed,
so the next run can exclude it (story-level dedup).
"""

import os
import re
import html
import json
import base64
import requests

GITHUB_REPO = os.getenv("GITHUB_REPO", "aryansinha-16/rk-newsletter-crew")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "master")
HISTORY_PATH = "sent_history.json"

# Two different jobs need two different horizons:
#   ARCHIVE_DAYS — how long a story stays in the file and in the code-level
#     harvest filter. This is the real "have we sent this before?" memory and
#     it must be long. It used to be 10, which meant a story was PERMANENTLY
#     deleted on day 11 and could be re-sent verbatim — the cause of the
#     ~2-week repeat cycle.
#   PROMPT_DAYS — how many headlines we paste into the model's DO-NOT-REPEAT
#     list. Kept short so the prompt doesn't bloat; the code filter below is
#     what actually guarantees no repeats.
ARCHIVE_DAYS = int(os.getenv("HISTORY_ARCHIVE_DAYS", "180"))
PROMPT_DAYS = int(os.getenv("HISTORY_PROMPT_DAYS", "14"))

# Back-compat alias for anything still importing the old name.
RETENTION_DAYS = ARCHIVE_DAYS

_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{HISTORY_PATH}"

# Set by load_history(). False means the archive could NOT be read — missing or
# rejected token, network error — which is very different from "read fine, but
# empty". A dead token once degraded a newsletter silently for a month: dedup
# was off and every company reported "no last known news", which looks like
# ordinary output. Callers must surface this, not absorb it.
LAST_LOAD_OK = True


def _headers():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# Serper/RSS routinely append the outlet: "... - Reuters", "... | Mint".
# That extra word changes the key, so the same story from two outlets used to
# read as two stories — the exact case this key is supposed to collapse.
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")


def normalize_headline(title: str) -> str:
    """Loose key so the same story from different outlets collapses together."""
    stripped = _SOURCE_SUFFIX_RE.sub("", title or "")
    # Only accept the strip if enough of the headline survives, so we don't
    # over-collapse a title whose real content sits after a dash.
    if len([w for w in stripped.split() if len(w) > 3]) >= 4:
        title = stripped
    title = title.lower()
    title = re.sub(r"[^a-z0-9 ]", " ", title)
    words = [w for w in title.split() if len(w) > 3]
    return " ".join(sorted(set(words)))


_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid",
                      "cmpid", "ref", "source", "at_medium", "at_campaign")


def normalize_url(url: str) -> str:
    """Stable dedup key for an article URL.

    Unescapes HTML entities (hrefs arrive as `&amp;`-encoded), then strips
    scheme, www, fragment, tracking params and trailing slash, so the same
    article shared with different tails collapses to one key.
    """
    u = html.unescape((url or "").strip()).lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("#")[0]
    base, _, qs = u.partition("?")
    base = base.rstrip("/")
    keep = [
        part for part in qs.split("&")
        if part and not part.split("=")[0].startswith(_TRACKING_PREFIXES)
    ]
    return base + ("?" + "&".join(sorted(keep)) if keep else "")


def load_history() -> tuple[list, str | None]:
    """Return (entries, file_sha). entries = [{date, title, url, key}]."""
    global LAST_LOAD_OK
    LAST_LOAD_OK = True
    headers = _headers()
    if not headers:
        LAST_LOAD_OK = False
        print("  [history] GITHUB_TOKEN not set — running without dedup memory.")
        return [], None
    try:
        resp = requests.get(_API, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
        if resp.status_code == 404:
            # A genuinely fresh repo. Say so rather than returning empty in
            # silence, which is indistinguishable from a wrong GITHUB_REPO.
            print(f"  [history] no {HISTORY_PATH} in {GITHUB_REPO}@{GITHUB_BRANCH} yet "
                  f"— starting a fresh archive.")
            return [], None
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        entries = json.loads(content) if content.strip() else []
        return entries, data["sha"]
    except Exception as e:
        LAST_LOAD_OK = False
        print(f"  [history] load failed ({e}) — running without dedup memory.")
        print("  [history] the edition will be marked as running without memory. "
              "Check GITHUB_TOKEN: 401 = dead credential, 403 = missing "
              "Contents:Read-and-write, 404 = wrong GITHUB_REPO.")
        return [], None


def recent_titles(entries: list, today_iso: str, days: int = PROMPT_DAYS) -> list[str]:
    """Headlines from the last `days` days, for the model's exclusion list."""
    cutoff = _shift_iso(today_iso, -days)
    return [e["title"] for e in entries if e.get("date", "") >= cutoff]


def last_story_per_company(entries: list, companies: list[str]) -> dict[str, dict]:
    """Most recent story we sent for each company → {company: {date, title}}.

    Matches a story to a company by name appearing in the title, or by the
    entry's stored `company` field. Used to show 'last major news: ...' next
    to a company that has nothing new today.
    """
    # If entries carry explicit company tags, trust them exclusively — fuzzy
    # title matching causes cross-company bleed (e.g. "Amazon" mentioned inside
    # a Blinkit headline). Only fall back to title matching for untagged data.
    any_tagged = any(e.get("company") for e in entries)

    result: dict[str, dict] = {}
    for company in companies:
        needle = company.lower().replace(" india", "").replace(" group", "").strip()
        best = None
        for e in entries:
            if any_tagged:
                if e.get("company", "").lower() != company.lower():
                    continue
            else:
                if not (needle and needle in e.get("title", "").lower()):
                    continue
            if best is None or e.get("date", "") >= best.get("date", ""):
                best = e
        if best:
            result[company] = {"date": best["date"], "title": best["title"]}
    return result


def sent_keys(entries: list) -> set[str]:
    return {e.get("key", "") for e in entries if e.get("key")}


def sent_urls(entries: list) -> set[str]:
    """Normalized URLs already emailed — catches the same article resurfacing
    under a reworded headline, which the headline key alone would miss."""
    return {normalize_url(e["url"]) for e in entries if e.get("url")}


def _match_company(title: str, companies: list[str]) -> str:
    """Best-guess company for a story title, or '' if none matches."""
    low = title.lower()
    for company in companies:
        needle = company.lower().replace(" india", "").replace(" group", "").strip()
        if needle and needle in low:
            return company
    return ""


def save_history(entries: list, new_stories: list[dict], sha: str | None,
                 today_iso: str, companies: list[str] | None = None) -> None:
    """Append new_stories, prune > RETENTION_DAYS old, write back to GitHub."""
    headers = _headers()
    if not headers:
        return

    companies = companies or []
    seen = sent_keys(entries)
    seen_urls = sent_urls(entries)
    added = 0
    for story in new_stories:
        key = normalize_headline(story["title"])
        nurl = normalize_url(story.get("url", ""))
        if not key or key in seen or (nurl and nurl in seen_urls):
            continue
        entries.append({
            "date": today_iso,
            "title": story["title"],
            "url": story["url"],
            "key": key,
            "company": _match_company(story["title"], companies),
        })
        seen.add(key)
        if nurl:
            seen_urls.add(nurl)
        added += 1

    cutoff = _shift_iso(today_iso, -ARCHIVE_DAYS)
    entries = [e for e in entries if e.get("date", "") >= cutoff]

    body = {
        "message": f"Update sent_history for {today_iso}",
        "content": base64.b64encode(json.dumps(entries, indent=2).encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    try:
        resp = requests.put(_API, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        print(f"  [history] saved {added} new stories "
              f"({len(entries)} retained, {ARCHIVE_DAYS}-day archive).")
    except Exception as e:
        print(f"  [history] save failed: {e}")


def _shift_iso(iso_date: str, delta_days: int) -> str:
    """Shift a YYYY-MM-DD string by delta_days, no datetime.now() needed."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso_date.split("-"))
    return (date(y, m, d) + timedelta(days=delta_days)).isoformat()
