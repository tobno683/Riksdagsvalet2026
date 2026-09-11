#!/usr/bin/env python3
"""
update_polls.py

Fetches Sweden's latest Verian väljarbarometer (published monthly in
partnership with SVT) and writes each party's poll share to
data/polls.json.

Why Verian instead of PolitPro (switched September 2026):
  - PolitPro's "Election Trend" is a black-box weighted average across
    many institutes and, at this stage, was still folding in individual
    polls from as far back as May — diluting how the race actually looks
    in the final stretch before an election. Verian's väljarbarometer is
    a single, large (~3,000 respondents), well-established, methodologi-
    cally transparent survey with a fixed, known fieldwork window, so
    there's no old data quietly blended in.
  - It's published in partnership with SVT, Sweden's public broadcaster,
    and is one of the most frequently cited polls in Swedish political
    journalism.
  - The monthly report page includes a chart image whose ALT TEXT states
    all eight parties' numbers in one clean, consistently-formatted
    sentence - e.g. "Socialdemokraterna (29,2 %), Sverigedemokraterna
    (18,9 %), ..." - which is far more reliable to parse than scattered
    prose paragraphs (each party is discussed with different phrasing
    throughout the article body).

URL pattern: Verian publishes each month's report at a predictable URL,
https://www.veriangroup.com/sv/news-and-insights/valjarbarometer-{swedish
month name}-{year} - confirmed by checking both the September 2026 page
and its own footer link to the August 2026 report, which followed the
identical pattern. Verian does not publish in July ("en gång i månaden
utom i juli"), so this script falls back to June's URL if run in July.

Design goals (unchanged from the original PolitPro version):
  - NEVER overwrite good data with garbage. If the fetch or parsing fails,
    or the numbers look implausible, exit without touching data/
    polls.json, so the site keeps showing the last known-good snapshot.
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

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "polls.json")

SWEDISH_MONTHS = {
    1: "januari", 2: "februari", 3: "mars", 4: "april", 5: "maj", 6: "juni",
    7: "juli", 8: "augusti", 9: "september", 10: "oktober", 11: "november", 12: "december",
}

# Full party names as they appear in Verian's chart-image alt text, mapped
# to the abbreviations used throughout the rest of the site.
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def build_report_url(year: int, month: int) -> str:
    month_name = SWEDISH_MONTHS[month]
    return f"https://www.veriangroup.com/sv/news-and-insights/valjarbarometer-{month_name}-{year}"


def fetch_page_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_alt_text_poll(html: str):
    """
    Looks for the chart image's alt text, which states every party's
    number in one sentence, e.g.:
      "...väljarbarometer tillsammans med SVT, september 2026.
       Socialdemokraterna (29,2 %), Sverigedemokraterna (18,9 %),
       Moderaterna (16,4 %), Vänsterpartiet (8,1 %), Miljöpartiet (8,0 %),
       Centerpartiet (7,4 %), Kristdemokraterna (6,3 %) och Liberalerna
       (3,0 %)."
    Rather than anchor tightly to that exact prefix (which could shift
    slightly wording-wise), this just searches the whole page for each
    party's "Name (X,X %)" pattern independently - more robust to minor
    phrasing changes from one month's report to the next.
    """
    values = {}
    misses = []

    for pid, full_name in FULL_NAMES.items():
        pattern = re.compile(re.escape(full_name) + r"\s*\((\d+(?:,\d+)?)\s*%\)")
        match = pattern.search(html)
        if match:
            values[pid] = float(match.group(1).replace(",", "."))
        else:
            misses.append(pid)

    if misses:
        hints = []
        for pid in misses:
            name = FULL_NAMES[pid]
            idx = html.find(name)
            if idx == -1:
                hints.append(f"{pid} ({name}): name not found on page at all")
            else:
                snippet = html[idx: idx + 60]
                hints.append(f"{pid} ({name}): found name but no nearby '(X,X %)' pattern — context: {snippet!r}")
        raise ValueError(
            f"Could not find percentages for {len(misses)}/8 parties. " + " | ".join(hints)
        )

    total = sum(values.values())
    plausible = 85 <= total <= 105 and all(0 <= v <= 55 for v in values.values())
    if not plausible:
        raise ValueError(
            f"Parsed values failed the sanity check (sum={total:.1f}, expected roughly 85-105): {values}"
        )

    return values


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    year, month = now.year, now.month
    if month == 7:  # Verian doesn't publish in July - fall back to June's report
        month = 6

    url = build_report_url(year, month)

    try:
        html = fetch_page_text(url)
        values = parse_alt_text_poll(html)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        print(f"::warning::Poll scrape failed for {url}, leaving data/polls.json untouched: {exc}")
        sys.exit(0)

    month_label = f"{SWEDISH_MONTHS[month]} {year}"
    output = {
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "poll_date_label": f"Verian, {month_label}",
        "source": (
            f"Verians väljarbarometer för {month_label}, publicerad i samarbete med SVT. "
            "Cirka 3 000 intervjuer, riksrepresentativt urval vägt efter kön, ålder, "
            "utbildning och partival vid senaste valet. Källa: Verian/SVT."
        ),
        "source_url": url,
        "parties": values,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("Updated data/polls.json:")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
