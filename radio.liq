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

# Liquidsoap's own log goes to a spool file in the station's log folder, which
# dj_agent.py forwards into the station's daily log files and empties as it
# goes (see radio_log.py). It still goes to stdout (journald) as well.
# Liquidsoap refuses to start if the log folder is missing, so create it first.
log_dir = "#{project_dir}stations/#{station_id}/logs"
system("mkdir -p '#{log_dir}'")
set("log.file", true)
set("log.file.path", "#{log_dir}/liquidsoap.spool")
set("log.stdout", true)

# Each station keeps its runtime files in its own stations/<station_id>/
# folder, so multiple stations can run from the same directory without
# overwriting each other's files (must match get_playlist_file() in
# dj_agent.py).
playlist_path = "#{project_dir}stations/#{station_id}/playlist.txt"

# Load playlist in "watch" mode (automatically reloads when the file changes,
# which is how it picks up each new block written by dj_agent.py --loop).
# mode="normal" plays it in file order (the DJ intro first, then the songs in
# the order it announces them); Liquidsoap's default, "randomize", shuffles it.
playlist_source = playlist(mode="normal", reload_mode="watch", playlist_path)

# Report every track as it starts playing, with its metadata as JSON, under
# the "now_playing" label (dj_agent.py turns these into readable "Now
# playing" lines). Unlike the callbacks described at the top of this file,
# on_track wrapping the final source does fire in sync with real playback.
def log_now_playing(m) =
  log.important(label="now_playing", json_of(compact=true, m))
end

# Fallback source to prevent dead air if files are temporarily missing (it
# shows up in the logs as a track with no filename)
safe_radio = on_track(log_now_playing, mksafe(playlist_source))

# Output stream configuration for Icecast2
output.icecast(
  %mp3,
  host = icecast_host,
  port = icecast_port,
  password = icecast_password,
  mount = icecast_mount,
  safe_radio
)
