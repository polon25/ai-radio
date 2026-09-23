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
import edge_tts
import imageio_ffmpeg
from mutagen import File as MutagenFile
from dotenv import load_dotenv

# Load environment variables from .env file (before importing radio_log,
# which reads its settings from the environment too)
load_dotenv()

from liquidsoap_client import Liquidsoap
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
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "60"))
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
# Every station keeps its own runtime files (intros, roster, play history,
# fallback list, logs) in STATIONS_DIR/<station_id>/, so they don't clutter
# the project root or clash with other stations.
STATIONS_DIR = "stations"
INTROS_DIR_NAME = "intros"


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(roster, f, ensure_ascii=False, indent=2)
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
    if os.path.basename(os.path.dirname(path)) == INTROS_DIR_NAME:
        return "DJ intro"
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

def folder_filter_clause(folder_filter):
    """folder_filter may be a single SQL LIKE pattern or a list of patterns
    (OR'd together), so a station can pull from several library folders at
    once. Returns (sql_clause, params)."""
    patterns = folder_filter if isinstance(folder_filter, list) else [folder_filter]
    clause = " OR ".join(["filepath LIKE ?"] * len(patterns))
    return clause, patterns


def get_all_artists(folder_filter):
    """Fetches every unique artist in the library matching the folder filter."""
    clause, params = folder_filter_clause(folder_filter)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT DISTINCT artist FROM tracks
        WHERE ({clause}) AND artist != 'Unknown Artist'
    """, params)
    artists = [row[0] for row in c.fetchall()]
    conn.close()
    return artists

def get_track_by_artist(artist, folder_filter):
    """Fetches a random track for a specific artist."""
    clause, params = folder_filter_clause(folder_filter)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""
        SELECT filepath, artist, title FROM tracks
        WHERE ({clause}) AND artist = ?
        ORDER BY RANDOM() LIMIT 1
    """, params + [artist])
    track = c.fetchone()
    conn.close()
    return track

def format_artist_list(artists):
    """Numbers an artist list for injection into a prompt (1-indexed, to
    match the `selected_indices` the AI is asked to return)."""
    return "\n".join([f"{i+1}. {artist}" for i, artist in enumerate(artists)])


def _shorten(text, limit=500):
    """Trims long API payloads so a single bad response can't flood the log."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def call_openrouter_json(prompt, purpose):
    """Sends a prompt to OpenRouter and parses the response as JSON. The
    prompt must instruct the model to reply with raw JSON. `purpose` only
    labels the request in the logs. Every failure is logged (under the "llm"
    component) before being raised."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing. Check your .env file.")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": "AI Radio Project"
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise radio automation agent. You always output valid raw JSON."},
            {"role": "user", "content": prompt}
        ]
    }

    llm_log.info(f"Request ({purpose}), model {payload['model']}, prompt {len(prompt)} chars")
    started = time.monotonic()
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=OPENROUTER_TIMEOUT,
        )
    except requests.RequestException as e:
        llm_log.warning(f"Request ({purpose}) failed after {time.monotonic() - started:.1f}s: {e}")
        raise
    elapsed = time.monotonic() - started

    try:
        result = response.json()
    except Exception as e:
        llm_log.warning(
            f"Response ({purpose}) is not JSON: HTTP {response.status_code} after {elapsed:.1f}s: "
            f"{_shorten(response.text)}"
        )
        raise ValueError(f"Failed to parse API response. Status: {response.status_code}, Text: {response.text}")

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
    # anyway, so parse from the first "{" to the last "}". strict=False
    # accepts raw newlines inside strings (e.g. a multi-line DJ script).
    start, end = raw_content.find("{"), raw_content.rfind("}")
    json_text = raw_content[start:end + 1] if 0 <= start < end else raw_content
    try:
        parsed = json.loads(json_text, strict=False)
    except json.JSONDecodeError as e:
        llm_log.warning(f"Invalid JSON ({purpose}) from {model}: {e}: {_shorten(raw_content)}")
        raise
    if not isinstance(parsed, dict):
        llm_log.warning(f"JSON ({purpose}) from {model} is not an object: {_shorten(raw_content)}")
        raise ValueError(f"{model} returned JSON that isn't an object.")
    return parsed


def ask_llm_json(prompt, purpose, validate):
    """call_openrouter_json(), retried up to OPENROUTER_ATTEMPTS times until
    `validate(parsed)` accepts the answer (it raises ValueError/KeyError on
    an unusable one) and returns what the caller needs from it. Raises the
    last error if every attempt fails."""
    for attempt in range(1, OPENROUTER_ATTEMPTS + 1):
        try:
            return validate(call_openrouter_json(prompt, purpose))
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


def classify_artists(candidates, prompt_template, station_name, description):
    """Asks the AI which of the candidate artists fit the station's theme.
    Returns (fitting, non_fitting) — candidates not selected are considered a
    "no", so the caller can ban them and never ask about them again."""
    prompt = prompt_template.format(
        artists_list=format_artist_list(candidates),
        station_name=station_name,
        description=description,
    )
    log.info(f"Asking AI to classify {len(candidates)} artist(s) against the station's theme...")

    def validate(parsed_data):
        if "selected_indices" not in parsed_data:
            raise KeyError("AI response lacks 'selected_indices'.")
        return pick_by_indices(parsed_data["selected_indices"], candidates, "roster")

    fitting = ask_llm_json(prompt, "roster", validate)
    non_fitting = [a for a in candidates if a not in fitting]
    return fitting, non_fitting

def ensure_stereo(path):
    """Edge TTS outputs mono MP3s, but some Liquidsoap decoders (e.g. 1.4.x)
    require the file's channel count to exactly match `frame.audio.channels`
    (2 by default) and refuse to play anything else. Re-encode in place with
    the channel duplicated to stereo so it can actually be played."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tmp_path = f"{path}.stereo.tmp.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-i", path, "-ac", "2", tmp_path],
        check=True, capture_output=True,
    )
    os.replace(tmp_path, path)


async def generate_audio(text, output_file, voice):
    """Converts the generated text to speech using Edge TTS."""
    log.info(f"Generating intro with voice '{voice}': {text}")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)
    ensure_stereo(output_file)

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
    artists = load_artist_roster(station_id)["allowed"]
    if not artists:
        return  # no roster yet; the first block builds it
    config = load_station_config(station_id)
    tracks = []
    for artist in random.sample(artists, min(len(artists), FALLBACK_TRACKS)):
        track = get_track_by_artist(artist, config['folder_filter'])
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
            path = player.metadata(rid).get("filename", "")
            paths.add(path)
            if path not in durations:
                durations[path] = track_duration(path)
            seconds += durations[path]
        for path in set(durations) - paths:
            del durations[path]
        self.pending_tracks = len(pending)
        self.seconds_left = seconds


def queue_block(player, files):
    """Pushes a block's files onto Liquidsoap's queue, in play order."""
    for path in files:
        player.push(path)


async def run_station(station_id):
    """Generates one block for a station. Returns the absolute paths of its
    audio files in play order (intro first), or None if it couldn't."""
    config = load_station_config(station_id)
    dj_audio_file = new_intro_audio_file(station_id)
    song_count = config.get('songs_per_block', DEFAULT_SONGS_PER_BLOCK)
    use_ai_roster = config.get('use_ai_roster', True)

    # 1. Load this station's artist roster (allowed + banned). Some stations
    # don't want AI curation at all (use_ai_roster=false) — e.g. a folder
    # that's already a dedicated, niche collection, where AI filtering could
    # wrongly exclude legitimate artists it doesn't recognize. Those just get
    # everything under folder_filter as "allowed".
    roster = load_artist_roster(station_id)
    current_artists = get_all_artists(config['folder_filter'])

    if not use_ai_roster:
        if set(roster["allowed"]) != set(current_artists) or roster["banned"]:
            roster["allowed"], roster["banned"] = current_artists, []
            save_artist_roster(station_id, roster)
    elif not roster["allowed"] and not roster["banned"]:
        log.info(f"No artist roster found for '{station_id}'. Building one from the whole library...")
        if not current_artists:
            log.error(f"No artists found matching filter: {config['folder_filter']}!")
            return
        try:
            fitting, non_fitting = classify_artists(
                current_artists, config['roster_prompt'], config['name'], config['description']
            )
            if not fitting:
                raise ValueError("AI did not select any artists for the roster.")
            roster["allowed"], roster["banned"] = fitting, non_fitting
        except Exception as e:
            log.warning(f"Error building artist roster: {e}. Using the full candidate pool instead.")
            roster["allowed"] = current_artists
        save_artist_roster(station_id, roster)
    else:
        # 1b. Check whether new artists have shown up in the library (e.g. a
        # rescan) since the roster was last built, and classify only those —
        # every artist we've ever seen ends up in either "allowed" or
        # "banned", so this stays cheap once the roster has caught up.
        known = set(roster["allowed"]) | set(roster["banned"])
        new_artists = [a for a in current_artists if a not in known]
        if new_artists:
            log.info(f"Found {len(new_artists)} new artist(s) in the library. Checking if they fit '{config['name']}'...")
            try:
                fitting, non_fitting = classify_artists(
                    new_artists, config['roster_prompt'], config['name'], config['description']
                )
                roster["allowed"].extend(fitting)
                roster["banned"].extend(non_fitting)
                save_artist_roster(station_id, roster)
                if fitting:
                    log.info(f"Added to roster: {', '.join(fitting)}")
            except Exception as e:
                log.warning(f"Error classifying new artists: {e}. Will retry next run.")

    if not roster["allowed"]:
        log.error(f"Artist roster for '{station_id}' is empty. Nothing to play.")
        return

    log.info(f"Preparing a new block ({len(roster['allowed'])} artists in roster)")

    # 2. Offer the AI a random subset of the roster for this block (keeps the
    # prompt small), leaving out artists still in cooldown so the same names
    # don't repeat every couple of blocks. A roster too small to fill the
    # cooldown gap just ignores it for this round rather than stalling.
    cooldown_fraction = config.get('artist_cooldown_fraction', DEFAULT_ARTIST_COOLDOWN_FRACTION)
    # Only artists that are still in the library right now: the roster keeps
    # every artist ever allowed, including ones whose files have since gone
    # (or that were added by hand), and those have no track to play.
    in_library = set(current_artists)
    playable = [a for a in roster['allowed'] if a in in_library]
    if len(playable) < len(roster['allowed']):
        gone = [a for a in roster['allowed'] if a not in in_library]
        log.info(f"Skipping {len(gone)} roster artist(s) not in the library: {', '.join(gone[:10])}")
    if not playable:
        log.error(f"None of the {len(roster['allowed'])} roster artists are in the library. Nothing to play.")
        return
    cooldown = artist_cooldown(len(playable), cooldown_fraction)
    history = load_recent_artists(station_id)
    eligible_artists = filter_by_cooldown(playable, history, cooldown)
    if len(eligible_artists) < song_count:
        eligible_artists = playable
    artists_pool = random.sample(eligible_artists, min(len(eligible_artists), 25))
    log.info(
        f"Offering {len(artists_pool)} artist(s) to the AI "
        f"({len(eligible_artists)} eligible, cooldown {cooldown} song(s))"
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

    # 4. Fetch one random track for each selected artist
    selected_tracks = []
    for artist in selected_artists:
        track = get_track_by_artist(artist, config['folder_filter'])
        if track:
            selected_tracks.append(track)
        else:
            log.warning(f"No track found in the library for artist '{artist}'")

    if not selected_tracks:
        log.error("Could not retrieve tracks for selected artists.")
        return

    await generate_audio(dj_text, dj_audio_file, config['voice'])
    files = [os.path.abspath(dj_audio_file)] + [track[0] for track in selected_tracks]
    log.info(
        f"Block ready (~{block_duration(files) / 60:.1f} min): intro + "
        + " | ".join(f"{artist} - {title}" for _, artist, title in selected_tracks),
        extra=SUMMARY,
    )
    for i, (path, artist, title) in enumerate(selected_tracks, 1):
        log.info(f"  {i}. {artist} - {title} [{path}]")
    return files


def run_scanner_once():
    """Runs scanner.py to (re)index the music library. Several stations may
    each run their own --loop process against the same music_library.db;
    scanner.py itself makes sure only one scan runs at a time."""
    log.info("Scanning music library in the background...")
    result = subprocess.run([sys.executable, "scanner.py"], check=False)
    if result.returncode != 0:
        log.error(f"scanner.py exited with code {result.returncode}")


async def scanner_loop():
    """Scans once immediately, then every SCAN_INTERVAL_HOURS (if > 0)."""
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, run_scanner_once)
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
        files = await run_station(station_id)
        if not files:
            log.error(f"Block generation failed; retrying in {BLOCK_RETRY_DELAY}s.")
            await asyncio.sleep(BLOCK_RETRY_DELAY)
            continue
        try:
            await loop.run_in_executor(None, queue_block, player, files)
            log.info(f"Queued the block ({len(files)} files).")
        except OSError as e:
            log.error(f"Couldn't queue the block, Liquidsoap unreachable ({e}); it will be regenerated.")
            await asyncio.sleep(QUEUE_POLL_INTERVAL)


async def main():
    args = [a for a in sys.argv[1:] if a != "--loop"]
    loop_mode = "--loop" in sys.argv[1:]

    if len(args) < 1:
        print("Usage: python dj_agent.py <station_id> [--loop]")
        return

    station_id = args[0]
    load_station_config(station_id)  # fail fast on an unknown station ID
    setup_logging(station_id, get_log_dir(station_id))
    ensure_station_dir(station_id)

    if not loop_mode:
        # One-shot: generate a single block and queue it if Liquidsoap is up.
        files = await run_station(station_id)
        if files:
            try:
                queue_block(Liquidsoap(get_socket_file(station_id)), files)
                log.info("Queued the block.")
            except OSError as e:
                log.warning(f"Block generated but not queued, Liquidsoap unreachable: {e}")
        return

    log.info("DJ agent started", extra=SUMMARY)

    # Forward Liquidsoap's log (radio.liq writes it to a spool file in the
    # station's log folder) into this station's logs.
    LiquidsoapLogForwarder(
        os.path.join(get_log_dir(station_id), LIQUIDSOAP_SPOOL_FILE), describe_track
    ).start()

    # Scan the library in the background: once now, then periodically. Runs
    # independently of block generation, so it never delays playback.
    asyncio.create_task(scanner_loop())

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
