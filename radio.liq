# Generic AI radio station player.
#
# Station-specific details (which station to run, Icecast connection) come
# from environment variables, so the same script can serve any station
# defined in stations.json — just launch another instance with a different
# STATION_ID.
#
# This script only plays whatever is in the station's playlist file; it does
# not trigger the AI DJ agent itself. That's a deliberate choice: on this
# deployment's Liquidsoap version, none of the track-boundary callbacks
# (on_end, on_track, on_metadata) fire reliably in sync with real playback
# for a playlist() source — testing showed on_end never firing at all when
# wrapping playlist(), on_track firing instantly for the whole list at load
# time, and on_metadata firing once with the wrong track's data. Instead,
# `dj_agent.py <station> --loop` runs as its own long-lived process (start it
# separately, e.g. via systemd or nohup) and schedules itself from each
# block's own known duration.

project_dir = "./"

station_id = getenv("STATION_ID")
icecast_password = getenv("ICECAST_PASSWORD")

# getenv() here has no "default" argument, so we fall back manually
requested_host = getenv("ICECAST_HOST")
icecast_host = if requested_host != "" then requested_host else "127.0.0.1" end

requested_port = getenv("ICECAST_PORT")
icecast_port = int_of_string(if requested_port != "" then requested_port else "8000" end)

# Mount defaults to "/<station_id>" unless explicitly overridden
requested_mount = getenv("ICECAST_MOUNT")
icecast_mount = if requested_mount != "" then requested_mount else "/#{station_id}" end

if station_id == "" then
  print("ERROR: STATION_ID env var is not set (see .env.example).")
end
if icecast_password == "" then
  print("ERROR: ICECAST_PASSWORD env var is not set (see .env.example).")
end

# Each station gets its own playlist file, so multiple stations can run from
# the same directory without overwriting each other's files.
playlist_path = "#{project_dir}dj_playlist_#{station_id}.txt"

# Load playlist in "watch" mode (automatically reloads when the file changes,
# which is how it picks up each new block written by dj_agent.py --loop)
playlist_source = playlist(reload_mode="watch", playlist_path)

# Fallback source to prevent dead air if files are temporarily missing
safe_radio = mksafe(playlist_source)

# Output stream configuration for Icecast2
output.icecast(
  %mp3,
  host = icecast_host,
  port = icecast_port,
  password = icecast_password,
  mount = icecast_mount,
  safe_radio
)
