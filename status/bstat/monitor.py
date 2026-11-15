"""monitor.py — one place the front-ends read from: the printer link (or a demo / a recording), the
camera, and the status built from them.
Version: 2026.11.14.1

snapshot() -> {printer: normalized status, connection: {conn, detail, since, messages, last},
camera: {camera, detail, time}, source: 'printer' | 'demo' | 'replay', version, now}.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from . import __version__, hms
from . import state as S


class Monitor:
    def __init__(self, cfg: dict | None = None, demo: bool = False, replay: str | None = None,
                 camera: bool = True, pushall: bool = True, record: str | None = None):
        self.cfg = cfg or {}
        self.source = "demo" if demo else "replay" if replay else "printer"
        self._replay = replay
        self._lock = threading.Lock()
        self._listeners = []
        self.link = self.cam = None
        if self.source == "printer":
            from .client import Link
            self.link = Link(self.cfg, on_update=self._changed, pushall=pushall, record=record)
            if camera:
                from .camera import Camera
                self.cam = Camera(self.cfg)
        else:
            from .client import Link
            self.link = Link({"serial": "", "host": "demo"}, on_update=self._changed, pushall=False)

    def start(self):
        if self.source == "printer":
            self.link.start()
            if self.cam:
                self.cam.start()
        elif self.source == "demo":
            threading.Thread(target=self._demo_loop, daemon=True, name="bstat-demo").start()
        else:
            threading.Thread(target=self._replay_loop, daemon=True, name="bstat-replay").start()
        return self

    def stop(self):
        if self.source == "printer":
            self.link.stop()
            if self.cam:
                self.cam.stop()

    # -- listeners (the web page's event stream)
    def subscribe(self, fn):
        with self._lock:
            self._listeners.append(fn)

    def unsubscribe(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _changed(self, raw):
        with self._lock:
            ls = list(self._listeners)
        for fn in ls:
            try:
                fn()
            except Exception:
                pass

    def snapshot(self) -> dict:
        conn = self.link.status()
        if self.source != "printer":
            conn = {**conn, "conn": self.source, "detail": "demo data" if self.source == "demo" else self._replay}
        cam = self.cam.status() if self.cam else {"camera": "off", "detail": None}
        if self.cam:
            cam["time"] = self.cam.latest()[1]
        printer = S.normalize(self.link.raw(), conn.get("last"), hms_text=hms.text)
        for e in printer["errors"]:
            e["link"] = hms.link(e["code"])
        return {"printer": printer, "connection": conn, "camera": cam, "source": self.source,
                "version": __version__, "now": time.time()}

    def camera_jpeg(self):
        return self.cam.latest()[0] if self.cam else None

    # -- demo and replay
    def _demo_loop(self):
        self.link.feed(json.dumps(demo_full()).encode())
        t0 = time.time()
        while True:
            time.sleep(1.0)
            k = (time.time() - t0) / 120 % 1          # a two-minute "print"
            self.link.feed(json.dumps(demo_tick(k)).encode())

    def _replay_loop(self):
        lines = Path(self._replay).read_text().splitlines()
        prev = None
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("t")
            if prev is not None and t is not None:
                time.sleep(min(max(t - prev, 0), 2.0))
            prev = t
            self.link.feed(json.dumps(rec.get("msg", rec)).encode(), rec.get("topic", ""))


def demo_full() -> dict:
    trays = lambda specs: [{"id": str(i), "tray_type": t, "tray_color": c, "remain": r, "tray_sub_brands": b,
                            "nozzle_temp_min": "190", "nozzle_temp_max": "230"} if t else {"id": str(i)}
                           for i, (t, c, r, b) in enumerate(specs)]
    return {"print": {
        "command": "push_status", "gcode_state": "RUNNING", "subtask_name": "two_colour_tile", "mc_percent": 0,
        "mc_remaining_time": 18, "layer_num": 1, "total_layer_num": 20, "mc_print_stage": 2, "spd_lvl": 2,
        "nozzle_temper": 220.0, "nozzle_target_temper": 220.0, "bed_temper": 55.0, "bed_target_temper": 55.0,
        "chamber_temper": 31.0, "cooling_fan_speed": "15", "big_fan1_speed": "0", "big_fan2_speed": "6",
        "heatbreak_fan_speed": "15", "nozzle_diameter": "0.4", "nozzle_type": "hardened_steel", "wifi_signal": "-52dBm",
        "lights_report": [{"node": "chamber_light", "mode": "on"}], "hms": [], "print_error": 0,
        "ams": {"tray_now": "0", "ams": [{"id": "0", "humidity": "4", "humidity_raw": "21", "temp": "27.5",
                                         "tray": trays([("PLA", "FFFFFFFF", 82, "PLA Basic"), ("PLA", "1E88E5FF", 64, "PLA Basic"),
                                                        ("PETG", "202020FF", 35, "PETG HF"), (None, None, None, None)])}]},
        "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000"}}}


def demo_tick(k: float) -> dict:
    layer = 1 + int(k * 19)
    out = {"print": {"mc_percent": int(k * 100), "layer_num": layer, "mc_remaining_time": max(0, int(18 * (1 - k))),
                     "nozzle_temper": round(220 + math.sin(time.time()) * 0.6, 1),
                     "bed_temper": round(55 + math.sin(time.time() / 3) * 0.3, 1),
                     "ams": {"tray_now": "0" if layer <= 10 else "1"}}}
    if 0.55 < k < 0.6:
        out["print"]["hms"] = [{"attr": 0x07000200, "code": 0x00020001}]
    elif k >= 0.6:
        out["print"]["hms"] = []
    return out
