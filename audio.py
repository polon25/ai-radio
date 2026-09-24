"""Audio helpers: the DJ's speech (Edge TTS, levelled to match the music),
and music files' length and loudness."""

import asyncio
import json
import logging
import os
import re
import subprocess

import edge_tts
import imageio_ffmpeg
from mutagen import File as MutagenFile

log = logging.getLogger("audio")

# Loudness (LUFS) DJ intros and news are raised to, before a limiter shaves
# their peaks (which leaves them about 1.5 LU below it): a little below the
# typical mastered music they play between, which is often around -8.
SPEECH_LOUDNESS_LUFS = float(os.getenv("SPEECH_LOUDNESS_LUFS", "-10"))

# Edge TTS is an online service: seconds to wait before each retry (so one
# try more than there are delays) before giving up, e.g. a block then goes
# on air without its intro. Blocks are prepared minutes ahead, so this can ride out a DNS or
# network hiccup of up to about a minute.
TTS_RETRY_DELAYS = (5, 15, 30)


def finish_speech_audio(path):
    """Re-encodes Edge TTS output in place so it plays well between songs:

    - Edge TTS outputs mono MP3s, but some Liquidsoap decoders (e.g. 1.4.x)
      require the file's channel count to exactly match
      `frame.audio.channels` (2 by default) and refuse to play anything
      else, so the channel is duplicated to stereo.
    - Its speech is around -20 LUFS, far quieter than most mastered music
      (around -8), so it's normalized to SPEECH_LOUDNESS_LUFS."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # Measure, then raise the level by the difference, with a limiter
    # catching the few peaks that would clip (loudnorm alone won't raise
    # speech that far without exceeding its peak limit).
    measured = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    loudness = float(json.loads(measured[measured.rindex("{"):measured.rindex("}") + 1])["input_i"])
    gain = SPEECH_LOUDNESS_LUFS - loudness
    tmp_path = f"{path}.finished.tmp.mp3"
    subprocess.run(
        [ffmpeg, "-y", "-i", path, "-af", f"volume={gain:.1f}dB,alimiter=limit=0.84:level=false",
         "-ar", "44100", "-ac", "2", "-b:a", "128k", tmp_path],
        check=True, capture_output=True,
    )
    os.replace(tmp_path, path)


async def generate_audio(text, output_file, voice):
    """Converts a DJ intro's or news segment's text to speech using Edge
    TTS (an online service), retrying a few times. Returns whether it
    succeeded, so e.g. a network hiccup costs a block its intro rather than
    the whole block.

    The audio is made in a temporary file and only moved into place once
    finished, so Liquidsoap never opens a half-made file (Edge TTS writes
    mono, which it refuses to play)."""
    log.info(f"Generating speech with voice '{voice}': {text}")
    attempts = len(TTS_RETRY_DELAYS) + 1
    tmp_file = f"{output_file}.tts.tmp.mp3"
    for attempt in range(1, attempts + 1):
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(tmp_file)
            finish_speech_audio(tmp_file)
            os.replace(tmp_file, output_file)
            return True
        except Exception as e:
            log.warning(f"Speech synthesis failed (attempt {attempt}/{attempts}): {e!r}")
            if attempt < attempts:
                await asyncio.sleep(TTS_RETRY_DELAYS[attempt - 1])
    log.error(f"Couldn't synthesize the speech for {os.path.basename(output_file)}.")
    return False


def track_duration(path):
    """Playback duration of an audio file in seconds, or 0 if it can't be
    read (e.g. briefly missing/locked) rather than crashing the loop."""
    try:
        return MutagenFile(path).info.length
    except Exception as e:
        log.warning(f"Could not read duration of {path}: {e}")
        return 0.0


def block_duration(file_paths):
    """Total playback duration (seconds) of a block."""
    return sum(track_duration(path) for path in file_paths)


def measure_loudness(path):
    """A file's integrated loudness (LUFS) and true peak (dBTP), per EBU
    R128. Decodes the whole file, so it takes a second or two."""
    output = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostats", "-i", path,
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    summary = output[output.rindex("Summary:"):]
    loudness = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary).group(1))
    return loudness, peak
