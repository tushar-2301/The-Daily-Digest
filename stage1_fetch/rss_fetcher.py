"""
stage1_fetch/rss_fetcher.py
---------------------------
Handles RSS sources. Unchanged in role from v1.

Change: caps output at TOP_HEADLINES_PER_SOURCE per source so RSS and
HTML paths produce the same max-7 output going into Stage 2.5/2.6.
The top-7 selection for RSS is done by recency (feed order) since RSS
feeds are already ordered newest-first. The actual business-relevance
ranking happens in Stage 3.
"""

import logging
import httpx
import feedparser
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import Source, Headline, DEFAULT_USER_AGENT, MAX_RETRIES, TOP_HEADLINES_PER_SOURCE

logger = logging.getLogger("news_pipeline.stage1.rss")


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)),
    reraise=True,
)
def _fetch_raw(url: str, client: httpx.Client) -> bytes:
    resp = client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT}, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def fetch_rss_source(source: Source, client: httpx.Client) -> list[Headline]:
    try:
        raw = _fetch_raw(source.url, client)
    except Exception as e:
        logger.warning(f"[RSS] {source.name} ({source.url}): fetch failed — {e}")
        return [Headline(
            source=source.name, country=source.country,
            title="", link=source.url,
            fetch_method="rss", status="error", error=str(e),
        )]

    parsed = feedparser.parse(raw)

    if parsed.bozo and not parsed.entries:
        err = str(parsed.get("bozo_exception", "Unknown feed parse error"))
        logger.warning(f"[RSS] {source.name}: malformed feed — {err}")
        return [Headline(
            source=source.name, country=source.country,
            title="", link=source.url,
            fetch_method="rss", status="error", error=err,
        )]

    headlines = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link  = (entry.get("link")  or "").strip()
        if not title or not link:
            continue

        # RSS summary may already give us a short description — Stage 2.6
        # will enrich it further from the actual article page
        summary = (entry.get("summary") or entry.get("description") or "").strip()

        headlines.append(Headline(
            source=source.name,
            country=source.country,
            title=title,
            link=link,
            summary=summary or None,
            published=entry.get("published", entry.get("updated", None)),
            fetch_method="rss",
            status="ok",
        ))

    if not headlines:
        logger.warning(f"[RSS] {source.name}: 0 entries parsed")
        return [Headline(
            source=source.name, country=source.country,
            title="", link=source.url,
            fetch_method="rss", status="error",
            error="Feed parsed but 0 entries found",
        )]

    # Cap at TOP_HEADLINES_PER_SOURCE — RSS feeds order newest-first
    headlines = headlines[:TOP_HEADLINES_PER_SOURCE]
    logger.info(f"[RSS] {source.name}: {len(headlines)} headlines (capped at {TOP_HEADLINES_PER_SOURCE})")
    return headlines


def fetch_all_rss(rss_sources: list[Source]) -> list[Headline]:
    from config import HTTPX_TIMEOUT
    headlines = []
    with httpx.Client(timeout=HTTPX_TIMEOUT) as client:
        for src in rss_sources:
            headlines.extend(fetch_rss_source(src, client))
    return headlines
