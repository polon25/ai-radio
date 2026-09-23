# AI Radio

An internet radio station that curates its own playlists. An LLM (via
[OpenRouter](https://openrouter.ai)) picks artists from a scanned local music
library that fit a station's theme, writes a short DJ intro script for them,
and [Edge TTS](https://github.com/rany2/edge-tts) turns that script into
audio. [Liquidsoap](https://www.liquidsoap.info) streams the result to an
Icecast server.

The system supports multiple independent stations from one codebase — each
station is just an entry in `stations.json` plus a running pair of processes.

## How it works

```
scanner.py          → indexes a music folder into music_library.db
dj_agent.py --loop   → repeatedly: picks artists (via AI), fetches one track
                        per artist, writes a DJ intro (AI text → Edge TTS
                        audio), and queues the block in Liquidsoap
radio.liq             → plays the queued blocks and streams them to Icecast
```

`dj_agent.py --loop` and `radio.liq` run as two independent, long-lived
processes per station, talking over Liquidsoap's command socket
(`stations/<station_id>/liquidsoap.sock`). The agent pushes each block (DJ
intro + songs) onto Liquidsoap's request queue and polls how much of it is
left: as soon as the last queued track starts playing — or earlier, if less
than `BLOCK_LEAD_TIME` seconds of music remain — it prepares the next block
and queues it behind. Queued tracks play exactly once, in order, and a new
block never cuts off the current track, however long the AI takes. See
[Why a request queue](#why-a-request-queue) for why it isn't done with a
playlist file.

If the queue runs dry anyway (the AI took longer than the last track, or the
agent is down), `radio.liq` fills in with random tracks from the station's
fallback list (`fallback.txt`, one track each from up to 50 random roster
artists, refreshed daily by the agent) rather than going silent. Each such
track is logged as a warning.

### Artist roster

Each station keeps an `artists.json` file in its runtime folder (see
[Station files](#station-files)):

```json
{
  "allowed": ["Metallica", "Iron Maiden", "..."],
  "banned": ["Taylor Swift", "..."]
}
```

On first run (or whenever the file is missing/empty), `dj_agent.py` asks the
AI to classify every artist in the library against the station's theme —
`fitting` goes to `allowed`, everything else to `banned`. Every later run
scans the library for artists it hasn't seen yet and classifies only those,
so this stays cheap once the roster has caught up with the library. A block
that falls back to a random pick (AI request failed) still draws only from
`allowed`, so it always stays on-theme. You can hand-edit either list at any
time — an artist you add to `banned` is never asked about again.

AI classification is optional — set `"use_ai_roster": false` on a station and
every artist under its `folder_filter` becomes `allowed`, no AI call
involved. This is the right choice for a folder that's already a dedicated,
niche collection (a specific fandom's music, a soundtrack folder, ...): AI
classification adds no value there, and risks wrongly banning legitimate
artists it just doesn't recognize.

### Artist cooldown

Each station also keeps a `recent_artists.json` file: a plain
list of who played, oldest first. Before offering artists to the AI (or to
the random fallback), `dj_agent.py` drops anyone who played within the last
`N` songs, where

```
N = max(1, round(artist_pool_size * artist_cooldown_fraction))
```

— so a 70-artist roster with the default `artist_cooldown_fraction` of `0.1`
keeps an artist off the air for at least 7 other songs, while a 10-artist
one only needs 1. This is deliberately proportional rather than a fixed
number: a small roster would either repeat constantly under a large fixed
cooldown or barely be affected by a small one. If the cooldown would leave
fewer eligible artists than a block needs, it's ignored for that round
rather than stalling generation. Set `artist_cooldown_fraction` to `0` to
disable it entirely.

### Station files

Everything a station generates at runtime lives in its own folder,
`stations/<station_id>/` (created automatically, gitignored):

| File | Contents |
|---|---|
| `intros/` | Generated DJ intros, one per block (the newest few are kept) |
| `fallback.txt` | Tracks `radio.liq` plays if the block queue runs dry |
| `liquidsoap.sock` | Liquidsoap's command socket, used by `dj_agent.py` |
| `artists.json` | The [artist roster](#artist-roster) |
| `recent_artists.json` | Play history for the [artist cooldown](#artist-cooldown) |
| `logs/` | The station's [logs](#logs) |

Shared files (`stations.json`, `music_library.db`, `.env`) stay in the
project root. Files left in the project root by older versions
(`artists_<station_id>.json` etc.) are moved into the station's folder
automatically the next time `dj_agent.py` starts for that station.

### Logs

Every process logs to stdout (so `journalctl` still works) and to daily log
files, one file per day, deleted after `LOG_RETENTION_DAYS` (default 7):

| Where | What |
|---|---|
| `stations/<station_id>/logs/YYYY-MM-DD.log` | Everything about one station: block preparation, which artists/tracks were picked and the DJ script, every LLM request (which model answered, how long it took, errors, malformed answers), what Liquidsoap actually started playing ("Now playing"), and Liquidsoap's own messages |
| `logs/YYYY-MM-DD.log` | The overview: warnings and errors from every station and the scanner, plus each station's key events (blocks prepared, "Now playing") |

Each line reads `time level [station] component: message`, so e.g.
`grep -h "Now playing" stations/*/logs/$(date +%F).log` shows what every
station has played today.

Liquidsoap can't write into these files directly, so `radio.liq` logs to a
`liquidsoap.spool` file in the station's log folder, which that station's
`dj_agent.py --loop` forwards into its log (keeping Liquidsoap's timestamps)
and then empties. If the agent isn't running, Liquidsoap's messages simply
wait in the spool until it is.

### Library scanning

`--loop` runs `scanner.py` in the background once at startup, then again
every `SCAN_INTERVAL_HOURS` (default 2). If several stations' `--loop`
processes are running against the same `music_library.db`, a `scanner.lock`
file makes sure only one of them actually scans at a time — the rest just
skip that round.

## Setup

1. **Python environment**

   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. **Configuration** — copy `.env.example` to `.env` and fill it in:

   | Variable | Meaning |
   |---|---|
   | `OPENROUTER_API_KEY` | OpenRouter API key, used for artist curation and DJ scripts |
   | `MUSIC_FOLDER` | Root folder `scanner.py` indexes |
   | `ICECAST_PASSWORD` | Must match `<source-password>` in `icecast.xml` |
   | `STATION_ID` | Which entry from `stations.json` this instance runs |
   | `ICECAST_HOST` / `ICECAST_PORT` | Icecast server connection |
   | `ICECAST_MOUNT` | Optional; defaults to `/<STATION_ID>` |
   | `OPENROUTER_SITE_URL` | Optional; sent as `HTTP-Referer` to OpenRouter |
   | `OPENROUTER_TIMEOUT` | Optional; seconds before an OpenRouter request is given up on (default 60) |
   | `BLOCK_LEAD_TIME` | Optional; the next block is prepared once the last queued track starts, or as soon as less than this many seconds of music are left in the queue (default 120) |
   | `LOG_RETENTION_DAYS` | Optional; days of daily [log files](#logs) to keep (default 7) |
   | `LOG_LEVEL` | Optional; minimum level written to the station logs and stdout (default `INFO`) |
   | `SCAN_INTERVAL_HOURS` | Optional; how often `--loop` rescans the music library in the background, in addition to always scanning once at startup. `0` disables the periodic rescan (default 2) |

3. **Stations** — copy `stations.json.example` to `stations.json` and edit it
   (see [Configuring a station](#configuring-a-station) below).
   `stations.json` is gitignored: it's where your actual stations and their
   local library paths live, so you can keep adding stations without any of
   them ending up in this repo.

4. **Scan your music library**

   ```bash
   ./venv/bin/python scanner.py
   ```

   `dj_agent.py --loop` re-scans automatically (see [Library
   scanning](#library-scanning)), so this manual run is mainly for seeding
   the database before the first `--loop` start, or for `dj_agent.py`'s
   one-shot mode.

## Configuring a station

Stations live in `stations.json` (start from `stations.json.example`),
keyed by station ID:

```json
{
  "ciezki_mlot": {
    "name": "Ciężki Młot",
    "description": "Polskie radio z muzyką metalową",
    "voice": "pl-PL-MarekNeural",
    "folder_filter": "%/Inna muzyka/%",
    "songs_per_block": 3,
    "use_ai_roster": true,
    "artist_cooldown_fraction": 0.1,
    "fallback_script": "...",
    "roster_prompt": "... {station_name} ... {description} ... {artists_list} ...",
    "prompt": "... {station_name} ... {description} ... {artists_list} ... {song_count} ..."
  }
}
```

- `folder_filter` is a SQL `LIKE` pattern matched against each track's file
  path in `music_library.db` — this is what scopes a station to a subset of
  the scanned library. It can also be a list of patterns (`["%/A/%",
  "%/B/%"]`) to pull from several folders that don't share a common parent.
- `songs_per_block` — how many tracks per generated block (plus the intro).
  Optional, defaults to 3.
- `use_ai_roster` — whether to AI-classify artists into allowed/banned (see
  [Artist roster](#artist-roster)) or just allow everything under
  `folder_filter`. Optional, defaults to `true`.
- `artist_cooldown_fraction` — see [Artist cooldown](#artist-cooldown).
  Optional, defaults to `0.1`.
- `roster_prompt` and `prompt` must each produce raw JSON with a
  `selected_indices` field (`prompt` additionally needs `dj_script`); see the
  `ciezki_mlot` entry for the exact contract each is held to. `prompt` can use
  `{song_count}` to reference `songs_per_block` instead of hardcoding a
  number.
- `voice` is any [Edge TTS voice name](https://github.com/rany2/edge-tts#usage).

## Running

Each station needs both processes running, e.g. for `ciezki_mlot`:

```bash
export $(grep -v '^#' .env | xargs)   # or set STATION_ID=ciezki_mlot directly
liquidsoap radio.liq &
./venv/bin/python dj_agent.py ciezki_mlot --loop &
```

### Running at boot (systemd)

Template units are in `systemd/`. Install once:

```bash
sudo cp systemd/radio-liquidsoap@.service systemd/radio-agent@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Then enable a station by its ID (the `@<id>` becomes `$STATION_ID`):

```bash
sudo systemctl enable --now radio-liquidsoap@ciezki_mlot.service
sudo systemctl enable --now radio-agent@ciezki_mlot.service
```

Adding another station later is just another `enable --now` pair with a
different ID — no new unit files needed. Logs: see [Logs](#logs), or
`journalctl -u radio-agent@ciezki_mlot -f`.

## Adding a new station

1. Add an entry to `stations.json` (copy `ciezki_mlot`'s shape).
2. Pick a `folder_filter` that scopes it to the right part of your library.
3. Run it once manually to build its artist roster and confirm the prompts
   produce sensible output, then enable the two systemd services for it.

## Why a request queue

Earlier versions wrote each block to a playlist file that Liquidsoap
reloaded, and `dj_agent.py` slept for the block's duration (minus a few
seconds) before generating the next one. That timer could never line up with
actual playback: generating a block (AI + TTS) takes anywhere from a few
seconds to a minute, so the new file usually arrived after the old block had
ended — Liquidsoap then looped the old block, replaying its intro or first
song — and since the next timer started when the file was written rather
than when the block actually started playing, the block after that was
written minutes early, dropping songs that hadn't played yet.

Liquidsoap 1.4.1 (the version available via `apt` on this deployment's
Ubuntu release) has no callback that reliably fires when a playlist file's
last track starts: `on_end` never fired when wrapping `playlist()`,
`playlist()`'s own `on_track` parameter fires for the whole list as soon as
it's loaded, and `on_metadata` fired once with the wrong track's data. So
the agent polls instead — Liquidsoap's command server reports the queue's
contents and the current track's remaining time, which is exact.

(`on_track` wrapping the *final* source, on the other hand, does fire in sync
with real playback, which is what the "Now playing" log lines use.)
