"""
pipeline.py
-----------
Orchestrates the full pipeline end-to-end:

  Stage 1   — fetch RSS headlines + load HTML homepages via Playwright
  Stage 2   — Gemini reads homepage HTML → top 7 headlines per source
              (title + link only; no description yet)
  Stage 2.5 — Playwright visits all 7 article links per source concurrently,
              returns cleaned article HTML
  Stage 2.6 — One Gemini call per source: given all 7 articles' cleaned HTML
              → write title + 2-3 sentence description for each
  Stage 3   — Dedupe headlines into StoryGroups, then rank by importance

Each stage's intermediate output is saved to disk so you can inspect or
resume from any point without re-running expensive earlier stages.
"""

import csv
import json
import logging
import asyncio
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from config import Source, Headline, RawPage, StoryGroup
from stage1_fetch.rss_fetcher import fetch_all_rss
from stage1_fetch.html_fetcher import fetch_html_sources
from stage2_extract.llm_headline_extractor import extract_all
from stage2_fetch.article_fetcher import fetch_article_pages
from stage2_fetch.description_extractor import enrich_all_sources
from stage3_process.dedupe_grouper import group_headlines
from stage3_process.ranker import rank_groups

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("news_pipeline.pipeline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_sources(csv_path: str) -> list[Source]:
    sources = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"name", "country", "url", "type"}
        missing  = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{csv_path} missing column(s): {missing}")
        for i, row in enumerate(reader, start=2):
            name    = row["name"].strip()
            country = row["country"].strip()
            url     = row["url"].strip()
            type_   = row["type"].strip().lower()
            if not name or not url:
                logger.warning(f"Row {i}: missing name or url — skipping")
                continue
            if type_ not in ("rss", "html"):
                logger.warning(f"Row {i} ('{name}'): type must be rss/html — skipping")
                continue
            sources.append(Source(name=name, country=country, url=url, type=type_))
    return sources


def _save_json(obj, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o)),
        encoding="utf-8",
    )
    logger.info(f"Saved → {path}")


def _group_by_source(headlines: list[Headline]) -> dict[str, list[Headline]]:
    grouped = defaultdict(list)
    for h in headlines:
        grouped[h.source].append(h)
    return dict(grouped)


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

async def run_stage1(
    sources: list[Source], work_dir: str
) -> tuple[list[Headline], list[RawPage]]:
    rss_sources  = [s for s in sources if s.type == "rss"]
    html_sources = [s for s in sources if s.type == "html"]
    logger.info(f"Stage 1: {len(rss_sources)} RSS + {len(html_sources)} HTML sources")

    rss_headlines = fetch_all_rss(rss_sources)
    raw_pages     = await fetch_html_sources(html_sources)

    _save_json([asdict(h) for h in rss_headlines], f"{work_dir}/stage1_rss_headlines.json")
    _save_json([asdict(p) for p in raw_pages],     f"{work_dir}/stage1_raw_pages.json")
    return rss_headlines, raw_pages


def run_stage2(raw_pages: list[RawPage], work_dir: str) -> list[Headline]:
    """Gemini selects top-7 headlines per HTML source."""
    logger.info(f"Stage 2: extracting top-7 headlines via Gemini from {len(raw_pages)} pages")
    llm_headlines = extract_all(raw_pages)
    _save_json([asdict(h) for h in llm_headlines], f"{work_dir}/stage2_llm_headlines.json")
    return llm_headlines


async def run_stage25_and_26(
    all_headlines: list[Headline], work_dir: str
) -> list[Headline]:
    """
    Stage 2.5: fetch all article pages concurrently.
    Stage 2.6: one Gemini call per source to write descriptions.
    """
    # 2.5 — concurrent Playwright fetch of all article URLs
    logger.info("Stage 2.5: fetching article pages …")
    url_to_html = await fetch_article_pages(all_headlines)
    _save_json(
        {url: len(html) for url, html in url_to_html.items()},
        f"{work_dir}/stage25_article_html_sizes.json",
    )

    # 2.6 — enrich with descriptions
    logger.info("Stage 2.6: generating descriptions via Gemini …")
    headlines_by_source = _group_by_source(all_headlines)
    enriched = enrich_all_sources(headlines_by_source, url_to_html)
    _save_json(
        [asdict(h) for h in enriched],
        f"{work_dir}/stage26_enriched_headlines.json",
    )
    return enriched


def run_stage3(all_headlines: list[Headline], work_dir: str) -> list[StoryGroup]:
    """
    Stage 3: deduplicate → rank.
    Two Gemini calls:
      3a: cluster headlines about the same story into StoryGroups
      3b: rank StoryGroups by importance for a business audience
    """
    logger.info(f"Stage 3a: deduplicating {len(all_headlines)} headlines into story groups")
    groups = group_headlines(all_headlines)
    _save_json([asdict(g) for g in groups], f"{work_dir}/stage3a_groups.json")

    logger.info(f"Stage 3b: ranking {len(groups)} story groups by importance")
    ranked_groups = rank_groups(groups)
    _save_json([asdict(g) for g in ranked_groups], f"{work_dir}/stage3b_ranked_groups.json")

    return ranked_groups


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def build_summary(
    sources: list[Source],
    all_headlines: list[Headline],
    ranked_groups: list[StoryGroup],
) -> dict:
    ok_count = defaultdict(int)
    failures = []
    for h in all_headlines:
        if h.status == "ok":
            ok_count[h.source] += 1
        else:
            failures.append({"source": h.source, "url": h.link, "reason": h.error})

    attempted = {s.name for s in sources}
    succeeded = set(ok_count.keys())
    return {
        "total_sources_attempted":          len(sources),
        "sources_with_at_least_1_headline": len(succeeded),
        "sources_fully_failed":             sorted(attempted - succeeded),
        "total_headlines_collected":        sum(ok_count.values()),
        "headlines_with_description":       sum(1 for h in all_headlines if h.description),
        "total_stories_after_dedup":        len(ranked_groups),
        "errors": failures,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(input_csv: str, work_dir: str = "output") -> dict:
    sources = load_sources(input_csv)
    if not sources:
        raise ValueError("No valid sources loaded — check your CSV.")

    # Stage 1
    rss_headlines, raw_pages = await run_stage1(sources, work_dir)

    # Stage 2 (HTML sources only)
    llm_headlines = run_stage2(raw_pages, work_dir)

    # Combine all headlines (RSS + LLM-extracted)
    all_headlines = rss_headlines + llm_headlines

    # Stage 2.5 + 2.6 — fetch article pages, enrich with descriptions
    all_headlines = await run_stage25_and_26(all_headlines, work_dir)

    # Stage 3 — dedupe + rank
    ranked_groups = run_stage3(all_headlines, work_dir)

    # Final output
    summary = build_summary(sources, all_headlines, ranked_groups)
    _save_json(summary, f"{work_dir}/summary.json")

    final = {
        "summary": summary,
        "headlines": [
            {
                "rank":            rank,
                "canonical_title": g.canonical_title,
                "num_sources":     len({m.source for m in g.members}),
                "sources":         sorted({m.source for m in g.members}),
                "articles": [
                    {
                        "source":      m.source,
                        "country":     m.country,
                        "title":       m.title,
                        "description": m.description,
                        "link":        m.link,
                        "published":   m.published,
                    }
                    for m in g.members if m.status == "ok"
                ],
            }
            for rank, g in enumerate(ranked_groups, start=1)
        ],
    }
    _save_json(final, f"{work_dir}/final_news.json")
    return final
