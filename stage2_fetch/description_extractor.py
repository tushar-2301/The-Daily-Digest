"""
Uses GEMINI_API_KEY2

stage2_fetch/description_extractor.py
---------------------------------------
Stage 2.6 — Given the 7 article pages' cleaned HTML for one source,
make a SINGLE Gemini call that returns title + 2-3 sentence description
for each of the 7 articles.

Design decisions:
  - One call per SOURCE (not per article) — sending all 7 articles in
    one prompt cuts API calls by 7x vs calling once per article.
  - We pass title + HTML together so Gemini can cross-reference the title
    against the article body to write a precise, accurate description.
  - If an article's HTML is empty (fetch failed), Gemini writes a
    best-effort description from the title alone (the prompt handles this).
  - RSS headlines already have a `summary` field from the feed; we use
    that as the article "html" input so no extra fetch is needed for RSS.

Fix (June 2026):
  - Auth keys (AQ.Ab...) require x-goog-api-key header, not ?key= query param
  - Removed thinkingConfig:{thinkingBudget:0} — caused empty responses when
    combined with responseMimeType on gemini-2.5-flash
"""

import json
import logging
import re
import time
from html.parser import HTMLParser

import httpx

from config import Headline, GEMINI_API_KEY2, GEMINI_MODEL

logger = logging.getLogger("news_pipeline.stage2_6.description")


def _strip_html(text: str) -> str:
    """Strip all HTML tags from a string and collapse whitespace."""
    if not text:
        return text

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)

    s = _Stripper()
    s.feed(text)
    clean = " ".join(s.parts)
    # Collapse multiple spaces / newlines
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

DESCRIPTION_PROMPT = """You are a business news editor. Below are {n} business news articles from {source} ({country}).

For each article you are given:
  - "id": a reference id
  - "title": the headline
  - "content": the article body HTML (may be empty if the page could not be loaded)

Your task: write a concise 2-3 sentence description for each article that:
  1. Summarises what happened (the key facts)
  2. Gives context (why it matters for business/economy)
  3. Is written in neutral, journalistic third-person

If "content" is empty or very short, DO NOT write any description from yourself, strictly stick to the information provided, do not add any information from your side, in case you dont have enough information to create a descp, give a short suitable error msg as the descp.

Articles:
{articles_json}

Return ONLY valid JSON (no markdown fences, no commentary):
{{
  "descriptions": [
    {{"id": "...", "title": "...", "description": "..."}}
  ]
}}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*",     "", text)
    text = re.sub(r"\s*```$",     "", text)
    return text.strip()


def _call_gemini(prompt: str, client: httpx.Client, max_retries: int = 3) -> str:
    if not GEMINI_API_KEY2:
        raise RuntimeError("GEMINI_API_KEY2 not set")
    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY2,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            # thinkingConfig intentionally omitted — thinkingBudget:0 + responseMimeType
            # caused empty responses on gemini-2.5-flash (June 2026 regression).
        },
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.post(url, json=payload, headers=headers, timeout=120.0)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Gemini 429 — backing off {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data  = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text  = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                raise ValueError("Empty response")
            time.sleep(20)
            return text
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_err = e
            time.sleep(2 ** attempt)
        except httpx.HTTPStatusError as e:
            last_err = e
            if e.response.status_code in (500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"Gemini failed after {max_retries} attempts: {last_err}")


def enrich_headlines_with_descriptions(
    headlines: list[Headline],
    url_to_html: dict[str, str],
    client: httpx.Client,
) -> list[Headline]:
    """
    Takes up to 7 Headlines for one source and enriches each with a
    .description via one Gemini call.

    `url_to_html` maps article URL -> cleaned HTML (may be empty string).
    For RSS headlines, we fall back to the existing .summary as content.

    Mutates headlines in-place and returns them.
    """
   # After
    ok = [h for h in headlines if h.status == "ok"]
    if not ok:
        return headlines

    source  = ok[0].source
    country = ok[0].country

    # RSS headlines: summary from the feed is already the description — skip Gemini entirely.
    # RSS summaries often contain raw HTML (e.g. <img> tags from Jakarta Post); strip them.
    if all(h.fetch_method == "rss" for h in ok):
        for h in ok:
            h.description = _strip_html(h.summary) or None
        logger.info(f"[Stage2.6] {source}: RSS source — using feed summaries directly (HTML stripped), skipping Gemini")
        return headlines

    # Build articles payload for the prompt
    articles = []
    
    id_to_hl: dict[str, Headline] = {}
    for i, h in enumerate(ok):
        hid = str(i)
        id_to_hl[hid] = h
        # Prefer fetched article HTML, fall back to RSS summary, then empty
        content = url_to_html.get(h.link, "") or h.summary or ""
        # Truncate to avoid bloating a single call (each article gets ~8k chars)
        content = content[:8_000] if content else ""
        articles.append({"id": hid, "title": h.title, "content": content})

    prompt = DESCRIPTION_PROMPT.format(
        n=len(articles),
        source=source,
        country=country,
        articles_json=json.dumps(articles, ensure_ascii=False, indent=2),
    )

    try:
        raw     = _call_gemini(prompt, client)
        parsed  = json.loads(_strip_fences(raw))
        results = parsed.get("descriptions", [])
    except Exception as e:
        logger.warning(f"[Stage2.6] {source}: Gemini call failed — {e}. "
                       f"Descriptions will be empty for this source.")
        return headlines

    # Map results back to Headline objects
    results_by_id = {r.get("id"): r for r in results}
    for hid, h in id_to_hl.items():
        r = results_by_id.get(hid)
        if r:
            h.description = (r.get("description") or "").strip() or None

    enriched = sum(1 for h in ok if h.description)
    logger.info(f"[Stage2.6] {source}: {enriched}/{len(ok)} headlines enriched with descriptions")
    return headlines


def enrich_all_sources(
    headlines_by_source: dict[str, list[Headline]],
    url_to_html: dict[str, str],
) -> list[Headline]:
    """
    Run Stage 2.6 for every source sequentially (one Gemini call per source).
    Returns a flat list of all enriched Headline objects.
    """
    all_headlines = []
    with httpx.Client() as client:
        for source_name, source_headlines in headlines_by_source.items():
            enriched = enrich_headlines_with_descriptions(
                source_headlines, url_to_html, client
            )
            all_headlines.extend(enriched)
    return all_headlines
