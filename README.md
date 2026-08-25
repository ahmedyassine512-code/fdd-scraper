# Wisconsin DFI FDD scraper

## Why this runs on GitHub Actions and not your machine

`apps.dfi.wi.gov` is unreachable at the TCP level from your network (tested:
DNS resolves fine, but the TLS handshake on port 443 times out — while
`dfi.wi.gov`, the parent domain, connects fine). It's reachable from
US-hosted infrastructure. GitHub Actions runners are US/EU-hosted with
normal outbound access, and the free tier covers this easily — this job
runs in minutes, not hours.

## Setup (5 minutes, zero dollars)

1. Create a new **public** GitHub repo (free tier gives public repos
   unlimited Actions minutes; private repos get 2,000 free minutes/month,
   also plenty).
2. Upload these four files, keeping the folder structure:
   - `scrape_wi_fdd.py`
   - `extract_item20_insurance.py`
   - `requirements.txt`
   - `.github/workflows/scrape.yml`
3. Go to the repo's **Actions** tab. You should see "Scrape Wisconsin DFI
   FDDs" listed. Click it, then click **Run workflow**.
4. Wait for the run to finish (check the log — this is the part that
   needed a live test I couldn't do myself; see "If it breaks" below).
5. Open the finished run, scroll to **Artifacts**, download
   `wi-fdd-results.zip`. Inside: `list.csv` (every registration found +
   download status), `fdds/` (the actual PDFs), `raw_extract.csv` (Item 20
   + insurance-section raw text per PDF).

## What you get

- `list.csv`: name, detail page URL, PDF URL, local path, download status
  per franchise registration found via a–z/0–9 name search.
- `fdds/*.pdf`: the actual FDD PDFs.
- `raw_extract.csv`: raw (unedited) Item 20 text and any paragraph
  mentioning insurance/liability/additional-insured/endorsement, per
  document. Deliberately not parsed into structured fields (limits,
  yes/no flags) — that step needs real reading, not regex, or you get
  confident wrong answers. Hand `raw_extract.csv` back to me and I'll do
  that pass directly against the actual text.

## If it breaks

Most likely failure point: the search form's field names don't match what
`guess_search_field`/`guess_submit_control` expect. If that happens, the
script writes `debug_search_page.html` into the artifact — download it,
and I can fix the field-name discovery against the real page in one pass
instead of guessing blind.

Second most likely: the site returns 0 results for single-character
queries (some "contains" search boxes require 2+ characters). If `list.csv`
comes back empty or tiny, tell me and I'll switch the enumeration strategy
to two-letter pairs (aa–zz), which is slower but far more exhaustive.
