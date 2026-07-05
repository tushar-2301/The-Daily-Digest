"""
generate_news_pdf.py
--------------------
Standalone script — reads output/final_news.json and produces a
professional news digest PDF.

Usage:
    python generate_news_pdf.py
    python generate_news_pdf.py --input output/final_news.json --output output/headlinesss.pdf

No existing pipeline files are modified.
"""

import argparse
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path


def _strip_html(text: str) -> str:
    """Strip all HTML tags and collapse whitespace — defensive guard for PDF rendering."""
    if not text:
        return text

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)

    s = _Stripper()
    try:
        s.feed(text)
    except Exception:
        # If the HTML is truly malformed, fall back to a simple regex strip
        return re.sub(r"<[^>]+>", " ", text).strip()
    clean = " ".join(s.parts)
    return re.sub(r"\s+", " ", clean).strip()

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
INK         = colors.HexColor("#1A1A2E")   # near-black
ACCENT      = colors.HexColor("#C0392B")   # deep red — masthead / rank badge
RULE        = colors.HexColor("#D5D5D5")   # light rule between stories
META_GREY   = colors.HexColor("#6B6B6B")   # source / country label
LINK_BLUE   = colors.HexColor("#1A5276")   # URL text
CREAM       = colors.HexColor("#FAFAF8")   # subtle story background
WHITE       = colors.white

PAGE_W, PAGE_H = A4
MARGIN_LR   = 18 * mm
MARGIN_TB   = 20 * mm
BODY_WIDTH  = PAGE_W - 2 * MARGIN_LR


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
def build_styles():
    base = getSampleStyleSheet()

    masthead = ParagraphStyle(
        "Masthead",
        fontName="Times-Bold",
        fontSize=28,
        textColor=INK,
        alignment=TA_CENTER,
        spaceAfter=2,
        leading=32,
    )
    tagline = ParagraphStyle(
        "Tagline",
        fontName="Helvetica",
        fontSize=9,
        textColor=META_GREY,
        alignment=TA_CENTER,
        spaceAfter=0,
        tracking=1.5,
    )
    section_label = ParagraphStyle(
        "SectionLabel",
        fontName="Helvetica-Bold",
        fontSize=7,
        textColor=WHITE,
        alignment=TA_CENTER,
        leading=10,
    )
    headline = ParagraphStyle(
        "Headline",
        fontName="Times-Bold",
        fontSize=13,
        textColor=INK,
        leading=17,
        spaceAfter=4,
    )
    description = ParagraphStyle(
        "Description",
        fontName="Times-Roman",
        fontSize=10,
        textColor=INK,
        leading=15,
        spaceAfter=5,
    )
    meta = ParagraphStyle(
        "Meta",
        fontName="Helvetica",
        fontSize=8,
        textColor=META_GREY,
        leading=12,
        spaceAfter=2,
    )
    link = ParagraphStyle(
        "Link",
        fontName="Helvetica",
        fontSize=8,
        textColor=LINK_BLUE,
        leading=11,
    )
    footer_style = ParagraphStyle(
        "Footer",
        fontName="Helvetica",
        fontSize=7.5,
        textColor=META_GREY,
        alignment=TA_CENTER,
    )
    return {
        "masthead": masthead,
        "tagline": tagline,
        "section_label": section_label,
        "headline": headline,
        "description": description,
        "meta": meta,
        "link": link,
        "footer": footer_style,
    }


# ---------------------------------------------------------------------------
# Header / footer callbacks
# ---------------------------------------------------------------------------
def make_page_template(date_str: str, total_stories: int):
    def on_page(canvas, doc):
        canvas.saveState()
        w, h = A4

        # ── top rule ──────────────────────────────────────────────────────
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(1.5)
        canvas.line(MARGIN_LR, h - MARGIN_TB + 4 * mm,
                    w - MARGIN_LR, h - MARGIN_TB + 4 * mm)

        # ── footer rule ───────────────────────────────────────────────────
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_LR, MARGIN_TB - 4 * mm,
                    w - MARGIN_LR, MARGIN_TB - 4 * mm)

        # ── footer text ───────────────────────────────────────────────────
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(META_GREY)
        canvas.drawString(MARGIN_LR, MARGIN_TB - 8 * mm,
                          f"Business News Digest  ·  {date_str}  ·  {total_stories} stories")
        canvas.drawRightString(w - MARGIN_LR, MARGIN_TB - 8 * mm,
                               f"Page {doc.page}")

        canvas.restoreState()

    return on_page


# ---------------------------------------------------------------------------
# Rank badge (coloured box + number)
# ---------------------------------------------------------------------------
def rank_badge(rank: int, style: ParagraphStyle) -> Table:
    label = Paragraph(f"#{rank}", style)
    tbl = Table([[label]], colWidths=[8 * mm], rowHeights=[8 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, -1), ACCENT),
        ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",(0, 0), (-1, -1), 0),
        ("TOPPADDING",  (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ("ROUNDEDCORNERS", (0, 0), (-1, -1), [2, 2, 2, 2]),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Story card builder
# ---------------------------------------------------------------------------
def story_card(story: dict, styles: dict) -> list:
    """Returns a list of flowables for one story."""
    elements = []

    rank    = story["rank"]
    title   = story["canonical_title"]
    sources = story["sources"]
    articles = story["articles"]

    # ── rank + headline row ───────────────────────────────────────────────
    badge     = rank_badge(rank, styles["section_label"])
    hl_para   = Paragraph(title, styles["headline"])

    header_tbl = Table(
        [[badge, hl_para]],
        colWidths=[10 * mm, BODY_WIDTH - 10 * mm],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (0, 0),   0),
        ("RIGHTPADDING", (0, 0), (0, 0),   3),
        ("LEFTPADDING",  (1, 0), (1, 0),   4),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 3 * mm))

    # ── per-article details ───────────────────────────────────────────────
    for i, art in enumerate(articles):
        source_name = art.get("source", "")
        country     = art.get("country", "")
        desc        = _strip_html(art.get("description", "") or "")
        link        = art.get("link", "")
        art_title   = art.get("title", "")

        # source / country pill
        meta_text = f"<b>{source_name}</b>  ·  {country}"
        elements.append(Paragraph(meta_text, styles["meta"]))

        # article headline (only if it differs meaningfully from canonical)
        if art_title and art_title.strip() != title.strip():
            elements.append(Paragraph(art_title, styles["description"]))

        # description
        if desc:
            elements.append(Paragraph(desc, styles["description"]))

        # source URL
        if link:
            safe_link = link.replace("&", "&amp;")
            elements.append(
                Paragraph(f'<link href="{safe_link}" color="#1A5276">{link}</link>',
                          styles["link"])
            )

        # separator between multiple articles in same story
        if i < len(articles) - 1:
            elements.append(Spacer(1, 2 * mm))
            elements.append(
                HRFlowable(width="100%", thickness=0.3,
                           color=RULE, spaceAfter=2 * mm)
            )

    elements.append(Spacer(1, 4 * mm))
    elements.append(
        HRFlowable(width="100%", thickness=0.8, color=RULE, spaceAfter=4 * mm)
    )
    return elements


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------
def build_pdf(data: dict, output_path: str):
    styles    = build_styles()
    stories   = data.get("headlines", [])
    summary   = data.get("summary", {})
    date_str  = datetime.now().strftime("%B %d, %Y")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN_LR,
        rightMargin=MARGIN_LR,
        topMargin=MARGIN_TB + 4 * mm,
        bottomMargin=MARGIN_TB + 4 * mm,
        title="Business News Digest",
        author="News Pipeline",
        subject=f"Digest — {date_str}",
    )

    flowables = []

    # ── masthead ──────────────────────────────────────────────────────────
    flowables.append(Spacer(1, 2 * mm))
    flowables.append(Paragraph("Business News Digest", styles["masthead"]))
    flowables.append(Spacer(1, 1 * mm))

    countries = sorted({
        art["country"]
        for s in stories for art in s["articles"]
        if art.get("country")
    })
    flowables.append(
        Paragraph(
            f"{date_str}  ·  {summary.get('total_stories_after_dedup', len(stories))} Stories  ·  "
            + "  &amp;  ".join(countries),
            styles["tagline"],
        )
    )
    flowables.append(Spacer(1, 3 * mm))

    # ── thick red rule under masthead ─────────────────────────────────────
    flowables.append(
        HRFlowable(width="100%", thickness=2, color=ACCENT, spaceAfter=5 * mm)
    )

    # ── stories ───────────────────────────────────────────────────────────
    for story in stories:
        flowables.extend(story_card(story, styles))

    # ── build ─────────────────────────────────────────────────────────────
    doc.build(
        flowables,
        onFirstPage=make_page_template(date_str, len(stories)),
        onLaterPages=make_page_template(date_str, len(stories)),
    )
    print(f"PDF saved → {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    today = f"{datetime.now().day} {datetime.now():%B}"   # e.g. "2 July"

    default_output = f"output/headlines_{today}.pdf"

    parser = argparse.ArgumentParser(
        description="Generate a professional news digest PDF from final_news.json"
    )
    parser.add_argument("--input",  default="output/final_news.json",
                        help="Path to final_news.json  (default: output/final_news.json)")
    parser.add_argument("--output",  default=default_output,
                        help=f"Output PDF path  (default: {default_output})")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    build_pdf(data, args.output)


if __name__ == "__main__":
    main()
