"""News segments: collects headlines from a station's news sources (web pages
or RSS/Atom feeds), has the AI pick the most important stories and rewrite
them for radio, and assembles the segment's script.

The AI steps go through a JSON-asking function passed in by the caller
(dj_agent.ask_llm_json), so this module has no dependency on the agent.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger("news")

# Some sites refuse requests that don't look like a browser.
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
}
HTTP_TIMEOUT = 20
# A link text shorter than this is navigation ("Sport", "More...") rather
# than a headline.
MIN_HEADLINE_CHARS = 40
# Front pages list their main stories first; more than this per source only
# adds noise, and long lists make weaker models pick badly.
MAX_HEADLINES_PER_SOURCE = 25
# An article's address has a descriptive slug ("...-incident-in-polish-...")
# or a long numeric ID; links to a site's services, sections and ads
# ("https://tv.example.com", "/products/") don't.
ARTICLE_PATH_RE = re.compile(r"(-[^/-]+){3,}|\d{6,}")
# "14:43 ", "34 min ", comment counts ("548 ") and the like, which some
# sites put before headlines.
HEADLINE_TIME_PREFIX_RE = re.compile(r"^((\d{1,2}:\d{2}|\d+\s*min|\d{1,5})\s+)+", re.IGNORECASE)
# How much of an article's text the AI gets to summarize.
MAX_ARTICLE_CHARS = 5000
# Speaking rate used to turn max_minutes into a word budget.
WORDS_PER_MINUTE = 140

DEFAULTS = {
    "count": 4,
    "topics": ["national news", "world news", "politics", "economy"],
    "max_minutes": 15,
    "title": "News",
    "intro": "It's {hour}:00 on {station_name}. In this news segment: {topics}.",
    "outro": "That's all the news for now. Back to the music.",
}


def news_settings(config):
    """The station's news settings with defaults filled in, or None if the
    station has no news sources (i.e. no news segments)."""
    news = config.get("news") or {}
    if not news.get("sources"):
        return None
    return dict(DEFAULTS, **news)


class _LinkParser(HTMLParser):
    """Collects (text, href) of every <a> on a page."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((" ".join(" ".join(self._text).split()), self._href))
            self._href = None


class _ParagraphParser(HTMLParser):
    """Collects the text of every <p> on a page."""

    def __init__(self):
        super().__init__()
        self.paragraphs = []
        self._depth = 0
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._depth += 1
            self._text = []

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self._depth:
            self._depth -= 1
            self.paragraphs.append(" ".join(" ".join(self._text).split()))


def _get(url):
    response = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response


def _strip_tags(text):
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())


def _site(url):
    """example.com for https://www.news.example.com/..., to tell a site's
    own links from ads and partner sites."""
    host = urlparse(url).netloc.lower().split(":")[0]
    return ".".join(host.split(".")[-2:])


def _feed_items(content):
    """Headlines from an RSS or Atom feed."""
    root = ET.fromstring(content)
    items = []
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.split("}")[-1]: child for child in item}
        title = (fields["title"].text or "") if "title" in fields else ""
        summary_el = fields.get("description") if "description" in fields else fields.get("summary")
        summary = _strip_tags(summary_el.text) if summary_el is not None else ""
        link_el = fields.get("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        if title.strip():
            items.append({"title": " ".join(title.split()), "summary": summary, "link": link})
    return items


def _page_headlines(url, html):
    """Headlines from a news site's front page: its own links whose text is
    long enough to be a headline."""
    parser = _LinkParser()
    parser.feed(html)
    site = _site(url)
    seen, items = set(), []
    for text, href in parser.links:
        text = HEADLINE_TIME_PREFIX_RE.sub("", text)
        if len(text) < MIN_HEADLINE_CHARS or not href:
            continue
        link = urljoin(url, href)
        if _site(link) != site or link in seen or not ARTICLE_PATH_RE.search(urlparse(link).path):
            continue
        seen.add(link)
        items.append({"title": text, "summary": "", "link": link})
    return items


def fetch_headlines(sources):
    """Headlines from every source, each tagged with its source's site.
    A source that can't be fetched is logged and skipped."""
    headlines = []
    for url in sources:
        try:
            response = _get(url)
            content = response.content.lstrip()
            is_feed = "xml" in response.headers.get("Content-Type", "") or content[:100].lower().startswith(
                (b"<?xml", b"<rss", b"<feed"))
            items = _feed_items(content) if is_feed else _page_headlines(url, response.text)
        except Exception as e:
            log.warning(f"Couldn't get headlines from {url}: {e}")
            continue
        for item in items[:MAX_HEADLINES_PER_SOURCE]:
            item["source"] = _site(url)
            headlines.append(item)
        log.info(f"{len(items)} headline(s) from {url}")
    return headlines


def fetch_article_text(url):
    """An article's text (its paragraphs), or "" if it can't be fetched
    (e.g. the site blocks it) — the headline's summary is used instead."""
    if not url:
        return ""
    try:
        parser = _ParagraphParser()
        parser.feed(_get(url).text)
    except Exception as e:
        log.info(f"Couldn't fetch article {url}: {e}")
        return ""
    text = "\n".join(p for p in parser.paragraphs if len(p) >= 60)
    return text[:MAX_ARTICLE_CHARS]


def _pick_stories(headlines, settings, ask_json):
    listing = "\n".join(
        f"{i}. [{h['source']}] {h['title']}" + (f" — {h['summary'][:200]}" if h["summary"] else "")
        for i, h in enumerate(headlines, 1)
    )
    count = settings["count"]
    prompt = (
        f"You are the news editor of a radio station. Below are the current headlines from several news "
        f"sites' front pages (some entries are ads, links to the sites' services or teasers, not news). "
        f"Pick the {count} most important news stories, focusing on these topics: "
        f"{', '.join(settings['topics'])}.\n"
        f"Rules: pick {count} DIFFERENT stories — the same event reported by several sites or in several "
        f"headlines counts once, so pick just one headline for it (the most informative). Only pick real, "
        f"current news about events; never pick ads, TV guides, horoscopes, weather, shopping, services, "
        f"sport results, celebrity gossip or opinion pieces.\n\n{listing}\n\n"
        f"You MUST respond strictly in valid JSON format with no markdown formatting around it, structured "
        f'like this:\n{{"selected_indices": [3, 17, 42, 58]}}'
    )

    def validate(parsed):
        indices = parsed.get("selected_indices")
        if not isinstance(indices, list):
            raise KeyError("AI response lacks 'selected_indices'.")
        picked = []
        for i in indices:
            if isinstance(i, int) and 0 < i <= len(headlines) and headlines[i - 1] not in picked:
                picked.append(headlines[i - 1])
        if not picked:
            raise ValueError("AI picked no valid stories.")
        return picked[:count]

    return ask_json(prompt, "news selection", validate)


def _write_stories(stories, settings, language, ask_json):
    max_words = settings["max_minutes"] * WORDS_PER_MINUTE // max(1, len(stories))
    material = "\n\n".join(
        f"STORY {i} (source: {s['source']})\nHeadline: {s['title']}\n"
        + (f"Summary: {s['summary']}\n" if s["summary"] else "")
        + (f"Article:\n{s['text']}" if s["text"] else "")
        for i, s in enumerate(stories, 1)
    )
    prompt = (
        f"You are a radio news presenter. Rewrite each story below as a short spoken news item for radio, "
        f"in the language of locale {language} (e.g. Polish for pl-PL). Summarize: keep the key facts (who, "
        f"what, where, when, why), leave out minor details, and stick strictly to what the material says — "
        f"don't add facts, numbers, quotes, opinions or speculation that aren't in it. If the material is "
        f"thin, keep the item short rather than padding it. Each item must be at most {max_words} words "
        f"(usually far fewer is fine), plain spoken sentences with no lists, headings, emojis or markdown. "
        f"Also give each story a topic label of 2-4 words in the same language, for the segment's opening "
        f"line.\n\n{material}\n\n"
        f"You MUST respond strictly in valid JSON format with no markdown formatting around it, structured "
        f'like this:\n{{"stories": [{{"topic": "...", "text": "..."}}]}}'
    )

    def validate(parsed):
        items = []
        for item in parsed.get("stories") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                topic = item.get("topic") if isinstance(item.get("topic"), str) else ""
                items.append({"topic": topic.strip(), "text": item["text"].strip()})
        if not items:
            raise ValueError("AI wrote no usable news items.")
        return items

    return ask_json(prompt, "news writing", validate)


def build_stories(settings, language, ask_json):
    """Collects headlines, has the AI pick settings["count"] stories and
    write them for radio. Returns [{"topic": ..., "text": ...}]; raises if
    anything along the way leaves nothing to read."""
    headlines = fetch_headlines(settings["sources"])
    if not headlines:
        raise ValueError("No headlines from any news source.")
    picked = _pick_stories(headlines, settings, ask_json)
    log.info("Picked stories: " + " | ".join(f"[{s['source']}] {s['title']}" for s in picked))
    for story in picked:
        story["text"] = fetch_article_text(story["link"])
    return _write_stories(picked, settings, language, ask_json)


def assemble_script(stories, settings, station_name, hour):
    """The segment's full text: the configured intro (with the hour, station
    name and topics), the stories, and the configured outro."""
    topics = ", ".join(s["topic"] for s in stories if s["topic"])
    intro = settings["intro"].format(hour=hour, station_name=station_name, topics=topics)
    return "\n\n".join([intro] + [s["text"] for s in stories] + [settings["outro"]])


def cache_key(settings, language):
    """Identifies the news content (not the station), so stations with the
    same sources and topics share one segment's stories per hour."""
    relevant = {k: settings.get(k) for k in ("sources", "count", "topics", "max_minutes", "model")}
    relevant["language"] = language
    return json.dumps(relevant, sort_keys=True, ensure_ascii=False)
