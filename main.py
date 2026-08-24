#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RK Group Daily Intelligence Newsletter
Plain Python + Anthropic API (no CrewAI)
"""

import os
import sys
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from dotenv import load_dotenv
import anthropic

import history as hist
import render

load_dotenv()
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', buffering=1)

MCP_URL = "https://valuecart-email-mcp-production.up.railway.app/mcp/valuecart2026"

COMPANIES = [
    "Amazon India",
    "Flipkart",
    "Myntra",
    "PharmEasy",
    "Nike India",
    "Blinkit",
    "Aditya Birla Group",
    "Cars24",
    "Scapia",
    "Smytten",
    "India China Relations",
]

BRAND = "RK Group Intelligence"
TAGLINE = "Daily business intelligence"

# (section label, companies). A single unlabelled section keeps the flat list
# the RK edition has always had; the renderer supports labelled sections too.
SECTIONS = [("", COMPANIES)]

RK_GROUP_CONTEXT = """RK Group is a brand distribution and retail business with interests in:
- Fashion and apparel distribution (including Nike partnership via Westbury)
- D2C brand building and marketplace management
- Health and wellness category
- Last-mile logistics and quick commerce
Focus on news that is directly actionable or reveals competitive intelligence relevant to these areas."""

RSS_FEEDS = {
    "Inc42": "https://inc42.com/feed/",
    "YourStory": "https://yourstory.com/feed",
    "Entrackr": "https://entrackr.com/feed/",
    "Mint": "https://www.livemint.com/rss/news",
}

# ---------------------------------------------------------------------------
# Run-scoped state
# ---------------------------------------------------------------------------

# Every article surfaced this run, keyed by URL → title. submit_edition checks
# each story's URL against this, so a link the model didn't get from a tool
# result never reaches the email.
CANDIDATES: dict[str, str] = {}

# Stories actually included in the email that went out (filled by _send_email).
SENT_STORIES: list[dict] = []

# Normalized headline keys already emailed in the archive window. Used to
# pre-filter search/RSS results so the model never even sees old stories.
SENT_KEYS: set[str] = set()

# Normalized URLs already emailed. A second filter alongside SENT_KEYS: it
# catches the same article resurfacing after an outlet rewords the headline,
# which the headline key on its own would let straight through.
SENT_URLS: set[str] = set()

# {company: {"date", "title"}} — the last story we sent per company, used for
# the "quiet today" column. Code owns this line now, so it cannot go missing.
RECAPS: dict[str, dict] = {}

# brand / tagline / date_display / footer for the renderer.
EDITION_META: dict = {}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _search_news(query: str) -> str:
    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        return "ERROR: SERPER_API_KEY not set."
    try:
        resp = requests.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5, "tbs": "qdr:1d", "gl": "in", "hl": "en"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("news", [])[:5]
        if not items:
            return "No recent news found."
        results = []
        for item in items:
            url = item.get("link", "")
            if not url:
                continue
            title = item.get("title", "")
            if hist.normalize_headline(title) in SENT_KEYS:
                continue  # already emailed inside the archive window
            if hist.normalize_url(url) in SENT_URLS:
                continue  # same article, possibly a reworded headline
            CANDIDATES[url] = title
            results.append(
                f"TITLE: {item.get('title', '')}\n"
                f"SOURCE: {item.get('source', '')} | DATE: {item.get('date', '')}\n"
                f"SNIPPET: {item.get('snippet', '')}\n"
                f"URL: {url}"
            )
        return "\n\n---\n\n".join(results) if results else "No recent news found."
    except Exception as e:
        return f"Search failed: {e}"


def _fetch_rss_news(company: str) -> str:
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    for source, url in RSS_FEEDS.items():
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            for item in items[:30]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")
                pub_date = item.findtext("pubDate", "")
                try:
                    if parsedate_to_datetime(pub_date) < cutoff:
                        continue
                except Exception:
                    continue  # unparseable date → can't confirm it's recent, skip it
                if company.lower() in title.lower() or company.lower() in desc.lower():
                    if link:
                        if hist.normalize_headline(title) in SENT_KEYS:
                            continue  # already emailed inside the archive window
                        if hist.normalize_url(link) in SENT_URLS:
                            continue  # same article, possibly a reworded headline
                        CANDIDATES[link] = title
                        results.append(
                            f"TITLE: {title}\n"
                            f"SOURCE: {source} | DATE: {pub_date}\n"
                            f"SNIPPET: {desc[:200]}\n"
                            f"URL: {link}"
                        )
        except Exception:
            continue
    return "\n\n---\n\n".join(results[:5]) if results else f"No RSS results found for {company}."


def _send_email(to: str, subject: str, body_html: str) -> str:

    recipients = [r.strip() for r in to.split(",") if r.strip()]
    results = []
    for recipient in recipients:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "send_email",
                    "arguments": {"to": recipient, "subject": subject, "body_html": body_html},
                },
            }
            resp = requests.post(
                MCP_URL,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                timeout=30,
                stream=True,
            )
            resp.raise_for_status()
            result_data = None
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                if decoded.startswith("data:"):
                    data_str = decoded[5:].strip()
                    if data_str:
                        try:
                            result_data = json.loads(data_str)
                        except Exception:
                            pass
            if result_data and "error" in result_data:
                results.append(f"{recipient}: Failed — {result_data['error']}")
            else:
                results.append(f"{recipient}: Sent")
        except Exception as e:
            results.append(f"{recipient}: Failed — {e}")
    return "\n".join(results)


def _submit_edition(subject: str, exec_summary: list, stories: list) -> str:
    """Render and send the edition from structured content.

    Replaces the old flow where the model hand-wrote HTML into send_email.
    Three things get better: the layout is identical every day, a fabricated
    URL cannot reach the email, and the per-company "quiet today" line is
    generated here rather than being something the model has to remember.
    """
    # A story may only cite a URL we actually harvested this run.
    harvested = {hist.normalize_url(u) for u in CANDIDATES}
    kept, rejected = [], []
    for st in stories or []:
        url = (st.get("url") or "").strip()
        if not url or hist.normalize_url(url) not in harvested:
            rejected.append(st.get("headline") or url or "?")
            continue
        company = st.get("company") or ""
        if company not in COMPANIES:
            company = _match_company(st.get("headline", ""), COMPANIES)
        if not company:
            rejected.append(st.get("headline") or "?")
            continue
        kept.append({
            "company": company,
            "headline": (st.get("headline") or "").strip(),
            "why": (st.get("why") or "").strip(),
            "url": url,
        })
    if rejected:
        print(f"  [edition] dropped {len(rejected)} story/ies with no harvested URL:")
        for r in rejected[:5]:
            print(f"            - {r[:88]}")

    by_company: dict[str, list] = {}
    for st in kept:
        by_company.setdefault(st["company"], []).append(st)

    sections = []
    for label, comps in SECTIONS:
        items = []
        for c in comps:
            if by_company.get(c):
                items.append({"company": c, "fresh": True, "bullets": by_company[c]})
            else:
                r = RECAPS.get(c)
                items.append({"company": c, "fresh": False,
                              "last": (r or {}).get("title", ""),
                              "last_date": (r or {}).get("date", "")})
        sections.append({"label": label, "items": items})

    edition = dict(EDITION_META)
    edition["exec_summary"] = [t for t in (exec_summary or []) if t][:3]
    edition["sections"] = sections

    body_html = render.render(edition)
    fresh = sum(len(v) for v in by_company.values())
    quiet = sum(1 for sec in sections for i in sec["items"] if not i.get("fresh"))
    print(f"  [edition] {fresh} fresh story/ies across {len(by_company)} companies, "
          f"{quiet} quiet, {len(body_html)} bytes of HTML.")

    # Exact URLs straight from the edition — no href scraping, nothing dropped.
    SENT_STORIES.extend({"title": st["headline"], "url": st["url"]} for st in kept)

    return _send_email(EDITION_META["recipients"], subject, body_html)


def dispatch_tool(name: str, inputs: dict) -> str:
    if name == "search_news":
        return _search_news(inputs["query"])
    if name == "fetch_rss_news":
        return _fetch_rss_news(inputs["company"])
    if name == "submit_edition":
        return _submit_edition(inputs.get("subject", ""),
                               inputs.get("exec_summary", []),
                               inputs.get("stories", []))
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Tool schemas for Anthropic API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_news",
        "description": "Search Google News for recent articles about a company or topic (past 2 days). Returns real articles with verified URLs only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, e.g. 'Flipkart India news'"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_rss_news",
        "description": "Fetch recent news about a company from Indian business RSS feeds (Inc42, YourStory, Entrackr, Mint). Only returns articles from the past 2 days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name to search for"}
            },
            "required": ["company"],
        },
    },
    {
        "name": "submit_edition",
        "description": (
            "Publish today's newsletter. Call this ONCE, after researching every "
            "company. Provide the content only — the newsletter's own template "
            "renders the HTML and emails it. Do not write HTML anywhere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string",
                            "description": "Email subject line"},
                "exec_summary": {
                    "type": "array",
                    "description": "The 3 most important things today, one sentence each.",
                    "items": {"type": "string"},
                },
                "stories": {
                    "type": "array",
                    "description": (
                        "One entry per genuinely new story. Omit companies with no "
                        "news — those are added automatically. Max 2 per company."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "enum": COMPANIES},
                            "headline": {"type": "string",
                                         "description": "The story headline."},
                            "why": {"type": "string",
                                    "description": "One sentence: why it matters to us."},
                            "url": {"type": "string",
                                    "description": "Article URL copied EXACTLY from a tool result."},
                        },
                        "required": ["company", "headline", "why", "url"],
                    },
                },
            },
            "required": ["subject", "exec_summary", "stories"],
        },
    },
]

# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def run_agent(system: str, user_prompt: str) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    messages = [{"role": "user", "content": user_prompt}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Collect any text output
        text_output = ""
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_output += block.text
            elif block.type == "tool_use":
                tool_uses.append(block)

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return text_output

        if response.stop_reason == "tool_use":
            tool_results = []
            for tu in tool_uses:
                print(f"  [tool] {tu.name}({json.dumps(tu.input)[:80]}...)")
                result = dispatch_tool(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return text_output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_newsletter():
    recipients_env = os.getenv("NEWSLETTER_RECIPIENTS", "aryan@valuecart.in")
    recipients = [r.strip() for r in recipients_env.split(",") if r.strip()]
    recipient_str = ", ".join(recipients)
    now = datetime.now()
    today = now.strftime("%B %d, %Y")
    today_iso = now.strftime("%Y-%m-%d")
    company_list = "\n".join(f"- {c}" for c in COMPANIES)

    print(f"\n{'='*60}")
    print(f"RK Group Daily Newsletter")
    print(f"Date      : {today}")
    print(f"Recipients: {recipient_str}")
    print(f"{'='*60}\n")

    # Load what we've already sent so we don't repeat stories. The code filter
    # uses the full archive; the model only sees the recent slice.
    history_entries, history_sha = hist.load_history()
    SENT_KEYS.update(hist.sent_keys(history_entries))
    SENT_URLS.update(hist.sent_urls(history_entries))
    already_covered = hist.recent_titles(history_entries, today_iso)
    print(f"  [history] {len(history_entries)} stories in the "
          f"{hist.ARCHIVE_DAYS}-day archive; {len(already_covered)} listed for "
          f"the model (last {hist.PROMPT_DAYS} days).")

    exclusion_block = ""
    if already_covered:
        listed = "\n".join(f"- {t}" for t in already_covered)
        exclusion_block = f"""

ALREADY COVERED — DO NOT REPEAT THESE STORIES (sent in the last {hist.PROMPT_DAYS} days).
This includes the same story reported by a different outlet. If a search result
is about any of these, SKIP it and look for genuinely new developments only:
{listed}
"""

    # The most recent story we sent per company, so the "no news today" line can
    # remind the reader what the last development was.
    RECAPS.update(hist.last_story_per_company(history_entries, COMPANIES))
    EDITION_META.update({
        "brand": BRAND,
        "tagline": TAGLINE,
        "date_display": today,
        "footer": f"{BRAND} | {today} | Confidential",
        "recipients": recipient_str,
        # Surfaced in the email itself. A dead GITHUB_TOKEN must never produce
        # an edition that looks normal.
        "degraded": not hist.LAST_LOAD_OK,
    })
    system = (
        "You research and publish a daily business intelligence newsletter for RK Group. "
        "You have tools to search news and to publish the edition. Be concise and factual. "
        "Only include genuinely NEW news from the past day. Never repeat a story that was "
        "already covered in a previous newsletter, even if a different outlet reported it. "
        "You never write HTML — you return content and the template does the rest."
    )

    prompt = f"""Today is {today}. Research and publish the RK Group Daily Intelligence Newsletter.

COMPANIES TO RESEARCH:
{company_list}

RK GROUP CONTEXT:
{RK_GROUP_CONTEXT}
{exclusion_block}
STEPS:
1. For each company, use search_news and fetch_rss_news to find news from the past day.
   The tools already filter out anything previously sent, but if a result clearly matches
   an ALREADY COVERED item above, skip it anyway.
2. Call submit_edition ONCE with:
   - subject: "RK Intelligence | {today} | <short hook from the top story>"
   - exec_summary: the 3 most important things today, one sentence each
   - stories: one entry per genuinely new story — company (from the list above),
     headline, why (one sentence on why it matters to RK Group), and url

RULES:
- A story needs a real URL taken EXACTLY from a tool result. No URL, no story;
  a URL that wasn't in the results is discarded before sending.
- At most 2 stories per company. Quality over filling every company.
- Do NOT write any HTML, and do NOT mention companies with no news. The
  newsletter template renders the layout and adds the quiet companies with
  their last-known story automatically.
- If nothing is new anywhere, still call submit_edition with an empty stories list.
"""

    result = run_agent(system, prompt)
    print("\nDone.")
    if result:
        print(result)

    # Remember the stories that actually went out, so tomorrow's run skips them.
    if SENT_STORIES:
        hist.save_history(history_entries, SENT_STORIES, history_sha, today_iso, COMPANIES)
    else:
        print("  [history] no stories captured from the email — nothing to save.")


if __name__ == "__main__":
    run_newsletter()
