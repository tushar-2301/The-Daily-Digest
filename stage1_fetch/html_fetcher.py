"""
stage1_fetch/html_fetcher.py
-----------------------------
Stage 1 — HTML sources: load each newspaper homepage with Playwright,
clean the HTML, and return a RawPage ready for Stage 2 (Gemini picks top-7).

Unchanged in role from v1. Minor improvements:
  - stealth init-script to reduce bot-detection
  - broader cookie-banner dismissal selectors
  - cleaner error classification messages
"""

import asyncio
import logging
import re

from bs4 import BeautifulSoup, Comment
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError, Error as PWError

from config import (
    Source, RawPage, DEFAULT_USER_AGENT,
    PLAYWRIGHT_NAV_TIMEOUT_MS, PLAYWRIGHT_EXTRA_WAIT_MS,
    MAX_CONCURRENT_BROWSER_PAGES, MAX_HTML_CHARS_FOR_LLM,
)

logger = logging.getLogger("news_pipeline.stage1.html")

STRIP_TAGS = [
    "script", "style", "svg", "noscript", "iframe", "form",
    "canvas", "video", "audio", "link", "meta",
]
STRIP_ATTRS = [
    "style", "onclick", "onload", "onerror", "onmouseover", "onmouseout",
    "data-testid", "aria-hidden", "tabindex", "role",
]

STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
"""

COOKIE_SELECTORS = [
    "text=Accept All", "text=Accept all", "text=I Accept",
    "text=Accept Cookies", "text=Accept & Continue",
    "text=Agree and continue", "text=Agree", "text=Got it",
    "[id*='accept']", "[class*='accept-cookie']", "[class*='cookie-accept']",
    "button[class*='consent']", "#onetrust-accept-btn-handler",
]


def clean_html_for_llm(html: str, char_cap: int = MAX_HTML_CHARS_FOR_LLM) -> str:
    soup = BeautifulSoup(html, "lxml")

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

    if soup.head:
        title_tag = soup.head.find("title")
        soup.head.clear()
        if title_tag:
            soup.head.append(title_tag)

    cleaned = str(soup)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)

    # After
    if len(cleaned) > char_cap:
        logger.warning(f"Cleaned HTML {len(cleaned)} chars > cap {char_cap}; truncating.")
        # Slice at the last closing tag boundary so we never send a broken fragment
        cut = cleaned.rfind("</", 0, char_cap)
        end = cleaned.find(">", cut)
        cleaned = cleaned[: end + 1 if cut != -1 and end != -1 else char_cap]

    return cleaned


def _classify_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, PWTimeoutError) or "Timeout" in msg:
        return "TIMEOUT: page too slow / geo-blocked / anti-bot JS challenge"
    if "ERR_NAME_NOT_RESOLVED" in msg:
        return "DNS_FAILURE: domain does not resolve"
    if "ERR_CONNECTION_REFUSED" in msg:
        return "CONNECTION_REFUSED: server refused connection"
    if "ERR_CONNECTION_RESET" in msg or "ERR_CONNECTION_CLOSED" in msg:
        return "CONNECTION_RESET: server dropped connection (WAF/anti-bot)"
    if "ERR_CERT" in msg or "SSL" in msg.upper():
        return "SSL_ERROR: certificate problem"
    if "ERR_TOO_MANY_REDIRECTS" in msg:
        return "REDIRECT_LOOP: cookie-consent / geo wall redirect loop"
    if "net::ERR_ABORTED" in msg:
        return "ABORTED: navigation aborted (download redirect or non-HTML resource)"
    return f"UNKNOWN_ERROR: {msg[:200]}"


async def _fetch_one_html_source(source: Source, browser, semaphore: asyncio.Semaphore) -> RawPage:
    async with semaphore:
        context = None
        try:
            context = await browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            await context.add_init_script(STEALTH_SCRIPT)

            # Block images/fonts/media to speed up load and cut timeout risk
            await context.route(
                re.compile(r".*\.(png|jpg|jpeg|gif|webp|woff2?|ttf|mp4|mp3)(\?.*)?$"),
                lambda route: route.abort(),
            )

            page = await context.new_page()
            page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)

            try:
                await page.goto(source.url, wait_until="domcontentloaded")
            except (PWTimeoutError, PWError) as e:
                logger.info(f"[HTML] {source.name}: first attempt failed; retrying with 'commit'")
                try:
                    await page.goto(source.url, wait_until="commit",
                                    timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
                except Exception as e2:
                    reason = _classify_error(e2)
                    logger.warning(f"[HTML] {source.name}: navigation failed — {reason}")
                    return RawPage(source=source.name, country=source.country,
                                   url=source.url, final_url=source.url,
                                   cleaned_html="", status="error", error=reason)

            #Waits for the js to load

            await page.wait_for_timeout(PLAYWRIGHT_EXTRA_WAIT_MS)

            try : 
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeoutError:
                pass
            
            await page.wait_for_timeout(1_000)
            

            # Dismiss cookie banners
            for sel in COOKIE_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=600):
                        await btn.click(timeout=600)
                        await page.wait_for_timeout(300)
                        break
                except Exception:
                    pass

            # Scroll to trigger lazy-loaders
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(800)

            raw_html  = await page.content()
            final_url = page.url

        except Exception as e:
            reason = _classify_error(e)
            logger.warning(f"[HTML] {source.name}: unexpected failure — {reason}")
            return RawPage(source=source.name, country=source.country,
                           url=source.url, final_url=source.url,
                           cleaned_html="", status="error", error=reason)
        finally:
            if context:
                await context.close()

    cleaned = clean_html_for_llm(raw_html)

    if len(cleaned) < 200:
        logger.warning(f"[HTML] {source.name}: suspiciously small HTML ({len(cleaned)} chars)")
        return RawPage(source=source.name, country=source.country,
                       url=source.url, final_url=final_url,
                       cleaned_html=cleaned, status="error",
                       error="SUSPICIOUSLY_EMPTY_PAGE: likely blocked or JS-challenge page")

    logger.info(f"[HTML] {source.name}: OK — {len(cleaned)} chars after cleaning")
    return RawPage(source=source.name, country=source.country,
                   url=source.url, final_url=final_url,
                   cleaned_html=cleaned, status="ok")


async def fetch_html_sources(sources: list[Source]) -> list[RawPage]:
    if not sources:
        return []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSER_PAGES)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            tasks = [_fetch_one_html_source(src, browser, semaphore) for src in sources]
            return await asyncio.gather(*tasks)
        finally:
            await browser.close()
