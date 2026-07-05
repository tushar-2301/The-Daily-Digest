"""
Uses GEMINI_API_KEY1

stage3_process/ranker.py
--------------------------
Stage 3 (part 2) — rank StoryGroups by newsworthiness / importance.

Since all sources are business-focused, we skip the business/non-business
classification entirely and go straight to ranking.

Ranking signals sent to Gemini per group:
  - canonical_title          : the story summary
  - num_sources              : how many distinct outlets covered it
  - sources                  : which outlets (gives Gemini breadth signal)
  - description_preview      : up to 300 chars of the best available
                               article description (signal of story depth)

Gemini returns an ordered list of group IDs from most to least important.
We apply that order; groups missing from Gemini's list are appended at the
end sorted by num_sources descending (safe fallback).

Batching strategy for scale (20 sources × 7 = 140 headlines → ~80-120 groups):
  A single Gemini ranking call handles up to RANK_BATCH_SIZE groups.
  If there are more groups than that, we rank in chunks and merge by
  interleaving the top results from each chunk (round-robin merge), which
  is a reasonable approximation without requiring a second-pass call.
  At the expected scale (5-20 sources) a single call is almost always enough.

Fix (June 2026):
  - Auth keys (AQ.Ab...) require x-goog-api-key header, not ?key= query param
  - Removed thinkingConfig:{thinkingBudget:0} — caused empty responses when
    combined with responseMimeType on gemini-2.5-flash
"""

import json
import logging
import re
import time
from dataclasses import asdict

import httpx

from config import StoryGroup, GEMINI_API_KEY1, GEMINI_MODEL
from . import rate_limiter

RATE_LIMIT_KEY = "GEMINI_API_KEY1"

logger = logging.getLogger("news_pipeline.stage3.rank")

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

# Max story groups to rank in a single Gemini call.
# At 20 sources × 7 headlines, after dedup expect ~60-100 groups — well within this.
RANK_BATCH_SIZE = 120

RANK_PROMPT = """You are a senior business news editor.
Rank the following story groups from MOST to LEAST important for a professional business news digest. 
Prioritize stories with: 

Consider:
1. BREADTH  — stories covered by more sources are generally more important
2. IMPACT   — macroeconomic, market-moving, or policy stories outrank niche corporate items
3. RECENCY  — breaking developments outrank routine updates
4. DEPTH    — stories with substantive descriptions signal more significant coverage


1. Highest macroeconomic, financial, or policy impact. 
2. Greatest effect on businesses, investors, markets, or industries. 
3. Wider geographic or economic reach (country/region > sector > company). 
4. Major market events (stock markets, IPOs, banking, trade, regulation, infrastructure, taxation, central banks, energy). 
5. Breaking or significant developments over routine updates. 
6. Coverage by multiple independent sources as a signal of importance.

Deprioritize routine company updates, product launches, awards, events, branch openings, promotional pieces, and lifestyle stories unless they have substantial business impact. 
If web search or grounding is available, use it only to verify the real-world significance of major stories. Never invent facts.

Story groups to rank (id, title, num_sources, sources covering it, description_preview):
{groups_json}

Return ONLY a valid JSON object (no markdown fences) with the group IDs in order from MOST to LEAST important:
{{
  "ranked_ids": ["0", "3", "1", ...]
}}

Include ALL {total} IDs exactly once.


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
    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY1,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            # thinkingConfig intentionally omitted — thinkingBudget:0 + responseMimeType
            # caused empty responses on gemini-2.5-flash (June 2026 regression).
        },
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        # Block here (never raise) until it's safe to make another call
        # without exceeding the shared RPM cap for this key.
        rate_limiter.wait_for_slot(RATE_LIMIT_KEY, logger=logger)
        try:
            resp = client.post(url, json=payload, headers=headers, timeout=120.0)
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
    raise RuntimeError(f"Gemini ranking failed after {max_retries} attempts: {last_err}")


def _chunk(lst, size):
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _best_description(group: StoryGroup, max_chars: int = 300) -> str:
    """Return the best (longest) description from group members, truncated."""
    descs = [
        m.description for m in group.members
        if m.status == "ok" and m.description
    ]
    if not descs:
        return ""
    best = max(descs, key=len)
    return best[:max_chars] + ("…" if len(best) > max_chars else "")


def _rank_batch(
    batch: list[tuple[str, StoryGroup]],
    client: httpx.Client,
) -> list[str]:
    """
    Ask Gemini to rank a batch of (id, group) pairs.
    Returns a list of ids in ranked order.
    Falls back to num_sources-descending order on any error.
    """
    payload = []
    for gid, g in batch:
        sources = sorted({m.source for m in g.members if m.status == "ok"})
        payload.append({
            "id":                   gid,
            "canonical_title":      g.canonical_title,
            "num_sources":          len(sources),
            "sources":              sources,
            "description_preview":  _best_description(g),
        })

    prompt = RANK_PROMPT.format(
        groups_json=json.dumps(payload, ensure_ascii=False, indent=2),
        total=len(batch),
    )

    try:
        raw    = _call_gemini(prompt, client)
        parsed = json.loads(_strip_fences(raw))
        ranked = parsed.get("ranked_ids", [])
        # Validate — must be strings that exist in our batch
        valid_ids = {gid for gid, _ in batch}
        ranked = [str(r) for r in ranked if str(r) in valid_ids]
        return ranked
    except Exception as e:
        logger.warning(f"Ranking call failed ({e}); falling back to source-count order")
        return [gid for gid, _ in sorted(batch, key=lambda x: len({m.source for m in x[1].members}), reverse=True)]


def _interleave(lists: list[list]) -> list:
    """Round-robin merge of ranked sub-lists (used when batching is needed)."""
    result = []
    iters  = [iter(lst) for lst in lists]
    while iters:
        next_iters = []
        for it in iters:
            val = next(it, None)
            if val is not None:
                result.append(val)
                next_iters.append(it)
        if not next_iters:
            break
        iters = next_iters
    return result


def rank_groups(groups: list[StoryGroup]) -> list[StoryGroup]:
    """
    Rank story groups by importance using Gemini.

    Only real (non-empty, non-error) groups are ranked.
    Error/empty groups are excluded from the final output.
    """
    real_groups = [
        g for g in groups
        if g.canonical_title and g.members and any(m.status == "ok" for m in g.members)
    ]

    if not real_groups:
        logger.warning("No valid story groups to rank.")
        return []

    # Assign stable string IDs for this ranking pass
    id_to_group = {str(i): g for i, g in enumerate(real_groups)}

    batches = _chunk(list(id_to_group.items()), RANK_BATCH_SIZE)
    logger.info(
        f"Ranking {len(real_groups)} story groups across {len(batches)} batch(es) "
        f"(batch size: {RANK_BATCH_SIZE})"
    )

    with httpx.Client() as client:
        ranked_id_lists = [_rank_batch(batch, client) for batch in batches]

    # Merge ranked lists from all batches
    if len(ranked_id_lists) == 1:
        final_ranked_ids = ranked_id_lists[0]
    else:
        logger.info(
            f"Merging {len(ranked_id_lists)} ranked sub-lists via round-robin interleave"
        )
        final_ranked_ids = _interleave(ranked_id_lists)

    # Build final ordered list
    seen = set()
    ordered: list[StoryGroup] = []
    for gid in final_ranked_ids:
        if gid in id_to_group and gid not in seen:
            ordered.append(id_to_group[gid])
            seen.add(gid)

    # Append any groups Gemini omitted (safety net), sorted by source breadth
    missing = [
        (gid, g) for gid, g in id_to_group.items() if gid not in seen
    ]
    if missing:
        logger.warning(f"{len(missing)} group(s) missing from Gemini ranking — appending at end")
        missing.sort(key=lambda x: len({m.source for m in x[1].members}), reverse=True)
        for _, g in missing:
            ordered.append(g)

    logger.info(f"Final ranked output: {len(ordered)} story groups")
    return ordered
