"""Minimal client for the command server radio.liq exposes on a unix socket.

Each command opens its own short-lived connection: the server answers with
the command's output followed by an "END" line, and closes the connection
after "quit".
"""

import socket

# Must match the request.equeue id in radio.liq.
QUEUE_ID = "blocks"


class Liquidsoap:
    def __init__(self, socket_path, timeout=5.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def command(self, command):
        """Runs one server command and returns its output lines. Raises
        OSError if Liquidsoap isn't reachable (not running, still starting)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall(f"{command}\nquit\n".encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        lines = b"".join(chunks).decode("utf-8", "replace").replace("\r", "").split("\n")
        return [line for line in lines if line and line not in ("END", "Bye!")]

    def push(self, path):
        """Appends a file to the block queue; returns its request ID."""
        return int(self.command(f"{QUEUE_ID}.push {path}")[0])

    def queue(self):
        """Request IDs in the block queue, including the one playing now."""
        return [int(rid) for line in self.command(f"{QUEUE_ID}.queue") for rid in line.split()]

    def on_air(self):
        """Request IDs currently on air."""
        return [int(rid) for line in self.command("request.on_air") for rid in line.split()]

    def remaining(self):
        """Seconds left in the track playing now (0 if unknown, e.g. while
        Liquidsoap plays silence, which reports an infinite length)."""
        lines = self.command("remaining")
        seconds = float(lines[0]) if lines else 0.0
        return seconds if 0.0 <= seconds < float("inf") else 0.0

    def metadata(self, rid):
        """A request's metadata as a dict (e.g. its "filename")."""
        metadata = {}
        for line in self.command(f"request.metadata {rid}"):
            key, sep, value = line.partition("=")
            if sep:
                metadata[key] = value.strip('"')
        return metadata
