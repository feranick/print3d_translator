"""client.py — the read-only link to the printer's status channel (MQTT over TLS on the local network).
Version: 2026.11.14.1

  broker   the printer itself, <host>:8883, TLS with the printer's own certificate (self-signed by
           Bambu: not verified, as every LAN client does)
  login    user 'bblp', password = the LAN access code shown on the printer
  listens  device/<serial>/report
  sends    only {"pushing": {"command": "pushall"}} on device/<serial>/request (a status request: the
           printer answers with its full state), once on connect and then at most every PUSHALL_EVERY_S;
           pushall=False sends nothing at all. There is no other publish in this module (a test checks it).

A refused login (MQTT 'not authorized') usually means the printer only allows LAN clients in Developer
Mode (P2S: LAN Only Mode, then Developer Mode); the message says so.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from pathlib import Path

from . import state as S

PORT = 8883
USER = "bblp"
PUSHALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}
PUSHALL_EVERY_S = 300
REFUSED = {4: "bad user name or password: check the access code (it changes when LAN mode is toggled)",
           5: "not authorized: either the access code is wrong, or the printer refuses LAN clients in this mode. On the "
              "P2S, LAN access for other programs may need LAN Only Mode + Developer Mode (Settings > Network); "
              "Bambu Cloud and Handy stop working there"}
REFUSED[134], REFUSED[135] = REFUSED[4], REFUSED[5]      # paho 2 reports MQTT 3.1.1 codes 4 / 5 as 134 / 135


class Link:
    """Connects in the background (paho's thread). status() -> {conn, detail, since, messages, last};
    raw() -> the merged report. on_update(raw) is called after every message."""

    def __init__(self, cfg: dict, on_update=None, pushall: bool = True, record: str | Path | None = None):
        self.cfg, self.on_update, self.pushall = cfg, on_update, pushall
        self.record = Path(record) if record else None
        self._raw: dict = {}
        self._lock = threading.Lock()
        self._status = {"conn": "idle", "detail": None, "since": time.time(), "messages": 0, "last": None}
        self._last_pushall = 0.0
        self._client = None

    # -- state
    def raw(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._raw))

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, conn, detail=None):
        with self._lock:
            if self._status["conn"] != conn or self._status["detail"] != detail:
                self._status.update(conn=conn, detail=detail, since=time.time())

    # -- connection
    def start(self):
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bstat-{int(time.time()) % 100000}",
                        protocol=mqtt.MQTTv311)
        c.username_pw_set(USER, self.cfg["access_code"])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 30)
        self._client = c
        self._set("connecting", f"{self.cfg['host']}:{PORT}")
        c.connect_async(self.cfg["host"], PORT, keepalive=30)
        c.loop_start()
        return self

    def stop(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        self._set("stopped")

    def _topic(self, kind: str) -> str:
        return f"device/{self.cfg['serial']}/{kind}"

    def _on_connect(self, client, userdata, flags, reason, properties=None):
        rc = getattr(reason, "value", reason)
        if rc != 0:
            self._set("refused", REFUSED.get(rc, f"MQTT refused the connection: {reason}"))
            client.disconnect()
            return
        self._set("connected", f"{self.cfg['host']}:{PORT}")
        client.subscribe(self._topic("report"), qos=0)
        self._request_pushall()

    def _on_disconnect(self, client, userdata, flags, reason, properties=None):
        with self._lock:
            refused = self._status["conn"] == "refused"
        if not refused:
            self._set("reconnecting", f"disconnected: {reason}")

    def _request_pushall(self):
        """The only publish: a status request. Nothing else is ever sent."""
        if not self.pushall or not self._client:
            return
        now = time.time()
        if now - self._last_pushall < 10:
            return
        self._last_pushall = now
        self._client.publish(self._topic("request"), json.dumps(PUSHALL), qos=0)

    def _on_message(self, client, userdata, msg):
        self.feed(msg.payload, msg.topic)
        if self.pushall and time.time() - self._last_pushall > PUSHALL_EVERY_S:
            self._request_pushall()

    def feed(self, payload: bytes, topic: str = "") -> bool:
        """One report message: merged into the state (also used by tests and replays)."""
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        with self._lock:
            S.merge(self._raw, data)
            self._status["messages"] += 1
            self._status["last"] = time.time()
        if self.record:
            self.record.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"t": round(time.time(), 3), "topic": topic, "msg": data})
            serial = self.cfg.get("serial") or ""
            if serial:
                line = line.replace(serial, "<serial>")          # recordings never carry the serial number
            with self.record.open("a") as f:
                f.write(line + "\n")
        if self.on_update:
            self.on_update(self.raw())
        return True
