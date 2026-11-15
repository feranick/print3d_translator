"""Tests of bstat without a printer: merging and decoding reports, the read-only rule, the monitor in demo
mode, the terminal and web front-ends, config and HMS lookup.
Version: 2026.11.14.1
"""
import json
import re
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bstat import camera, client, config as CF, hms, monitor, state as S, tui  # noqa: E402


# ------------------------------------------------------------- state

def test_merge_partial_reports_and_ams_by_id():
    base = monitor.demo_full()
    S.merge(base, {"print": {"mc_percent": 40, "ams": {"tray_now": "1", "ams": [{"id": "0", "tray": [{"id": "1", "remain": 50}]}]}}})
    p = base["print"]
    assert p["mc_percent"] == 40 and p["nozzle_temper"] == 220.0             # untouched fields kept
    trays = p["ams"]["ams"][0]["tray"]
    assert len(trays) == 4 and trays[1]["remain"] == 50 and trays[1]["tray_type"] == "PLA"
    S.merge(base, {"print": {"hms": [{"attr": 1, "code": 2}]}})
    S.merge(base, {"print": {"hms": []}})
    assert base["print"]["hms"] == []                                         # an empty list clears errors


def test_normalize_demo():
    n = S.normalize(monitor.demo_full(), received=1000.0)
    assert n["state"] == "printing" and n["file"] == "two_colour_tile" and n["layer"] == [1, 20]
    assert n["eta"] == 1000.0 + 18 * 60
    assert n["temps"] == {"nozzle": [220.0, 220.0], "bed": [55.0, 55.0], "chamber": 31.0}
    assert n["fans"] == {"part": 100, "aux": 0, "chamber": 40, "heatbreak": 100}
    u = n["ams"]["units"][0]
    assert [t["label"] for t in u["trays"]] == ["A1", "A2", "A3", "A4"]
    assert u["trays"][0]["active"] and u["trays"][0]["color"] == "#FFFFFF" and u["trays"][3]["empty"]
    assert u["humidity_pct"] == 21 and n["ams"]["active"] == "A1" and n["ams"]["external"]["empty"]
    assert n["speed"] == "standard" and n["errors"] == []


def test_normalize_packed_device_temperatures_and_errors():
    raw = {"print": {"gcode_state": "PAUSE", "device": {"extruder": {"info": [{"id": 0, "temp": (220 << 16) | 218}]},
                                                       "bed": {"info": {"temp": (60 << 16) | 59}}},
                     "hms": [{"attr": 0x07000200, "code": 0x00020001}], "print_error": 0x0300800A,
                     "ams": {"tray_now": "254"}, "vt_tray": {"tray_type": "PETG", "tray_color": "FF8000FF"}}}
    n = S.normalize(raw, hms_text=lambda c: "AMS slot 1 is empty" if c == "0700_0200_0002_0001" else None)
    assert n["state"] == "paused" and n["temps"]["nozzle"] == [218.0, 220.0] and n["temps"]["bed"] == [59.0, 60.0]
    assert n["errors"][0] == {"code": "0700_0200_0002_0001", "kind": "hms", "text": "AMS slot 1 is empty"}
    assert n["errors"][1]["code"] == "0300_800A" and n["errors"][1]["text"] is None
    assert n["ams"]["active"] == "External" and n["ams"]["external"]["active"] and n["ams"]["external"]["color"] == "#FF8000"


def test_normalize_empty_report():
    n = S.normalize({})
    assert n["state"] is None and n["percent"] is None and n["ams"]["units"] == [] and n["errors"] == []


# ------------------------------------------------------------- read-only

def test_the_only_publish_is_pushall():
    src = (Path(client.__file__)).read_text()
    assert src.count(".publish(") == 1
    body = src[src.index("def _request_pushall"):src.index("def _on_message")]
    assert ".publish(" in body and 'self._topic("request"), json.dumps(PUSHALL)' in body
    assert client.PUSHALL == {"pushing": {"sequence_id": "0", "command": "pushall"}}
    for f in Path(client.__file__).parent.glob("*.py"):           # no other module talks MQTT
        if f.name != "client.py":
            assert ".publish(" not in f.read_text(), f.name


class FakeMQTT:
    def __init__(self):
        self.published, self.subscribed, self.disconnected = [], [], False

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def disconnect(self):
        self.disconnected = True


def test_connect_subscribes_and_requests_status_once():
    link = client.Link({"host": "10.0.0.5", "serial": "01P00A123456789", "access_code": "12345678"})
    link._client = fake = FakeMQTT()
    link._on_connect(fake, None, {}, 0)
    assert fake.subscribed == ["device/01P00A123456789/report"]
    assert fake.published == [("device/01P00A123456789/request", client.PUSHALL)]
    link._on_connect(fake, None, {}, 0)                           # rate-limited
    assert len(fake.published) == 1 and link.status()["conn"] == "connected"


def test_no_pushall_sends_nothing_and_refusal_is_explained():
    link = client.Link({"host": "h", "serial": "S", "access_code": "c"}, pushall=False)
    link._client = fake = FakeMQTT()
    link._on_connect(fake, None, {}, 0)
    assert fake.published == []
    link._on_connect(fake, None, {}, 5)
    st = link.status()
    assert st["conn"] == "refused" and "Developer Mode" in st["detail"] and fake.disconnected


def test_feed_records_without_serial_or_code(tmp_path):
    rec = tmp_path / "r.jsonl"
    link = client.Link({"host": "h", "serial": "01P00A123456789", "access_code": "secret12"}, record=rec)
    assert link.feed(json.dumps({"print": {"sn": "01P00A123456789", "mc_percent": 5}}).encode(), "device/01P00A123456789/report")
    assert not link.feed(b"not json")
    text = rec.read_text()
    assert "01P00A123456789" not in text and "<serial>" in text and "secret12" not in text
    assert link.raw()["print"]["mc_percent"] == 5 and link.status()["messages"] == 1


def test_camera_login_packet():
    p = camera._login("12345678")
    assert len(p) == 80 and p[16:20] == b"bblp" and p[48:56] == b"12345678"


# ------------------------------------------------------------- monitor and front-ends

def demo_monitor():
    m = monitor.Monitor(demo=True)
    m.link.feed(json.dumps(monitor.demo_full()).encode())
    m.link.feed(json.dumps(monitor.demo_tick(0.58)).encode())       # with an HMS error
    return m


def test_snapshot_demo():
    s = demo_monitor().snapshot()
    assert s["source"] == "demo" and s["connection"]["conn"] == "demo" and s["camera"]["camera"] == "off"
    assert s["printer"]["percent"] == 58 and s["printer"]["ams"]["active"] == "A2"
    e = s["printer"]["errors"][0]
    assert e["code"] == "0700_0200_0002_0001" and e["link"].startswith("https://wiki.bambulab.com/")
    json.dumps(s)                                                  # serializable for the web page


def test_tui_render():
    out = tui.render(demo_monitor().snapshot(), 90)
    plain = re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", out)
    for want in ("Printer status", "printing", "two_colour_tile", "58%", "layer 12 of 20", "nozzle 2", "→ 220°",
                 "A2", "◀ in use", "0700_0200_0002_0001", "read-only"):
        assert want in plain, want


def test_web_page_state_events_and_no_camera():
    from http.server import ThreadingHTTPServer
    from bstat import web
    m = demo_monitor()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(m))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>Printer status</title>" in page and "EventSource" in page and "http" not in page.split("<script>")[0].replace("http-equiv", "")
        st = json.loads(urllib.request.urlopen(base + "/state").read())
        assert st["printer"]["file"] == "two_colour_tile"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/camera.jpg")
        r = urllib.request.urlopen(base + "/events", timeout=5)
        line = r.readline().decode()
        assert line.startswith("data: ") and json.loads(line[6:])["source"] == "demo"
        r.close()
    finally:
        srv.shutdown()


# ------------------------------------------------------------- config and HMS

def test_config_save_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(CF, "PATH", tmp_path / "printer.json")
    for k in ("BSTAT_HOST", "BSTAT_SERIAL", "BSTAT_CODE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(CF.ConfigError, match="bstat config"):
        CF.require(CF.load())
    p = CF.save("192.168.1.50", "01P00A123456789", "12345678")
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    cfg = CF.require(CF.load())
    assert cfg["access_code"] == "12345678"
    r = CF.redacted(cfg)
    assert r["access_code"] == "set" and "123456789" not in r["serial"]
    monkeypatch.setenv("BSTAT_HOST", "10.0.0.9")
    assert CF.load()["host"] == "10.0.0.9"


def test_hms_text_from_local_files(tmp_path, monkeypatch):
    d = tmp_path / "hms"
    d.mkdir()
    (d / "hms_en_P2S.json").write_text(json.dumps({"data": {"device_hms": {"en": [
        {"ecode": "0700020000020001", "intro": "AMS A Slot 1 filament has run out."}]},
        "device_error": {"en": [{"ecode": "0300800A", "intro": "A foreign object was detected on the plate."}]}}}))
    monkeypatch.setattr(hms, "SEARCH", [str(d)])
    hms.table.cache_clear()
    assert hms.text("0700_0200_0002_0001") == "AMS A Slot 1 filament has run out."
    assert hms.text("0300_800A").startswith("A foreign object")
    assert hms.text("FFFF_FFFF") is None
    hms.table.cache_clear()
