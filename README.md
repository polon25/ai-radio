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
                        audio), and writes the station's playlist file
radio.liq             → plays that playlist file and streams it to Icecast
```

`dj_agent.py` and `radio.liq` run as two independent, long-lived processes
per station — `dj_agent.py --loop` isn't triggered by Liquidsoap. That's a
deliberate choice: on the Liquidsoap version this project currently targets
(1.4.1 — see [Why the Python-side timer](#why-the-python-side-timer) below),
none of the track-boundary callbacks fire reliably in sync with real
playback for a `playlist()` source, so `dj_agent.py` instead reads each
block's own audio duration and schedules its next run from that.

### Artist roster

Each station keeps an `artists_<station_id>.json` file:

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
   | `BLOCK_LEAD_TIME` | Optional; seconds before a block's natural end that the next one starts generating (default 15) |

3. **Stations** — copy `stations.json.example` to `stations.json` and edit it
   (see [Configuring a station](#configuring-a-station) below).
   `stations.json` is gitignored: it's where your actual stations and their
   local library paths live, so you can keep adding stations without any of
   them ending up in this repo.

4. **Scan your music library**

   ```bash
   ./venv/bin/python scanner.py
   ```

   Re-run this whenever the library changes — `dj_agent.py` picks up newly
   scanned artists on its own on the next run (see [Artist
   roster](#artist-roster)).

## Configuring a station

Stations live in `stations.json` (start from `stations.json.example`),
keyed by station ID:

```json
{
  "metal_pl": {
    "name": "Ciężki Młot",
    "description": "Polskie radio z muzyką metalową",
    "voice": "pl-PL-MarekNeural",
    "folder_filter": "%/Inna muzyka/%",
    "fallback_script": "...",
    "roster_prompt": "... {station_name} ... {description} ... {artists_list} ...",
    "prompt": "... {station_name} ... {description} ... {artists_list} ..."
  }
}
```

- `folder_filter` is a SQL `LIKE` pattern matched against each track's file
  path in `music_library.db` — this is what scopes a station to a subset of
  the scanned library.
- `roster_prompt` and `prompt` must each produce raw JSON with a
  `selected_indices` field (`prompt` additionally needs `dj_script`); see the
  `metal_pl` entry for the exact contract each is held to.
- `voice` is any [Edge TTS voice name](https://github.com/rany2/edge-tts#usage).

## Running

Each station needs both processes running, e.g. for `metal_pl`:

```bash
export $(grep -v '^#' .env | xargs)   # or set STATION_ID=metal_pl directly
liquidsoap radio.liq &
./venv/bin/python dj_agent.py metal_pl --loop &
```

### Running at boot (systemd)

Template units are in `systemd/`. Install once:

```bash
sudo cp systemd/radio-liquidsoap@.service systemd/radio-agent@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Then enable a station by its ID (the `@<id>` becomes `$STATION_ID`):

```bash
sudo systemctl enable --now radio-liquidsoap@metal_pl.service
sudo systemctl enable --now radio-agent@metal_pl.service
```

Adding another station later is just another `enable --now` pair with a
different ID — no new unit files needed. Logs: `journalctl -u radio-agent@metal_pl -f`.

## Adding a new station

1. Add an entry to `stations.json` (copy `metal_pl`'s shape).
2. Pick a `folder_filter` that scopes it to the right part of your library.
3. Run it once manually to build its artist roster and confirm the prompts
   produce sensible output, then enable the two systemd services for it.

## Why the Python-side timer

Liquidsoap 1.4.1 (the version available via `apt` on this deployment's
Ubuntu release) was tried with three different track-boundary callbacks
before landing on the current design, in case a future contributor is
tempted to switch back:

- **`on_end`** (fires when remaining time in a track drops below a
  threshold) never fired at all when wrapping a `playlist()` source in
  testing, despite working correctly wrapping a `single()` source.
- **`playlist()`'s own `on_track` parameter** reports `last` (whether the
  upcoming track is the final one in the list) — but it fires for the whole
  list within milliseconds of the list loading, not synchronized with actual
  playback at all.
- **`on_metadata`** fired once, immediately, with the *last* track's
  metadata rather than the first — looked like a prefetch/resolution
  artifact rather than a real playback event.

If this project ever moves to Liquidsoap ≥ 2.0 (not available for this
deployment's Ubuntu release without building from source or a newer distro),
`source.on_position`/`remaining_files` reintroduces a reliable version of
this and the reactive design becomes viable again.
