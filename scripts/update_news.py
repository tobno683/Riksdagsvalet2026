#!/usr/bin/env python3
"""
update_news.py

Fetches SVT Nyheter's official "Inrikes" (domestic news) RSS feed and
writes a filtered, politics/election-focused subset to data/news.json.

Why RSS instead of a social-media embed:
  - This is a genuinely stable, official, standard format that SVT publishes
    on purpose for exactly this kind of use — unlike embedding a social
    media widget (which this site tried and removed twice, because the
    widget depends on an unofficial backend that isn't designed for public
    traffic and can rate-limit at any time). RSS has no such fragility.
  - It's free and requires no API key, unlike X's official API.

The raw feed mixes politics with general news (weather, crime, sports,
health). We filter for politics/election relevance using keyword matching
against party names, politician names, and election-specific vocabulary,
rather than showing the unfiltered firehose.

Design goals (same as update_polls.py):
  - NEVER overwrite good data with garbage. If the fetch fails or yields
    zero matching items, exit without touching data/news.json, so the site
    keeps showing the last known-good snapshot.
  - Fail with a diagnostic message, not a bare "didn't work".

Run manually with:  python scripts/update_news.py
"""

import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import requests

SOURCE_URL = "https://www.svt.se/nyheter/inrikes/rss.xml"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "news.json")
MAX_ITEMS = 12

HEADERS = {
    "User-Agent": "RiksdagenPartiguide-NewsUpdater/1.0 (educational hobby project)"
}

# Keywords that mark an item as politics/election-relevant. Deliberately
# broad (party names, politician names, and election vocabulary) so we
# don't under-filter and miss genuinely relevant coverage, but specific
# enough to exclude the unrelated general-news items (weather, crime,
# health) that also appear in SVT's Inrikes feed.
KEYWORDS = [
    # Parties (full names and common short forms as they appear in prose)
    "socialdemokrat", "moderater", "sverigedemokrat", "vänsterpart",
    "centerpart", "kristdemokrat", "liberalern", "miljöpart",
    "tidöpart", "tidöavtal", "tidöregeringen", "tidösamarbet",
    "rödgrön",
    # Party leaders / prominent politicians (current as of 2026 campaign)
    "kristersson", "andersson", "åkesson", "busch", "dadgostar",
    "thand ringqvist", "mohamsson", "lind", "jomshof", "svantesson",
    "strömmer", "damberg",
    # Election / government-formation vocabulary
    "valet", "riksdagsval", "opinionsmätning", "opinionsläge",
    "väljarbarometer", "regeringsbildning", "statsminister",
    "regeringsunderlag", "mandatperiod", "partiledarutfrågning",
    "partiledardebatt", "valkompass", "förtidsröst", "röstkort",
    "valmyndigheten", "sakfråg", "väljarnas", "partiernas politik",
]

NS = {"": ""}  # SVT's feed has no namespaces beyond default RSS


def fetch_feed_items():
    resp = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed has no <channel> element — unexpected format")
    return channel.findall("item")


def is_politics_relevant(title: str, description: str) -> bool:
    haystack = f"{title} {description}".lower()
    return any(kw in haystack for kw in KEYWORDS)


def parse_pubdate(raw: str):
    # RSS pubDate format: "Thu, 27 Aug 2026 18:09:13 +0200"
    try:
        dt = datetime.datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def main():
    try:
        items = fetch_feed_items()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        print(f"::warning::News fetch failed, leaving data/news.json untouched: {exc}")
        sys.exit(0)

    filtered = []
    for item in items:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pubdate_el = item.find("pubDate")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        if not title or not link:
            continue
        # Skip video-only items (svtplay.se links) — they have no article
        # page to link to in the same way, and mostly lack a description.
        if "svtplay.se" in link:
            continue
        if not is_politics_relevant(title, description):
            continue

        pubdate_raw = pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else ""
        filtered.append({
            "title": title,
            "link": link,
            "description": description,
            "pubDate": parse_pubdate(pubdate_raw),
        })

        if len(filtered) >= MAX_ITEMS:
            break

    if not filtered:
        print("::warning::News fetch succeeded but found zero politics-relevant items — leaving data/news.json untouched (possible keyword-filter issue).")
        sys.exit(0)

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "SVT Nyheter — Inrikes (RSS)",
        "source_url": "https://www.svt.se/nyheter/inrikes/",
        "items": filtered,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Updated data/news.json with {len(filtered)} items:")
    for it in filtered:
        print(f"  - {it['title']}")


if __name__ == "__main__":
    main()
