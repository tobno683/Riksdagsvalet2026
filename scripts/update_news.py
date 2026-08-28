#!/usr/bin/env python3
"""
update_news.py

Fetches Swedish politics/election news from multiple official news-outlet
feeds and writes a merged, filtered, chronologically-sorted list to
data/news.json.

Sources:
  - SVT Nyheter Inrikes (RSS 2.0) - public service, politically neutral
  - Sveriges Radio Ekot (Atom)    - public service, politically neutral
  - Aftonbladet (RSS 2.0)         - independent, generally left-of-centre
  - Svenska Dagbladet (RSS 2.0)   - independent, generally right-of-centre
  Blogs (opinion/commentary, not news reporting) - all six verified as
  genuinely widely-known rather than small/niche circles (concrete
  follower counts, external press coverage, or comparable-institution
  validation - see the comment on each entry below for specifics):
    - Ledarsidorna  - nationalist-adjacent right-wing commentary
    - Jens Ganman   - right-leaning satirist/columnist
    - Kvartal       - right-of-centre intellectual magazine, hundreds of
                       thousands of monthly readers/listeners
    - Dagens Arena  - independent, investigative, progressive
    - Svensson      - one of Sweden's most-read individual political blogs
    - Parabol       - left-intellectual magazine, comparable to Kvartal/Axess
  Deliberately spans the spectrum: SVT/SR are legally required to stay
  neutral, but Aftonbladet and SvD are independent papers with genuinely
  different editorial leanings, so mixing all four gives a more balanced
  picture than public-service coverage alone.
  Two different feed *formats* (RSS 2.0 vs. Atom) are in play, so each
  format has its own parser below rather than assuming they share one
  XML shape.

Why RSS/Atom instead of a social-media embed: this is a genuinely stable,
official, standard format each outlet publishes on purpose for exactly
this kind of use — unlike embedding a social media widget (which this site
tried and removed twice, because the widget depends on an unofficial
backend that isn't designed for public traffic and can rate-limit at any
time). RSS/Atom has no such fragility, and needs no API key or payment,
unlike X's official API.

Each raw feed mixes politics with general news (weather, crime, sports,
health, foreign royals, etc). We filter for politics/election relevance
using keyword matching against party names, politician names, and
election-specific vocabulary, rather than showing the unfiltered firehose.

Design goals (same as update_polls.py):
  - NEVER overwrite good data with garbage. If every source fails, exit
    without touching data/news.json, so the site keeps showing the last
    known-good snapshot. If only SOME sources fail, still publish whatever
    succeeded - one outlet's feed hiccuping shouldn't blank out the others.
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

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "news.json")
MAX_ITEMS = 15

HEADERS = {
    "User-Agent": "RiksdagenPartiguide-NewsUpdater/1.0 (educational hobby project)"
}

# Keywords that mark an item as politics/election-relevant. Deliberately
# broad (party names, politician names, and election vocabulary) so we
# don't under-filter and miss genuinely relevant coverage, but specific
# enough to exclude the unrelated general-news items (weather, crime,
# health, foreign royals) that also appear in these feeds.
KEYWORDS = [
    "socialdemokrat", "moderater", "sverigedemokrat", "vänsterpart",
    "centerpart", "kristdemokrat", "liberalern", "miljöpart",
    "tidöpart", "tidöavtal", "tidöregeringen", "tidösamarbet", "rödgrön",
    "kristersson", "andersson", "åkesson", "busch", "dadgostar",
    "thand ringqvist", "mohamsson", "lind", "jomshof", "svantesson",
    "strömmer", "damberg",
    "valet", "riksdagsval", "opinionsmätning", "opinionsläge",
    "väljarbarometer", "regeringsbildning", "statsminister",
    "regeringsunderlag", "mandatperiod", "partiledarutfrågning",
    "partiledardebatt", "valkompass", "förtidsröst", "röstkort",
    "valmyndigheten", "sakfråg", "väljarnas", "partiernas politik",
]

# Party mentions (including each party's known leader, so an article about
# "Kristersson" is tagged "M" even if it never spells out "Moderaterna")
# map to a short topic chip; general election themes map to a readable
# Swedish label. An item can carry multiple topics.
PARTY_TOPIC_KEYWORDS = {
    "S": ["socialdemokrat", "andersson", "damberg"],
    "M": ["moderater", "kristersson", "svantesson", "strömmer"],
    "SD": ["sverigedemokrat", "åkesson", "jomshof"],
    "V": ["vänsterpart", "dadgostar"],
    "C": ["centerpart", "thand ringqvist"],
    "KD": ["kristdemokrat", "busch"],
    "L": ["liberalern", "mohamsson"],
    "MP": ["miljöpart", "lind"],
}
THEME_TOPIC_KEYWORDS = {
    "Opinion": ["opinionsmätning", "opinionsläge", "väljarbarometer"],
    "Regeringsbildning": ["regeringsbildning", "statsminister", "regeringsunderlag"],
    "Debatt": ["partiledardebatt", "partiledarutfrågning"],
}


def detect_topics(title: str, description: str) -> list:
    haystack = f"{title} {description}".lower()
    topics = []
    for topic, kws in {**PARTY_TOPIC_KEYWORDS, **THEME_TOPIC_KEYWORDS}.items():
        if any(kw in haystack for kw in kws):
            topics.append(topic)
    return topics


def favicon_url(domain: str) -> str:
    # Google's favicon service - used instead of guessing each outlet's own
    # favicon path directly, since this is a stable, widely-used pattern
    # that doesn't depend on knowing (and maintaining) each site's exact
    # icon URL.
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


SOURCES = [
    {
        "name": "SVT Nyheter",
        "domain": "svt.se",
        "feed_url": "https://www.svt.se/nyheter/inrikes/rss.xml",
        "format": "rss",
        "type": "news",
    },
    {
        "name": "Sveriges Radio",
        "domain": "sverigesradio.se",
        "feed_url": "https://api.sr.se/api/rss/program/83",
        "format": "atom",
        "type": "news",
    },
    {
        # Independent, generally seen as left-of-centre (Sweden's largest
        # daily paper; historically part-owned by the Swedish Trade Union
        # Confederation).
        "name": "Aftonbladet",
        "domain": "aftonbladet.se",
        "feed_url": "https://rss.aftonbladet.se/rss2/small/pages/sections/senastenytt",
        "format": "rss",
        "type": "news",
    },
    {
        # Independent, generally seen as moderate/right-of-centre.
        "name": "Svenska Dagbladet",
        "domain": "svd.se",
        "feed_url": "https://www.svd.se/feed/articles.rss",
        "format": "rss",
        "type": "news",
    },
    {
        # Independent liberal - Sweden's paper of record. Confirmed
        # genuinely live via a third-party RSS reader showing real current
        # articles, giving higher confidence than the /feed/ URL guesses
        # used for the blog sources below.
        "name": "Dagens Nyheter",
        "domain": "dn.se",
        "feed_url": "https://www.dn.se/rss/",
        "format": "rss",
        "type": "news",
    },
    {
        # Liberal-conservative tabloid, one of Sweden's largest.
        "name": "Expressen",
        "domain": "expressen.se",
        "feed_url": "https://feeds.expressen.se/nyheter",
        "format": "rss",
        "type": "news",
    },
    {
        # Right-leaning / nationalist-adjacent commentary blog with real
        # reach and real controversy (media researcher Kristoffer Holt has
        # described it as immigration-critical alternative media). Feed
        # URL is the standard WordPress /feed/ convention, inferred from
        # the site's platform rather than directly confirmed - if wrong,
        # this source simply fails independently without affecting others.
        "name": "Ledarsidorna",
        "domain": "ledarsidorna.se",
        "feed_url": "https://ledarsidorna.se/feed/",
        "format": "rss",
        "type": "blog",
    },
    {
        # Left-leaning commentary/opinion outlet, self-described as
        # "obunden, granskande och progressiv" (independent, investigative,
        # progressive), running since 2010, linked to Arenagruppen. Same
        # caveat on the feed URL as Ledarsidorna above.
        "name": "Dagens Arena",
        "domain": "dagensarena.se",
        "feed_url": "https://www.dagensarena.se/feed/",
        "format": "rss",
        "type": "blog",
    },
    {
        # Long-running, well-known left-leaning individual political blog
        # (author: Anders S.) - self-described and externally referenced
        # (including by Expressen's own influential-blogger coverage) as
        # one of Sweden's most-read political blogs. Same feed-URL caveat
        # as the other two blogs.
        "name": "Svensson",
        "domain": "blog.zaramis.se",
        "feed_url": "https://blog.zaramis.se/feed/",
        "format": "rss",
        "type": "blog",
    },
    {
        # Right-leaning satirist/columnist. Verified well-known, not niche:
        # ranked in Favikon's "Top 20 X Influencers in Sweden 2026" list,
        # own Substack states 14K+ subscribers directly, profiled by Fokus
        # magazine. RSS URL uses Substack's own officially documented
        # convention (support.substack.com), so - unlike several other
        # blog entries here - this one is confirmed, not inferred.
        "name": "Jens Ganman",
        "domain": "jensganman.substack.com",
        "feed_url": "https://jensganman.substack.com/feed",
        "format": "rss",
        "type": "blog",
    },
    {
        # Left-intellectual magazine co-founded 2023 by journalist/author
        # Kajsa Ekis Ekman (not her own small personal Substack, which has
        # only ~hundreds of subscribers - this is her actual current
        # primary outlet). Verified well-known: Aftonbladet's own coverage
        # explicitly frames it as the left's counterpart to the
        # established magazines Kvartal and Axess, and its contributor
        # roster includes notable figures like sociologist Göran Therborn
        # and musician Mikael Wiehe. Feed URL is an educated guess based on
        # the .press domain (commonly a Ghost-platform publication, whose
        # default RSS path is /rss/) rather than directly confirmed - if
        # wrong, this source simply fails independently without affecting
        # others.
        "name": "Parabol",
        "domain": "parabol.press",
        "feed_url": "https://www.parabol.press/rss/",
        "format": "rss",
        "type": "blog",
    },
]

PODCAST_SOURCES = [
    {
        # Right-of-centre (self-described independent, but per Wikipedia
        # "repeatedly labeled a right-wing publication by commentators on
        # the left" - the same contested-positioning pattern as
        # Ledarsidorna) intellectual media house founded 2016. Verified
        # genuinely widely-known, not niche: its own channel states
        # hundreds of thousands of readers/listeners monthly - an order of
        # magnitude above every blog entry above - and it was
        # independently used by Aftonbladet as the reference point for
        # describing Parabol's significance (see above), not something
        # Kvartal claims about itself. This is their CONFIRMED podcast
        # feed (verified live via direct fetch, current episodes include
        # a same-day interview with Finance Minister Svantesson) - not the
        # earlier guessed text-article URL, which has been dropped now
        # that this real feed is in use instead.
        "name": "Kvartal",
        "domain": "kvartal.se",
        "feed_url": "https://feed.pod.space/kvartal",
        "format": "rss",
        "type": "podcast",
    },
    {
        # Sveriges Radio's flagship daily current-affairs/politics
        # program - public service, politically neutral, the same
        # standing as SR's Ekot in the news list above. Feed URL
        # confirmed via two independent sources (a podcast directory
        # listing and a direct fetch of the feed itself, showing real
        # current episodes).
        "name": "Studio Ett",
        "domain": "sverigesradio.se",
        "feed_url": "https://api.sr.se/api/rss/pod/4021",
        "format": "rss",
        "type": "podcast",
    },
]


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def is_politics_relevant(title: str, description: str) -> bool:
    haystack = f"{title} {description}".lower()
    return any(kw in haystack for kw in KEYWORDS)


def parse_rss_pubdate(raw: str):
    # RSS 2.0 format: "Thu, 27 Aug 2026 18:09:13 +0200"
    try:
        dt = datetime.datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def parse_atom_pubdate(raw: str):
    # Atom format: "2026-08-27T16:02:00+02:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def fetch_rss_items(source):
    resp = requests.get(source["feed_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed has no <channel> element - unexpected format")

    results = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pubdate_el = item.find("pubDate")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        description = strip_html(desc_el.text) if desc_el is not None and desc_el.text else ""
        pubdate_raw = pubdate_el.text.strip() if pubdate_el is not None and pubdate_el.text else ""

        if not title or not link or "svtplay.se" in link:
            continue  # skip video-only items - no article page, mostly no description
        results.append({
            "title": title, "link": link, "description": description,
            "pubDate": parse_rss_pubdate(pubdate_raw),
        })
    return results


def fetch_atom_items(source):
    resp = requests.get(source["feed_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    results = []
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        link_el = entry.find("atom:link", ns)
        summary_el = entry.find("atom:summary", ns)
        published_el = entry.find("atom:published", ns)

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.get("href", "").strip() if link_el is not None else ""
        description = strip_html(summary_el.text) if summary_el is not None and summary_el.text else ""
        published_raw = published_el.text.strip() if published_el is not None and published_el.text else ""

        if not title or not link:
            continue
        results.append({
            "title": title, "link": link, "description": description,
            "pubDate": parse_atom_pubdate(published_raw),
        })
    return results


def main():
    all_items = []
    failures = []
    all_sources = SOURCES + PODCAST_SOURCES

    for source in all_sources:
        try:
            fetcher = fetch_rss_items if source["format"] == "rss" else fetch_atom_items
            raw_items = fetcher(source)
        except Exception as exc:  # noqa: BLE001 - one source failing shouldn't block the others
            failures.append(f"{source['name']}: {exc}")
            continue

        for it in raw_items:
            if not is_politics_relevant(it["title"], it["description"]):
                continue
            it["source"] = source["name"]
            it["sourceIcon"] = favicon_url(source["domain"])
            it["sourceType"] = source["type"]
            it["topics"] = detect_topics(it["title"], it["description"])
            all_items.append(it)

    if failures:
        print("::warning::Some news sources failed: " + " | ".join(failures))

    if not all_items:
        print("::warning::News fetch found zero usable items across all sources - leaving data/news.json untouched.")
        sys.exit(0)

    # Sort newest-first; items with no parseable date sink to the bottom
    # rather than crashing the sort. News, blogs, and podcasts are split
    # into separate, independently-capped lists so one type can't crowd
    # out the others on the frontend's three tabs.
    all_items.sort(key=lambda it: it["pubDate"] or "", reverse=True)
    news_items = [it for it in all_items if it["sourceType"] == "news"][:MAX_ITEMS]
    blog_items = [it for it in all_items if it["sourceType"] == "blog"][:MAX_ITEMS]
    podcast_items = [it for it in all_items if it["sourceType"] == "podcast"][:MAX_ITEMS]

    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": [{"name": s["name"], "url": s["feed_url"], "type": s["type"]} for s in all_sources],
        "news": news_items,
        "blogs": blog_items,
        "podcasts": podcast_items,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Updated data/news.json with {len(news_items)} news, {len(blog_items)} blog, {len(podcast_items)} podcast items:")
    for it in news_items + blog_items + podcast_items:
        print(f"  - [{it['sourceType']}/{it['source']}] {it['title']}")


if __name__ == "__main__":
    main()
