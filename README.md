# News Pipeline

An automated pipeline that pulls business news from RSS feeds and news homepages across South & Southeast Asia, uses Gemini to select and summarize the most important stories, deduplicates and ranks them, and produces a polished PDF news digest that can be emailed out automatically.

## How it works

The pipeline runs in stages. Each stage writes its output to `output/` as JSON, so you can inspect intermediate results or resume from any point without re-running earlier (expensive) stages.

| Stage | What happens | Output |
|---|---|---|
| **1** | Fetch headlines from RSS feeds directly; load HTML homepages via Playwright (headless browser) for sources without RSS | `stage1_rss_headlines.json`, `stage1_raw_pages.json` |
| **2** | Gemini reads each HTML homepage and selects the top headlines (title + link) | `stage2_llm_headlines.json` |
| **2.5** | Playwright visits every selected article link concurrently and grabs cleaned article HTML | `stage25_article_html_sizes.json` |
| **2.6** | Gemini reads each source's article batch and writes a title + 2–3 sentence description per story | `stage26_enriched_headlines.json` |
| **3a** | Gemini clusters headlines that refer to the same underlying story across sources | `stage3a_groups.json` |
| **3b** | Gemini ranks the story groups by importance for a business audience | `stage3b_ranked_groups.json` |
| — | Final combined output | `final_news.json`, `summary.json` |

After the pipeline finishes, `generate_news_pdf.py` turns `final_news.json` into a formatted PDF digest, and `mailer.py` can email that PDF as an attachment via Gmail SMTP.

## Project structure

```
.
├── run.py                     # CLI entrypoint — runs the full pipeline (or stops early)
├── run_from_stage3.py         # Resume: dedupe + rank only, from an existing stage2.6 file
├── pipeline.py                # Orchestrates all stages
├── config.py                  # Shared dataclasses, constants, .env loading
├── mailer.py                  # Emails a PDF via Gmail SMTP
├── generate_news_pdf.py       # Renders final_news.json into a PDF digest
├── sources.csv                # List of news sources to pull from
├── requirements.txt
├── stage1_fetch/
│   ├── rss_fetcher.py         # Parses RSS/Atom feeds
│   └── html_fetcher.py        # Loads JS-rendered homepages via Playwright
├── stage2_extract/
│   └── llm_headline_extractor.py   # Gemini: pick top headlines from homepage HTML
├── stage2_fetch/
│   ├── article_fetcher.py     # Concurrently fetches individual article pages
│   └── description_extractor.py    # Gemini: write descriptions for each article batch
├── stage3_process/
│   ├── dedupe_grouper.py      # Gemini: cluster headlines into story groups
│   ├── ranker.py              # Gemini: rank story groups by importance
│   └── rate_limiter.py        # Shared rate limiter for Gemini calls
└── output/                    # All intermediate + final JSON, and generated PDFs
```

## Requirements

- Python 3.12+
- A Chromium browser for Playwright (installed via `playwright install`)
- Three Gemini API keys (see [Environment variables](#environment-variables) — usage is split across keys to spread out rate limits)
- A Gmail account with an App Password, if you want automatic email delivery

## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Create a `.env` file** in the project root:

   ```ini
   GEMINI_API_KEY1=your_key_here
   GEMINI_API_KEY2=your_key_here
   GEMINI_API_KEY3=your_key_here
   GEMINI_MODEL=gemini-2.5-flash

   # Optional — only needed for mailer.py
   EMAIL_FROM=youraddress@gmail.com
   EMAIL_TO=youraddress@gmail.com          # comma-separated for multiple recipients
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx  # NOT your normal Gmail password
   ```

   > **Gmail App Password setup:** Enable 2-Step Verification at [myaccount.google.com/security](https://myaccount.google.com/security), then create an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) for "Mail" / a custom name like `news-pipeline`.

3. **Edit `sources.csv`** to configure which sources to pull from. Columns: `name, country, url, type`, where `type` is either `rss` or `html`.

## Usage

### Run the full pipeline

```bash
python run.py
```

This fetches all sources, selects and describes headlines, dedupes and ranks them, and writes `output/final_news.json` + `output/summary.json`.

### Run partial stages (for debugging / inspection)

```bash
python run.py --stop-after stage1     # fetch only — no Gemini calls
python run.py --stop-after stage2     # + top-headline selection
python run.py --stop-after stage2_6   # + article fetch + descriptions
```

### Custom input/output paths

```bash
python run.py --input sources.csv --output output/
```

### Resume from an existing enriched-headlines file

If you already have `output/stage26_enriched_headlines.json` and just want to re-run dedupe/ranking (e.g. after tweaking `dedupe_grouper.py` or `ranker.py`):

```bash
python run_from_stage3.py
python run_from_stage3.py --input output/stage26_enriched_headlines.json --output output/
```

### Generate the PDF digest

```bash
python generate_news_pdf.py
python generate_news_pdf.py --input output/final_news.json --output output/headlines_latest.pdf
```

### Email the PDF

```bash
python mailer.py --file output/headlines_latest.pdf
```

### End-to-end (pipeline → PDF → email)

```bash
python run.py
python generate_news_pdf.py
python mailer.py --file output/headlinesss_latest.pdf
```

## Configuration

Key tunables live in `config.py`:

| Constant | Default | Purpose |
|---|---|---|
| `TOP_HEADLINES_PER_SOURCE` | `3` | How many headlines Gemini selects per HTML source in Stage 2 |
| `MAX_CONCURRENT_BROWSER_PAGES` | `4` | Playwright concurrency for homepage fetches (Stage 1) |
| `MAX_CONCURRENT_ARTICLE_PAGES` | `6` | Playwright concurrency for article fetches (Stage 2.5) |
| `MAX_HTML_CHARS_FOR_LLM` | `50,000` | Cap on homepage HTML sent to Gemini |
| `MAX_ARTICLE_HTML_CHARS` | `30,000` | Cap on article HTML sent to Gemini |
| `MAX_HEADLINES_PER_DEDUPE_BATCH` | `150` | Batch size for Stage 3 dedupe calls |
| `HTTPX_TIMEOUT` / `PLAYWRIGHT_NAV_TIMEOUT_MS` | `20s` / `30s` | Fetch timeouts |
| `MAX_RETRIES` | `3` | Retry attempts for fetches |