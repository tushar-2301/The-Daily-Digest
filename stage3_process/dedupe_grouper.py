"""
Uses GEMINI_API_KEY1

stage3_process/dedupe_grouper.py
----------------------------------
Stage 3 (part 1) — cluster headlines that report the same story into StoryGroups.

Operates on enriched Headline objects (with .description from Stage 2.6).
Sends title + source to Gemini for deduplication — descriptions are NOT sent
here (they're used in the ranking step to keep prompt size manageable at scale).

Batching: MAX_HEADLINES_PER_DEDUPE_BATCH controls how many headlines go into
a single Gemini call. At 20 sources × 7 headlines = 140 headlines, a single
batch of 150 is fine; if volume grows further the chunking handles it
automatically.
"""

import json
import logging
import re
import time

import httpx

from config import (
    Headline,
    StoryGroup,
    GEMINI_API_KEY1,
    GEMINI_MODEL,
    MAX_HEADLINES_PER_DEDUPE_BATCH,
)
from . import rate_limiter

RATE_LIMIT_KEY = "GEMINI_API_KEY1"

logger = logging.getLogger("news_pipeline.stage3.dedupe")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

DEDUPE_PROMPT = """You are deduplicating news headlines collected from {num_sources} different newspapers.

Group headlines that report the SAME underlying story together.

Rules:
- Group ONLY headlines about the same specific event/story — not just the same broad topic.
  Examples of DIFFERENT stories: "Oil prices rise 2%" vs "Oil falls on demand fears"
  Examples of SAME story: "Fed raises rates by 25bps" vs "Federal Reserve hikes interest rates"
- Each headline belongs to exactly ONE group.
- A unique headline that has no duplicate forms its own singleton group.
- Write a clear, neutral "canonical_title" that best summarises the story (prefer specifics over vague titles).
- Reference headlines by their "id" only — do not alter headline text.

Headlines:
{headlines_json}

Return ONLY valid JSON (no markdown fences):
{{
  "groups": [
    {{"canonical_title": "...", "member_ids": ["id1", "id2"]}}
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
    if not GEMINI_API_KEY1:
        raise RuntimeError("GEMINI_API_KEY1 not set in environment")
    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL, api_key=GEMINI_API_KEY1)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        # Block here (never raise) until it's safe to make another call
        # without exceeding the shared RPM cap for this key.
        rate_limiter.wait_for_slot(RATE_LIMIT_KEY, logger=logger)
        try:
            resp = client.post(url, json=payload, timeout=120.0)
            if resp.status_code == 429:
                rate_limiter.record_429(RATE_LIMIT_KEY, logger=logger)
                wait = rate_limiter.WINDOW_SECONDS + rate_limiter.BUFFER_SECONDS
                logger.warning(f"Gemini 429 — backing off a full window ({wait}s)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data  = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text  = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                raise ValueError("Empty response from Gemini")
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
    raise RuntimeError(f"Gemini dedupe failed after {max_retries} attempts: {last_err}")


def _chunk(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def group_headlines(headlines: list[Headline]) -> list[StoryGroup]:
    """
    Cluster all ok headlines into StoryGroups via Gemini.
    Error headlines are appended as empty-titled singletons so they propagate
    through the pipeline without being lost.
    """
    ok_headlines    = [h for h in headlines if h.status == "ok" and h.title]
    error_headlines = [h for h in headlines if h.status == "error"]

    if not ok_headlines:
        logger.warning("No successfully-fetched headlines to group.")
        return []

    all_groups: list[StoryGroup] = []
    batches = _chunk(ok_headlines, MAX_HEADLINES_PER_DEDUPE_BATCH)
    logger.info(
        f"Deduplicating {len(ok_headlines)} headlines across {len(batches)} batch(es) "
        f"(batch size: {MAX_HEADLINES_PER_DEDUPE_BATCH})"
    )

    with httpx.Client() as client:
        for batch_num, batch in enumerate(batches, start=1):
            id_to_headline = {str(i): h for i, h in enumerate(batch)}

            # Send title + source only — descriptions go to the ranker
            payload = [
                {"id": hid, "title": h.title, "source": h.source}
                for hid, h in id_to_headline.items()
            ]
            prompt = DEDUPE_PROMPT.format(
                num_sources=len({h.source for h in batch}),
                headlines_json=json.dumps(payload, ensure_ascii=False, indent=2),
            )

            try:
                raw        = _call_gemini(prompt, client)
                parsed     = json.loads(_strip_fences(raw))
                groups_raw = parsed.get("groups", [])
            except Exception as e:
                logger.warning(
                    f"Batch {batch_num}: grouping failed ({e}); falling back to singletons"
                )
                groups_raw = [
                    {"canonical_title": h.title, "member_ids": [hid]}
                    for hid, h in id_to_headline.items()
                ]

            seen_ids = set()
            for g in groups_raw:
                member_ids = [
                    mid for mid in g.get("member_ids", [])
                    if mid in id_to_headline
                ]
                if not member_ids:
                    continue
                members = [id_to_headline[mid] for mid in member_ids]
                seen_ids.update(member_ids)
                all_groups.append(StoryGroup(
                    canonical_title=g.get("canonical_title", members[0].title),
                    members=members,
                ))

            # Guarantee every headline ends up in exactly one group
            missed = set(id_to_headline.keys()) - seen_ids
            for mid in missed:
                h = id_to_headline[mid]
                all_groups.append(StoryGroup(canonical_title=h.title, members=[h]))

    # Carry error headlines through as empty-titled singletons
    for h in error_headlines:
        all_groups.append(StoryGroup(canonical_title="", members=[h]))

    logger.info(f"Produced {len(all_groups)} story groups from {len(ok_headlines)} headlines")
    return all_groups
