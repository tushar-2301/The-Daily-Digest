"""
config.py
---------
Shared dataclasses, constants, and environment config.
All stages import from here — single source of truth.

Changes from v1:
  - Headline gains `description` field (filled by Stage 2.6)
  - New constants: TOP_HEADLINES_PER_SOURCE, MAX_CONCURRENT_ARTICLE_PAGES
  - Stage 2.5 / 2.6 constants added
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Environment / API config
# ---------------------------------------------------------------------------

def _load_dotenv_if_present():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env") # idk why but changed this recently
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv_if_present()

GEMINI_API_KEY1 = os.environ.get("GEMINI_API_KEY1", "")
GEMINI_API_KEY2 = os.environ.get("GEMINI_API_KEY2", "")
GEMINI_API_KEY3 = os.environ.get("GEMINI_API_KEY3", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class Source:
    name:    str
    country: str
    url:     str
    type:    str   # "rss" | "html"


@dataclass
class RawPage:
    """Output of Stage 1 for an HTML source."""
    source:       str
    country:      str
    url:          str
    final_url:    str
    cleaned_html: str
    status:       str = "ok"    # "ok" | "error"
    error: Optional[str] = None


@dataclass
class Headline:
    """
    A single headline as it travels through the pipeline.
    `description` is None until Stage 2.6 visits the article page.
    """
    source:       str
    country:      str
    title:        str
    link:         str
    description:  Optional[str] = None   # ← NEW: filled by Stage 2.6
    summary:      Optional[str] = None   # RSS summary (may pre-fill description)
    published:    Optional[str] = None
    fetch_method: str = ""               # "rss" | "llm_extracted"
    status:       str = "ok"             # "ok" | "error"
    error: Optional[str] = None
    site_section: Optional[str] = None


@dataclass
class StoryGroup:
    """Stage 3a output: cluster of headlines about the same story."""
    canonical_title: str
    members:         list = field(default_factory=list)   # list[Headline]
    category: Optional[str]  = None
    is_business: Optional[bool] = None


# ---------------------------------------------------------------------------
# Fetch / browser constants
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

HTTPX_TIMEOUT                  = 20.0
PLAYWRIGHT_NAV_TIMEOUT_MS      = 30_000
PLAYWRIGHT_EXTRA_WAIT_MS       = 1_000
MAX_RETRIES                    = 3
MAX_CONCURRENT_BROWSER_PAGES   = 4    # Stage 1 homepage concurrency

# ── Stage 2 / 2.5 / 2.6 ────────────────────────────────────────────────────

# How many top headlines Gemini selects per source in Stage 2
TOP_HEADLINES_PER_SOURCE       = 3

# Max concurrent Playwright pages when visiting article links (Stage 2.5)
# Keep lower than Stage 1 — we open up to 7 tabs per source simultaneously
MAX_CONCURRENT_ARTICLE_PAGES   = 6

# Character cap on cleaned article HTML sent to Gemini (Stage 2.6)
# A single article page is much smaller than a homepage — 60 k is plenty
MAX_ARTICLE_HTML_CHARS         = 30_000

# Character cap on cleaned homepage HTML sent to Gemini (Stage 2)
MAX_HTML_CHARS_FOR_LLM         = 50_000

# Stage 3 batching
MAX_HEADLINES_PER_DEDUPE_BATCH = 150
