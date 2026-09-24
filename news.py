"""News segments: collects headlines from a station's news sources (web pages
or RSS/Atom feeds), has the AI pick the most important stories and rewrite
them for radio, and assembles the segment's script.

The AI steps go through a JSON-asking function passed in by the caller
(llm.ask_llm_json, via dj_agent), so this module has no dependency on the agent.
"""

import datetime
import email.utils
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

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
MAX_ARTICLE_CHARS = 8000
# An article with less text than this can't fill a couple of minutes without
# padding; stories with full articles are preferred (see build_stories).
MIN_FULL_ARTICLE_CHARS = 1500
# Stories picked beyond `count`, to fall back on when articles are thin,
# can't be fetched or turn out too old.
EXTRA_STORIES = 3
# A front page doesn't say how old its stories are, only their articles do,
# so picked stories whose articles turn out older than max_age_hours are
# dropped. If that leaves fewer than `count` stories with a full article,
# the AI picks more from the other headlines (up to this many times in all).
PICK_ROUNDS = 3
# Where articles give their publication time: a meta tag or JSON-LD field
# ("article:published_time", "datePublished"), or else a <time> element.
PUBLISHED_RE = re.compile(
    r"""(?:article:published_time|datePublished)["']?[^>]{0,120}?"""
    r"""(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)"""
)
TIME_ELEMENT_RE = re.compile(r"""<time[^>]*\bdatetime=["']([^"']+)["']""")
# A rewrite this far below its word target is redone (by the next model).
MIN_LENGTH_SHARE = 0.6
WRITING_TRIES = 3
# Speaking rate used to turn max_minutes into a word budget.
WORDS_PER_MINUTE = 140
# When a story continues one from earlier bulletins, the AI writing it gets
# those bulletins' texts (to recap them briefly and focus on what's new),
# but only from the last this many hours: a whole day's would bloat the
# prompt. Earlier ones are still avoided by headline and summary.
RECAP_HOURS = 3

DEFAULTS = {
    "count": 4,
    "topics": ["national news", "world news", "politics", "economy"],
    "story_minutes": 2,
    "max_minutes": 15,
    "avoid_repeat_hours": 12,
    "max_age_hours": 12,
    "models": [],
    "avoid_models": [],
    "title": "News",
    "intro": "It's {hour}:00 on {station_name}. Here's what's in the news: {topics}",
    "outro": "That's all the news for now. Back to the music.",
}


def news_settings(config):
    """The station's news settings with defaults filled in, or None if the
    station has no news sources (i.e. no news segments)."""
    news = config.get("news") or {}
    if not news.get("sources"):
        return None
    settings = dict(DEFAULTS, **news)
    if isinstance(settings.get("model"), str):  # a single preferred model
        settings["models"] = [settings["model"]] + list(settings["models"])
    return settings


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


def _clean_link(url):
    """An article's address without its fragment and tracking parameters
    (e.g. gazeta.pl's "#do_w=...&s=BoxOpLink", which depends on the front
    page box it was linked from), so the same article is recognized as such."""
    parts = urlparse(url or "")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    return parts._replace(query=urlencode(query), fragment="").geturl()


def _parse_time(text):
    """A timestamp from an ISO 8601 date/time (local time if it has no
    offset), or None if there isn't one."""
    text = re.sub(r"\.\d+", "", (text or "").strip()).replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _feed_time(fields):
    """When a feed item was published (RSS pubDate, Atom published or
    updated), or None."""
    for name in ("pubDate", "published", "updated", "date"):
        text = (fields[name].text or "").strip() if name in fields else ""
        if not text:
            continue
        try:
            return email.utils.parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError):
            published = _parse_time(text)
            if published:
                return published
    return None


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
            items.append({"title": " ".join(title.split()), "summary": summary, "link": link,
                          "published": _feed_time(fields)})
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
        link = _clean_link(urljoin(url, href))
        if _site(link) != site or link in seen or not ARTICLE_PATH_RE.search(urlparse(link).path):
            continue
        seen.add(link)
        items.append({"title": text, "summary": "", "link": link, "published": None})
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
            item["link"] = _clean_link(item["link"])
            headlines.append(item)
        log.info(f"{len(items)} headline(s) from {url}")
    return headlines


def fetch_article(url):
    """An article's text (its paragraphs) and publication time (None if it
    doesn't say), or ("", None) if it can't be fetched (e.g. the site blocks
    it) — the headline's summary is used instead."""
    if not url:
        return "", None
    try:
        html = _get(url).text
        parser = _ParagraphParser()
        parser.feed(html)
    except Exception as e:
        log.info(f"Couldn't fetch article {url}: {e}")
        return "", None
    text = "\n".join(p for p in parser.paragraphs if len(p) >= 60)
    match = PUBLISHED_RE.search(html) or TIME_ELEMENT_RE.search(html)
    return text[:MAX_ARTICLE_CHARS], _parse_time(match.group(1)) if match else None


def _age_hours(story, now):
    """How many hours ago a story was published, or None if unknown."""
    return (now - story["published"]) / 3600 if story.get("published") else None


def _too_old(story, settings, now):
    age = _age_hours(story, now)
    return age is not None and age > settings["max_age_hours"]


def _normalized(text):
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _drop_aired(headlines, recent):
    """Leaves out headlines of articles already read in recent bulletins
    (same link or same headline): the AI is also told not to repeat them,
    but weaker models don't always listen. Other articles about the same
    events stay, for the AI to judge whether there's anything new."""
    aired_links = {_clean_link(r.get("link")) for r in recent if r.get("link")}
    aired_titles = {_normalized(r.get("headline")) for r in recent if r.get("headline")}
    kept = [h for h in headlines if h["link"] not in aired_links and _normalized(h["title"]) not in aired_titles]
    if len(kept) < len(headlines):
        log.info(f"Left out {len(headlines) - len(kept)} headline(s) of articles already aired recently.")
    return kept


def _recent_listing(recent):
    """The stories of recent bulletins, newest first, each listed once (the
    same article can be aired in several bulletins in a row)."""
    lines, seen = [], set()
    for r in recent:
        key = _normalized(r.get("headline")) or _normalized(r.get("topic"))
        if key in seen:
            continue
        seen.add(key)
        aired = f"[{r['aired']}] " if r.get("aired") else ""
        lines.append(f"- {aired}{r.get('headline', '')} — {r.get('topic', '')}")
    return "\n".join(lines)


def _pick_stories(headlines, settings, ask_json, recent, count):
    """The `count` most important distinct stories, most important first.
    Stories in `recent` (aired in the last few hours) are avoided unless
    there's something new."""
    listing = "\n".join(
        f"{i}. [{h['source']}] {h['title']}" + (f" — {h['summary'][:200]}" if h["summary"] else "")
        for i, h in enumerate(headlines, 1)
    )
    count = min(count, len(headlines))
    prompt = (
        f"You are the news editor of a radio station. It's {time.strftime('%A, %d %B %Y, %H:%M')}. Below "
        f"are the current headlines from several news sites' front pages (some entries are ads, links to "
        f"the sites' services or teasers, not news). Pick the {count} most important news stories, "
        f"focusing on these topics: {', '.join(settings['topics'])}.\n"
        f"Rules: pick {count} DIFFERENT stories — the same event reported by several sites or in several "
        f"headlines counts once, so pick just one headline for it (the most informative). Only pick real, "
        f"current news about events; never pick ads, TV guides, horoscopes, weather, shopping, services, "
        f"sport results, celebrity gossip or opinion pieces. The bulletin should bring fresh news: prefer "
        f"today's events, and skip stories about something that happened a day or more ago unless there's "
        f"news about it today. Normally include at least one story for each "
        f"of the topics above rather than several on the same one; only depart from that for a good reason, "
        f"e.g. there's no real news for a topic, or one topic has several stories clearly more important "
        f"than anything else. List them from most to least important."
        + (f"\n\nThese stories were already covered in the last {settings['avoid_repeat_hours']} hours' "
           f"bulletins (with when they were aired; headline — summary). Don't pick them again, nor other "
           f"headlines about the same events: a new article or a differently worded headline about an "
           f"event already covered is still the same story. Only pick one if the headline shows a "
           f"significant new development (not just the same event retold):\n{_recent_listing(recent)}"
           if recent else "")
        + f"\n\n{listing}\n\n"
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

    return ask_json(prompt, "news selection", validate, settings["models"])


def _write_stories(stories, settings, language, ask_json, recent):
    target_words = settings["story_minutes"] * WORDS_PER_MINUTE
    max_words = max(target_words, settings["max_minutes"] * WORDS_PER_MINUTE // max(1, len(stories)))
    material = "\n\n".join(
        f"STORY {i} (source: {s['source']})\nHeadline: {s['title']}\n"
        + (f"Summary: {s['summary']}\n" if s["summary"] else "")
        + (f"Article:\n{s['text']}" if s["text"] else "")
        for i, s in enumerate(stories, 1)
    )
    recent = [r for r in recent if r.get("hours_ago", 0) <= RECAP_HOURS]
    earlier = "\n".join(f"- {r.get('topic', '')}: {r.get('text', '')[:400]}" for r in recent)
    prompt = (
        f"You are a radio news presenter. Rewrite each story below as a spoken news item for radio, in the "
        f"language of locale {language} (e.g. Polish for pl-PL). Each item should take about "
        f"{settings['story_minutes']} minutes to read aloud: aim for about {target_words} words (never more "
        f"than {max_words}). Cover the story properly — what happened, who is involved, the background, "
        f"reactions and what happens next — using the details in the material, but stick strictly to what "
        f"the material says: don't add facts, numbers, quotes, opinions or speculation that aren't in it. "
        f"If the material really is too thin for that length, keep the item shorter rather than padding it "
        f"or making anything up. No filler: don't repeat what you've already said, don't restate the "
        f"headline at the end, and skip empty phrases and generic commentary — every sentence should add "
        f"information. Use plain, correct spoken sentences with no lists, headings, emojis or markdown. "
        f"Also give each story a one-sentence summary in the same language (a full sentence of about 10-20 "
        f"words, ending with a full stop), which the segment's opening reads out as its list of topics."
        + (f"\n\nEarlier bulletins already covered the stories below. If a story here continues one of "
           f"them, recap it in a sentence at most and focus on what's new, without repeating the same "
           f"details:\n{earlier}" if recent else "")
        + f"\n\n{material}\n\n"
        f"You MUST respond strictly in valid JSON format with no markdown formatting around it, structured "
        f'like this:\n{{"stories": [{{"summary": "...", "text": "..."}}]}}'
    )

    def validate(parsed):
        items = []
        for item in parsed.get("stories") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                topic = item.get("summary") or item.get("topic")
                topic = topic.strip() if isinstance(topic, str) else ""
                if topic and topic[-1] not in ".!?":
                    topic += "."
                items.append({"topic": topic, "text": item["text"].strip()})
        if not items:
            raise ValueError("AI wrote no usable news items.")
        return items

    # Models sometimes write far less than asked; redo a too-short rewrite,
    # starting with the next preferred model each time, and keep the
    # longest if none gets there.
    wanted = MIN_LENGTH_SHARE * target_words * len(stories)
    models = list(settings["models"])
    best = None
    for attempt in range(WRITING_TRIES):
        rotated = models[attempt:] + models[:attempt] if models else []
        items = ask_json(prompt, "news writing", validate, rotated)
        words = sum(len(item["text"].split()) for item in items)
        if best is None or words > best[0]:
            best = (words, items)
        if words >= wanted:
            break
        log.info(f"News came out at {words} words, below the ~{int(wanted)} wanted; rewriting.")
    return best[1]


def _choose_stories(candidates, count):
    """Of the picked stories (most important first), the first `count` whose
    full article could be fetched, topped up with thinner ones if needed —
    in their original order."""
    full = [s for s in candidates if len(s["text"]) >= MIN_FULL_ARTICLE_CHARS]
    chosen = full[:count]
    for story in candidates:
        if len(chosen) >= count:
            break
        if story not in chosen:
            chosen.append(story)
    return [s for s in candidates if s in chosen]


def build_stories(settings, language, ask_json, recent=()):
    """Collects headlines, has the AI pick settings["count"] stories and
    write them for radio. `recent` holds the stories of the last few hours'
    bulletins (as returned here before, plus when they were "aired" and how
    many "hours_ago"), newest first, which aren't repeated unless there's
    something new. Returns [{"topic", "headline", "text"}]; raises
    if anything along the way leaves nothing to read."""
    recent = list(recent)
    headlines = fetch_headlines(settings["sources"])
    if not headlines:
        raise ValueError("No headlines from any news source.")
    headlines = _drop_aired(headlines, recent)
    now = time.time()
    old = [h for h in headlines if _too_old(h, settings, now)]
    if old:
        log.info(f"Left out {len(old)} feed headline(s) older than {settings['max_age_hours']}h.")
        headlines = [h for h in headlines if h not in old]

    # Pick stories, fetch their articles and drop those that turn out too
    # old; if that leaves too few with a full article, pick more from the
    # remaining headlines.
    candidates, tried = [], []
    for _ in range(PICK_ROUNDS):
        remaining = [h for h in headlines if h not in tried]
        full = [c for c in candidates if len(c["text"]) >= MIN_FULL_ARTICLE_CHARS]
        wanted = settings["count"] - len(full) + EXTRA_STORIES
        if not remaining or wanted <= EXTRA_STORIES:
            break
        try:
            picks = _pick_stories(remaining, settings, ask_json, recent, wanted)
        except Exception as e:
            if not candidates:
                raise
            log.warning(f"Couldn't pick more stories ({e}); going with what there is.")
            break
        for story in picks:
            tried.append(story)
            story["text"], published = fetch_article(story["link"])
            story["published"] = story["published"] or published
            if _too_old(story, settings, now):
                log.info(f"Left out a story from {_age_hours(story, now):.0f}h ago: {story['title']}")
            else:
                candidates.append(story)
    if not candidates:
        raise ValueError(f"No stories from the last {settings['max_age_hours']} hours.")
    picked = _choose_stories(candidates, settings["count"])

    def age(story):
        hours = _age_hours(story, now)
        return "age unknown" if hours is None else f"{hours:.0f}h old"

    log.info("Picked stories: " + " | ".join(
        f"[{s['source']}] {s['title']} ({len(s['text'])} chars of article, {age(s)})" for s in picked))
    items = _write_stories(picked, settings, language, ask_json, recent)
    # Remember what each item was about, for the next bulletins' `recent`.
    for item, story in zip(items, picked):
        item["headline"] = story["title"]
        item["link"] = story["link"]
    return items


def assemble_script(stories, settings, station_name, hour):
    """The segment's full text: the configured intro (with the hour, station
    name and the stories' one-sentence summaries as {topics}), the stories,
    and the configured outro."""
    topics = " ".join(s["topic"] for s in stories if s["topic"])
    intro = settings["intro"].format(hour=hour, station_name=station_name, topics=topics)
    return "\n\n".join([intro] + [s["text"] for s in stories] + [settings["outro"]])


def cache_key(settings, language):
    """Identifies the news content (not the station), so stations with the
    same news settings share one segment's stories per hour."""
    relevant = {k: settings.get(k) for k in ("sources", "count", "topics", "story_minutes", "max_minutes", "models")}
    relevant["language"] = language
    return json.dumps(relevant, sort_keys=True, ensure_ascii=False)


def history_key(settings, language):
    """Identifies which bulletins count as "the same news" for not repeating
    stories: same sources, topics and language, regardless of e.g. the
    models or length (which may change from one hour to the next)."""
    relevant = {k: settings.get(k) for k in ("sources", "topics")}
    relevant["language"] = language
    return json.dumps(relevant, sort_keys=True, ensure_ascii=False)
