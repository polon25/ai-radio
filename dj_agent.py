import sqlite3
import os
import sys
import json
import logging
import random
import subprocess
import time
import requests
import asyncio
import collections
import datetime
import fcntl
import hashlib
import edge_tts
import imageio_ffmpeg
from mutagen import File as MutagenFile
from dotenv import load_dotenv

# Load environment variables from .env file (before importing radio_log,
# which reads its settings from the environment too)
load_dotenv()

import news
from liquidsoap_client import NEWS_QUEUE_ID, Liquidsoap
from radio_log import (
    LIQUIDSOAP_SPOOL_FILE, SUMMARY, LiquidsoapLogForwarder, setup_logging,
)

# Force line-buffered stdout so log lines show up promptly in journald
# even when running unattended (e.g. as a long-lived --loop process), not
# just when attached to a terminal.
sys.stdout.reconfigure(line_buffering=True)

log = logging.getLogger("agent")
llm_log = logging.getLogger("llm")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://github.com/your-username/your-project")
# Seconds a whole OpenRouter request (including reading the answer) may take.
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "60"))
# The same for background work nothing on air waits for (classifying
# artists, filling in track info): long lists take slow models a while.
OPENROUTER_BACKGROUND_TIMEOUT = int(os.getenv("OPENROUTER_BACKGROUND_TIMEOUT", "180"))
# "openrouter/free" routes each request to some free model, which now and
# then is one that can't follow the prompt (e.g. a content-safety classifier
# answering "User Safety: safe"), so unusable answers are retried.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_ATTEMPTS = int(os.getenv("OPENROUTER_ATTEMPTS", "3"))
# In --loop mode, the next block is prepared as soon as the last queued track
# starts playing, or earlier if less than this many seconds of music are left
# in Liquidsoap's queue (so a short last track doesn't leave too little time
# for the AI and TTS).
BLOCK_LEAD_TIME = int(os.getenv("BLOCK_LEAD_TIME", "120"))
# How often (seconds) --loop checks Liquidsoap's queue, and how long it waits
# before retrying a block that failed to generate.
QUEUE_POLL_INTERVAL = 5
BLOCK_RETRY_DELAY = 15
# Loudness (LUFS) DJ intros and news are raised to, before a limiter shaves
# their peaks (which leaves them about 1.5 LU below it): a little below the
# typical mastered music they play between, which is often around -8.
SPEECH_LOUDNESS_LUFS = float(os.getenv("SPEECH_LOUDNESS_LUFS", "-10"))
# Edge TTS is an online service: seconds to wait before each retry (so one
# try more than there are delays) before a block goes on air without its
# intro. Blocks are prepared minutes ahead, so this can ride out a DNS or
# network hiccup of up to about a minute.
TTS_RETRY_DELAYS = (5, 15, 30)
# The fallback list radio.liq plays from if the block queue runs dry: this
# many random tracks from the station's roster, refreshed this often.
FALLBACK_TRACKS = 50
FALLBACK_REFRESH_HOURS = 24
# Generated intros are kept this many at a time (older ones are deleted), so
# one that's still queued is never overwritten or removed before it plays.
INTROS_TO_KEEP = 5
# In --loop mode, how often to re-scan the music library in the background
# (in addition to always scanning once at startup). 0 disables the periodic
# rescan (a scan still runs at startup).
SCAN_INTERVAL_HOURS = float(os.getenv("SCAN_INTERVAL_HOURS", "2"))
DB_FILE = "music_library.db"
CONFIG_FILE = "stations.json"
DEFAULT_SONGS_PER_BLOCK = 3
# Fraction of a station's artist pool that must play before an artist can
# repeat, e.g. 0.1 with a 70-artist roster means 7 other songs minimum
# between two plays of the same artist. 0 disables the cooldown.
DEFAULT_ARTIST_COOLDOWN_FRACTION = 0.1
# Shorter tracks (intros, interludes, skits...) aren't played, unless a
# station sets its own min_track_seconds.
DEFAULT_MIN_TRACK_SECONDS = 90
# A track that played on a station within the last track_cooldown_hours
# (default below) isn't picked there again while the artist has other
# tracks, and an artist whose tracks all played that recently isn't picked
# at all. So that small stations don't end up cycling through their whole
# library in the same order, the cooldown never exceeds this share of the
# station's total music: about half its tracks are always eligible.
DEFAULT_TRACK_COOLDOWN_HOURS = 24
TRACK_COOLDOWN_LIBRARY_SHARE = 0.5
# Play history older than this is deleted.
PLAY_HISTORY_DAYS = 30
# Tracks per AI request when filling in missing release years/genres.
TRACK_INFO_BATCH_SIZE = 40
# Artists are classified into a station's roster this many per AI request:
# with hundreds in one numbered list, models lose track of the numbers and
# pick huge swathes of off-theme artists.
ROSTER_BATCH_SIZE = 40
# Each batch is classified this many times (openrouter/free picks a
# different model each time, and their answers vary a lot: one picks exactly
# the right artists, the next half the list) and an artist gets in only if
# most of the answers picked it.
ROSTER_VOTES = 3
# Every station keeps its own runtime files (intros, roster, play history,
# fallback list, logs) in STATIONS_DIR/<station_id>/, so they don't clutter
# the project root or clash with other stations.
STATIONS_DIR = "stations"
INTROS_DIR_NAME = "intros"
# Each station's generated news segments (see news.py), in its own folder.
NEWS_DIR_NAME = "news"
# News stories shared by all stations with the same news settings, one file
# per hour, so the AI writes them once (project root; gitignored).
NEWS_CACHE_DIR = "news_cache"
# A news segment is prepared this long before the top of the hour it's for,
# queued this long before it (it then plays as soon as the current track
# ends, i.e. around the top of the hour), and dropped if it's still not ready
# this long after it.
NEWS_PREPARE_MINUTES = 15
NEWS_QUEUE_MINUTES = 2
NEWS_MAX_LATE_MINUTES = 10


def get_station_dir(station_id):
    """Directory holding all of this station's runtime files."""
    return os.path.join(STATIONS_DIR, station_id)


def ensure_station_dir(station_id):
    """Creates the station's directory and moves over any files left in the
    project root by older versions (which named them <kind>_<station_id>.*),
    so an upgrade keeps the existing roster and play history. Playlist and
    intro files from older versions are no longer used and get deleted."""
    station_dir = get_station_dir(station_id)
    os.makedirs(get_intros_dir(station_id), exist_ok=True)
    legacy_files = {
        f"artists_{station_id}.json": get_artists_file(station_id),
        f"recent_artists_{station_id}.json": get_recent_artists_file(station_id),
    }
    for old_path, new_path in legacy_files.items():
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.replace(old_path, new_path)
            log.info(f"Moved {old_path} -> {new_path}")
    obsolete_files = [
        f"dj_playlist_{station_id}.txt",
        f"dj_intro_{station_id}.mp3",
        os.path.join(station_dir, "playlist.txt"),
        os.path.join(station_dir, "intro.mp3"),
    ]
    for path in obsolete_files:
        if os.path.exists(path):
            os.remove(path)
            log.info(f"Removed obsolete {path}")


def get_intros_dir(station_id):
    """Directory holding this station's generated DJ intros."""
    return os.path.join(get_station_dir(station_id), INTROS_DIR_NAME)


def new_intro_audio_file(station_id):
    """Path for a new block's DJ intro. Each block gets its own file: its
    intro may still be waiting in Liquidsoap's queue while the next block is
    being generated. Deletes all but the newest INTROS_TO_KEEP intros."""
    intros_dir = get_intros_dir(station_id)
    existing = sorted(f for f in os.listdir(intros_dir) if f.endswith(".mp3"))
    for name in existing[:max(0, len(existing) - INTROS_TO_KEEP + 1)]:
        os.remove(os.path.join(intros_dir, name))
    return os.path.join(intros_dir, time.strftime("%Y%m%d-%H%M%S") + ".mp3")


def get_socket_file(station_id):
    """Liquidsoap's command socket for this station (radio.liq builds the
    same path, so keep the two in sync)."""
    return os.path.join(get_station_dir(station_id), "liquidsoap.sock")


def get_fallback_file(station_id):
    """Tracks radio.liq plays if the block queue runs dry (radio.liq builds
    the same path, so keep the two in sync)."""
    return os.path.join(get_station_dir(station_id), "fallback.txt")


def get_artists_file(station_id):
    """Path to this station's artist roster: which artists are allowed to be
    played, and which are banned (manually, or because the AI already ruled
    them out as off-theme, so they aren't re-asked about every run)."""
    return os.path.join(get_station_dir(station_id), "artists.json")


def load_artist_roster(station_id):
    """Loads this station's {"allowed": [...], "banned": [...]} roster. A
    missing file yields two empty lists; a bare JSON list (old format) is
    treated as the allowed list."""
    path = get_artists_file(station_id)
    if not os.path.exists(path):
        return {"allowed": [], "banned": []}
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"allowed": [], "banned": []}
    if isinstance(data, list):
        return {"allowed": data, "banned": []}
    return {"allowed": data.get("allowed", []), "banned": data.get("banned", [])}


def save_artist_roster(station_id, roster):
    """Persists the artist roster so future runs reuse it instead of
    reclassifying the whole library from scratch."""
    path = get_artists_file(station_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(roster, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)  # atomic: the station's agent may be reading it
    log.info(
        f"Saved roster to {path}: {len(roster['allowed'])} allowed, "
        f"{len(roster['banned'])} banned"
    )


def get_log_dir(station_id):
    """Directory holding this station's daily log files (and the spool file
    radio.liq writes Liquidsoap's log to, see radio_log.py)."""
    return os.path.join(get_station_dir(station_id), "logs")


def describe_track(metadata):
    """Human-readable "Artist - Title" for a track's metadata (as reported by
    Liquidsoap), or "DJ intro" for a station's generated intro."""
    path = metadata.get("filename", "")
    folder = os.path.basename(os.path.dirname(path))
    if folder == INTROS_DIR_NAME:
        return "DJ intro"
    if folder == NEWS_DIR_NAME:
        return "News"
    artist, title = metadata.get("artist"), metadata.get("title")
    if artist and title:
        return f"{artist} - {title}"
    return os.path.basename(path) or "(unknown)"


def get_recent_artists_file(station_id):
    """Path to this station's recently-played-artists history, used to
    enforce the artist cooldown."""
    return os.path.join(get_station_dir(station_id), "recent_artists.json")


def load_recent_artists(station_id):
    """Loads the play history (oldest first). Missing/corrupt file -> none."""
    path = get_recent_artists_file(station_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def save_recent_artists(station_id, history):
    """Persists the play history, trimmed so the file doesn't grow forever."""
    path = get_recent_artists_file(station_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history[-500:], f, ensure_ascii=False)


def artist_cooldown(pool_size, fraction):
    """How many other songs must play before an artist already in `history`
    is eligible again."""
    if fraction <= 0:
        return 0
    return max(1, round(pool_size * fraction))


def filter_by_cooldown(artists, history, cooldown):
    """Drops artists who played within the last `cooldown` songs."""
    if cooldown <= 0:
        return list(artists)
    recently_played = set(history[-cooldown:])
    return [a for a in artists if a not in recently_played]


def load_station_config(station_id):
    """Loads configuration for a specific station from the JSON file."""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Configuration file {CONFIG_FILE} not found!")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if station_id not in data:
        raise ValueError(f"Station '{station_id}' not found in config.")
    return data[station_id]

def track_filter_clause(config, use_years=True):
    """SQL condition (and its params) selecting the tracks a station may
    play: those under its folder_filter (a single SQL LIKE pattern, or a
    list of patterns OR'd together, so a station can pull from several
    library folders at once) that are at least min_track_seconds long and,
    if the station sets "years": [first, last], released in that range
    (tracks of unknown year too, unless allow_unknown_year is false).
    Tracks whose length couldn't be read are let through."""
    folder_filter = config['folder_filter']
    patterns = folder_filter if isinstance(folder_filter, list) else [folder_filter]
    folders = " OR ".join(["filepath LIKE ?"] * len(patterns))
    min_seconds = config.get('min_track_seconds', DEFAULT_MIN_TRACK_SECONDS)
    clause = f"({folders}) AND (duration IS NULL OR duration >= ?)"
    params = list(patterns) + [min_seconds]
    if use_years and config.get('years'):
        first, last = config['years']
        unknown = " OR year IS NULL" if config.get('allow_unknown_year', True) else ""
        clause += f" AND (year BETWEEN ? AND ?{unknown})"
        params += [first, last]
    return clause, params


def get_all_artists(config):
    """Fetches every unique artist with at least one track the station may
    play."""
    clause, params = track_filter_clause(config)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT DISTINCT artist FROM tracks
        WHERE {clause} AND artist != 'Unknown Artist'
    """, params)
    artists = [row[0] for row in c.fetchall()]
    conn.close()
    return artists

def get_track_by_artist(artist, config):
    """Fetches a random track for a specific artist."""
    clause, params = track_filter_clause(config)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT filepath, artist, title FROM tracks
        WHERE {clause} AND artist = ?
        ORDER BY RANDOM() LIMIT 1
    """, params + [artist])
    track = c.fetchone()
    conn.close()
    return track

def get_station_tracks(config):
    """Every track the station may play, as (filepath, artist, title,
    duration) tuples."""
    clause, params = track_filter_clause(config)
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        f"SELECT filepath, artist, title, duration FROM tracks WHERE {clause}", params
    ).fetchall()
    conn.close()
    return rows


def init_play_history():
    """Creates the play history table if needed and drops entries older
    than PLAY_HISTORY_DAYS."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS plays
                    (station TEXT NOT NULL, filepath TEXT NOT NULL, played_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS plays_by_track ON plays (station, filepath, played_at)")
    conn.execute("DELETE FROM plays WHERE played_at < ?", (time.time() - PLAY_HISTORY_DAYS * 86400,))
    conn.commit()
    conn.close()


def record_play(station_id, metadata, played_at):
    """Records that a track started playing on the station (called for every
    "now playing" Liquidsoap reports; DJ intros and news aren't recorded)."""
    path = metadata.get("filename", "")
    if not path or os.path.basename(os.path.dirname(path)) in (INTROS_DIR_NAME, NEWS_DIR_NAME):
        return
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("INSERT INTO plays (station, filepath, played_at) VALUES (?, ?, ?)", (station_id, path, played_at))
    conn.commit()
    conn.close()


def get_last_plays(station_id):
    """When each track last played on the station: {filepath: timestamp}."""
    conn = sqlite3.connect(DB_FILE, timeout=30)
    rows = conn.execute(
        "SELECT filepath, MAX(played_at) FROM plays WHERE station = ? GROUP BY filepath", (station_id,)
    ).fetchall()
    conn.close()
    return dict(rows)


def track_cooldown_seconds(config, tracks):
    """The station's track cooldown: track_cooldown_hours, capped at
    TRACK_COOLDOWN_LIBRARY_SHARE of the total length of `tracks`."""
    hours = config.get('track_cooldown_hours', DEFAULT_TRACK_COOLDOWN_HOURS)
    library_seconds = sum(duration or 0 for _, _, _, duration in tracks)
    return min(hours * 3600, library_seconds * TRACK_COOLDOWN_LIBRARY_SHARE)


def pick_track(artist_tracks, last_plays, cutoff):
    """One of an artist's tracks: a random one among those that haven't
    played since `cutoff`, or else the one that played longest ago."""
    fresh = [t for t in artist_tracks if last_plays.get(t[0], 0) < cutoff]
    if fresh:
        return random.choice(fresh)
    return min(artist_tracks, key=lambda t: last_plays.get(t[0], 0))


def format_artist_list(artists):
    """Numbers an artist list for injection into a prompt (1-indexed, to
    match the `selected_indices` the AI is asked to return)."""
    return "\n".join([f"{i+1}. {artist}" for i, artist in enumerate(artists)])


def _shorten(text, limit=500):
    """Trims long API payloads so a single bad response can't flood the log."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def call_openrouter_json(prompt, purpose, timeout=OPENROUTER_TIMEOUT, model=None):
    """Sends a prompt to OpenRouter and parses the response as JSON. The
    prompt must instruct the model to reply with raw JSON. `purpose` only
    labels the request in the logs. `model` asks for a specific model, with
    OpenRouter falling back to OPENROUTER_MODEL if it's unavailable. Every failure is logged (under the "llm"
    component) before being raised."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing. Check your .env file.")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": "AI Radio Project"
    }

    payload = {
        "model": model or OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise radio automation agent. You always output valid raw JSON."},
            {"role": "user", "content": prompt}
        ]
    }

    if model and model != OPENROUTER_MODEL:
        payload["models"] = [model, OPENROUTER_MODEL]
    llm_log.info(f"Request ({purpose}), model {payload['model']}, prompt {len(prompt)} chars")
    started = time.monotonic()
    try:
        # requests' timeout only limits the wait for each next chunk, and
        # OpenRouter keeps sending whitespace while a model is still working,
        # so a slow model could hold a request for many minutes. Read the
        # body as it arrives and enforce `timeout` on the total.
        with requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
            stream=True,
        ) as response:
            chunks = []
            for chunk in response.iter_content(chunk_size=8192):
                chunks.append(chunk)
                if time.monotonic() - started > timeout:
                    raise requests.Timeout(f"no complete answer within {timeout}s")
            body = b"".join(chunks).decode("utf-8", "replace")
    except requests.RequestException as e:
        llm_log.warning(f"Request ({purpose}) failed after {time.monotonic() - started:.1f}s: {e}")
        raise
    elapsed = time.monotonic() - started

    try:
        result = json.loads(body)
    except Exception as e:
        llm_log.warning(
            f"Response ({purpose}) is not JSON: HTTP {response.status_code} after {elapsed:.1f}s: "
            f"{_shorten(body)}"
        )
        raise ValueError(f"Failed to parse API response. Status: {response.status_code}, Text: {body}")

    # Check if the response contains the expected 'choices' key
    if 'choices' not in result:
        llm_log.warning(
            f"API error ({purpose}): HTTP {response.status_code} after {elapsed:.1f}s: "
            f"{_shorten(json.dumps(result.get('error', result), ensure_ascii=False))}"
        )
        raise KeyError("OpenRouter did not return 'choices'.")

    model = result.get("model", "?")
    usage = result.get("usage") or {}
    llm_log.info(
        f"Response ({purpose}) from {model} in {elapsed:.1f}s "
        f"(tokens: {usage.get('prompt_tokens', '?')} in, {usage.get('completion_tokens', '?')} out)"
    )

    raw_content = (result['choices'][0]['message'].get('content') or "").strip()
    if not raw_content:
        llm_log.warning(f"Empty response ({purpose}) from {model}")
        raise ValueError(f"{model} returned an empty response.")

    # Models sometimes wrap the JSON in a markdown code block or some prose
    # anyway, or follow it with more text or even a second JSON object, so
    # parse the first complete object starting at the first "{". strict=False
    # accepts raw newlines inside strings (e.g. a multi-line DJ script).
    start = raw_content.find("{")
    try:
        parsed, _ = json.JSONDecoder(strict=False).raw_decode(raw_content[max(start, 0):])
    except json.JSONDecodeError as e:
        llm_log.warning(f"Invalid JSON ({purpose}) from {model}: {e}: {_shorten(raw_content)}")
        raise
    if not isinstance(parsed, dict):
        llm_log.warning(f"JSON ({purpose}) from {model} is not an object: {_shorten(raw_content)}")
        raise ValueError(f"{model} returned JSON that isn't an object.")
    return parsed


def ask_llm_json(prompt, purpose, validate, timeout=OPENROUTER_TIMEOUT, models=()):
    """call_openrouter_json(), retried up to OPENROUTER_ATTEMPTS times until
    `validate(parsed)` accepts the answer (it raises ValueError/KeyError on
    an unusable one) and returns what the caller needs from it. Raises the
    last error if every attempt fails.

    `models` are preferred models, in order: each attempt moves on to the
    next one, and once they're used up, attempts go to OPENROUTER_MODEL. So
    a preferred model that's down, slow or answering badly costs one try."""
    for attempt in range(1, OPENROUTER_ATTEMPTS + 1):
        model = models[attempt - 1] if attempt <= len(models) else None
        try:
            return validate(call_openrouter_json(prompt, purpose, timeout, model))
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == OPENROUTER_ATTEMPTS:
                raise
            llm_log.info(f"Retrying ({purpose}), attempt {attempt + 1}/{OPENROUTER_ATTEMPTS}, after: {e}")


def pick_by_indices(indices, candidates, purpose):
    """Maps the 1-based `selected_indices` an AI returned onto `candidates`,
    logging (and skipping) any that are malformed, out of range or repeated."""
    if not isinstance(indices, list):
        llm_log.warning(f"'selected_indices' ({purpose}) is not a list: {_shorten(indices)}")
        return []
    picked, invalid = [], []
    for i in indices:
        if isinstance(i, int) and 0 < i <= len(candidates) and candidates[i - 1] not in picked:
            picked.append(candidates[i - 1])
        else:
            invalid.append(i)
    if invalid:
        llm_log.warning(
            f"Ignored {len(invalid)} invalid/duplicate index(es) ({purpose}) "
            f"out of 1..{len(candidates)}: {_shorten(invalid, 200)}"
        )
    return picked


def curate_artists_and_script(artists, prompt_template, station_name, description, song_count):
    """Asks the AI to pick `song_count` artists for the next block and write
    the DJ intro."""
    prompt = prompt_template.format(
        artists_list=format_artist_list(artists),
        station_name=station_name,
        description=description,
        song_count=song_count,
    )
    log.info("Asking AI to curate artists and write the script...")

    def validate(parsed_data):
        script = parsed_data.get("dj_script")
        if "selected_indices" not in parsed_data or not isinstance(script, str) or not script.strip():
            llm_log.warning(f"Curation response is missing fields: {_shorten(json.dumps(parsed_data, ensure_ascii=False))}")
            raise KeyError("AI response lacks 'selected_indices' or 'dj_script'.")
        selected = pick_by_indices(parsed_data["selected_indices"], artists, "curation")
        if not selected:
            raise ValueError("AI did not select any valid artists.")
        if len(selected) != song_count:
            llm_log.warning(f"Asked for {song_count} artist(s), AI picked {len(selected)} valid one(s)")
        return selected[:song_count], script.strip()

    return ask_llm_json(prompt, "curation", validate)


def describe_artists(artists, config):
    """One line per artist for the roster prompt: the name plus a couple of
    its track titles (with years), their tagged genre and the folder they're
    in (e.g. "Eurobeat/..."), which tells the AI far more about an obscure
    artist than its name alone."""
    clause, params = track_filter_clause(config)
    conn = sqlite3.connect(DB_FILE)
    lines = []
    for artist in artists:
        rows = conn.execute(f"""
            SELECT filepath, title, year, genre FROM tracks
            WHERE {clause} AND artist = ?
            ORDER BY RANDOM() LIMIT 2
        """, params + [artist]).fetchall()
        if not rows:
            lines.append(artist)
            continue
        titles = ", ".join(f'"{title}"' + (f" ({year})" if year else "") for _, title, year, _ in rows)
        genres = sorted({genre for _, _, _, genre in rows if genre})
        genre = f"; tagged genre: {'/'.join(genres)}" if genres else ""
        folder = "/".join(os.path.dirname(rows[0][0]).split(os.sep)[-2:])
        lines.append(f"{artist} (e.g. {titles}{genre}; folder: {folder})")
    conn.close()
    return lines


def classify_artists(candidates, config):
    """Asks the AI, ROSTER_BATCH_SIZE artists at a time, which candidates
    fit the station's theme, ROSTER_VOTES times per batch; an artist fits if
    a majority of the answers picked it. Returns (fitting, non_fitting);
    candidates of a batch that got fewer than two usable answers are in
    neither, so they get asked about again next time instead of being
    decided by a single (possibly bad) answer."""
    log.info(f"Asking AI to classify {len(candidates)} artist(s) against the station's theme...")
    fitting, non_fitting = [], []
    for start in range(0, len(candidates), ROSTER_BATCH_SIZE):
        batch = candidates[start:start + ROSTER_BATCH_SIZE]
        prompt = config['roster_prompt'].format(
            artists_list=format_artist_list(describe_artists(batch, config)),
            station_name=config['name'],
            description=config['description'],
        )

        def validate(parsed_data):
            if "selected_indices" not in parsed_data:
                raise KeyError("AI response lacks 'selected_indices'.")
            return pick_by_indices(parsed_data["selected_indices"], batch, "roster")

        votes = collections.Counter()
        answers = 0
        for _ in range(ROSTER_VOTES):
            try:
                votes.update(ask_llm_json(prompt, "roster", validate, OPENROUTER_BACKGROUND_TIMEOUT))
                answers += 1
            except Exception as e:
                log.warning(f"One classification of {len(batch)} artist(s) failed ({e}).")
        if answers < 2:
            log.warning(f"Too few answers to classify {len(batch)} artist(s); they'll be retried next time.")
            continue
        majority = answers // 2 + 1
        batch_fitting = [a for a in batch if votes[a] >= majority]
        disputed = [a for a in batch if 0 < votes[a] < majority]
        log.info(
            f"Batch of {len(batch)}: {len(batch_fitting)} fit by majority of {answers} answers"
            + (f"; outvoted: {', '.join(disputed)}" if disputed else "")
        )
        fitting.extend(batch_fitting)
        non_fitting.extend(a for a in batch if a not in batch_fitting)
    log.info(
        f"Classified {len(fitting) + len(non_fitting)} of {len(candidates)} artist(s): "
        f"{len(fitting)} fit, {len(non_fitting)} don't"
    )
    return fitting, non_fitting


def update_roster(station_id, config, current_artists, rebuild=False, classify=True):
    """Loads this station's artist roster (allowed + banned) and brings it
    up to date with the library, saving any change. Every artist ever seen
    ends up in either "allowed" or "banned", so only new ones get classified
    and this stays cheap once the roster has caught up. rebuild=True
    classifies every artist from scratch instead; classify=False skips
    classifying (for callers that can't wait for the AI).

    Some stations don't want AI curation at all (use_ai_roster=false), e.g.
    a folder that's already a dedicated, niche collection, where AI filtering
    could wrongly exclude legitimate artists it doesn't recognize. Those just
    get everything under folder_filter as "allowed"."""
    roster = {"allowed": [], "banned": []} if rebuild else load_artist_roster(station_id)

    if not config.get('use_ai_roster', True):
        if set(roster["allowed"]) != set(current_artists) or roster["banned"]:
            roster["allowed"], roster["banned"] = current_artists, []
            save_artist_roster(station_id, roster)
        return roster

    known = set(roster["allowed"]) | set(roster["banned"])
    new_artists = [a for a in current_artists if a not in known]
    if not new_artists or not classify:
        return roster
    if known:
        log.info(f"Found {len(new_artists)} new artist(s) in the library. Checking if they fit '{config['name']}'...")
    else:
        log.info(f"Building the artist roster for '{station_id}' from the whole library...")
    fitting, non_fitting = classify_artists(new_artists, config)
    if fitting or non_fitting:
        roster["allowed"].extend(fitting)
        roster["banned"].extend(non_fitting)
        save_artist_roster(station_id, roster)
        if fitting and known:
            log.info(f"Added to roster: {', '.join(fitting)}")
    return roster


def finish_speech_audio(path):
    """Re-encodes Edge TTS output in place so it plays well between songs:

    - Edge TTS outputs mono MP3s, but some Liquidsoap decoders (e.g. 1.4.x)
      require the file's channel count to exactly match
      `frame.audio.channels` (2 by default) and refuse to play anything
      else, so the channel is duplicated to stereo.
    - Its speech is around -20 LUFS, far quieter than most mastered music
      (around -8), so it's normalized to SPEECH_LOUDNESS_LUFS."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # Measure, then raise the level by the difference, with a limiter
    # catching the few peaks that would clip (loudnorm alone won't raise
    # speech that far without exceeding its peak limit).
    measured = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    loudness = float(json.loads(measured[measured.rindex("{"):measured.rindex("}") + 1])["input_i"])
    gain = SPEECH_LOUDNESS_LUFS - loudness
    tmp_path = f"{path}.finished.tmp.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-i", path, "-af", f"volume={gain:.1f}dB,alimiter=limit=0.84:level=false",
         "-ar", "44100", "-ac", "2", "-b:a", "128k", tmp_path],
        check=True, capture_output=True,
    )
    os.replace(tmp_path, path)


async def generate_audio(text, output_file, voice):
    """Converts the generated text to speech using Edge TTS (an online
    service), retrying a few times. Returns whether it succeeded, so a
    network hiccup costs the block its intro rather than the whole block."""
    log.info(f"Generating intro with voice '{voice}': {text}")
    attempts = len(TTS_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_file)
            finish_speech_audio(output_file)
            return True
        except Exception as e:
            log.warning(f"Intro speech synthesis failed (attempt {attempt}/{attempts}): {e!r}")
            if attempt < attempts:
                await asyncio.sleep(TTS_RETRY_DELAYS[attempt - 1])
    log.error("Couldn't synthesize the intro; the block goes on air without it.")
    return False

def track_duration(path):
    """Playback duration of an audio file in seconds, or 0 if it can't be
    read (e.g. briefly missing/locked) rather than crashing the loop."""
    try:
        return MutagenFile(path).info.length
    except Exception as e:
        log.warning(f"Could not read duration of {path}: {e}")
        return 0.0


def block_duration(file_paths):
    """Total playback duration (seconds) of a block."""
    return sum(track_duration(path) for path in file_paths)


def refresh_fallback_list(station_id):
    """(Re)writes the fallback list radio.liq falls back to when the block
    queue runs dry, if it's missing or older than FALLBACK_REFRESH_HOURS:
    one random track each from up to FALLBACK_TRACKS random roster artists,
    so even a stall stays on-theme instead of going silent."""
    path = get_fallback_file(station_id)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < FALLBACK_REFRESH_HOURS * 3600:
        return
    config = load_station_config(station_id)
    # Only roster artists with a track the station may play (e.g. within
    # its years), so the list isn't left short.
    playable = set(get_all_artists(config))
    artists = [a for a in load_artist_roster(station_id)["allowed"] if a in playable]
    if not artists:
        return  # no roster yet; the first block builds it
    tracks = []
    for artist in random.sample(artists, min(len(artists), FALLBACK_TRACKS)):
        track = get_track_by_artist(artist, config)
        if track:
            tracks.append(track[0])
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(f"{track}\n" for track in tracks)
    os.replace(tmp_path, path)  # atomic, so Liquidsoap never reads half a file
    log.info(f"Refreshed the fallback list: {len(tracks)} track(s)")


class QueueState:
    """What's left in Liquidsoap's block queue: how many tracks are still
    waiting to play, and roughly how many seconds of music that is,
    including the rest of the current track."""

    def __init__(self, player, durations):
        queued = player.queue()
        on_air = set(player.on_air())
        pending = [rid for rid in queued if rid not in on_air]
        seconds = player.remaining()
        paths = set()
        for rid in pending:
            # Keyed by path, not request ID: IDs start over when Liquidsoap
            # restarts.
            path = player.request_path(rid)
            paths.add(path)
            if path not in durations:
                durations[path] = track_duration(path)
            seconds += durations[path]
        for path in set(durations) - paths:
            del durations[path]
        self.pending_tracks = len(pending)
        self.seconds_left = seconds


def queue_block(player, files, station_name):
    """Pushes a block's files onto Liquidsoap's queue, in play order. The
    DJ intro has no tags of its own, so it's given the station's name as
    artist and "DJ" as title for the stream's "now playing"."""
    for path in files:
        is_intro = os.path.basename(os.path.dirname(path)) == INTROS_DIR_NAME
        player.push(path, {"artist": station_name, "title": "DJ"} if is_intro else None)


async def run_station(station_id):
    """Generates one block for a station. Returns the absolute paths of its
    audio files in play order (intro first), or None if it couldn't."""
    config = load_station_config(station_id)
    dj_audio_file = new_intro_audio_file(station_id)
    song_count = config.get('songs_per_block', DEFAULT_SONGS_PER_BLOCK)

    # 1. Load this station's artist roster. New artists get classified in
    # the background after each library scan (see scanner_loop), so a slow
    # AI never holds up a block.
    current_artists = get_all_artists(config)
    if not current_artists:
        log.error(f"No artists found matching filter: {config['folder_filter']}!")
        return
    roster = update_roster(station_id, config, current_artists, classify=False)
    if not roster["allowed"] and not roster["banned"]:
        # Not classified yet (a new station, or the AI is down); play from
        # the whole library meanwhile, without saving that as the roster.
        log.warning("No artist roster yet; using every artist in the library for this block.")
        roster = {"allowed": current_artists, "banned": []}

    if not roster["allowed"]:
        log.error(f"Artist roster for '{station_id}' is empty. Nothing to play.")
        return

    log.info(f"Preparing a new block ({len(roster['allowed'])} artists in roster)")

    # 2. Offer the AI a random subset of the roster for this block (keeps the
    # prompt small), leaving out artists still in cooldown so the same names
    # don't repeat every couple of blocks. A roster too small to fill the
    # cooldown gap just ignores it for this round rather than stalling.
    cooldown_fraction = config.get('artist_cooldown_fraction', DEFAULT_ARTIST_COOLDOWN_FRACTION)
    # Only artists with a track the station may play right now: the roster
    # keeps every artist ever allowed, including ones whose files have since
    # gone (or that were added by hand), or with no track in the station's
    # years, and those have no track to play.
    in_library = set(current_artists)
    playable = [a for a in roster['allowed'] if a in in_library]
    if len(playable) < len(roster['allowed']):
        gone = [a for a in roster['allowed'] if a not in in_library]
        log.info(f"Skipping {len(gone)} roster artist(s) with nothing to play here: {', '.join(gone[:10])}")
    if not playable:
        log.error(f"None of the {len(roster['allowed'])} roster artists are in the library. Nothing to play.")
        return
    cooldown = artist_cooldown(len(playable), cooldown_fraction)
    history = load_recent_artists(station_id)

    # Leave out artists all of whose tracks played too recently (see
    # DEFAULT_TRACK_COOLDOWN_HOURS), e.g. one with a single track that
    # already played today. If that leaves too few, the track cooldown is
    # ignored, then the artist cooldown too.
    playable_set = set(playable)
    tracks_by_artist = {}
    for track in get_station_tracks(config):
        if track[1] in playable_set:
            tracks_by_artist.setdefault(track[1], []).append(track)
    track_cooldown = track_cooldown_seconds(config, [t for ts in tracks_by_artist.values() for t in ts])
    cutoff = time.time() - track_cooldown
    last_plays = get_last_plays(station_id)
    fresh_artists = [
        a for a in playable
        if any(last_plays.get(t[0], 0) < cutoff for t in tracks_by_artist.get(a, []))
    ]
    eligible_artists = filter_by_cooldown(fresh_artists, history, cooldown)
    if len(eligible_artists) < song_count:
        log.info("Too few artists with tracks outside the track cooldown; ignoring it this time.")
        eligible_artists = filter_by_cooldown(playable, history, cooldown)
    if len(eligible_artists) < song_count:
        eligible_artists = playable
    artists_pool = random.sample(eligible_artists, min(len(eligible_artists), 25))
    log.info(
        f"Offering {len(artists_pool)} artist(s) to the AI ({len(eligible_artists)} eligible; "
        f"artist cooldown {cooldown} song(s), track cooldown {track_cooldown / 3600:.1f}h "
        f"leaves {len(fresh_artists)} of {len(playable)} artists)"
    )

    # 3. Ask AI to pick best artists and write intro using prompt from config
    try:
        selected_artists, dj_text = curate_artists_and_script(
            artists_pool, config['prompt'], config['name'], config['description'], song_count
        )
    except Exception as e:
        log.warning(f"Error during AI curation: {e}. Falling back to random selection and the fallback script.")
        selected_artists = random.sample(artists_pool, min(len(artists_pool), song_count))
        # Use fallback script defined in JSON config, or default English text if missing
        dj_text = config.get('fallback_script', "Coming up next, some great music on our station.")

    save_recent_artists(station_id, history + selected_artists)

    # 4. Pick a track for each selected artist, avoiding recently played ones
    selected_tracks = []
    for artist in selected_artists:
        artist_tracks = tracks_by_artist.get(artist)
        if not artist_tracks:
            log.warning(f"No track found in the library for artist '{artist}'")
            continue
        path, _, title, _ = track = pick_track(artist_tracks, last_plays, cutoff)
        if last_plays.get(path, 0) >= cutoff:
            ago = (time.time() - last_plays[path]) / 3600
            log.info(f"Every track by '{artist}' played recently; repeating the oldest, '{title}' ({ago:.1f}h ago)")
        selected_tracks.append(track[:3])

    if not selected_tracks:
        log.error("Could not retrieve tracks for selected artists.")
        return

    intro = [os.path.abspath(dj_audio_file)] if await generate_audio(dj_text, dj_audio_file, config['voice']) else []
    files = intro + [track[0] for track in selected_tracks]
    log.info(
        f"Block ready (~{block_duration(files) / 60:.1f} min): {'intro + ' if intro else 'no intro, '}"
        + " | ".join(f"{artist} - {title}" for _, artist, title in selected_tracks),
        extra=SUMMARY,
    )
    for i, (path, artist, title) in enumerate(selected_tracks, 1):
        log.info(f"  {i}. {artist} - {title} [{path}]")
    return files


def fill_missing_track_info(station_id):
    """For a station limited to certain years, asks the AI for the release
    year (and genre) of its roster's tracks that have none in their tags, so
    they can be filtered too. Each track is asked about once; a batch the AI
    couldn't answer usably is retried next time."""
    config = load_station_config(station_id)
    if not config.get('years'):
        return
    allowed = set(load_artist_roster(station_id)["allowed"])
    clause, params = track_filter_clause(config, use_years=False)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    rows = [
        row for row in conn.execute(f"""
            SELECT filepath, artist, title, album FROM tracks
            WHERE {clause} AND (year IS NULL OR genre IS NULL) AND ai_info_checked = 0
        """, params).fetchall()
        if row[1] in allowed
    ]
    if rows:
        log.info(f"Asking AI for the release year/genre of {len(rows)} track(s) missing them in their tags...")
    current_year = time.localtime().tm_year
    filled = 0
    for start in range(0, len(rows), TRACK_INFO_BATCH_SIZE):
        batch = rows[start:start + TRACK_INFO_BATCH_SIZE]
        listing = "\n".join(
            f'{i}. {artist} - "{title}" (album: {album})' for i, (_, artist, title, album) in enumerate(batch, 1)
        )
        prompt = (
            "For each of these music tracks, give the year it was first released (the original release, not "
            "a later remaster or compilation) and its main genre. Use null for anything you don't know; "
            "don't guess.\n\n" + listing + "\n\nYou MUST respond strictly in valid JSON format with no "
            'markdown formatting around it, structured like this:\n{"tracks": [{"index": 1, "year": 1984, '
            '"genre": "Heavy Metal"}, {"index": 2, "year": null, "genre": null}]}'
        )

        def validate(parsed_data):
            answers = {}
            for item in parsed_data.get("tracks") or []:
                if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                    continue
                if not 0 < item["index"] <= len(batch):
                    continue
                year, genre = item.get("year"), item.get("genre")
                year = year if isinstance(year, int) and 1800 <= year <= current_year else None
                genre = genre.strip() if isinstance(genre, str) and genre.strip() else None
                answers[item["index"]] = (year, genre)
            if not answers:
                raise ValueError("AI answered about none of the tracks.")
            return answers

        try:
            answers = ask_llm_json(prompt, "track info", validate, OPENROUTER_BACKGROUND_TIMEOUT)
        except Exception as e:
            log.warning(f"Couldn't get track info for {len(batch)} track(s) ({e}); will retry next time.")
            continue
        for i, (path, _, _, _) in enumerate(batch, 1):
            year, genre = answers.get(i, (None, None))
            filled += year is not None
            conn.execute(
                "UPDATE tracks SET year = COALESCE(year, ?), genre = COALESCE(genre, ?), ai_info_checked = 1 "
                "WHERE filepath = ?",
                (year, genre, path),
            )
        conn.commit()
    conn.close()
    if rows:
        log.info(f"AI filled in the release year of {filled} of {len(rows)} track(s).")


def station_language(config):
    """The locale of the station's TTS voice, e.g. "pl-PL" for
    "pl-PL-MarekNeural"; tells the AI which language to write news in."""
    return "-".join(config['voice'].split("-")[:2])


def get_news_stories(settings, language, hour):
    """This hour's news stories for these settings. The first station to
    ask has the AI write them (see news.build_stories) while holding a lock;
    stations with the same settings then reuse them from NEWS_CACHE_DIR."""
    os.makedirs(NEWS_CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(news.cache_key(settings, language).encode("utf-8")).hexdigest()[:12]
    base = os.path.join(NEWS_CACHE_DIR, f"{hour:%Y%m%d-%H}-{digest}")
    with open(base + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(base + ".json"):
            log.info("Using this hour's news stories already written for another station.")
            with open(base + ".json", encoding="utf-8") as f:
                return json.load(f)

        def ask_json(prompt, purpose, validate, models=()):
            return ask_llm_json(prompt, purpose, validate, OPENROUTER_BACKGROUND_TIMEOUT, models)

        # The last few hours' bulletins (same settings), so they aren't
        # repeated hour after hour.
        recent = []
        for hours_ago in range(1, settings['avoid_repeat_hours'] + 1):
            earlier = hour - datetime.timedelta(hours=hours_ago)
            path = os.path.join(NEWS_CACHE_DIR, f"{earlier:%Y%m%d-%H}-{digest}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    recent.extend(json.load(f))
        stories = news.build_stories(settings, language, ask_json, recent)
        with open(base + ".json.tmp", "w", encoding="utf-8") as f:
            json.dump(stories, f, ensure_ascii=False, indent=1)
        os.replace(base + ".json.tmp", base + ".json")
    # Drop other hours' stories (and locks) older than a day.
    for name in os.listdir(NEWS_CACHE_DIR):
        path = os.path.join(NEWS_CACHE_DIR, name)
        if time.time() - os.path.getmtime(path) > 86400:
            os.remove(path)
    return stories


async def prepare_news_segment(station_id, config, settings, hour):
    """Writes and synthesizes the news segment for `hour` (a datetime on the
    hour). Returns the audio file's path, or None if it couldn't be made."""
    loop = asyncio.get_event_loop()
    try:
        stories = await loop.run_in_executor(
            None, get_news_stories, settings, station_language(config), hour
        )
    except Exception as e:
        log.error(f"Couldn't prepare the {hour:%H:%M} news: {e}")
        return None
    script = news.assemble_script(stories, settings, config['name'], hour.hour)
    log.info(f"News for {hour:%H:%M} ({len(script.split())} words): {script}")
    news_dir = os.path.join(get_station_dir(station_id), NEWS_DIR_NAME)
    os.makedirs(news_dir, exist_ok=True)
    for name in sorted(f for f in os.listdir(news_dir) if f.endswith(".mp3"))[:-3]:
        os.remove(os.path.join(news_dir, name))
    path = os.path.abspath(os.path.join(news_dir, f"{hour:%Y%m%d-%H}.mp3"))
    if not await generate_audio(script, path, config['voice']):
        return None
    return path


def queue_news(player, path, config, settings):
    """Queues a news segment: radio.liq plays it as soon as the current
    track ends, then carries on with the next track."""
    player.push(path, {"artist": config['name'], "title": settings['title']}, queue=NEWS_QUEUE_ID)
    log.info(
        f"News queued ({track_duration(path) / 60:.1f} min); it starts when the current track ends, "
        f"in ~{player.remaining():.0f}s.",
        extra=SUMMARY,
    )


async def news_loop(station_id):
    """For a station with news sources (see news.news_settings), prepares a
    news segment NEWS_PREPARE_MINUTES before every full hour and queues it
    NEWS_QUEUE_MINUTES before, so it plays at the first track boundary from
    then on. Re-reads the station's settings every time, so news can be
    switched on or off without a restart."""
    player = Liquidsoap(get_socket_file(station_id))
    while True:
        now = datetime.datetime.now()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        prepare_at = next_hour - datetime.timedelta(minutes=NEWS_PREPARE_MINUTES)
        if now < prepare_at:
            await asyncio.sleep(min((prepare_at - now).total_seconds(), 600))
            continue
        try:
            config = load_station_config(station_id)
            settings = news.news_settings(config)
            path = settings and await prepare_news_segment(station_id, config, settings, next_hour)
            queue_at = next_hour - datetime.timedelta(minutes=NEWS_QUEUE_MINUTES)
            await asyncio.sleep(max(0.0, (queue_at - datetime.datetime.now()).total_seconds()))
            late = (datetime.datetime.now() - next_hour).total_seconds() / 60
            if path and late > NEWS_MAX_LATE_MINUTES:
                log.warning(f"The {next_hour:%H:%M} news was ready {late:.0f} min late; skipping it.")
            elif path:
                await loop_run(queue_news, player, path, config, settings)
        except OSError as e:
            log.error(f"Couldn't put the news on air, Liquidsoap unreachable: {e}")
        except Exception:
            log.exception("News segment failed")
        await asyncio.sleep(max(1.0, (next_hour - datetime.datetime.now()).total_seconds() + 1))


async def loop_run(func, *args):
    """Runs a blocking function in the default executor."""
    return await asyncio.get_event_loop().run_in_executor(None, func, *args)


def run_scanner_once():
    """Runs scanner.py to (re)index the music library. Several stations may
    each run their own --loop process against the same music_library.db;
    scanner.py itself makes sure only one scan runs at a time."""
    log.info("Scanning music library in the background...")
    result = subprocess.run([sys.executable, "scanner.py"], check=False)
    if result.returncode != 0:
        log.error(f"scanner.py exited with code {result.returncode}")


def refresh_roster(station_id):
    """Classifies artists that are new in the library (or, for a new
    station, builds its roster)."""
    config = load_station_config(station_id)
    update_roster(station_id, config, get_all_artists(config))


async def scanner_loop(station_id):
    """Scans once immediately, then every SCAN_INTERVAL_HOURS (if > 0), each
    time followed by classifying new artists into the station's roster and
    filling in missing track info the station needs."""
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, run_scanner_once)
        for task in (refresh_roster, fill_missing_track_info):
            try:
                await loop.run_in_executor(None, task, station_id)
            except Exception:
                log.exception(f"Background task {task.__name__} failed")
        if SCAN_INTERVAL_HOURS <= 0:
            return
        await asyncio.sleep(SCAN_INTERVAL_HOURS * 3600)


async def feed_player(station_id):
    """Keeps Liquidsoap's block queue topped up: whenever the last queued
    track starts playing (or less than BLOCK_LEAD_TIME seconds of music are
    left), generates the next block and queues it behind. Queued tracks
    play exactly once, in order, and never cut off what's playing, however
    long the AI takes; if it takes longer than what's left, radio.liq bridges
    the gap with tracks from the fallback list."""
    player = Liquidsoap(get_socket_file(station_id))
    loop = asyncio.get_event_loop()
    durations = {}  # file path -> seconds, cached while the files are queued
    unreachable_since = None

    while True:
        try:
            await loop.run_in_executor(None, refresh_fallback_list, station_id)
            state = await loop.run_in_executor(None, QueueState, player, durations)
        except OSError as e:
            if unreachable_since is None:
                unreachable_since = time.time()
                log.warning(f"Can't reach Liquidsoap at {player.socket_path} ({e}); retrying until it's up.")
            await asyncio.sleep(QUEUE_POLL_INTERVAL)
            continue
        if unreachable_since is not None:
            log.info(f"Liquidsoap is reachable again (after {time.time() - unreachable_since:.0f}s).")
            unreachable_since = None

        if state.pending_tracks and state.seconds_left >= BLOCK_LEAD_TIME:
            await asyncio.sleep(QUEUE_POLL_INTERVAL)
            continue

        log.info(
            f"{state.pending_tracks} track(s) and ~{state.seconds_left:.0f}s of music left in the queue; "
            "preparing the next block."
        )
        try:
            files = await run_station(station_id)
        except Exception:
            # Anything unexpected (a library or network error...) shouldn't
            # take the whole agent down; Liquidsoap keeps playing meanwhile.
            log.exception("Unexpected error while preparing a block")
            files = None
        if not files:
            log.error(f"Block generation failed; retrying in {BLOCK_RETRY_DELAY}s.")
            await asyncio.sleep(BLOCK_RETRY_DELAY)
            continue
        try:
            station_name = load_station_config(station_id)['name']
            await loop.run_in_executor(None, queue_block, player, files, station_name)
            log.info(f"Queued the block ({len(files)} files).")
        except OSError as e:
            log.error(f"Couldn't queue the block, Liquidsoap unreachable ({e}); it will be regenerated.")
            await asyncio.sleep(QUEUE_POLL_INTERVAL)


async def main():
    flags = {"--loop", "--rebuild-roster", "--news-now"}
    args = [a for a in sys.argv[1:] if a not in flags]
    loop_mode = "--loop" in sys.argv[1:]

    if len(args) < 1:
        print("Usage: python dj_agent.py <station_id> [--loop | --rebuild-roster | --news-now]")
        return

    station_id = args[0]
    load_station_config(station_id)  # fail fast on an unknown station ID
    setup_logging(station_id, get_log_dir(station_id))
    ensure_station_dir(station_id)

    if "--rebuild-roster" in sys.argv[1:]:
        # Reclassify every artist from scratch. Stop the station's agent
        # first, or it may save its own (old) roster over this one; Liquidsoap
        # keeps playing meanwhile.
        config = load_station_config(station_id)
        roster = update_roster(station_id, config, get_all_artists(config), rebuild=True)
        log.info(f"Rebuilt roster: {len(roster['allowed'])} allowed, {len(roster['banned'])} banned", extra=SUMMARY)
        # The fallback list was drawn from the old roster; the agent
        # regenerates it from the new one.
        if os.path.exists(get_fallback_file(station_id)):
            os.remove(get_fallback_file(station_id))
        return

    if "--news-now" in sys.argv[1:]:
        # Prepare this hour's news segment and queue it right away (it plays
        # when the current track ends).
        config = load_station_config(station_id)
        settings = news.news_settings(config)
        if not settings:
            log.error("This station has no news sources configured.")
            return
        hour = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
        path = await prepare_news_segment(station_id, config, settings, hour)
        if path:
            queue_news(Liquidsoap(get_socket_file(station_id)), path, config, settings)
        return

    if not loop_mode:
        # One-shot: generate a single block and queue it if Liquidsoap is up.
        files = await run_station(station_id)
        if files:
            try:
                queue_block(Liquidsoap(get_socket_file(station_id)), files, load_station_config(station_id)['name'])
                log.info("Queued the block.")
            except OSError as e:
                log.warning(f"Block generated but not queued, Liquidsoap unreachable: {e}")
        return

    log.info("DJ agent started", extra=SUMMARY)

    # Forward Liquidsoap's log (radio.liq writes it to a spool file in the
    # station's log folder) into this station's logs, and record every track
    # it starts playing for the track cooldown.
    init_play_history()
    LiquidsoapLogForwarder(
        os.path.join(get_log_dir(station_id), LIQUIDSOAP_SPOOL_FILE), describe_track,
        on_track_start=lambda metadata, played_at: record_play(station_id, metadata, played_at),
    ).start()

    # Scan the library in the background: once now, then periodically. Runs
    # independently of block generation, so it never delays playback.
    asyncio.create_task(scanner_loop(station_id))
    # Hourly news segments, if the station has news sources.
    asyncio.create_task(news_loop(station_id))

    await feed_player(station_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        # Record the crash in the log files too, not just on stderr, before
        # letting it take the process down (systemd restarts it).
        log.exception("DJ agent crashed")
        raise
