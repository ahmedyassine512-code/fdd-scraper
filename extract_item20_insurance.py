#!/usr/bin/env python3
"""
Pulls raw text for Item 20 (system size table) and the insurance-requirement
section out of each downloaded FDD PDF.

This is deliberately dumb: it finds "ITEM 20" and grabs text up to the next
"ITEM 21", and separately scans the whole document for paragraphs containing
insurance-related keywords. It does NOT try to parse out specific dollar
limits, additional-insured status, or endorsement requirements -- that step
needs a real reader (a person, or an LLM given the raw text) because FDD
insurance language is inconsistent enough across franchisors that a regex
parser will confidently produce wrong structured data. This script's job is
just to get the right raw text in front of whoever/whatever does that next
pass, with zero fabrication risk.

Usage:
    python extract_item20_insurance.py

Reads list.csv (written by scrape_wi_fdd.py) and fdds/*.pdf.
Writes raw_extract.csv with columns:
    name, local_path, item20_raw_text, insurance_raw_text, extraction_status
"""

import csv
import re

import pdfplumber

LIST_CSV = "list.csv"
OUT_CSV = "raw_extract.csv"

ITEM20_START = re.compile(r"ITEM\s*20\b", re.IGNORECASE)
ITEM20_END = re.compile(r"ITEM\s*21\b", re.IGNORECASE)

# Anchor on the outlet-summary table itself, not the "ITEM 20" heading text --
# item titles vary too much across franchisors ("OUTLETS AND FRANCHISEE
# INFORMATION" vs "LIST OF OUTLETS AND FRANCHISE INFORMATION" vs no "ITEM"
# prefix at all in a few cases), but the table underneath is far more
# standardized. Confirmed against the real cohort of 52 downloaded FDDs.
OUTLET_SUMMARY_MARKER = re.compile(
    r"SYSTEM\s*-?\s*WIDE\s+(OUTLET|HOTEL|RESTAURANT|STUDIO)\s+SUMMARY", re.IGNORECASE
)
BARE_TABLE1_MARKER = re.compile(r"TABLE\s*N[O0]\.?\s*1\b", re.IGNORECASE)
ITEM20_ANY = re.compile(r"ITEM\s*20\b", re.IGNORECASE)

INSURANCE_KEYWORDS = re.compile(
    r"(insurance|liability coverage|additional insured|certificate of "
    r"insurance|endorsement|workers['\u2019]?\s*compensation)",
    re.IGNORECASE,
)


def extract_full_text(pdf_path):
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    return "\n".join(text_parts)


def extract_item20(full_text):
    """
    Find the real Item 20 section body -- not an informal front-matter
    reference to it, and not its own Table-of-Contents line.

    v1 (first attempt) took the *first* "ITEM 20" match -- always grabbed
    FTC-mandated intro FAQ boilerplate or the TOC line, since those come
    before the real section. item20_raw_text kept landing around 250-320
    chars across unrelated companies as a result.

    v2 tried "keep the (item20, next item21) pair with the largest gap",
    reasoning boilerplate/TOC hits sit close to their next "ITEM 21" match
    while the real section runs for thousands of characters first. This
    mostly worked but backfired on MIF, L.L.C. (Marriott/JW Marriott):
    the front-matter mention's "next ITEM 21" landed on the TOC's own
    Item 21 line, and the intervening legal boilerplate happened to be
    LONGER (11,719 chars) than the real outlet table (11,062 chars) --
    a numeric coincidence, not a structural signal, so v2 picked the
    boilerplate.

    v3 (this version) anchors on the outlet-summary TABLE itself instead
    of the "ITEM 20" heading text. Item titles vary too much across
    franchisors to use as an anchor ("OUTLETS AND FRANCHISEE INFORMATION"
    vs "LIST OF OUTLETS AND FRANCHISE INFORMATION" vs no "ITEM" prefix at
    all in a couple of real filings), but the table underneath is far
    more standardized ("Systemwide Outlet Summary" / "Table No. 1").
    Finds the first such table marker in the document, walks backward up
    to 400 chars to find the nearest preceding "ITEM 20" (whatever its
    exact wording), and runs from there to the next "ITEM 21" match.
    Validated against the real cohort of 52 downloaded Wisconsin FDDs.
    """
    anchor = OUTLET_SUMMARY_MARKER.search(full_text)
    if not anchor:
        anchor = BARE_TABLE1_MARKER.search(full_text)
    if not anchor:
        return ""

    table_pos = anchor.start()
    window_start = max(0, table_pos - 400)
    preceding = full_text[window_start:table_pos]
    item20_matches = list(ITEM20_ANY.finditer(preceding))
    if not item20_matches:
        # table found but no "ITEM 20" heading nearby -- rare, but take
        # the table position itself rather than returning nothing
        start_pos = table_pos
    else:
        start_pos = window_start + item20_matches[-1].start()

    end = ITEM20_END.search(full_text, table_pos)
    end_pos = end.start() if end else min(len(full_text), start_pos + 30000)
    return full_text[start_pos:end_pos].strip()


def extract_insurance_paragraphs(full_text):
    # Split on blank lines / page breaks into rough paragraphs, keep any
    # paragraph that mentions insurance-adjacent terms. Cap total length so
    # one document with the word "insurance" scattered everywhere doesn't
    # produce a 50-page blob.
    paras = re.split(r"\n\s*\n", full_text)
    hits = [p.strip() for p in paras if INSURANCE_KEYWORDS.search(p)]
    joined = "\n\n---\n\n".join(hits)
    return joined[:15000]


def main():
    try:
        with open(LIST_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"{LIST_CSV} not found -- run scrape_wi_fdd.py first.")
        return

    out_rows = []
    for row in rows:
        path = row.get("local_path")
        name = row.get("name", "")
        if not path:
            out_rows.append({
                "name": name, "local_path": "",
                "item20_raw_text": "", "insurance_raw_text": "",
                "extraction_status": "no_pdf_downloaded",
            })
            continue

        try:
            full_text = extract_full_text(path)
            item20 = extract_item20(full_text)
            insurance = extract_insurance_paragraphs(full_text)
            status = "ok"
            if not item20:
                status += "; item20_not_found"
            if not insurance:
                status += "; insurance_text_not_found"
        except Exception as e:
            full_text = ""
            item20 = ""
            insurance = ""
            status = f"error: {e}"

        out_rows.append({
            "name": name,
            "local_path": path,
            "item20_raw_text": item20,
            "insurance_raw_text": insurance,
            "extraction_status": status,
        })
        print(f"{name[:60]:60s} -> {status}")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["name", "local_path", "item20_raw_text",
                        "insurance_raw_text", "extraction_status"],
        )
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"\nDone. Wrote {OUT_CSV} ({len(out_rows)} rows).")


if __name__ == "__main__":
    main()
