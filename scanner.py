import datetime
import fcntl
import logging
import os
import re
import sqlite3
from mutagen import File # Universal File reader (instead of format-specific EasyID3), so it handles mp3/flac/ogg/etc. uniformly
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from radio_log import SUMMARY, setup_logging

log = logging.getLogger("scanner")

# Configuration
MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "./music")
DB_FILE = "music_library.db"
# Held (flock) for the duration of a scan, so several stations' agents, or a
# manual run, never scan at once. The OS releases it when the process ends,
# however it ends, so a killed scan can't leave a stale lock behind.
LOCK_FILE = "scanner.lock"
SUPPORTED_FORMATS = ('.mp3', '.wav', '.flac', '.ogg')
# Bump whenever the scanner starts reading something new from the files:
# tracks scanned by an older version are then read once more to fill it in,
# while tracks already up to date are skipped without opening the file.
SCAN_VERSION = 3
# Columns added after the table was first created, as (name, SQL type).
ADDED_COLUMNS = [
    ("duration", "REAL"),  # seconds; NULL if the file's length can't be read
    ("scan_version", "INTEGER NOT NULL DEFAULT 1"),
    ("year", "INTEGER"),  # release year, from the tags or filled in by the AI
    ("genre", "TEXT"),  # from the tags (often vague) or filled in by the AI
    # Whether dj_agent.py already asked the AI for a missing year/genre
    ("ai_info_checked", "INTEGER NOT NULL DEFAULT 0"),
]
# Commit every this many tracks, so a long (re)scan never holds the database
# for long.
COMMIT_EVERY = 200


def create_database():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''CREATE TABLE IF NOT EXISTS tracks
                 (id INTEGER PRIMARY KEY, filepath TEXT UNIQUE, artist TEXT, title TEXT, album TEXT)''')
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    for name, sql_type in ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {sql_type}")
    conn.commit()
    return conn


def parse_year(date):
    """The year in a date tag ("1984", "1984-05-01", ...), or None if there
    isn't a plausible one."""
    match = re.search(r"\b(1[89]\d\d|20\d\d)\b", date or "")
    if match and int(match.group(1)) <= datetime.date.today().year:
        return int(match.group(1))
    return None


def read_track(filepath):
    """Reads a file's tags and length. Missing tags fall back to defaults
    (the file name as the title); a missing year or genre is None."""
    artist = 'Unknown Artist'
    title = os.path.splitext(os.path.basename(filepath))[0]
    album = 'Unknown Album'
    duration = year = genre = None
    # easy=True normalizes tags across formats to a common interface
    audio = File(filepath, easy=True)
    # Overwrite defaults if the file actually has tags
    if audio is not None:
        artist = audio.get('artist', [artist])[0]
        title = audio.get('title', [title])[0]
        album = audio.get('album', [album])[0]
        year = parse_year((audio.get('originaldate') or audio.get('date') or [""])[0])
        genre = (audio.get('genre') or [""])[0].strip() or None
        if audio.info is not None and audio.info.length:
            duration = float(audio.info.length)
    return artist, title, album, duration, year, genre


def scan_folder(conn):
    added_count = updated_count = 0
    log.info(f"Scanning folder: {MUSIC_FOLDER}...")

    if not os.path.exists(MUSIC_FOLDER):
        log.error(f"Folder '{MUSIC_FOLDER}' does not exist. Check your .env file.")
        return

    known = {row[0]: row[1] for row in conn.execute("SELECT filepath, scan_version FROM tracks")}

    for root, dirs, files in os.walk(MUSIC_FOLDER):
        for file in files:
            if not file.lower().endswith(SUPPORTED_FORMATS):
                continue
            filepath = os.path.join(root, file)
            if known.get(filepath, 0) >= SCAN_VERSION:
                continue
            try:
                artist, title, album, duration, year, genre = read_track(filepath)
            except Exception as e:
                log.warning(f"Error reading {filepath}: {e}")
                continue
            # A year/genre missing from the tags keeps whatever the AI filled
            # in for it earlier.
            conn.execute(
                """INSERT INTO tracks (filepath, artist, title, album, duration, year, genre, scan_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(filepath) DO UPDATE SET
                     artist = excluded.artist, title = excluded.title, album = excluded.album,
                     duration = excluded.duration, scan_version = excluded.scan_version,
                     year = COALESCE(excluded.year, year), genre = COALESCE(excluded.genre, genre)""",
                (filepath, artist, title, album, duration, year, genre, SCAN_VERSION),
            )
            if filepath in known:
                updated_count += 1
            else:
                added_count += 1
            if (added_count + updated_count) % COMMIT_EVERY == 0:
                conn.commit()

    conn.commit()
    log.info(
        f"Done! Added {added_count} new tracks to the database, re-read {updated_count}.",
        extra=SUMMARY,
    )


if __name__ == "__main__":
    setup_logging()
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("A library scan is already in progress (elsewhere); skipping this one.")
            raise SystemExit(0)
        db_conn = create_database()
        scan_folder(db_conn)
        db_conn.close()
