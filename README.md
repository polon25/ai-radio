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

After each library scan (see [Library scanning](#library-scanning)),
`dj_agent.py --loop` asks the AI to classify the artists it hasn't seen yet
against the station's theme — `fitting` goes to `allowed`, everything else
to `banned`. The first time (or whenever the file is missing/empty) that's
every artist in the library; after that only new ones, so this stays cheap
once the roster has caught up. It happens in the background, so a slow AI
never holds up a block; a station with no roster yet plays from its whole
library until it's built.

Artists are classified 40 per request (with hundreds in one numbered list,
models lose track of the numbers and pick huge swathes of off-theme
artists), each listed with two example track titles and the folder they're
in, e.g. `Radiorama (e.g. "Chance to Desire", ...; folder: SuperEurobeat/...)`,
which tells the AI far more than an obscure artist's name alone. Each batch
is classified three times — `openrouter/free` picks a different model each
time, and their answers vary a lot (one picks exactly the right artists,
the next half the list) — and an artist gets in only if most answers picked
it. Artists in a batch with fewer than two usable answers are left
unclassified and asked about again next time. The `roster_prompt` should ask the AI to be strict — a
lenient "include anything that reasonably fits" lets whole neighbouring
genres in.

To reclassify a station's whole roster (e.g. after changing its
`roster_prompt` or description), run

```bash
./venv/bin/python dj_agent.py <station_id> --rebuild-roster
```

with the station's agent stopped (so it can't save the roster at the same
time). The station keeps playing from Liquidsoap's queue meanwhile; start
the agent again when it's done. A block
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

### Track cooldown

Every track a station starts playing is recorded in a `plays` table in
`music_library.db` (fallback tracks included, DJ intros not; entries older
than 30 days are dropped). When a block is prepared:

- artists all of whose tracks played on this station within the track
  cooldown aren't offered to the AI (so an artist with a single track in
  the library plays it at most once per cooldown),
- and each picked artist gets a random track among those that didn't play
  within the cooldown — or, if there are none, the one that played longest
  ago.

The cooldown is `track_cooldown_hours` (default 24), but never more than
half the total length of the station's music: a small station (say, a few
hours of music) would otherwise run out of eligible tracks and end up
cycling through its library in the same order every time. With the cap,
roughly half its tracks are always eligible and picks stay random. If the
cooldown still leaves fewer artists than a block needs, it's ignored for
that block.

### Release years and genres

The scanner stores each track's release year (the `originaldate` or `date`
tag, or the year in the album folder's name — `Album (2004)`, `Album [2004]` —
if that's later, since tags are sometimes plain wrong) and genre (the `genre` tag, which is often vague or plain wrong, so
it's only used as a hint). On a station with `years`, the agent asks the AI
— in the background, after each library scan — for the original release
year and genre of its roster's tracks that have none in their tags; each
track is asked about once. Years and genres also appear, next to example
track titles, in the artist lists the AI classifies rosters from.

### News

A station with news sources gets a news segment at the top of every hour.
Twenty-five minutes before, the agent

1. collects headlines from the station's `news.sources` — news sites' front
   pages (links that look like articles) or RSS/Atom feeds,
2. asks the AI to pick the most important distinct stories — normally at
   least one for each of the `topics`, unless there's good reason not to —
   a few more than `count`, most important first, leaving out
   stories covered in the last `avoid_repeat_hours` bulletins unless the
   headlines show something new (a story that does come back is written as
   an update, focusing on what's new). Articles already read out in those
   bulletins (same link or headline) are removed from the list beforehand,
   since weaker models don't always follow that instruction,
3. fetches those articles' text (for sites that block it, e.g. the New York
   Times, the feed's summary is used instead) and keeps the `count` most
   important stories whose full article it got, as a summary alone can't
   fill a couple of minutes without padding,
4. asks the AI to rewrite each story as a spoken news item of about
   `story_minutes` — keeping strictly to the facts in the material, with no
   filler — plus a one-sentence summary of it for the segment's opening, and
   has it rewritten (by the next preferred model) if it comes out far too
   short,
5. wraps them in the station's `intro` and `outro` and turns the text into
   speech.

Two minutes before the hour the segment is queued, and `radio.liq` plays it
as soon as the current track ends — so it starts around the top of the hour,
between a couple of minutes early and one track late — then carries on with
the next track. (Cutting into the current track isn't reliable on Liquidsoap
1.4: skipping a source that isn't playing, or one inside a `fallback`,
either doesn't skip it or skips a track too many.) A segment still not ready
10 minutes past the hour is dropped; until then, a failed attempt is retried
every minute. Stations with the same news settings share
each hour's stories (written once, cached in `news_cache/`), so only the
station's name in the intro and its voice differ. `dj_agent.py <station_id>
--news-now` prepares a segment and queues it right away, e.g. to try
the settings out. See [Configuring a station](#configuring-a-station) for
the settings.

### Programs

A station can have recurring programs: time slots (e.g. Mondays 12:00-13:00)
in which its blocks follow the program's rules instead of the usual ones.
Whether a block belongs to a program is decided by when it's expected to
start playing, so a program starts with the first block after its slot
opens and ends with the last one starting before it closes. Programs don't
change the news: it still plays around the top of the hour, between blocks
(so a program at 12:00 effectively starts after the 12:00 news).

Three kinds (`mode`):

- `artist` — each episode is about one artist, picked when it starts: at
  random from the program's `artists` (or else the station's roster), among
  those with at least `min_minutes` of music the station may play (default:
  the slot's length), leaving out `exclude_artists` and, if possible, the
  artists of the last `repeat_after_episodes` episodes. Its blocks play that
  artist's songs (none twice in an episode, least recently played first).
  The DJ opens the episode (welcoming the listeners, announcing the program
  and the artist, with a short introduction), shares something about the
  artist, their albums or the songs before each later block — given the
  songs' albums and years from the library, and what was already said in
  the episode, so as not to repeat it — and closes the last block.
- `collection` — like `artist`, but each episode is about one collection of
  tracks instead: an album, or (`"group_by": "folder"`) one of the folders
  right below the folder named `folder_after` — e.g. with game soundtracks
  kept as `Soundtracks/<game>/...`, each episode plays one game's
  soundtrack, and the DJ talks about the game, its composers and music.
  `subject_label` tells the DJ what a collection is (e.g. "the soundtrack of
  the game"); it's told the collection's name as it appears in the library
  (folder or album name) and to use its proper name.
- `theme` — each block is picked by the AI from the station's roster to fit
  the program's `theme` (e.g. "power ballads of the 80s"), with the program's
  instructions for the DJ; the station's cooldowns still apply.

Each episode's state (its artist, blocks so far, what the DJ said) is kept
in `stations/<station_id>/programs/`, so a restarted agent carries on with
the same episode.

### Station files

Everything a station generates at runtime lives in its own folder,
`stations/<station_id>/` (created automatically, gitignored):

| File | Contents |
|---|---|
| `intros/` | Generated DJ intros, one per block (the newest few are kept) |
| `news/` | Generated news segments (the newest few are kept) |
| `programs/` | Each program's episodes (see [Programs](#programs)) |
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
processes are running against the same `music_library.db`, a lock on
`scanner.lock` makes sure only one scan (theirs or a manual one) runs at a
time — the rest just skip that round. The lock is released automatically
when the scan's process ends, even if it's killed, so it can't go stale.

A scan only opens files it doesn't know yet, plus files indexed by an older
version of the scanner that didn't read everything it reads now (e.g. track
lengths, years, genres), so rescans stay cheap.

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
   | `OPENROUTER_TIMEOUT` | Optional; seconds a whole OpenRouter request for a block (artist picks + DJ script) may take before it's given up on (default 60) |
   | `OPENROUTER_BACKGROUND_TIMEOUT` | Optional; the same for background work nothing on air waits for — classifying artists, filling in track info (default 180) |
   | `OPENROUTER_MODEL` | Optional; OpenRouter model to use (default `openrouter/free`, which picks some free model per request) |
   | `MUSIC_LOUDNESS_LUFS` | Optional; loudness songs are evened out to (default -13). Each song is measured the first time it's queued (kept in `music_library.db`) and played with a gain that brings it there: louder songs always, quieter ones only as far as their peaks allow without clipping (with most masters peaking near 0 dB, often not at all — hence a target a little below typical music, around -8, taking the loudest songs down the most) |
   | `SPEECH_LOUDNESS_LUFS` | Optional; loudness DJ intros and news are raised to before a limiter shaves their peaks, which leaves them about 1.5-2 LU below it (default -10, i.e. about -12; Edge TTS speech is around -20, most mastered music around -8) |
   | `OPENROUTER_ATTEMPTS` | Optional; how many times a request whose answer is unusable (not JSON, missing fields, no valid artists) is tried before falling back to a random pick and the station's `fallback_script` (default 3) |
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
    "genre": "Metal",
    "voice": "pl-PL-MarekNeural",
    "folder_filter": "%/Inna muzyka/%",
    "songs_per_block": 3,
    "use_ai_roster": true,
    "artist_cooldown_fraction": 0.1,
    "min_track_seconds": 90,
    "track_cooldown_hours": 24,
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
- `min_track_seconds` — tracks shorter than this (intros, interludes,
  skits, ...) are never played, and artists with only such tracks aren't
  picked. Optional, defaults to `90`.
- `track_cooldown_hours` — see [Track cooldown](#track-cooldown). Optional,
  defaults to `24`.
- `years` — `[first, last]` (inclusive) limits the station to tracks
  released in those years, e.g. `[1970, 1999]` for an oldies station whose
  artists also released newer music. See [Release years and
  genres](#release-years-and-genres). Optional; no limit by default.
- `allow_unknown_year` — whether tracks with no known release year may play
  on a station with `years`. Optional, defaults to `true`.
- `news` — hourly news segments (see [News](#news)); leave it out, or leave
  `sources` empty, for none. Its fields:
  - `sources` — news sites' front pages or RSS/Atom feed URLs, e.g.
    `["https://www.gazeta.pl", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"]`.
  - `count` — how many stories (default 4).
  - `topics` — what to focus on, e.g. `["national politics", "world news",
    "economy"]`.
  - `story_minutes` — roughly how long each story is read for (default 2).
  - `avoid_repeat_hours` — how many previous hours' bulletins a story isn't
    repeated from, unless there's news in it (default 3).
  - `max_minutes` — upper limit on the whole segment's length (default 15).
  - `models` — preferred OpenRouter models for the news, in order (default
    none, i.e. `OPENROUTER_MODEL`). Worth listing large models that write the
    station's language well, since `openrouter/free` sometimes picks small
    ones that garble it. Each is tried once, in order (moving on after an
    error, a timeout, or an unusable or too short answer), before the usual
    `OPENROUTER_ATTEMPTS` tries with `OPENROUTER_MODEL`.
  - `avoid_models` — model name prefixes whose answers are rejected (and the
    request retried), e.g. small models `openrouter/free` sometimes picks
    that garble the station's language. Each rejection costs an extra
    request, so keep the list to the worst offenders.
  - `title` — the segment's title in the stream (default `"News"`).
  - `intro` / `outro` — the segment's fixed opening and closing lines;
    `intro` can use `{hour}`, `{station_name}` and `{topics}` (the stories'
    one-sentence summaries, one after another). They default to English.
  The news is written in the language of the station's `voice` (e.g. Polish
  for `pl-PL-...`).
- `programs` — recurring programs (see [Programs](#programs)), a list of:
  - `id` — identifies the program (and its episodes' file); keep it stable.
  - `title` — its name, as the DJ announces it.
  - `schedule` — its slots, e.g. `[{"days": ["mon"], "start": "12:00",
    "end": "13:00"}]`; `days` are `mon`...`sun` or `"daily"` (the default);
    a slot ending before it starts runs past midnight.
  - `mode` — `"artist"` (default), `"collection"` or `"theme"`.
  - `instructions` — free-text instructions for the DJ (tone, what to talk
    about), in any language.
  - `songs_per_block` — songs between the DJ's words (default: the
    station's).
  - `theme` — theme mode: what the program plays.
  - `artists`, `exclude` (or `exclude_artists`), `min_minutes`,
    `repeat_after_episodes` (default 8) — artist and collection modes: which
    artists or collections may be picked (see above).
  - `group_by` (`"folder"`, the default, or `"album"`), `folder_after`,
    `subject_label` — collection mode (see above).
  - `models`, `avoid_models` — preferred and rejected OpenRouter models for
    the DJ's words, as for news.
  - `fallback_script` — said if the AI can't write the DJ's words; can use
    `{station_name}`, `{title}` and `{artist}` or `{subject}` (the episode's
    artist or collection).
  The DJ speaks the language of the station's `voice`.
- `roster_prompt` and `prompt` must each produce raw JSON with a
  `selected_indices` field (`prompt` additionally needs `dj_script`); see the
  `ciezki_mlot` entry for the exact contract each is held to. `prompt` can use
  `{song_count}` to reference `songs_per_block` instead of hardcoding a
  number.
- `voice` is any [Edge TTS voice name](https://github.com/rany2/edge-tts#usage).
- `name`, `description`, `genre` and `url` (the last two optional) are also
  the stream's details on its Icecast mount, shown in Icecast's status page
  and in players. `radio.liq` reads them at startup through
  `stream_info.py`, so restart the station's Liquidsoap after changing them.
  While a DJ intro plays, the stream's "now playing" reads
  `<name> - DJ`.

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

## Web page

`web_server.py` serves a simple page (in `web/`) for listeners: every
station with a player, what's on air right now, the last 5 songs with the
time they started (from the play history, so without DJ intros or news),
its genre, description and schedule (news, programs), and buttons to copy
the stream's link or download it as an `.m3u` playlist. It refreshes itself
every 15 seconds.

It uses only the standard library: static files plus one JSON endpoint,
`/api/status`, built from `stations.json`, Icecast's status and
`music_library.db`. Run it from the project directory:

```bash
./venv/bin/python web_server.py      # http://<host>:8080
```

or install `systemd/radio-web.service` like the other units and
`sudo systemctl enable --now radio-web.service`. Settings (in `.env`):

| Variable | Meaning |
|---|---|
| `WEB_PORT` / `WEB_HOST` | Where the page is served (default `0.0.0.0:8080`) |
| `WEB_TITLE` | The page's title (default "AI Radio") |
| `ICECAST_PUBLIC_URL` | The streams' base URL for listeners, e.g. `https://radio.example.com`; by default the page's own host name with `ICECAST_PORT` |

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
