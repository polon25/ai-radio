"""Logging shared by every AI Radio process.

Each process logs to stdout (so journald/systemd still has everything) and to
daily log files:

    logs/YYYY-MM-DD.log                 main log: warnings/errors from every
                                        station, plus each station's key
                                        events (blocks, what's playing)
    stations/<id>/logs/YYYY-MM-DD.log   everything about a single station:
                                        the DJ agent, LLM calls, Liquidsoap

Several processes append to the same files at once (one agent per station,
the scanner, ...), so logs aren't rotated by renaming files, which would
race. Instead every day simply gets its own file, and whenever a process
moves on to a new day it deletes files older than LOG_RETENTION_DAYS.

Liquidsoap can't write to these files itself, so radio.liq logs to a spool
file (LIQUIDSOAP_SPOOL_FILE in the station's log folder) that the station's
DJ agent forwards into its log with LiquidsoapLogForwarder.
"""

import datetime
import json
import logging
import os
import re
import sys
import threading
import time

MAIN_LOG_DIR = "logs"
LIQUIDSOAP_SPOOL_FILE = "liquidsoap.spool"
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(station)s] %(component)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")

# Pass as `extra=` to also send an INFO record to the main log (by default
# it only gets warnings and errors), e.g. log.info("...", extra=SUMMARY).
SUMMARY = {"summary": True}


class DailyFileHandler(logging.Handler):
    """Appends each record to <directory>/<record's date>.log and deletes
    log files older than LOG_RETENTION_DAYS whenever it starts a new day."""

    def __init__(self, directory, level=logging.NOTSET):
        super().__init__(level)
        self.directory = directory
        self._day = None
        self._stream = None
        self._cleaned_up_on = None

    def _open(self, day):
        if self._stream:
            self._stream.close()
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{day.isoformat()}.log")
        self._stream = open(path, "a", encoding="utf-8")
        self._day = day
        today = datetime.date.today()
        if self._cleaned_up_on != today:
            self._cleaned_up_on = today
            self._delete_old_files(today)

    def _delete_old_files(self, today):
        cutoff = today - datetime.timedelta(days=LOG_RETENTION_DAYS)
        for name in os.listdir(self.directory):
            match = LOG_FILE_RE.match(name)
            if not match:
                continue
            try:
                if datetime.date.fromisoformat(match.group(1)) < cutoff:
                    os.remove(os.path.join(self.directory, name))
            except (ValueError, FileNotFoundError):
                pass  # not a real date, or another process got there first

    def emit(self, record):
        try:
            day = datetime.date.fromtimestamp(record.created)
            if day != self._day:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


class _ContextFilter(logging.Filter):
    """Fills in the [station] and component fields of the log format."""

    def __init__(self, station):
        super().__init__()
        self.station = station

    def filter(self, record):
        if not hasattr(record, "station"):
            record.station = self.station
        record.component = record.name
        return True


class _MainLogFilter(logging.Filter):
    """Lets through warnings/errors, and INFO records marked as SUMMARY."""

    def filter(self, record):
        return record.levelno >= logging.WARNING or getattr(record, "summary", False)


def setup_logging(station_id=None, station_log_dir=None):
    """Configures the root logger: stdout + main log, and the station's own
    log when station_log_dir is given. Records get their component from the
    logger name, e.g. logging.getLogger("agent")."""
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    context = _ContextFilter(station_id or "-")

    handlers = [logging.StreamHandler(sys.stdout)]
    main_handler = DailyFileHandler(MAIN_LOG_DIR)
    main_handler.addFilter(_MainLogFilter())
    handlers.append(main_handler)
    if station_log_dir:
        handlers.append(DailyFileHandler(station_log_dir))

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context)
        root.addHandler(handler)

    # Chatty third-party loggers would otherwise flood the station logs.
    for name in ("urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


# Liquidsoap log lines look like: "2026/09/23 10:10:38 [decoder:3] message",
# where the digit is its log level (1 = critical ... 5 = debug).
_LIQUIDSOAP_LINE_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[([^\]]+):(\d)\] (.*)$")
_LIQUIDSOAP_LEVELS = {
    1: logging.CRITICAL,
    2: logging.ERROR,
    3: logging.INFO,
    4: logging.DEBUG,
    5: logging.DEBUG,
}
# Level-3 labels that only repeat what the "now playing" lines already say,
# describe Liquidsoap's own startup (library versions, frame sizes, ...), or
# note every connection dj_agent.py makes to its command socket.
_LIQUIDSOAP_NOISY_LABELS = {
    "decoder", "main", "frame", "sandbox", "video.converter",
    "audio.converter", "gstreamer.loader", "dynamic.loader", "server",
}
# Written by Liquidsoap when it opens its log, i.e. on start.
_LIQUIDSOAP_START_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) >>> LOG START$")
# Liquidsoap source id of radio.liq's fallback list, which only plays when the
# queue of blocks from dj_agent.py has run dry.
FALLBACK_SOURCE_ID = "fallback"
# Silence shorter than this isn't reported as dead air: Liquidsoap briefly
# plays silence on every start, before its first track is ready.
DEAD_AIR_GRACE_SECONDS = 5


class LiquidsoapLogForwarder(threading.Thread):
    """Forwards the station's Liquidsoap log (the spool file radio.liq writes
    to) into the Python logs, keeping Liquidsoap's own timestamps, and
    truncates the spool once everything in it has been forwarded. Liquidsoap
    opens it in append mode, so it simply carries on writing from the start.

    radio.liq logs every track that starts playing under the "now_playing"
    label, with its metadata as JSON; those become readable "Now playing"
    lines (or a dead-air warning if nothing is available to play), and are
    passed to on_track_start(metadata, timestamp) if given."""

    def __init__(self, spool_path, describe_track, on_track_start=None, interval=2.0):
        super().__init__(name="liquidsoap-log", daemon=True)
        self.spool_path = spool_path
        self.describe_track = describe_track
        self.on_track_start = on_track_start
        self.interval = interval
        self.offset = 0
        self.log = logging.getLogger("liquidsoap")
        self.silent_since = None  # when Liquidsoap started playing silence
        self.dead_air_reported = False

    def run(self):
        while True:
            try:
                self.poll()
            except Exception:
                self.log.exception("Failed to forward the Liquidsoap log")
            time.sleep(self.interval)

    def poll(self):
        self._forward_new_lines()
        # Only after reading what's new: a track that started since the last
        # poll ends the silence, which then wasn't dead air.
        self._check_dead_air(time.time())

    def _forward_new_lines(self):
        if not os.path.exists(self.spool_path):
            return
        with open(self.spool_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < self.offset:  # truncated elsewhere, e.g. Liquidsoap restarted
                self.offset = 0
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        if not end:
            return
        self.offset += end
        for line in data[:end].decode("utf-8", "replace").splitlines():
            if line.strip():
                self.forward_line(line)
        if os.path.getsize(self.spool_path) == self.offset:
            os.truncate(self.spool_path, 0)
            self.offset = 0

    def forward_line(self, line):
        start = _LIQUIDSOAP_START_RE.match(line)
        if start:
            created = time.mktime(time.strptime(start.group(1), "%Y/%m/%d %H:%M:%S"))
            self._emit(logging.INFO, "Liquidsoap started", created, summary=True)
            return
        match = _LIQUIDSOAP_LINE_RE.match(line)
        if not match:  # e.g. a continuation of a multi-line message
            self._emit(logging.INFO, line, time.time())
            return
        timestamp, label, level, message = match.groups()
        created = time.mktime(time.strptime(timestamp, "%Y/%m/%d %H:%M:%S"))

        if label == "now_playing":
            self._emit_now_playing(message, created)
            return
        if label == "main" and message.startswith("Shutdown started"):
            self._emit(logging.INFO, "Liquidsoap stopping", created, summary=True)
            return

        levelno = _LIQUIDSOAP_LEVELS.get(int(level), logging.DEBUG)
        if levelno == logging.INFO and label in _LIQUIDSOAP_NOISY_LABELS:
            levelno = logging.DEBUG
        self._emit(levelno, f"[{label}] {message}", created)

    def _emit_now_playing(self, message, created):
        try:
            metadata = json.loads(message)
        except ValueError:
            self._emit(logging.INFO, f"Now playing: {message}", created, summary=True)
            return
        if not metadata.get("filename"):
            # Only reported once it has lasted a while, see _check_dead_air().
            self.silent_since = created
            self.dead_air_reported = False
            return
        if self.silent_since is not None:
            if self.dead_air_reported:
                self._emit(
                    logging.WARNING,
                    f"Dead air ended after {created - self.silent_since:.0f}s",
                    created,
                )
            self.silent_since = None
        if self.on_track_start:
            try:
                self.on_track_start(metadata, created)
            except Exception:
                self.log.exception("Failed to record a track start")
        if metadata.get("source") == FALLBACK_SOURCE_ID:
            self._emit(
                logging.WARNING,
                f"Block queue ran dry, playing from the fallback list: {self.describe_track(metadata)}",
                created,
            )
            return
        self._emit(logging.INFO, f"Now playing: {self.describe_track(metadata)}", created, summary=True)

    def _check_dead_air(self, now):
        if self.silent_since is None or self.dead_air_reported:
            return
        if now - self.silent_since >= DEAD_AIR_GRACE_SECONDS:
            self.dead_air_reported = True
            self._emit(logging.WARNING, "Dead air: Liquidsoap has nothing to play", self.silent_since)

    def _emit(self, levelno, message, created, summary=False):
        if not self.log.isEnabledFor(levelno):
            return
        record = self.log.makeRecord(
            self.log.name, levelno, "(liquidsoap)", 0, message, None, None,
            extra={"summary": summary},
        )
        record.created = created
        record.msecs = 0
        self.log.handle(record)
