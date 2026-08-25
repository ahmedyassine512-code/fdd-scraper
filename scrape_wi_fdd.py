#!/usr/bin/env python3
"""
Wisconsin DFI Franchise Search scraper.

Enumerates franchise registrations at
https://apps.dfi.wi.gov/apps/FranchiseSearch/MainSearch.aspx and downloads
the FDD PDF for each one it can find, into ./fdds/.

Why this exists: apps.dfi.wi.gov is geo-blocked (or otherwise unreachable)
from some networks at the TCP level. This script is meant to run somewhere
with normal US-reachable egress -- a GitHub Actions runner, in our case --
not on a residential connection that can't complete a TCP handshake to the
host at all.

Design notes:
- This is classic ASP.NET WebForms (__VIEWSTATE / __EVENTVALIDATION /
  __VIEWSTATEGENERATOR postback pattern). Those tokens are per-session and
  change on every request, so nothing is hardcoded -- the script parses the
  live page for hidden fields and for the actual name/id of the search
  textbox and submit control on every run.
- The search box appears to be a "contains" search on legal/trade name with
  no visible "browse all" option. To enumerate broadly for free, we run the
  single-character alphabet (a-z, 0-9) as successive queries and de-dupe
  results by file number. This is a known workaround for "must type
  something" search boxes and should surface the large majority of active
  registrations without needing a paid bulk-export product.
- Untested against a live POST at the time of writing (my own reachability
  to this host is fetch-only, not POST-capable, from the environment I
  built this in). Expect to debug the field-name discovery on the first
  real run -- the debug dump this script writes on failure
  (debug_search_page.html) is there for exactly that.
"""

import csv
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

BASE = "https://apps.dfi.wi.gov"
SEARCH_URL = f"{BASE}/apps/FranchiseSearch/MainSearch.aspx"

OUT_DIR = "fdds"
LIST_CSV = "list.csv"
DEBUG_HTML = "debug_search_page.html"

ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def get_form_state(session, url):
    """Fetch a page and return (soup, hidden_field_dict)."""
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hidden = {}
    for inp in soup.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            hidden[name] = inp.get("value", "")
    return soup, hidden


def guess_search_field(soup):
    """Find the visible text input most likely to be the name search box."""
    candidates = soup.find_all("input", type="text")
    if not candidates:
        # some ASP.NET controls omit type= and default to text
        candidates = [
            i for i in soup.find_all("input")
            if i.get("type") not in ("hidden", "submit", "button", "checkbox", "radio")
        ]
    for c in candidates:
        name = (c.get("name") or "").lower()
        if "search" in name or "name" in name or "txt" in name:
            return c.get("name")
    return candidates[0].get("name") if candidates else None


def guess_submit_control(soup):
    """Find the search/submit button's name (ASP.NET postback control)."""
    for tag in soup.find_all(["input", "button"]):
        ttype = (tag.get("type") or "").lower()
        val = (tag.get("value") or tag.text or "").strip().lower()
        name = (tag.get("name") or "").lower()
        if ttype in ("submit", "button") or "btn" in name:
            if "search" in val or "search" in name or ttype == "submit":
                return tag.get("name")
    return None


def do_search(session, query):
    """Submit the search form with `query` in the name field. Returns soup of results page."""
    soup, hidden = get_form_state(session, SEARCH_URL)
    field = guess_search_field(soup)
    submit_ctl = guess_submit_control(soup)

    if not field:
        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
            f.write(soup.prettify())
        raise RuntimeError(
            "Could not find a search text field on the page. "
            f"Dumped page to {DEBUG_HTML} for inspection."
        )

    payload = dict(hidden)
    payload[field] = query
    if submit_ctl:
        payload[submit_ctl] = "Search"

    r = session.post(SEARCH_URL, data=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_results(soup):
    """
    Pull (name, file_number-ish text, detail_url) out of the results table.
    Falls back to 'any link that looks like a detail/filing link' if there's
    no clean <table> to key off of.
    """
    rows = []
    seen_hrefs = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if not text:
            continue
        low = href.lower()
        if any(k in low for k in ("detail", "filing", "franchise", "view")) and href not in seen_hrefs:
            seen_hrefs.add(href)
            rows.append({
                "name": text,
                "detail_url": urllib.parse.urljoin(BASE, href),
            })
    return rows


def find_pdf_link(session, detail_url):
    r = session.get(detail_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            return urllib.parse.urljoin(BASE, href)
    return None


def download_pdf(session, pdf_url, dest_path):
    r = session.get(pdf_url, headers=HEADERS, timeout=60, stream=True)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def safe_filename(name):
    name = re.sub(r"[^\w\s-]", "", name).strip()
    name = re.sub(r"[\s]+", "_", name)
    return name[:120] or "unnamed"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    session = requests.Session()

    all_results = {}  # keyed by detail_url to de-dupe

    for letter in ALPHABET:
        try:
            soup = do_search(session, letter)
        except Exception as e:
            print(f"[query={letter!r}] search failed: {e}", file=sys.stderr)
            continue

        results = parse_results(soup)
        new = 0
        for r in results:
            if r["detail_url"] not in all_results:
                all_results[r["detail_url"]] = r
                new += 1
        print(f"[query={letter!r}] {len(results)} results, {new} new, "
              f"{len(all_results)} total so far")
        time.sleep(1)  # be polite to a state government server

    print(f"\nEnumeration done: {len(all_results)} unique registrations found.")

    rows_out = []
    for i, (detail_url, rec) in enumerate(all_results.items(), 1):
        row = {
            "name": rec["name"],
            "detail_url": detail_url,
            "pdf_url": "",
            "local_path": "",
            "download_status": "not_attempted",
        }
        try:
            pdf_url = find_pdf_link(session, detail_url)
            row["pdf_url"] = pdf_url or ""
            if pdf_url:
                fname = safe_filename(rec["name"]) + ".pdf"
                dest = os.path.join(OUT_DIR, fname)
                download_pdf(session, pdf_url, dest)
                row["local_path"] = dest
                row["download_status"] = "ok"
            else:
                row["download_status"] = "no_pdf_link_found"
        except Exception as e:
            row["download_status"] = f"error: {e}"

        rows_out.append(row)
        print(f"[{i}/{len(all_results)}] {rec['name'][:60]:60s} "
              f"-> {row['download_status']}")
        time.sleep(1)

    with open(LIST_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["name", "detail_url", "pdf_url", "local_path", "download_status"]
        )
        writer.writeheader()
        writer.writerows(rows_out)

    ok = sum(1 for r in rows_out if r["download_status"] == "ok")
    print(f"\nDone. {ok}/{len(rows_out)} PDFs downloaded. See {LIST_CSV} and {OUT_DIR}/.")


if __name__ == "__main__":
    main()
