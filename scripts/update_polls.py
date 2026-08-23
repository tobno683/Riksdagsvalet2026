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


# Phrases that introduce a percentage which is NOT a poll result — e.g.
# PolitPro's Monte Carlo "chance of entering parliament" simulations for
# parties near the 4% threshold (relevant for Liberalerna specifically).
# A percentage is skipped as a false match if one of these phrases appears
# between the party's name and that percentage.
NON_POLL_PHRASES = [
    "simulations",
    "chance",
    "probability",
    "enter parliament",
    "margin of error",
    "PolitPro Score",
    "accuracy",
    "would win",
]

PERCENT_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")


def find_party_percentage(text, full_name, all_names):
    """Find the nearest percentage after `full_name` that isn't disqualified
    by crossing into another party's name or a known non-poll phrase in
    between. Unlike a single greedy/lazy regex, this evaluates every
    candidate percentage independently, so a disqualified one (e.g. a
    "chance of entering parliament" stat) doesn't block reaching a valid
    one further along — it's simply skipped in favour of the next."""
    others = [n for n in all_names if n != full_name]

    for name_match in re.finditer(re.escape(full_name), text):
        search_start = name_match.end()
        window_text = text[search_start: search_start + 200]

        for pct_match in PERCENT_RE.finditer(window_text):
            between = window_text[: pct_match.start()]  # full prefix, for the cross-party check
            local = window_text[max(0, pct_match.start() - 40): pct_match.start()]  # just-before-this-number, for the phrase check
            if any(other in between for other in others):
                continue  # crossed into another party's mention — disqualified
            if any(phrase.lower() in local.lower() for phrase in NON_POLL_PHRASES):
                continue  # a non-poll stat immediately before this number — disqualified
            value = float(pct_match.group(1).replace(",", "."))
            context = full_name + between + pct_match.group(0)
            return value, context

    return None, None


def parse_latest_poll(text: str):
    values = {}
    misses = []
    matched_context = {}  # pid -> the text snippet that produced the value, for diagnostics
    all_names = list(FULL_NAMES.values())

    for pid, full_name in FULL_NAMES.items():
        value, context = find_party_percentage(text, full_name, all_names)
        if value is not None:
            values[pid] = value
            matched_context[pid] = context
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
        # Include what was actually matched for each party, so a bad number
        # (like a stray unrelated percentage near a party's name) is
        # immediately visible in the log instead of just the final total.
        context_dump = " | ".join(f"{pid}: {matched_context[pid]!r}" for pid in values)
        raise ValueError(
            f"Parsed values failed the sanity check (sum={total:.1f}, "
            f"expected roughly 85-105): {values}. Matched text per party: {context_dump}"
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
