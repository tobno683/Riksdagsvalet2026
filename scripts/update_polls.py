#!/usr/bin/env python3
"""
update_polls.py

Fetches the latest Swedish opinion-poll averages for the 2026 Riksdag
election from Wikipedia and writes them to data/polls.json.

Why Wikipedia: the article "Opinionsundersokningar infor riksdagsvalet i
Sverige 2026" is maintained by editors who transcribe numbers from Novus,
Demoskop, Verian, SCB, Ipsos etc. into one table. It's not an official API,
but it's a single stable, well-structured, scrape-friendly page rather than
guessing which of a dozen newspaper sites to parse.

Design goals:
  - NEVER overwrite good data with garbage. If parsing fails or the numbers
    look implausible, we exit without touching data/polls.json, so the site
    keeps showing the last known-good snapshot.
  - Be defensive about markup: don't assume exact HTML structure, instead
    search all tables on the page for the one whose header row best matches
    the 8 Riksdag party abbreviations, and sanity-check the resulting row
    (percentages should roughly sum to ~100 and be within a plausible range)
    before accepting it.

Run manually with:  python scripts/update_polls.py
"""

import datetime
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

WIKI_API = "https://sv.wikipedia.org/w/api.php"
PAGE_TITLE = "Opinionsundersökningar inför riksdagsvalet i Sverige 2026"
PARTY_IDS = ["S", "M", "SD", "V", "C", "KD", "L", "MP"]
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "polls.json")

HEADERS = {
    # Wikipedia asks bots/scripts to identify themselves. Replace the email
    # with your own contact info if you want to be a good citizen.
    "User-Agent": "RiksdagenPartiguide-PollUpdater/1.0 (educational hobby project)"
}


def fetch_page_html() -> str:
    """Fetch the rendered HTML of the Wikipedia article via the official API."""
    params = {
        "action": "parse",
        "page": PAGE_TITLE,
        "prop": "text",
        "format": "json",
        "formatversion": "2",
    }
    resp = requests.get(WIKI_API, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "parse" not in data or "text" not in data["parse"]:
        raise ValueError(f"Unexpected API response shape: {list(data.keys())}")
    return data["parse"]["text"]


def cell_label(cell):
    """Extract a usable text label from a table cell. Wikipedia poll tables
    sometimes use a small party logo image instead of plain text in header
    cells, in which case the visible text is empty — fall back to the
    image's alt text, then a link's title attribute."""
    text = cell.get_text(strip=True)
    if text:
        # Strip footnote markers like "S[1]" or "S[a]" that would otherwise
        # break an exact match against the plain party abbreviation.
        return re.sub(r"\[.*?\]", "", text).strip()
    img = cell.find("img")
    if img and img.get("alt"):
        return img["alt"].strip()
    link = cell.find("a")
    if link and link.get("title"):
        return link["title"].strip()
    return ""


def find_best_table(soup: BeautifulSoup):
    """Find the wikitable whose header row best matches the party abbreviations."""
    tables = soup.find_all("table", class_="wikitable")
    best_table, best_score, best_header_cells = None, 0, []
    diagnostics = []

    for table in tables:
        first_row = table.find("tr")
        if not first_row:
            continue
        header_cells = [cell_label(c) for c in first_row.find_all(["th", "td"])]
        score = sum(1 for pid in PARTY_IDS if pid in header_cells)
        diagnostics.append((score, header_cells[:12]))  # keep first 12 cells for logging
        if score > best_score:
            best_table, best_score, best_header_cells = table, score, header_cells

    if best_table is None or best_score < 5:
        # Print what we actually saw, so a failed run's log tells us why,
        # instead of just "0/8" with no further clue.
        diagnostics.sort(key=lambda d: -d[0])
        preview = "; ".join(f"{s}/8 -> {cells}" for s, cells in diagnostics[:5])
        raise ValueError(
            f"Could not find a polling table with enough recognisable party "
            f"columns (best match found {best_score}/8 parties). "
            f"Top candidate header rows seen: {preview}"
        )
    return best_table, best_header_cells


def clean_number(text: str):
    """Turn '30,2 %' or '30.2' etc. into 30.2, or return None if not parseable."""
    text = text.replace(",", ".")
    match = re.search(r"-?\d+(\.\d+)?", text)
    return float(match.group()) if match else None


def parse_latest_poll(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table, header_cells = find_best_table(soup)

    col_index = {}
    for i, cell in enumerate(header_cells):
        for pid in PARTY_IDS:
            if cell == pid and pid not in col_index:
                col_index[pid] = i

    rows = table.find_all("tr")[1:]  # skip header row

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) <= max(col_index.values(), default=-1):
            continue

        values = {}
        row_ok = True
        for pid, idx in col_index.items():
            num = clean_number(cell_label(cells[idx]) or cells[idx].get_text(strip=True))
            if num is None:
                row_ok = False
                break
            values[pid] = num

        if not row_ok:
            continue  # try the next row — this one had missing/unparsable cells

        total = sum(values.values())
        plausible = 85 <= total <= 105 and all(0 <= v <= 55 for v in values.values())
        if not plausible:
            continue  # sanity check failed, keep looking

        date_label = cells[0].get_text(strip=True) if cells else "okänt datum"
        return values, date_label

    raise ValueError("No row in the table passed the sanity checks (sum ~100%, each 0-55%).")


def main():
    try:
        html = fetch_page_html()
        values, date_label = parse_latest_poll(html)
    except Exception as exc:  # noqa: BLE001 - we deliberately want to catch everything
        # Fail loudly in the Action log, but exit 0 so the workflow doesn't
        # mark itself red every time Wikipedia tweaks its table formatting.
        # The site keeps showing the last good data/polls.json until someone
        # fixes the parser.
        print(f"::warning::Poll scrape failed, leaving data/polls.json untouched: {exc}")
        sys.exit(0)

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "poll_date_label": date_label,
        "source": "Wikipedia (sv): sammanvägning av Demoskop, Novus, Verian, SCB, Ipsos m.fl. via pollofpolls.se",
        "source_url": "https://sv.wikipedia.org/wiki/" + PAGE_TITLE.replace(" ", "_"),
        "parties": values,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("Updated data/polls.json:")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
