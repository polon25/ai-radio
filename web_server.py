"""The radio's web page: listen to the stations, see what's on and what has
played. Serves the static page in web/ and a small JSON API built from the
radio's own data, using only the standard library:

    GET /api/status  every station's details (from stations.json), what's
                     on air and how many listen (from Icecast), and its
                     last songs (from the play history in music_library.db;
                     DJ intros and news aren't recorded there)

Run it from the project directory: python3 web_server.py
"""

import json
import logging
import os
import sqlite3
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv()

from radio_log import setup_logging

log = logging.getLogger("web")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
# Shown as the page's title.
WEB_TITLE = os.getenv("WEB_TITLE", "AI Radio")
ICECAST_HOST = os.getenv("ICECAST_HOST") or "127.0.0.1"
ICECAST_PORT = int(os.getenv("ICECAST_PORT") or "8000")
# Where listeners reach Icecast, e.g. "https://radio.example.com"; by
# default the page's own host name with ICECAST_PORT.
ICECAST_PUBLIC_URL = os.getenv("ICECAST_PUBLIC_URL", "").rstrip("/")
CONFIG_FILE = "stations.json"
DB_FILE = "music_library.db"
HISTORY_LENGTH = 5
# Icecast's status is cached this long, so many open pages don't flood it.
ICECAST_CACHE_SECONDS = 5

_icecast_cache = {"at": 0.0, "sources": {}}


def icecast_sources():
    """{mount: Icecast's status for it}, or {} if Icecast is unreachable."""
    if time.time() - _icecast_cache["at"] < ICECAST_CACHE_SECONDS:
        return _icecast_cache["sources"]
    sources = {}
    try:
        url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/status-json.xsl"
        with urllib.request.urlopen(url, timeout=5) as response:
            stats = json.loads(response.read().decode("utf-8", "replace"))["icestats"]
        listed = stats.get("source") or []
        for source in listed if isinstance(listed, list) else [listed]:
            mount = "/" + source.get("listenurl", "").split("/", 3)[-1]
            sources[mount] = source
    except Exception as e:
        log.warning(f"Couldn't get Icecast's status: {e}")
    _icecast_cache.update(at=time.time(), sources=sources)
    return sources


def recent_songs(conn, station_id):
    """The station's last songs, newest first."""
    rows = conn.execute("""
        SELECT p.played_at, t.artist, t.title, p.filepath
        FROM plays p LEFT JOIN tracks t ON t.filepath = p.filepath
        WHERE p.station = ?
        ORDER BY p.played_at DESC LIMIT ?
    """, (station_id, HISTORY_LENGTH)).fetchall()
    return [
        {"played_at": played_at, "artist": artist or "", "title": title or os.path.basename(path)}
        for played_at, artist, title, path in rows
    ]


def status():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    sources = icecast_sources()
    stations = []
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        for station_id, station in config.items():
            mount = f"/{station_id}"
            source = sources.get(mount)
            try:
                history = recent_songs(conn, station_id)
            except sqlite3.Error:  # e.g. no play history yet
                history = []
            stations.append({
                "id": station_id,
                "name": station.get("name", station_id),
                "description": station.get("description", ""),
                "genre": station.get("genre", ""),
                "mount": mount,
                "on_air": source is not None,
                "now_playing": (source or {}).get("title") or "",
                "listeners": (source or {}).get("listeners", 0),
                "history": history,
                "news": bool((station.get("news") or {}).get("sources")),
                "programs": [
                    {"title": p.get("title", p.get("id", "")), "schedule": p.get("schedule", [])}
                    for p in station.get("programs") or []
                ],
            })
    finally:
        conn.close()
    return {
        "title": WEB_TITLE,
        "stream_base": ICECAST_PUBLIC_URL or None,
        "icecast_port": ICECAST_PORT,
        "generated_at": time.time(),
        "stations": stations,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            try:
                body = json.dumps(status(), ensure_ascii=False).encode("utf-8")
            except Exception:
                log.exception("Couldn't build the status")
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format, *args):
        log.debug(f"{self.address_string()} {format % args}")


def main():
    setup_logging("web")
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    log.info(f"Serving the web page on http://{WEB_HOST}:{WEB_PORT}", extra={"summary": True})
    server.serve_forever()


if __name__ == "__main__":
    main()
