"""
run_daily.py
------------
Standalone orchestrator for the daily scheduled job. Does NOT modify any
existing pipeline file — it simply calls the existing, already-working
pieces in order:

    1. run_pipeline()        (pipeline.py)   -> output/final_news.json
    2. build_pdf()            (generate_news_pdf.py) -> output/headlines_{today}.pdf
    3. send_pdf_email()       (mailer.py)     -> emails the PDF via Gmail

This is the single entry point that Windows Task Scheduler should call
every day at 8:00 AM. See README_SCHEDULING.md for setup steps.

Usage:
    python run_daily.py
"""

import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from pipeline import run_pipeline
from generate_news_pdf import build_pdf
from mailer import send_pdf_email

INPUT_CSV = "sources.csv"
OUTPUT_DIR = "output"
_today = f"{datetime.now().day} {datetime.now():%B}"   # e.g. "2 July" — matches generate_news_pdf.py
PDF_PATH = f"{OUTPUT_DIR}/headlines_{_today}.pdf"


def main():
    try:
        print("=" * 72)
        print("STEP 1/3 — Running news pipeline (fetch -> select -> dedupe -> rank)")
        print("=" * 72)
        final = asyncio.run(run_pipeline(INPUT_CSV, OUTPUT_DIR))

        print("\n" + "=" * 72)
        print("STEP 2/3 — Generating PDF")
        print("=" * 72)
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        build_pdf(final, PDF_PATH)

        print("\n" + "=" * 72)
        print("STEP 3/3 — Emailing PDF")
        print("=" * 72)
        send_pdf_email(PDF_PATH)

        print("\nDaily run complete.")

    except Exception as e:
        print(f"\nDaily run FAILED: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
