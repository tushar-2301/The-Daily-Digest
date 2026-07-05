"""
rate_limiter.py
----------------
Shared, process-safe RPM limiter for Gemini API calls.

dedupe_grouper.py and ranker.py both hit GEMINI_API_KEY1. Each file only
tracked spacing between its *own* calls (a fixed sleep(20) after success),
which has no idea what the other script (or another batch, or a retry) is
doing. Two scripts that each individually look "safe" can still add up to
more than the RPM quota when run back-to-back — which is what happened.

This module fixes that by making the limit proactive and shared:
  - Before every single HTTP attempt (including retries — a 429 still
    counts as a request against the quota), we check a rolling log of
    call timestamps persisted to disk.
  - If we're at the safe cap, we sleep until a slot frees up, then re-check.
  - The cap is set BELOW the documented RPM limit as a safety margin,
    since we're only estimating the provider's window boundaries, not
    observing them directly.

Speed is not a concern here (per project preference) — only avoiding the
rate limit matters, so we err heavily on the side of waiting.
"""

import json
import os
import sys
import time
import tempfile
from contextlib import contextmanager

# Documented limit is 5 RPM. We stay well under it on purpose.
RPM_LIMIT = 5
SAFE_RPM_CAP = 3            # never allow more than this many calls / window
WINDOW_SECONDS = 60
BUFFER_SECONDS = 5          # extra cushion on top of the 60s window

STATE_DIR = os.path.join(tempfile.gettempdir(), "gemini_rate_limiter")
os.makedirs(STATE_DIR, exist_ok=True)

# File locking is OS-specific: fcntl exists on Linux/Mac only, msvcrt on
# Windows. Pick whichever is available at import time.
if sys.platform.startswith("win"):
    import msvcrt

    def _lock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def _locked_file(path):
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump([], f)
            # Ensure at least 1 byte exists for msvcrt.locking to lock on.
            f.write(" ")
    f = open(path, "r+")
    try:
        _lock(f)
        yield f
    finally:
        _unlock(f)
        f.close()


def _state_path(key_name: str) -> str:
    safe_name = "".join(c for c in key_name if c.isalnum() or c in ("_", "-"))
    return os.path.join(STATE_DIR, f"{safe_name}.json")


def wait_for_slot(key_name: str = "GEMINI_API_KEY1", logger=None):
    """
    Block until it is safe to make another call for this API key without
    exceeding SAFE_RPM_CAP within WINDOW_SECONDS. Reserves the slot
    (records the timestamp) before returning, so concurrent/sequential
    callers across both scripts can't race each other into over-calling.
    """
    path = _state_path(key_name)
    while True:
        with _locked_file(path) as f:
            f.seek(0)
            try:
                timestamps = json.load(f)
            except json.JSONDecodeError:
                timestamps = []

            now = time.time()
            timestamps = [t for t in timestamps if now - t < WINDOW_SECONDS]

            if len(timestamps) < SAFE_RPM_CAP:
                timestamps.append(now)
                f.seek(0)
                f.truncate()
                json.dump(timestamps, f)
                return  # slot reserved

            sleep_for = WINDOW_SECONDS - (now - timestamps[0]) + BUFFER_SECONDS

        if logger:
            logger.info(
                f"[rate-limit] at cap ({SAFE_RPM_CAP}/{WINDOW_SECONDS}s) for "
                f"{key_name} — sleeping {sleep_for:.1f}s"
            )
        time.sleep(max(sleep_for, 1))


def record_429(key_name: str = "GEMINI_API_KEY1", logger=None):
    """
    Call this when the API returns 429 despite wait_for_slot() saying it was
    safe — it means our estimate of the provider's window was wrong (clock
    skew, another process using this key, provider bucketing differently).
    Pads the log so subsequent calls back off harder rather than repeating
    the same mistake.
    """
    path = _state_path(key_name)
    with _locked_file(path) as f:
        f.seek(0)
        try:
            timestamps = json.load(f)
        except json.JSONDecodeError:
            timestamps = []
        now = time.time()
        timestamps = [t for t in timestamps if now - t < WINDOW_SECONDS]
        timestamps.extend([now] * SAFE_RPM_CAP)
        f.seek(0)
        f.truncate()
        json.dump(timestamps, f)
    if logger:
        logger.warning(f"[rate-limit] 429 received for {key_name} — padded window, backing off fully")
