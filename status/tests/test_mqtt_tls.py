"""bstat's Link against a small fake printer: an MQTT 3.1.1 broker over TLS with a self-signed certificate,
user 'bblp' and an access code, as the printer runs it. Checks the real paho-mqtt path: login, subscribe,
the pushall request (the only message sent), reports merged, and a refused login explained.
Version: 2026.11.14.1
"""
import json
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
paho = pytest.importorskip("paho.mqtt.client")
from bstat import client  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not found")


def _remlen(sock):
    mult, val = 1, 0
    while True:
        b = sock.recv(1)[0]
        val += (b & 127) * mult
        if not b & 128:
            return val
        mult *= 128


def _enc_len(n):
    out = b""
    while True:
        d, n = n % 128, n // 128
        out += bytes([d | (128 if n else 0)])
        if not n:
            return out


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError
        buf += c
    return buf


def _str(b, i):
    n = int.from_bytes(b[i:i + 2], "big")
    return b[i + 2:i + 2 + n], i + 2 + n


class FakePrinter:
    def __init__(self, tmp, code="12345678", serial="01P00A000000001", reports=()):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=printer",
                        "-keyout", str(tmp / "k.pem"), "-out", str(tmp / "c.pem")], check=True, capture_output=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
        self.code, self.serial, self.reports = code, serial, list(reports)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.received, self.logins, self.subscribed = [], [], []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def _session(self, raw):
        try:
            s = self.ctx.wrap_socket(raw, server_side=True)
            while True:
                h = _recv(s, 1)[0]
                body = _recv(s, _remlen(s))
                kind = h >> 4
                if kind == 1:                                       # CONNECT
                    i = 6 + 1                                        # protocol name (2+4) + level
                    flags = body[i]
                    i += 3                                           # flags + keepalive
                    _, i = _str(body, i)                             # client id
                    user, i = _str(body, i) if flags & 0x80 else (b"", i)
                    pw, i = _str(body, i) if flags & 0x40 else (b"", i)
                    self.logins.append((user.decode(), pw.decode()))
                    ok = user == b"bblp" and pw.decode() == self.code
                    s.sendall(bytes([0x20, 2, 0, 0 if ok else 5]))
                    if not ok:
                        s.close()
                        return
                elif kind == 8:                                     # SUBSCRIBE
                    pid = body[:2]
                    topic, _ = _str(body, 2)
                    self.subscribed.append(topic.decode())
                    s.sendall(bytes([0x90, 3]) + pid + b"\x00")
                    for r in self.reports:
                        t = f"device/{self.serial}/report".encode()
                        p = len(t).to_bytes(2, "big") + t + json.dumps(r).encode()
                        s.sendall(bytes([0x30]) + _enc_len(len(p)) + p)
                elif kind == 3:                                     # PUBLISH from the client
                    topic, i = _str(body, 0)
                    self.received.append((topic.decode(), json.loads(body[i:])))
                elif kind == 12:                                    # PINGREQ
                    s.sendall(bytes([0xD0, 0]))
                elif kind == 14:                                    # DISCONNECT
                    s.close()
                    return
        except (ConnectionError, OSError, IndexError):
            return

    def close(self):
        self.sock.close()


def _wait(cond, t=10.0):
    t0 = time.time()
    while time.time() - t0 < t:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_link_reads_reports_and_sends_only_pushall(tmp_path, monkeypatch):
    fp = FakePrinter(tmp_path, reports=[{"print": {"gcode_state": "RUNNING", "mc_percent": 12, "nozzle_temper": 219.5}},
                                        {"print": {"mc_percent": 13}}])
    monkeypatch.setattr(client, "PORT", fp.port)
    link = client.Link({"host": "127.0.0.1", "serial": fp.serial, "access_code": fp.code}).start()
    try:
        assert _wait(lambda: link.status()["messages"] >= 2), link.status()
        assert link.status()["conn"] == "connected"
        p = link.raw()["print"]
        assert p["mc_percent"] == 13 and p["nozzle_temper"] == 219.5 and p["gcode_state"] == "RUNNING"
        assert fp.logins == [("bblp", fp.code)] and fp.subscribed == [f"device/{fp.serial}/report"]
        assert _wait(lambda: fp.received)
        assert fp.received == [(f"device/{fp.serial}/request", client.PUSHALL)]
    finally:
        link.stop()
        fp.close()


def test_link_refused_is_explained(tmp_path, monkeypatch):
    fp = FakePrinter(tmp_path)
    monkeypatch.setattr(client, "PORT", fp.port)
    link = client.Link({"host": "127.0.0.1", "serial": fp.serial, "access_code": "wrong000"}, pushall=False).start()
    try:
        assert _wait(lambda: link.status()["conn"] == "refused"), link.status()
        assert "Developer Mode" in link.status()["detail"] and fp.received == []
    finally:
        link.stop()
        fp.close()
