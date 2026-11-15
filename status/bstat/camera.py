"""camera.py — camera snapshots from the printer, read-only, when the printer allows it.
Version: 2026.11.14.1

The P1 / A1 family streams JPEG frames on TLS port 6000 after an 80-byte login (user 'bblp' and the access
code); the X1 family uses RTSPS on port 322 instead, which is not read here. Which one the P2S uses, and
whether it answers outside Developer Mode, is what 'bstat probe' finds out. On any failure the camera is
reported as unavailable and everything else keeps working.
"""
from __future__ import annotations

import socket
import ssl
import struct
import threading
import time

PORT = 6000


def _login(code: str) -> bytes:
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.encode()[:32].ljust(32, b"\0")


def _read(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("camera closed the connection")
        buf += chunk
    return buf


def snapshot(host: str, code: str, timeout: float = 8.0) -> bytes:
    """One JPEG frame. Raises OSError / ConnectionError / ValueError when the camera does not answer."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, PORT), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            s.settimeout(timeout)
            s.sendall(_login(code))
            for _ in range(5):
                head = _read(s, 16)
                size = int.from_bytes(head[0:4], "little")
                if not 0 < size < 8_000_000:
                    raise ValueError(f"camera sent an unexpected frame header ({head.hex()})")
                img = _read(s, size)
                if img[:2] == b"\xff\xd8":
                    return img
    raise ValueError("camera sent no JPEG frame")


class Camera:
    """Takes a snapshot every period_s in a background thread. latest() -> (jpeg bytes or None, time);
    status() -> {camera: 'ok' | 'unavailable' | 'off', detail}."""

    def __init__(self, cfg: dict, period_s: float = 5.0):
        self.cfg, self.period = cfg, period_s
        self._img, self._t, self._detail, self._state = None, None, None, "off"
        self._stop = threading.Event()
        self._fails = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="bstat-camera").start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._img, self._t = snapshot(self.cfg["host"], self.cfg["access_code"]), time.time()
                self._state, self._detail, self._fails = "ok", None, 0
            except (OSError, ConnectionError, ValueError) as e:
                self._fails += 1
                self._state, self._detail = "unavailable", f"port {PORT}: {e}"
            # back off when the camera keeps refusing (up to 2 minutes)
            self._stop.wait(self.period if self._fails == 0 else min(120, self.period * 2 ** min(self._fails, 5)))

    def latest(self):
        return self._img, self._t

    def status(self) -> dict:
        return {"camera": self._state, "detail": self._detail}
