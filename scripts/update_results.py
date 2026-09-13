#!/usr/bin/env python3
"""
update_results.py

Fetches Sweden's real, live riksdagsval 2026 results directly from
Valmyndigheten's official result files and writes them to
data/results.json in the exact shape the site's frontend expects.

THE OFFICIAL SOURCE AND FORMAT (verified 12 Sep 2026, the night before
the election, against Valmyndigheten's own technical documentation at
val.se/valresultat-och-statistik/statistik-och-data/teknisk-beskrivning-av-resultatfiler,
last updated by them 10 Sep 2026):

  - Results are published as ZIP files containing JSON, updated
    continuously through election night and into the following days.
  - Index of all available files (with checksums), fetched first:
      https://resultat.val.se/resultatfiler/val2026/index.md5
  - The file this script actually needs is the national riksdagsval
    "underordnad summering" (subordinate summary) - it contains vote
    counts broken down per KOMMUN (with a "lankod" field on each kommun
    entry), which lets this script aggregate up to both NATIONAL and
    REGIONAL (län) totals from a single, comparatively small file -
    rather than the much larger per-voting-district "röstfördelning"
    file (documented as up to ~250 MB uncompressed), which this site
    doesn't need for what it displays.
      zip:  Val_2026_preliminar_00_RD.zip
      json: Val_2026_preliminar_summering_00_RD.json  (the one file we
            actually parse out of that zip - the other two files bundled
            in the same zip, mandatfördelning and röstfördelning, are
            intentionally left unopened)
  - Field names confirmed against Valmyndigheten's own published schema
    doc for this exact file (prel-summering-rd-rf.md):
      root.test                    - bool. True = simulation/rehearsal
                                      data, not a real result. MUST be
                                      false before this script trusts
                                      the file at all.
      root.antalValdistriktRaknade / antalValdistriktSomSkaRaknas
                                    - counted / total districts, used
                                      for counted_pct.
      root.kommuner[]              - one entry per kommun:
        .kommunkod, .lankod        - codes (lankod -> LAN_NAMES below)
        .rostfordelning.rosterPaverkaMandat.partiRoster[]
              .partiforkortning    - party abbreviation, e.g. "S"
              .antalRoster         - raw vote COUNT for this kommun
                                      (NOT already a national/regional
                                      percentage - this script sums raw
                                      counts across kommuner itself
                                      before computing any percentage,
                                      since averaging pre-computed
                                      per-kommun percentages directly
                                      would be mathematically wrong)

SAFETY DESIGN (same philosophy as update_polls.py / update_news.py):
  - Never overwrite good data with garbage. Any failure - fetch error,
    unexpected JSON shape, test=true, zero districts counted yet - exits
    quietly without touching data/results.json, so the site keeps
    showing its last known-good snapshot rather than something broken.
  - Every failure prints a specific, diagnostic ::warning:: message, not
    a bare "didn't work" - this runs unattended and needs to be
    debuggable from the Action log alone, especially given this only
    gets ONE real shot at working correctly (there's no way to test it
    against genuine 2026 data before election night itself - Valmyndig-
    heten does not publish any 2026 files before then. It was checked
    as carefully as possible against their official documentation and
    schema instead).

INTENDED RUN PATTERN: unlike the monthly poll scraper, this needs tight,
frequent polling specifically during election night - tighter than
GitHub Actions' scheduled cron can reliably deliver on its own. This
script itself is single-shot (one fetch-parse-write cycle per run,
same pattern as update_polls.py / update_news.py) - the tight polling
and the git commit/push after each successful write both live in
update-results.yml's own bash loop, not in this script, to keep the
responsibilities cleanly separated and each piece independently
debuggable.
"""

import datetime
import io
import json
import os
import sys
import zipfile

import requests

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "results.json")

INDEX_URL = "https://resultat.val.se/resultatfiler/val2026/index.md5"
RESULTS_ROOT = "https://resultat.val.se/resultatfiler/val2026/"
ZIP_NAME = "Val_2026_preliminar_00_RD.zip"
JSON_NAME_IN_ZIP = "Val_2026_preliminar_summering_RD.json"
# Confirmed directly from a live production zip's actual contents on
# election night (13 Sep 2026) - this specific file omits the "_00_"
# valområdeskod segment that the OTHER two files in the same zip
# (rostfordelning, mandatfordelning) DO include, and that Valmyndighetens
# own documentation's worked example showed for this file too. Real
# behavior differs from the documented example for this one file - fixed
# after seeing the exact zip contents in the scraper's own error log
# rather than guessing.

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Sweden's 21 län, standard 2-digit administrative codes (SCB/Valmyndigheten
# convention - long-stable, not something that changes election to
# election) mapped to the exact "X län" name strings this site's frontend
# already uses as keys in data.regions.
LAN_NAMES = {
    "01": "Stockholms län", "03": "Uppsala län", "04": "Södermanlands län",
    "05": "Östergötlands län", "06": "Jönköpings län", "07": "Kronobergs län",
    "08": "Kalmar län", "09": "Gotlands län", "10": "Blekinge län",
    "12": "Skåne län", "13": "Hallands län", "14": "Västra Götalands län",
    "17": "Värmlands län", "18": "Örebro län", "19": "Västmanlands län",
    "20": "Dalarnas län", "21": "Gävleborgs län", "22": "Västernorrlands län",
    "23": "Jämtlands län", "24": "Västerbottens län", "25": "Norrbottens län",
}


def fetch_index() -> str:
    resp = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_zip_relative_path(index_text: str) -> str:
    """
    Parses index.md5 (lines of "<md5> ./relative/path/file.zip") to find
    the RD preliminär zip's actual current relative path, rather than
    assuming a hardcoded subdirectory - Valmyndigheten's own documented
    example shows these files live under a subdirectory like "./p/rd/",
    not at the bucket root, and this script would rather read that path
    from their own index each time than risk silently guessing wrong.
    """
    for line in index_text.splitlines():
        line = line.strip()
        if line.endswith(ZIP_NAME):
            parts = line.split(None, 1)
            if len(parts) == 2:
                return parts[1].lstrip("./")
    raise ValueError(f"{ZIP_NAME} not found in index.md5 - Valmyndigheten may not have published it yet.")


def fetch_summering_json(zip_relative_path: str) -> dict:
    zip_resp = requests.get(RESULTS_ROOT + zip_relative_path, headers=HEADERS, timeout=60)
    zip_resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        names = zf.namelist()
        matches = [n for n in names if n.endswith(JSON_NAME_IN_ZIP)]
        if not matches:
            raise ValueError(f"{JSON_NAME_IN_ZIP} not found inside zip. Zip contained: {names}")
        with zf.open(matches[0]) as f:
            return json.load(f)


def aggregate(data: dict):
    """
    Sums raw per-kommun vote COUNTS into national and per-län totals,
    then converts to percentages once, at the end - not by averaging
    already-computed per-kommun percentages, which would misweight
    small and large kommuner equally.
    """
    if data.get("test") is True:
        raise ValueError("File is marked test=true (simulation/rehearsal data) - not a real result yet.")

    counted = data.get("antalValdistriktRaknade")
    total_districts = data.get("antalValdistriktSomSkaRaknas")
    if not counted or counted == 0:
        raise ValueError(f"antalValdistriktRaknade is {counted} - no districts counted yet, nothing to show.")

    kommuner = data.get("kommuner")
    if not kommuner:
        raise ValueError("No 'kommuner' array in the parsed JSON - unexpected file shape.")

    national_counts = {}
    regional_counts = {}  # lan_name -> {party: count}

    for kommun in kommuner:
        if not kommun:
            continue  # defensive: skip any null entries in the kommuner array itself
        lankod = kommun.get("lankod")
        lan_name = LAN_NAMES.get(lankod)
        # Defensive at every level: real data from an in-progress count
        # apparently represents a not-yet-reported kommun as an explicit
        # "rostfordelning": null rather than omitting the key or giving an
        # empty object - dict.get(key, default) only falls back to
        # default when the KEY is missing, not when it's present with a
        # None value, so a plain chained .get().get() crashes on exactly
        # this real-world shape. Checking for None explicitly at each
        # step instead of trusting the default parameter avoids that.
        rostfordelning = kommun.get("rostfordelning") or {}
        roster_paverka_mandat = rostfordelning.get("rosterPaverkaMandat") or {}
        parti_roster = roster_paverka_mandat.get("partiRoster") or []
        for p in parti_roster:
            if not p:
                continue
            party = p.get("partiforkortning")
            count = p.get("antalRoster")
            if not party or count is None:
                continue
            national_counts[party] = national_counts.get(party, 0) + count
            if lan_name:
                regional_counts.setdefault(lan_name, {})
                regional_counts[lan_name][party] = regional_counts[lan_name].get(party, 0) + count

    def counts_to_pct(counts: dict) -> dict:
        total = sum(counts.values())
        if total == 0:
            return {}
        return {party: round(100 * c / total, 1) for party, c in counts.items()}

    national_pct = counts_to_pct(national_counts)
    regional_pct = {lan: counts_to_pct(counts) for lan, counts in regional_counts.items()}
    counted_pct = round(100 * counted / total_districts, 1) if total_districts else None

    return national_pct, regional_pct, counted_pct


def write_results(national: dict, regions: dict, counted_pct):
    output = {
        "status": "live",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "counted_pct": counted_pct,
        "national": national,
        "regions": regions,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return output


def run_once() -> bool:
    """Returns True if data/results.json was updated with fresh real data."""
    try:
        index_text = fetch_index()
        zip_path = find_zip_relative_path(index_text)
        data = fetch_summering_json(zip_path)
        national, regions, counted_pct = aggregate(data)
        write_results(national, regions, counted_pct)
        print(f"Updated data/results.json — {counted_pct}% of districts counted. National: {national}")
        return True
    except Exception as exc:  # noqa: BLE001 - see module docstring: never crash, never write garbage
        print(f"::warning::Results fetch/parse failed this cycle, leaving data/results.json untouched: {exc}")
        return False


def main():
    success = run_once()
    sys.exit(0 if success else 0)  # exit 0 either way — a quiet cycle isn't a workflow failure,
    # see module docstring: never overwrite good data with garbage.


if __name__ == "__main__":
    main()
