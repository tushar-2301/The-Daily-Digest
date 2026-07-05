"""
stage2_fetch/article_fetcher.py
--------------------------------
Stage 2.5 — For every Headline object that came out of Stage 2
(either from RSS or from Gemini's homepage extraction), open the actual
article page with Playwright and return its cleaned HTML.

Why Playwright here too (not just httpx)?
  Many news article pages are also JS-rendered (paywalled intro teasers,
  lazy-loaded body text, single-page-app routing).  Playwright guarantees
  we get the rendered DOM, not just the server-side shell.

Concurrency: we open up to MAX_CONCURRENT_ARTICLE_PAGES tabs at once
across ALL sources in one shared browser instance, so the total wall-clock
time is bounded by network latency, not the number of articles.

Output: dict mapping  headline.link  ->  cleaned article HTML string
        (empty string if the fetch failed — Stage 2.6 handles that
        gracefully by generating a description from the title alone)
"""

import asyncio
import logging
import re

from bs4 import BeautifulSoup, Comment
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from config import (
    Headline, DEFAULT_USER_AGENT,
    PLAYWRIGHT_NAV_TIMEOUT_MS, PLAYWRIGHT_EXTRA_WAIT_MS,
    MAX_CONCURRENT_ARTICLE_PAGES, MAX_ARTICLE_HTML_CHARS,
)
from stage1_fetch.html_fetcher import STEALTH_SCRIPT, STRIP_TAGS, STRIP_ATTRS

logger = logging.getLogger("news_pipeline.stage2_5.article_fetch")


def clean_article_html(html: str) -> str:
    """
    Heavier cleaning for article pages vs homepages:
    - Same noise removal (scripts, styles, etc.)
    - Also remove nav, header, footer, sidebar — we only want body text
    - Keep <p>, <h1>-<h4>, <blockquote> — that's where article content lives
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove structural chrome
    for sel in ["nav", "header", "footer", "aside",
                "[class*='sidebar']", "[class*='related']",
                "[class*='newsletter']", "[class*='subscribe']",
                "[class*='social']", "[class*='share']",
                "[class*='comment']", "[id*='comment']",
                "[class*='advertisement']", "[class*='ads']"]:
        for el in soup.select(sel):
            el.decompose()

    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    for tag in soup.find_all(True):
        for attr in STRIP_ATTRS:
            tag.attrs.pop(attr, None)
        for attr_name, attr_val in list(tag.attrs.items()):
            if isinstance(attr_val, str) and len(attr_val) > 300:
                del tag.attrs[attr_name]

    cleaned = str(soup)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)

    if len(cleaned) > MAX_ARTICLE_HTML_CHARS:
        cleaned = cleaned[:MAX_ARTICLE_HTML_CHARS]

    return cleaned


async def _fetch_one_article(
    url: str,
    browser,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str]:
    """Returns (url, cleaned_html). cleaned_html is "" on failure."""
    async with semaphore:
        context = None
        try:
            context = await browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            await context.add_init_script(STEALTH_SCRIPT)

            # Block images & media — we only need text content
            await context.route(
                re.compile(r".*\.(png|jpg|jpeg|gif|webp|woff2?|ttf|mp4|mp3)(\?.*)?$"),
                lambda route: route.abort(),
            )

            page = await context.new_page()
            page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)

            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PWTimeoutError:
                # Partial load — still grab what we have
                logger.debug(f"[2.5] Timeout on {url} — using partial HTML")
            except Exception as e:
                logger.debug(f"[2.5] Navigation error for {url}: {e}")
                return url, ""
            
            # Waits until the network remains idle for some time
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeoutError:
                pass
            await page.wait_for_timeout(500)


            raw_html = await page.content()

        except Exception as e:
            logger.debug(f"[2.5] Context error for {url}: {e}")
            return url, ""
        finally:
            if context:
                await context.close()

    cleaned = clean_article_html(raw_html)
    if len(cleaned) < 100:
        logger.debug(f"[2.5] Suspiciously small article HTML for {url} ({len(cleaned)} chars)")
        return url, ""

    return url, cleaned


async def fetch_article_pages(headlines: list[Headline]) -> dict[str, str]:
    """
    Fetch all article pages concurrently.
    Returns dict: { url -> cleaned_html }
    Only fetches headlines with status="ok" and a non-empty link.
    """
    ok_headlines = [h for h in headlines if h.status == "ok" and h.link and h.fetch_method != "rss"]

    if not ok_headlines:
        return {}

    urls = list({h.link for h in ok_headlines})   # dedupe URLs
    logger.info(f"[Stage 2.5] Fetching {len(urls)} article pages concurrently …")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ARTICLE_PAGES)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            tasks   = [_fetch_one_article(url, browser, semaphore) for url in urls]
            results = await asyncio.gather(*tasks)
        finally:
            await browser.close()

    url_to_html = dict(results)
    success = sum(1 for h in url_to_html.values() if h)
    logger.info(f"[Stage 2.5] {success}/{len(urls)} article pages fetched successfully")
    return url_to_html
