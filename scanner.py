import logging
import os
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
SUPPORTED_FORMATS = ('.mp3', '.wav', '.flac', '.ogg')

def create_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tracks 
                 (id INTEGER PRIMARY KEY, filepath TEXT UNIQUE, artist TEXT, title TEXT, album TEXT)''')
    conn.commit()
    return conn

def scan_folder(conn):
    c = conn.cursor()
    added_count = 0
    log.info(f"Scanning folder: {MUSIC_FOLDER}...")
    
    if not os.path.exists(MUSIC_FOLDER):
        log.error(f"Folder '{MUSIC_FOLDER}' does not exist. Check your .env file.")
        return

    for root, dirs, files in os.walk(MUSIC_FOLDER):
        for file in files:
            if file.lower().endswith(SUPPORTED_FORMATS):
                filepath = os.path.join(root, file)
                
                # Default values (filename without extension as fallback title)
                artist = 'Unknown Artist'
                title = os.path.splitext(file)[0]
                album = 'Unknown Album'

                try:
                    # easy=True normalizes tags across formats to a common interface
                    audio = File(filepath, easy=True)

                    # Overwrite defaults if the file actually has tags
                    if audio is not None:
                        artist = audio.get('artist', [artist])[0]
                        title = audio.get('title', [title])[0]
                        album = audio.get('album', [album])[0]
                        
                    c.execute("INSERT OR IGNORE INTO tracks (filepath, artist, title, album) VALUES (?, ?, ?, ?)", 
                              (filepath, artist, title, album))
                    if c.rowcount > 0: 
                        added_count += 1
                except Exception as e:
                    log.warning(f"Error reading {filepath}: {e}")
    
    conn.commit()
    log.info(f"Done! Added {added_count} new tracks to the database.", extra=SUMMARY)

if __name__ == "__main__":
    setup_logging()
    db_conn = create_database()
    scan_folder(db_conn)
    db_conn.close()
