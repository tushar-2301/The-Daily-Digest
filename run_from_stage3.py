# run_from_stage3.py
"""
Runs only Stage 3 (dedupe → rank → final output) using an existing
stage26_enriched_headlines.json file. Drop this in your project root and run:

    python run_from_stage3.py
    python run_from_stage3.py --input output/stage26_enriched_headlines.json --output output/
"""

import argparse
import json
import logging
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from config import Headline, StoryGroup
from stage3_process.dedupe_grouper import group_headlines
from stage3_process.ranker import rank_groups

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_from_stage3")


def _save_json(obj, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(obj, indent=2, ensure_ascii=False,
                   default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o)),
        encoding="utf-8",
    )
    logger.info(f"Saved → {path}")


def load_enriched_headlines(path: str) -> list[Headline]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Headline(**item) for item in raw]


def main():
    parser = argparse.ArgumentParser(description="Run Stage 3 only from an existing stage26 file.")
    parser.add_argument("--input",  default="output/stage26_enriched_headlines.json")
    parser.add_argument("--output", default="output")
    args = parser.parse_args()

    logger.info(f"Loading enriched headlines from: {args.input}")
    all_headlines = load_enriched_headlines(args.input)
    ok_headlines = [h for h in all_headlines if h.status == "ok"]
    logger.info(f"Loaded {len(all_headlines)} headlines ({len(ok_headlines)} with status=ok)")

    # Stage 3a — deduplicate into story groups
    logger.info("Stage 3a: deduplicating into story groups …")
    groups = group_headlines(all_headlines)
    _save_json([asdict(g) for g in groups], f"{args.output}/stage3a_groups.json")

    # Stage 3b — rank by importance
    logger.info(f"Stage 3b: ranking {len(groups)} story groups …")
    ranked_groups = rank_groups(groups)
    _save_json([asdict(g) for g in ranked_groups], f"{args.output}/stage3b_ranked_groups.json")

    # Summary
    ok_count = defaultdict(int)
    failures = []
    for h in all_headlines:
        if h.status == "ok":
            ok_count[h.source] += 1
        else:
            failures.append({"source": h.source, "url": h.link, "reason": h.error})

    summary = {
        "total_headlines_loaded":       len(all_headlines),
        "headlines_ok":                 len(ok_headlines),
        "headlines_with_description":   sum(1 for h in all_headlines if h.description),
        "total_stories_after_dedup":    len(ranked_groups),
        "errors": failures,
    }
    _save_json(summary, f"{args.output}/summary.json")

    # Final output (same shape as the full pipeline)
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
    _save_json(final, f"{args.output}/final_news.json")

    # Print top stories
    print("\n" + "=" * 72)
    print("STAGE 3 COMPLETE")
    print("=" * 72)
    print(f"  Headlines loaded:            {summary['total_headlines_loaded']}")
    print(f"  Headlines with description:  {summary['headlines_with_description']}")
    print(f"  Stories after dedup:         {summary['total_stories_after_dedup']}")
    print(f"\n  Top 10 stories:\n")
    for story in final["headlines"][:10]:
        desc = ""
        if story["articles"] and story["articles"][0].get("description"):
            desc = "  →  " + story["articles"][0]["description"][:80] + "…"
        print(f"  #{story['rank']}  {story['canonical_title']}")
        print(f"    ({story['num_sources']} source(s): {', '.join(story['sources'])})")
        if desc:
            print(f"    {desc}")
        print()
    print(f"  Full results → {args.output}/final_news.json")
    print("=" * 72)


if __name__ == "__main__":
    main()