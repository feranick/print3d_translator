"""cli.py — bstat's commands.
Version: 2026.11.14.1

  bstat config --host IP --serial SN --code ACCESS_CODE   save the printer's address (~/.config/bstat, mode 600)
  bstat probe [--seconds 20]       try the connection once: does the printer answer, what does it send,
                                   does the camera answer; records the messages for analysis
  bstat web  [--port 8765] [--lan] the local web page
  bstat tui                        the terminal dashboard
  bstat gui                        the desktop window
Common: --demo (made-up data, no printer), --replay FILE (a recording), --no-camera, --no-pushall
(send nothing at all to the printer), --record FILE.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from . import config as CF

RECORD_DIR = Path("~/p3dt_runs/bstat").expanduser()


def _monitor(a):
    from .monitor import Monitor
    if a.demo or a.replay:
        return Monitor(demo=a.demo, replay=a.replay).start()
    try:
        cfg = CF.require(CF.load())
    except CF.ConfigError as e:
        sys.exit(f"bstat: {e}")
    return Monitor(cfg, camera=not a.no_camera, pushall=not a.no_pushall, record=a.record).start()


def cmd_config(a):
    if not (a.host or a.serial or a.code):
        print(json.dumps({"file": str(CF.PATH), **CF.redacted(CF.load())}, indent=1))
        return
    p = CF.save(a.host, a.serial, a.code)
    print(f"saved {p} (mode 600): {json.dumps(CF.redacted(CF.load()))}")


def cmd_probe(a):
    """One connection attempt, reported in plain words; the messages are recorded for analysis."""
    try:
        cfg = CF.require(CF.load())
    except CF.ConfigError as e:
        sys.exit(f"bstat: {e}")
    from . import state as S
    from .client import Link
    rec = Path(a.record) if a.record else RECORD_DIR / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    print(f"bstat {__version__} probe: {json.dumps(CF.redacted(cfg))}")
    print(f"  status channel: {cfg['host']}:8883 (MQTT over TLS), listening for {a.seconds:g} s"
          + ("" if not a.no_pushall else " (no pushall: nothing is sent)"))
    try:
        link = Link(cfg, pushall=not a.no_pushall, record=rec).start()
    except ImportError:
        sys.exit("bstat: paho-mqtt is missing: pip install paho-mqtt (in the Python you run bstat with)")
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        time.sleep(0.5)
        st = link.status()
        if st["conn"] == "refused":
            break
    st = link.status()
    link.stop()
    raw = link.raw()
    out = {"connection": st["conn"], "detail": st["detail"], "messages": st["messages"]}
    p = raw.get("print") or {}
    out["fields"] = sorted(p)[:200]
    out["other_blocks"] = sorted(k for k in raw if k != "print")
    if st["messages"]:
        n = S.normalize(raw)
        out["decoded"] = {"state": n["state"], "file": n["file"], "percent": n["percent"], "layer": n["layer"],
                          "temps": n["temps"], "fans": n["fans"], "ams_units": len(n["ams"]["units"]),
                          "trays": [t["label"] + " " + (t["type"] or "empty") for u in n["ams"]["units"] for t in u["trays"]],
                          "errors": [e["code"] for e in n["errors"]]}
        missing = [k for k, v in (("state", n["state"]), ("nozzle temperature", n["temps"]["nozzle"][0]),
                                  ("bed temperature", n["temps"]["bed"][0])) if v is None]
        if missing:
            out["not_decoded"] = missing
    if not a.no_camera:
        from .camera import PORT, snapshot
        try:
            img = snapshot(cfg["host"], cfg["access_code"])
            jp = rec.with_suffix(".jpg")
            jp.write_bytes(img)
            out["camera"] = f"ok: {len(img)} bytes, saved {jp}"
        except Exception as e:                       # noqa: BLE001 — any failure is a finding here
            out["camera"] = f"not available on port {PORT}: {type(e).__name__}: {e}"
    out["recorded"] = str(rec) if rec.exists() else None
    print(json.dumps(out, indent=1, default=str))
    if st["conn"] == "connected" and st["messages"]:
        print("\nThe printer answers: 'bstat web', 'bstat tui' or 'bstat gui' will work.")
    elif st["conn"] == "refused":
        print(f"\nRefused: {st['detail']}")
    elif st["conn"] == "connected":
        print("\nConnected but no message arrived: the printer may send only on change; try again during a print, "
              "or without --no-pushall.")
    else:
        print("\nNo connection: check the IP (printer: Settings > Network), that the Mac is on the same network, "
              "and that LAN mode is enabled on the printer.")


def cmd_web(a):
    from .web import serve
    serve(_monitor(a), port=a.port, lan=a.lan, open_browser=not a.no_browser)


def cmd_tui(a):
    from .tui import run
    run(_monitor(a))


def cmd_gui(a):
    from .gui import run
    run(_monitor(a))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bstat", description="Read-only status of a Bambu Lab printer on the LAN.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--demo", action="store_true", help="made-up data, no printer")
        p.add_argument("--replay", help="play back a recording (.jsonl from 'probe' or --record)")
        p.add_argument("--no-camera", action="store_true", help="do not read the camera")
        p.add_argument("--no-pushall", action="store_true", help="send nothing at all to the printer")
        p.add_argument("--record", help="append every message to this .jsonl file")
    p = sub.add_parser("config", help="save the printer's host, serial and access code")
    p.add_argument("--host")
    p.add_argument("--serial")
    p.add_argument("--code", help="the LAN access code shown on the printer")
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser("probe", help="try the connection once and report")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--no-camera", action="store_true")
    p.add_argument("--no-pushall", action="store_true")
    p.add_argument("--record")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("web", help="the local web page")
    common(p)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lan", action="store_true", help="also serve to other devices on your network")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser("tui", help="the terminal dashboard")
    common(p)
    p.set_defaults(fn=cmd_tui)
    p = sub.add_parser("gui", help="the desktop window")
    common(p)
    p.set_defaults(fn=cmd_gui)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
