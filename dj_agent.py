import sqlite3
import os
import sys
import json
import random
import subprocess
import time
import requests
import asyncio
import edge_tts
import imageio_ffmpeg
from mutagen import File as MutagenFile
from dotenv import load_dotenv

# Force line-buffered stdout so `print()` shows up promptly in a log file
# even when running unattended (e.g. as a long-lived --loop process), not
# just when attached to a terminal.
sys.stdout.reconfigure(line_buffering=True)

# Load environment variables from .env file
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://github.com/your-username/your-project")
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "60"))
# In --loop mode, how many seconds before the current block's natural end to
# start preparing the next one (Liquidsoap has no reliable way to signal
# "the last track just started" on this deployment's version, so dj_agent.py
# schedules itself using each block's own known duration instead).
BLOCK_LEAD_TIME = int(os.getenv("BLOCK_LEAD_TIME", "15"))
DB_FILE = "music_library.db"
CONFIG_FILE = "stations.json"


def get_playlist_file(station_id):
    """Path to this station's Liquidsoap playlist file (one per station, so
    multiple stations can share this directory without clashing)."""
    return f"dj_playlist_{station_id}.txt"


def get_intro_audio_file(station_id):
    """Path to this station's generated DJ intro audio file."""
    return f"dj_intro_{station_id}.mp3"


def get_artists_file(station_id):
    """Path to this station's artist roster: which artists are allowed to be
    played, and which are banned (manually, or because the AI already ruled
    them out as off-theme, so they aren't re-asked about every run)."""
    return f"artists_{station_id}.json"


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
    print(
        f"Saved roster to {path}: {len(roster['allowed'])} allowed, "
        f"{len(roster['banned'])} banned"
    )


def load_station_config(station_id):
    """Loads configuration for a specific station from the JSON file."""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Configuration file {CONFIG_FILE} not found!")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if station_id not in data:
        raise ValueError(f"Station '{station_id}' not found in config.")
    return data[station_id]

def get_all_artists(folder_filter):
    """Fetches every unique artist in the library matching the folder filter."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT artist FROM tracks
        WHERE filepath LIKE ? AND artist != 'Unknown Artist'
    """, (folder_filter,))
    artists = [row[0] for row in c.fetchall()]
    conn.close()
    return artists

def get_track_by_artist(artist, folder_filter):
    """Fetches a random track for a specific artist."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT filepath, artist, title FROM tracks 
        WHERE filepath LIKE ? AND artist = ? 
        ORDER BY RANDOM() LIMIT 1
    """, (folder_filter, artist))
    track = c.fetchone()
    conn.close()
    return track

def format_artist_list(artists):
    """Numbers an artist list for injection into a prompt (1-indexed, to
    match the `selected_indices` the AI is asked to return)."""
    return "\n".join([f"{i+1}. {artist}" for i, artist in enumerate(artists)])


def call_openrouter_json(prompt):
    """Sends a prompt to OpenRouter and parses the response as JSON. The
    prompt must instruct the model to reply with raw JSON."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing. Check your .env file.")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_SITE_URL,
        "X-Title": "AI Radio Project"
    }

    payload = {
        "model": "openrouter/free",
        "messages": [
            {"role": "system", "content": "You are a precise radio automation agent. You always output valid raw JSON."},
            {"role": "user", "content": prompt}
        ]
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=OPENROUTER_TIMEOUT,
    )

    try:
        result = response.json()
    except Exception as e:
        raise ValueError(f"Failed to parse API response. Status: {response.status_code}, Text: {response.text}")

    # Check if the response contains the expected 'choices' key
    if 'choices' not in result:
        print("\n--- OPENROUTER API ERROR ---")
        print(json.dumps(result, indent=2))
        print("----------------------------\n")
        raise KeyError("OpenRouter did not return 'choices'.")

    raw_content = result['choices'][0]['message']['content'].strip()

    # Clean potential markdown code blocks if the model wrapped the JSON anyway
    if raw_content.startswith("```json"):
        raw_content = raw_content[7:]
    if raw_content.endswith("```"):
        raw_content = raw_content[:-3]
    raw_content = raw_content.strip()

    return json.loads(raw_content)


def curate_artists_and_script(artists, prompt_template, station_name, description):
    """Asks the AI to pick 3 artists for the next block and write the DJ intro."""
    prompt = prompt_template.format(
        artists_list=format_artist_list(artists),
        station_name=station_name,
        description=description,
    )
    print("Asking AI to curate artists and write the script...")
    parsed_data = call_openrouter_json(prompt)
    return parsed_data["selected_indices"], parsed_data["dj_script"]


def classify_artists(candidates, prompt_template, station_name, description):
    """Asks the AI which of the candidate artists fit the station's theme.
    Returns (fitting, non_fitting) — candidates not selected are considered a
    "no", so the caller can ban them and never ask about them again."""
    prompt = prompt_template.format(
        artists_list=format_artist_list(candidates),
        station_name=station_name,
        description=description,
    )
    print(f"Asking AI to classify {len(candidates)} artist(s) against the station's theme...")
    parsed_data = call_openrouter_json(prompt)
    indices = parsed_data["selected_indices"]
    fitting = [candidates[i - 1] for i in indices if 0 < i <= len(candidates)]
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
    print(f"Generating audio with voice '{voice}': {text}")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)
    ensure_stereo(output_file)

def update_playlist(selected_tracks, dj_audio_file, playlist_file):
    """Updates the text playlist file for Liquidsoap. Returns the absolute
    paths written, in play order, for the caller to inspect (e.g. to time
    the block's total duration)."""
    files = [os.path.abspath(dj_audio_file)] + [track[0] for track in selected_tracks]
    with open(playlist_file, "w", encoding="utf-8") as f:
        for path in files:
            f.write(f"{path}\n")
    print(f"Playlist updated successfully in {playlist_file}")
    return files


def block_duration(file_paths):
    """Total playback duration (seconds) of a block, for scheduling the next
    generation. Files that fail to read (e.g. briefly missing/locked) are
    skipped rather than crashing the loop."""
    total = 0.0
    for path in file_paths:
        try:
            total += MutagenFile(path).info.length
        except Exception as e:
            print(f"Warning: could not read duration of {path}: {e}")
    return total

async def run_station(station_id):
    """Generates and writes one block for a station. Returns the list of
    audio file paths written (intro + songs), or None if it couldn't."""
    config = load_station_config(station_id)
    playlist_file = get_playlist_file(station_id)
    dj_audio_file = get_intro_audio_file(station_id)

    # 1. Load this station's artist roster (allowed + banned), building it on
    # first run so that even a random fallback pick stays on-theme instead of
    # drawing from the whole library.
    roster = load_artist_roster(station_id)
    if not roster["allowed"] and not roster["banned"]:
        print(f"No artist roster found for '{station_id}'. Building one from the whole library...")
        candidates = get_all_artists(config['folder_filter'])
        if not candidates:
            print(f"No artists found matching filter: {config['folder_filter']}!")
            return
        try:
            fitting, non_fitting = classify_artists(
                candidates, config['roster_prompt'], config['name'], config['description']
            )
            if not fitting:
                raise ValueError("AI did not select any artists for the roster.")
            roster["allowed"], roster["banned"] = fitting, non_fitting
        except Exception as e:
            print(f"Error building artist roster: {e}. Using the full candidate pool instead.")
            roster["allowed"] = candidates
        save_artist_roster(station_id, roster)
    else:
        # 1b. Check whether new artists have shown up in the library (e.g. a
        # rescan) since the roster was last built, and classify only those —
        # every artist we've ever seen ends up in either "allowed" or
        # "banned", so this stays cheap once the roster has caught up.
        known = set(roster["allowed"]) | set(roster["banned"])
        new_artists = [a for a in get_all_artists(config['folder_filter']) if a not in known]
        if new_artists:
            print(f"Found {len(new_artists)} new artist(s) in the library. Checking if they fit '{config['name']}'...")
            try:
                fitting, non_fitting = classify_artists(
                    new_artists, config['roster_prompt'], config['name'], config['description']
                )
                roster["allowed"].extend(fitting)
                roster["banned"].extend(non_fitting)
                save_artist_roster(station_id, roster)
                if fitting:
                    print(f"Added to roster: {', '.join(fitting)}")
            except Exception as e:
                print(f"Error classifying new artists: {e}. Will retry next run.")

    if not roster["allowed"]:
        print(f"Artist roster for '{station_id}' is empty. Nothing to play.")
        return

    print(f"--- Starting artist curation for station: {station_id} ({len(roster['allowed'])} artists in roster) ---")

    # 2. Offer the AI a random subset of the roster for this block (keeps the prompt small)
    artists_pool = random.sample(roster["allowed"], min(len(roster["allowed"]), 25))

    # 3. Ask AI to pick best artists and write intro using prompt from config
    try:
        indices, dj_text = curate_artists_and_script(
            artists_pool, config['prompt'], config['name'], config['description']
        )
        selected_artists = [artists_pool[i - 1] for i in indices if 0 < i <= len(artists_pool)]

        if len(selected_artists) == 0:
            raise ValueError("AI did not select any valid artists.")
    except Exception as e:
        print(f"Error during AI curation: {e}. Falling back to random selection.")
        selected_artists = random.sample(artists_pool, min(len(artists_pool), 3))
        # Use fallback script defined in JSON config, or default English text if missing
        dj_text = config.get('fallback_script', "Coming up next, some great music on our station.")

    # 4. Fetch one random track for each selected artist
    selected_tracks = []
    for artist in selected_artists:
        track = get_track_by_artist(artist, config['folder_filter'])
        if track:
            selected_tracks.append(track)

    if not selected_tracks:
        print("Error: Could not retrieve tracks for selected artists.")
        return

    await generate_audio(dj_text, dj_audio_file, config['voice'])
    return update_playlist(selected_tracks, dj_audio_file, playlist_file)


async def main():
    args = [a for a in sys.argv[1:] if a != "--loop"]
    loop_mode = "--loop" in sys.argv[1:]

    if len(args) < 1:
        print("Usage: python dj_agent.py <station_id> [--loop]")
        return

    station_id = args[0]

    if not loop_mode:
        await run_station(station_id)
        return

    # If a playlist file from a previous run is already sitting there (e.g.
    # this process crashed and got restarted by a supervisor), don't
    # immediately overwrite it — Liquidsoap may well be mid-way through
    # playing it. Work out how much of it is probably left and wait that out
    # first, so a restart never cuts off whatever's currently playing.
    playlist_file = get_playlist_file(station_id)
    if os.path.exists(playlist_file):
        with open(playlist_file, "r", encoding="utf-8") as f:
            existing_files = [line.strip() for line in f if line.strip()]
        duration = block_duration(existing_files)
        elapsed = time.time() - os.path.getmtime(playlist_file)
        remaining = max(0.0, duration - elapsed - BLOCK_LEAD_TIME)
        if remaining > 0:
            print(f"Existing block still has ~{remaining:.1f}s left; waiting before generating a new one.")
            await asyncio.sleep(remaining)

    # Liquidsoap on this deployment has no reliable way to signal "the last
    # track of the block just started" (see radio.liq), so instead of being
    # triggered reactively, this process keeps running and schedules its own
    # next run from each block's actual duration.
    while True:
        files = await run_station(station_id)
        if not files:
            print(f"Block generation failed for '{station_id}'; retrying in {BLOCK_LEAD_TIME}s.")
            await asyncio.sleep(BLOCK_LEAD_TIME)
            continue

        duration = block_duration(files)
        sleep_time = max(0.0, duration - BLOCK_LEAD_TIME)
        print(f"Block duration ~{duration:.1f}s. Sleeping {sleep_time:.1f}s before preparing the next one.")
        await asyncio.sleep(sleep_time)


if __name__ == "__main__":
    asyncio.run(main())
