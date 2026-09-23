"""Prints a station's stream details (for its Icecast mount) as a flat JSON
object of strings: name, description, genre and url, taken from its entry in
stations.json (empty if unset).

radio.liq runs this at startup: Liquidsoap 1.4 can only parse JSON whose
values all share one type, which stations.json's don't. Uses only the
standard library, so any python3 will do.

Usage: python3 stream_info.py <station_id>
"""

import json
import sys

CONFIG_FILE = "stations.json"
FIELDS = ("name", "description", "genre", "url")


def main():
    station = {}
    if len(sys.argv) > 1:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                station = json.load(f).get(sys.argv[1], {})
        except (OSError, ValueError) as e:
            print(f"stream_info.py: can't read {CONFIG_FILE}: {e}", file=sys.stderr)
    info = {field: station.get(field, "") for field in FIELDS}
    print(json.dumps({k: v if isinstance(v, str) else "" for k, v in info.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
