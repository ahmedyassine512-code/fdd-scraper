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


POSTBACK_RE = re.compile(
    r"__doPostBack\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)"
)

NON_NAME_CELL_TEXT = {"details", "view", "select", "download", ""}


def do_search(session, query):
    """
    Submit the search form with `query` in the name field. Returns
    (soup, hidden_fields_of_result_page) -- the hidden fields are needed
    to follow pagination postbacks, since __VIEWSTATE changes on every
    postback.
    """
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
    result_soup = BeautifulSoup(r.text, "html.parser")
    result_hidden = {
        inp.get("name"): inp.get("value", "")
        for inp in result_soup.find_all("input", type="hidden")
        if inp.get("name")
    }
    return result_soup, result_hidden


def find_results_table(soup):
    """
    Locate the results grid specifically. Confirmed from a live run: the
    ASP.NET GridView control's id contains 'grdSearchResults' (visible in
    its sort-header postback calls, e.g.
    __doPostBack('grdSearchResults','Sort$franchiseFilingID')). Scoping to
    this table avoids picking up the top-nav 'Franchise E-Filing' link and
    the sortable column-header postback links, both of which polluted the
    first run's results.
    """
    for table in soup.find_all("table"):
        tid = (table.get("id") or "").lower()
        if "grdsearchresults" in tid:
            return table
    # fallback: largest table on the page, if the id ever changes
    tables = soup.find_all("table")
    if tables:
        return max(tables, key=lambda t: len(t.find_all("tr")))
    return None


def parse_results(soup, page_url):
    """
    Pull (name, detail_url) out of the results grid.

    Confirmed from a live run: the link text in the 'Details' column is
    literally the word "Details" on every row -- it is NOT the franchisor
    name. The name has to come from a data cell in the same row.
    """
    rows = []
    table = find_results_table(soup)
    if table is None:
        return rows

    trs = table.find_all("tr")
    if not trs:
        return rows

    header_cells = [c.get_text(strip=True).lower() for c in trs[0].find_all(["th", "td"])]
    name_col_idx = None
    for i, h in enumerate(header_cells):
        if any(k in h for k in ("name", "franchisor", "trade")):
            name_col_idx = i
            break

    for tr in trs[1:]:  # skip header row
        cells = tr.find_all("td")
        if not cells:
            continue

        # find a real (non-javascript:) link in this row -- that's the detail link
        detail_href = None
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("javascript:"):
                continue  # sort headers / postback controls, not real links
            detail_href = href
            break
        if not detail_href:
            continue  # likely a pager row or spacer row, not a data row

        if name_col_idx is not None and name_col_idx < len(cells):
            name = cells[name_col_idx].get_text(strip=True)
        else:
            texts = [c.get_text(strip=True) for c in cells]
            texts = [t for t in texts if t.lower() not in NON_NAME_CELL_TEXT]
            name = max(texts, key=len) if texts else "UNKNOWN"

        rows.append({
            "name": name,
            "detail_url": urllib.parse.urljoin(page_url, detail_href),
        })

    return rows


def find_pager_targets(soup, current_page_arg=None):
    """
    Look for additional __doPostBack('grdSearchResults', 'Page$N') calls in
    the page (typical ASP.NET GridView pager). Returns a list of
    (event_target, event_argument) tuples for pages not yet visited.
    Best-effort: if there's no pager, this returns an empty list and
    pagination is simply skipped, which is correct behavior for
    single-page result sets.
    """
    targets = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = POSTBACK_RE.search(href)
        if not m:
            continue
        target, arg = m.group(1), m.group(2)
        if "grdsearchresults" in target.lower() and arg.lower().startswith("page"):
            targets.append((target, arg))
    return targets


def fetch_all_pages(session, first_soup, first_hidden):
    """
    Given the first results page, follow any GridView pager postbacks and
    return a combined list of (name, detail_url) rows across all pages.
    """
    all_rows = parse_results(first_soup, SEARCH_URL)
    seen_pager_args = set()

    soup, hidden = first_soup, first_hidden
    pager_targets = find_pager_targets(soup)

    while pager_targets:
        target, arg = pager_targets[0]
        if arg in seen_pager_args:
            break
        seen_pager_args.add(arg)

        payload = dict(hidden)
        payload["__EVENTTARGET"] = target
        payload["__EVENTARGUMENT"] = arg
        r = session.post(SEARCH_URL, data=payload, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        hidden = {
            inp.get("name"): inp.get("value", "")
            for inp in soup.find_all("input", type="hidden")
            if inp.get("name")
        }
        all_rows.extend(parse_results(soup, SEARCH_URL))
        pager_targets = [t for t in find_pager_targets(soup) if t[1] not in seen_pager_args]

    return all_rows


DOWNLOAD_KEYWORDS = ("download", "fdd", "pdf", "viewfile", "getfile", "docview", "attachment")


def find_pdf_link(session, detail_url):
    """
    Try a plain <a href="....pdf"> first (handles the simple case).
    If that's not present -- confirmed to be the case on this site's detail
    pages, which show 'File uploaded on <date>' with no visible link text
    in the fetched view -- fall back to hunting for an ASP.NET postback
    control (__doPostBack) whose target name suggests a download/view
    action, and test each candidate by actually POSTing to it and checking
    whether the response comes back as a PDF. This is a best-effort guess
    absent seeing the raw control markup; see debug_detail_page.html
    (dumped for the first detail page visited each run) for ground truth
    if this still misses.

    Returns (pdf_bytes_or_none, pdf_url_or_none). Exactly one of the two
    will be set on success: a direct href gives a URL to download
    separately; a postback gives the bytes directly, since the postback
    response *is* the file.
    """
    r = session.get(detail_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Case 1: a plain link to a .pdf (possibly with a query string)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = urllib.parse.urlsplit(href).path
        if path.lower().endswith(".pdf"):
            return None, urllib.parse.urljoin(detail_url, href)

    # Case 2: hunt for a postback-driven download control
    hidden = {
        inp.get("name"): inp.get("value", "")
        for inp in soup.find_all("input", type="hidden")
        if inp.get("name")
    }
    candidates = []
    for tag in soup.find_all(["a", "input", "button"]):
        for attr in ("href", "onclick"):
            val = tag.get(attr, "")
            m = POSTBACK_RE.search(val)
            if m:
                target, arg = m.group(1), m.group(2)
                if any(k in target.lower() for k in DOWNLOAD_KEYWORDS):
                    candidates.append((target, arg))

    for target, arg in candidates:
        payload = dict(hidden)
        payload["__EVENTTARGET"] = target
        payload["__EVENTARGUMENT"] = arg
        resp = session.post(detail_url, data=payload, headers=HEADERS, timeout=30)
        ctype = resp.headers.get("Content-Type", "").lower()
        if "pdf" in ctype:
            return resp.content, None

    return None, None


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

    # Preflight: this site has been observed to be intermittently
    # unreachable from specific GitHub Actions Azure regions (a WAF or
    # similar likely blocks some cloud IP ranges) while working fine from
    # others -- which region a given run lands on is outside our control.
    # Rather than burning the full per-request timeout across the whole
    # alphabet sweep only to find out at the end that every single one
    # failed the same way, fail fast on one check and say so plainly.
    try:
        session.get(SEARCH_URL, headers=HEADERS, timeout=15)
    except requests.exceptions.RequestException as e:
        print(
            "PREFLIGHT FAILED: could not reach "
            f"{SEARCH_URL} at all ({e}). This looks like the intermittent "
            "region-specific reachability issue, not a code problem -- "
            "the site was reachable from other Actions runs and is "
            "reachable from other infra. Just re-run the workflow; a new "
            "run gets a new runner IP and has a good chance of landing "
            "somewhere that isn't blocked.",
            file=sys.stderr,
        )
        sys.exit(1)

    all_results = {}  # keyed by detail_url to de-dupe
    dumped_sample = False

    for letter in ALPHABET:
        try:
            soup, hidden = do_search(session, letter)
        except Exception as e:
            print(f"[query={letter!r}] search failed: {e}", file=sys.stderr)
            continue

        if not dumped_sample:
            # Always leave one real results page on disk for inspection,
            # win or lose -- this is what let us fix the last two bugs
            # from a single run's logs instead of guessing again.
            with open("debug_results_page.html", "w", encoding="utf-8") as f:
                f.write(soup.prettify())
            dumped_sample = True

        results = fetch_all_pages(session, soup, hidden)
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
    dumped_detail_sample = False
    for i, (detail_url, rec) in enumerate(all_results.items(), 1):
        row = {
            "name": rec["name"],
            "detail_url": detail_url,
            "pdf_url": "",
            "local_path": "",
            "download_status": "not_attempted",
        }
        try:
            if not dumped_detail_sample:
                # Ground truth for the next debugging pass if this run's
                # postback-guessing still misses -- one real detail page,
                # raw HTML, regardless of outcome.
                dr = session.get(detail_url, headers=HEADERS, timeout=30)
                with open("debug_detail_page.html", "w", encoding="utf-8") as f:
                    f.write(BeautifulSoup(dr.text, "html.parser").prettify())
                dumped_detail_sample = True

            pdf_bytes, pdf_url = find_pdf_link(session, detail_url)
            row["pdf_url"] = pdf_url or ""
            fname = safe_filename(rec["name"]) + ".pdf"
            dest = os.path.join(OUT_DIR, fname)

            if pdf_bytes:
                with open(dest, "wb") as f:
                    f.write(pdf_bytes)
                row["local_path"] = dest
                row["download_status"] = "ok_via_postback"
            elif pdf_url:
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
