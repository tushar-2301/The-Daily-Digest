"""
Uses GEMINI_API_KEY3

stage2_extract/llm_headline_extractor.py
-----------------------------------------
Stage 2 — Ask Gemini to read each homepage's cleaned HTML and return
exactly the TOP 7 business headlines with their article URLs.

Key changes from v1:
  - Prompt now asks for TOP 7 only (not all headlines)
  - Prompt emphasises BUSINESS focus
  - Returns exactly 7 Headline objects per source (or fewer if page
    genuinely has fewer business stories)
  - No description yet — that's Stage 2.6's job after article pages load

Fix (June 2026):
  - Auth keys (AQ.Ab...) require x-goog-api-key header, not ?key= query param
"""

import json
import logging
import re
import time
from urllib.parse import urljoin

import httpx

from config import (
    RawPage, Headline,
    GEMINI_API_KEY3, GEMINI_MODEL,
    TOP_HEADLINES_PER_SOURCE,
)

logger = logging.getLogger("news_pipeline.stage2.llm_extract")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

EXTRACTION_PROMPT = """You are a business news editor reviewing a newspaper homepage.

Your task: identify the TOP {top_n} most important BUSINESS news headlines on this page.

"Business news" includes: economy, markets, stocks, finance, banking, trade, corporate earnings,
mergers & acquisitions, startups, currency, commodities, inflation, interest rates, economic policy,
employment economics, real estate markets.

Rules:
- Select ONLY genuine article headlines — NOT navigation links, section labels (e.g. "Business"),
  "Read More" / "Subscribe" / "Login" buttons, social links, or ads.
- A headline is a sentence/phrase about a specific news story (e.g. "Fed holds rates steady amid inflation concerns").
- If a business story appears multiple times on the page, include it only ONCE.
- Rank them: most important / most prominent business story first.
- Respect the news provider's editorial ranking. The position and visual prominence of an article on the source website are strong indicators of its importance. Treat homepage ordering, featured placement, headline size, and screen space allocation as signals of priority. Articles given greater prominence by the publisher should receive correspondingly higher priority in your ranking.
- Use the href exactly as it appears in the HTML (relative or absolute — do not modify it).
- If you can identify a sub-section the site assigns (e.g. "Markets", "Economy"), include it as
  "site_section"; otherwise null.
- If there are fewer than {top_n} business headlines on the page, return however many there are.

Page base URL (for resolving relative links): {base_url}

Return ONLY valid JSON (no markdown fences, no commentary):
{{
  "headlines": [
    {{"title": "...", "link": "...", "site_section": "..." }}
  ]
}}

Here is the cleaned page HTML:

{html}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*",     "", text)
    text = re.sub(r"\s*```$",     "", text)
    return text.strip()


def _call_gemini(prompt: str, client: httpx.Client, max_retries: int = 3) -> str:
    if not GEMINI_API_KEY3:
        raise RuntimeError(
            "GEMINI_API_KEY3 not set — add it to .env or export as env var."
        )
    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY3,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # thinkingConfig intentionally omitted — let the model use its default.
        # Do NOT set thinkingBudget:0 with responseMimeType — causes empty responses.
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.post(url, json=payload, headers=headers, timeout=120.0)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Gemini 429 — backing off {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data  = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text  = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                raise ValueError("Empty response from Gemini")
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
    raise RuntimeError(f"Gemini call failed after {max_retries} attempts: {last_err}")


def extract_top7_from_page(page: RawPage, client: httpx.Client) -> list[Headline]:
    """
    Send one cleaned homepage HTML to Gemini.
    Returns up to TOP_HEADLINES_PER_SOURCE Headline objects (no descriptions yet).
    """
    if page.status == "error":
        return [Headline(
            source=page.source, country=page.country,
            title="", link=page.url,
            fetch_method="llm_extracted", status="error", error=page.error,
        )]

    prompt = EXTRACTION_PROMPT.format(
        top_n=TOP_HEADLINES_PER_SOURCE,
        base_url=page.final_url,
        html=page.cleaned_html,
    )

    try:
        raw = _call_gemini(prompt, client)
    except Exception as e:
        logger.warning(f"[Stage2] {page.source}: Gemini call failed — {e}")
        return [Headline(
            source=page.source, country=page.country,
            title="", link=page.url,
            fetch_method="llm_extracted", status="error",
            error=f"GEMINI_CALL_FAILED: {e}",
        )]

    try:
        parsed      = json.loads(_strip_fences(raw))
        raw_items   = parsed.get("headlines", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[Stage2] {page.source}: bad JSON from Gemini — {e}")
        return [Headline(
            source=page.source, country=page.country,
            title="", link=page.url,
            fetch_method="llm_extracted", status="error",
            error=f"GEMINI_BAD_JSON: {e}. Raw: {raw[:200]!r}",
        )]

    if not raw_items:
        logger.warning(f"[Stage2] {page.source}: Gemini returned 0 headlines")
        return [Headline(
            source=page.source, country=page.country,
            title="", link=page.url,
            fetch_method="llm_extracted", status="error",
            error="GEMINI_ZERO_HEADLINES: page loaded but model found nothing",
        )]

    headlines = []
    for item in raw_items[:TOP_HEADLINES_PER_SOURCE]:
        title = (item.get("title") or "").strip()
        link  = (item.get("link")  or "").strip()
        if not title or not link:
            continue
        abs_link = urljoin(page.final_url, link)
        headlines.append(Headline(
            source=page.source,
            country=page.country,
            title=title,
            link=abs_link,
            description=None,        # filled by Stage 2.6
            fetch_method="llm_extracted",
            status="ok",
            site_section=item.get("site_section"),
        ))

    logger.info(f"[Stage2] {page.source}: {len(headlines)} top-{len(headlines)} headlines selected")
    return headlines


def extract_all(pages: list[RawPage]) -> list[Headline]:
    """Run Stage 2 across all HTML pages sequentially."""
    headlines = []
    with httpx.Client() as client:
        for page in pages:
            headlines.extend(extract_top7_from_page(page, client))
    return headlines
