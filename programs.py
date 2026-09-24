"""Recurring programs: scheduled slots in which a station's blocks follow
program-specific rules instead of its usual ones (see README, "Programs").

This module handles the configuration, the schedule and each program's
episode history; dj_agent.py prepares the blocks.
"""

import datetime
import json
import os

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULTS = {
    "mode": "artist",
    "instructions": "",
    "theme": "",
    "artists": [],
    "exclude_artists": [],
    "min_minutes": None,  # artist mode: defaults to the slot's length
    "repeat_after_episodes": 8,
    "fallback_script": "",
    "models": [],  # preferred models for the DJ's words, as for news
}
# Episodes remembered per program (for repeat_after_episodes).
MAX_EPISODES_KEPT = 100


def station_programs(config):
    """The station's programs, with defaults filled in."""
    programs = []
    for program in config.get("programs") or []:
        if not program.get("id") or not program.get("schedule"):
            continue
        programs.append(dict(DEFAULTS, **program))
    return programs


def _slot_days(slot):
    days = slot.get("days") or DAYS
    if days in ("daily", ["daily"]):
        return set(range(7))
    return {DAYS.index(day[:3].lower()) for day in days}


def _at(date, hhmm):
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return datetime.datetime.combine(date, datetime.time()) + datetime.timedelta(hours=hours, minutes=minutes)


def active_slot(program, when):
    """(start, end) of the program's slot `when` falls in, or None. A slot
    ending at or before its start (e.g. 23:00-01:00) runs past midnight."""
    for slot in program["schedule"]:
        days = _slot_days(slot)
        # A slot that started yesterday may still be running past midnight.
        for day_offset in (0, -1):
            date = (when + datetime.timedelta(days=day_offset)).date()
            if date.weekday() not in days:
                continue
            start = _at(date, slot["start"])
            end = _at(date, slot["end"])
            if end <= start:
                end += datetime.timedelta(days=1)
            if start <= when < end:
                return start, end
    return None


def current_program(config, when):
    """The first of the station's programs on air at `when`, as
    (program, slot start, slot end), or None."""
    for program in station_programs(config):
        slot = active_slot(program, when)
        if slot:
            return (program,) + slot
    return None


class Episodes:
    """A program's episodes on one station: which artist each was about
    (artist mode) and how many blocks it has had, kept in
    stations/<id>/programs/<program id>.json so a restarted agent carries on
    with the same episode."""

    def __init__(self, station_dir, program_id):
        self.path = os.path.join(station_dir, "programs", f"{program_id}.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.episodes = json.load(f).get("episodes", [])
        except (OSError, ValueError):
            self.episodes = []

    def get(self, start):
        """The episode of the slot starting at `start`, if there is one."""
        key = start.isoformat()
        return next((e for e in self.episodes if e["start"] == key), None)

    def start(self, start, **fields):
        episode = dict(fields, start=start.isoformat(), blocks=0)
        self.episodes.append(episode)
        self.episodes = self.episodes[-MAX_EPISODES_KEPT:]
        self.save()
        return episode

    def recent_artists(self, count):
        """Artists of the last `count` episodes."""
        return [e["artist"] for e in self.episodes[-count:] if e.get("artist")] if count else []

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"episodes": self.episodes}, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self.path)
