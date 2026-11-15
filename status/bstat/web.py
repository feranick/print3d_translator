"""web.py — the status as a local web page that updates by itself.
Version: 2026.11.14.1

  /            the page (self-contained: no CDN, no external fonts)
  /state       the snapshot as JSON
  /events      the snapshot pushed on every change (server-sent events), at least every 2 s
  /camera.jpg  the latest camera snapshot (404 when there is none)

Bound to 127.0.0.1 unless --lan is given (then any device on your network can read the status, never
control the printer: the page has no controls).
"""
from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("page.html")


def make_handler(mon):
    class H(BaseHTTPRequestHandler):
        server_version = "bstat"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/state":
                return self._send(200, json.dumps(mon.snapshot()).encode(), "application/json")
            if path == "/camera.jpg":
                img = mon.camera_jpeg()
                return self._send(200, img, "image/jpeg") if img else self._send(404, b"no camera image", "text/plain")
            if path == "/events":
                return self._events()
            return self._send(404, b"not found", "text/plain")

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = queue.Queue(maxsize=1)

            def poke():
                try:
                    q.put_nowait(1)
                except queue.Full:
                    pass
            mon.subscribe(poke)
            try:
                while True:
                    self.wfile.write(b"data: " + json.dumps(mon.snapshot()).encode() + b"\n\n")
                    self.wfile.flush()
                    try:
                        q.get(timeout=2.0)
                    except queue.Empty:
                        pass
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                mon.unsubscribe(poke)
    return H


def serve(mon, port: int = 8765, lan: bool = False, open_browser: bool = True):
    host = "0.0.0.0" if lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), make_handler(mon))
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"bstat: {url}" + (" (also on this Mac's network address)" if lan else "") + "  — Ctrl-C to stop")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
