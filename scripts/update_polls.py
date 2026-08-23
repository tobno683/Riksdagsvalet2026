#!/usr/bin/env python3
"""
update_polls.py

Fetches the latest Swedish opinion-poll averages for the 2026 Riksdag
election from PolitPro (politpro.eu) and writes them to data/polls.json.

Why PolitPro, after two failed attempts elsewhere:
  - Wikipedia's polling article only tabulates the OPPOSITION-vs-TIDÖ bloc
    totals as text; individual party percentages aren't present as
    machine-readable text on that page (likely only as an inline chart
    image), so no text-based parser can extract them from there.
  - PolitPro's Sweden page (https://politpro.eu/en/sweden) instead presents
    its aggregated "Election Trend" as a plain sentence — e.g. "...shows
    Socialdemokraterna leading with 30.5%, followed by Sverigedemokraterna
    with 19.3%, ..." — which is far more robust to scrape than a table,
    since we just search for each party's full name followed by a nearby
    percentage, with no dependency on column order, table markup, or
    whether a cell uses an image instead of text.

PolitPro's terms require clear attribution ("Source: PolitPro.eu") for any
public display of their data — this is reflected in the "source" field
written to polls.json, which the site displays to visitors.

Design goals (unchanged from before):
  - NEVER overwrite good data with garbage. If parsing fails or the numbers
    look implausible, exit without touching data/polls.json, so the site
    keeps showing the last known-good snapshot.
  - Fail with a diagnostic message, not a bare "didn't work", so a future
    failure is debuggable from the Action log alone.

Run manually with:  python scripts/update_polls.py
"""

import datetime
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://politpro.eu/en/sweden"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "polls.json")

# Full party names as they appear in PolitPro's English-language prose,
# mapped to the abbreviations used throughout the rest of the site.
FULL_NAMES = {
    "S": "Socialdemokraterna",
    "M": "Moderaterna",
    "SD": "Sverigedemokraterna",
    "V": "Vänsterpartiet",
    "C": "Centerpartiet",
    "KD": "Kristdemokraterna",
    "L": "Liberalerna",
    "MP": "Miljöpartiet",
}

HEADERS = {
    "User-Agent": "RiksdagenPartiguide-PollUpdater/1.0 (educational hobby project)"
}


def fetch_page_text() -> str:
    """Fetch the Sweden page and return its visible text as one flat string."""
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Collapse to plain text with single spaces so "Name\n30.5%" and
    # "Name 30.5%" both match the same regex pattern below.
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse_latest_poll(text: str):
    values = {}
    misses = []
    all_names = list(FULL_NAMES.values())

    for pid, full_name in FULL_NAMES.items():
        # Match "<Full party name> ... 30.5%", but refuse to let the gap
        # between the name and the percentage cross into any OTHER party's
        # name — otherwise a party mentioned with no percentage nearby
        # ("Miljöpartiet is trending, Vänsterpartiet with 7.4%") could
        # silently steal the next party's number instead of failing loudly.
        others = [n for n in all_names if n != full_name]
        exclude = "|".join(re.escape(n) for n in others)
        gap = f"(?:(?!{exclude}).){{0,35}}?" if others else ".{0,35}?"
        pattern = re.escape(full_name) + gap + r"(\d{1,2}(?:[.,]\d+)?)\s*%"
        match = re.search(pattern, text)
        if match:
            values[pid] = float(match.group(1).replace(",", "."))
        else:
            misses.append(pid)

    if misses:
        # Show a short snippet of text around wherever the party's bare
        # name *does* appear (if it appears at all), to make a future
        # failure diagnosable without needing to re-fetch the page by hand.
        hints = []
        for pid in misses:
            name = FULL_NAMES[pid]
            idx = text.find(name)
            if idx == -1:
                hints.append(f"{pid} ({name}): name not found on page at all")
            else:
                snippet = text[idx: idx + 60]
                hints.append(f"{pid} ({name}): found name but no nearby %% — context: {snippet!r}")
        raise ValueError(
            f"Could not find percentages for {len(misses)}/8 parties. " + " | ".join(hints)
        )

    total = sum(values.values())
    plausible = 85 <= total <= 105 and all(0 <= v <= 55 for v in values.values())
    if not plausible:
        raise ValueError(
            f"Parsed values failed the sanity check (sum={total:.1f}, "
            f"expected roughly 85-105): {values}"
        )

    return values


def main():
    try:
        text = fetch_page_text()
        values = parse_latest_poll(text)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        print(f"::warning::Poll scrape failed, leaving data/polls.json untouched: {exc}")
        sys.exit(0)

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "poll_date_label": "PolitPro Election Trend (löpande uppdaterad)",
        "source": "PolitPro.eu — sammanvägning av svenska opinionsinstitut (källa krävs vid publik visning: Source: PolitPro.eu)",
        "source_url": SOURCE_URL,
        "parties": values,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("Updated data/polls.json:")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
