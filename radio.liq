# Generic AI radio station player.
#
# Station-specific details (which station to run, Icecast connection) come
# from environment variables, so the same script can serve any station
# defined in stations.json — just launch another instance with a different
# STATION_ID.
#
# This script doesn't pick any music itself. `dj_agent.py <station> --loop`
# runs as its own long-lived process (start it separately, e.g. via systemd)
# and, over this script's command socket, pushes each block (DJ intro +
# songs) onto the "blocks" request queue, and watches how much of it is left
# to know when to prepare the next one. Queued tracks play exactly once, in
# order, and a new block never cuts off what's playing. If the queue runs dry
# (the agent is late or down), tracks from the station's fallback list, also
# written by dj_agent.py, fill in rather than silence.

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

# Stream details shown by Icecast and players (name, description, genre,
# url), from the station's entry in stations.json via stream_info.py, since
# this Liquidsoap version can't parse stations.json itself.
stream_info = of_json(
  default=[("name", "")],
  get_process_output("python3 #{project_dir}stream_info.py #{string.quote(station_id)}")
)
def stream_field(key, fallback) =
  value = list.assoc(default="", key, stream_info)
  if value != "" then value else fallback end
end

if station_id == "" then
  print("ERROR: STATION_ID env var is not set (see .env.example).")
end
if icecast_password == "" then
  print("ERROR: ICECAST_PASSWORD env var is not set (see .env.example).")
end

# Each station keeps its runtime files in its own stations/<station_id>/
# folder, so multiple stations can run from the same directory without
# overwriting each other's files (the paths below must match the get_*_file()
# helpers in dj_agent.py).
station_dir = "#{project_dir}stations/#{station_id}"

# Liquidsoap's own log goes to a spool file in the station's log folder, which
# dj_agent.py forwards into the station's daily log files and empties as it
# goes (see radio_log.py). It still goes to stdout (journald) as well.
# Liquidsoap refuses to start if the log folder is missing, so create it first.
log_dir = "#{station_dir}/logs"
system("mkdir -p '#{log_dir}'")
set("log.file", true)
set("log.file.path", "#{log_dir}/liquidsoap.spool")
set("log.stdout", true)

# Command server dj_agent.py talks to (see liquidsoap_client.py).
set("server.socket", true)
set("server.socket.path", "#{station_dir}/liquidsoap.sock")

# The blocks dj_agent.py queues, played in the order they were pushed.
blocks = request.equeue(id="blocks")

# Plays only while the block queue is empty. Watched, so it picks up the list
# as soon as dj_agent.py (re)writes it, even if it didn't exist at startup.
fallback_tracks = playlist(
  id="fallback", mode="randomize", reload_mode="watch",
  "#{station_dir}/fallback.txt"
)

# track_sensitive: only ever switch between the two at track boundaries.
radio = fallback(track_sensitive=true, [blocks, fallback_tracks])

# Report every track as it starts playing, with its metadata as JSON, under
# the "now_playing" label (dj_agent.py turns these into readable "Now
# playing" lines). on_track wrapping the final source fires in sync with real
# playback (unlike e.g. playlist()'s own on_track parameter, which fires for
# a whole list as soon as it's loaded).
# Its artist/title are also kept, to resend to Icecast after reconnecting.
current_title = ref []
def log_now_playing(m) =
  log.important(label="now_playing", json_of(compact=true, m))
  current_title := list.filter(fun (kv) -> fst(kv) == "artist" or fst(kv) == "title", m)
end

# mksafe plays silence if there's nothing at all to play (e.g. the fallback
# list isn't written yet); it shows up in the logs as a track with no filename
safe_radio = on_track(log_now_playing, mksafe(radio))

# Icecast only learns the stream's title when a track starts, so after it
# restarts (or the connection drops) it would show no title until the next
# track. On every (re)connection, re-insert the current track's artist/title.
with_title = insert_metadata(safe_radio)
insert_title = fst(with_title)
def resend_title() =
  if !current_title != [] then insert_title(new_track=false, !current_title) end
end

# Lets dj_agent.py see how much of the current track is left.
server.register(
  usage="remaining", description="Seconds left in the track playing now.",
  "remaining", fun (_) -> string_of(source.remaining(safe_radio))
)

# Output stream configuration for Icecast2
output.icecast(
  %mp3,
  host = icecast_host,
  port = icecast_port,
  password = icecast_password,
  mount = icecast_mount,
  name = stream_field("name", station_id),
  description = stream_field("description", ""),
  genre = stream_field("genre", ""),
  url = stream_field("url", ""),
  on_connect = resend_title,
  snd(with_title)
)
