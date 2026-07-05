"""
run.py
------
CLI entrypoint.

  python run.py                           # full pipeline
  python run.py --stop-after stage1       # fetch only (no Gemini calls)
  python run.py --stop-after stage2       # fetch + top-7 selection (no article fetch)
  python run.py --stop-after stage2_6     # fetch + top-7 + article pages + descriptions
  python run.py --input sources.csv --output output/
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from pipeline import (
    run_pipeline, run_stage1, run_stage2, run_stage25_and_26,
    load_sources, build_summary, _save_json,
)


def _print_stage1_summary(sources, rss_headlines, raw_pages, output_dir):
    from collections import defaultdict
    rss_by_source = defaultdict(list)
    for h in rss_headlines:
        rss_by_source[h.source].append(h)

    print("\n" + "=" * 72)
    print("STAGE 1 SUMMARY  (fetch only — no Gemini calls made)")
    print("=" * 72)

    print(f"\nRSS sources ({len(rss_by_source)} attempted):")
    for src, hs in rss_by_source.items():
        ok = [h for h in hs if h.status == "ok"]
        print(f"  {'OK  ' if ok else 'FAIL'}  {src}: "
              f"{len(ok)} headlines" if ok else f"  FAIL  {src}: {hs[0].error}")

    print(f"\nHTML sources ({len(raw_pages)} attempted, Playwright-loaded):")
    for p in raw_pages:
        if p.status == "ok":
            print(f"  OK    {p.source}: {len(p.cleaned_html):,} chars cleaned HTML")
        else:
            print(f"  FAIL  {p.source}: {p.error}")

    print("\n" + "=" * 72)
    print(f"Stage 1 outputs: {output_dir}/stage1_*.json")


def main():
    parser = argparse.ArgumentParser(
        description="News pipeline: fetch → top-7 selection → article descriptions → dedupe → rank"
    )
    parser.add_argument("--input",  default="sources.csv")
    parser.add_argument("--output", default="output")
    parser.add_argument(
        "--stop-after",
        choices=["stage1", "stage2", "stage2_6", "stage3"],
        default="stage3",
        help=(
            "stage1   = fetch only\n"
            "stage2   = + Gemini top-7 selection\n"
            "stage2_6 = + article fetch + descriptions\n"
            "stage3   = full pipeline (default)"
        ),
    )
    args = parser.parse_args()

    try:
        if args.stop_after == "stage1":
            sources = load_sources(args.input)
            rss_headlines, raw_pages = asyncio.run(run_stage1(sources, args.output))
            _print_stage1_summary(sources, rss_headlines, raw_pages, args.output)
            return

        if args.stop_after == "stage2":
            sources = load_sources(args.input)
            rss_headlines, raw_pages = asyncio.run(run_stage1(sources, args.output))
            llm_headlines = run_stage2(raw_pages, args.output)
            all_hl = rss_headlines + llm_headlines
            print(f"\nTotal headlines selected (top-7 per source): {sum(1 for h in all_hl if h.status=='ok')}")
            print(f"Inspect: {args.output}/stage2_llm_headlines.json")
            return

        if args.stop_after == "stage2_6":
            sources = load_sources(args.input)
            rss_headlines, raw_pages = asyncio.run(run_stage1(sources, args.output))
            llm_headlines = run_stage2(raw_pages, args.output)
            all_hl = rss_headlines + llm_headlines
            enriched = asyncio.run(run_stage25_and_26(all_hl, args.output))
            with_desc = sum(1 for h in enriched if h.description)
            print(f"\nHeadlines enriched with descriptions: {with_desc}/{len(enriched)}")
            print(f"Inspect: {args.output}/stage26_enriched_headlines.json")
            return

        # Full pipeline
        final = asyncio.run(run_pipeline(args.input, args.output))

    except Exception as e:
        print(f"\nPipeline failed: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)

    summary = final["summary"]
    print("\n" + "=" * 72)
    print("PIPELINE COMPLETE")
    print("=" * 72)
    print(f"  Sources attempted:                {summary['total_sources_attempted']}")
    print(f"  Sources with >=1 headline:        {summary['sources_with_at_least_1_headline']}")
    print(f"  Total headlines (top-7 per src):  {summary['total_headlines_collected']}")
    print(f"  Headlines with description:       {summary['headlines_with_description']}")
    print(f"  Stories after dedup (ranked):     {summary['total_stories_after_dedup']}")

    if summary["sources_fully_failed"]:
        print(f"\n  FAILED SOURCES ({len(summary['sources_fully_failed'])}):")
        for s in summary["sources_fully_failed"]:
            print(f"    - {s}")

    print(f"\n  Top stories (ranked by importance):\n")
    for story in final["headlines"][:10]:
        desc_preview = ""
        if story["articles"] and story["articles"][0].get("description"):
            desc_preview = "  →  " + story["articles"][0]["description"][:80] + "…"
        print(f"  #{story['rank']}  {story['canonical_title']}")
        print(f"    ({story['num_sources']} source(s): {', '.join(story['sources'])})")
        if desc_preview:
            print(f"    {desc_preview}")
        print()

    print(f"  Full results → {args.output}/final_news.json")
    print("=" * 72)


if __name__ == "__main__":
    main()
