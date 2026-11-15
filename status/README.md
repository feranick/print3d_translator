# bstat: printer status, read-only

A small monitor for a Bambu Lab printer on your local network (built for the P2S). It shows the print's
progress, temperatures, fans, the AMS trays, errors and a camera snapshot, as a web page, a terminal
dashboard or a desktop window.

It is **read-only**. It never starts, pauses, stops or changes a print. The only message it sends is the
status request `pushall`, which asks the printer to send its full state. `--no-pushall` turns that off, and
then it sends nothing at all. A test checks that the code has no other way to send.

It lives in `print3d_translator/status/` but is a **separate tool**: p3dt does not import it, and p3dt
itself still never talks to a printer.

Version: 2026.11.14.1

## How it reaches the printer

- **Status:** MQTT over TLS on the printer's port 8883, user `bblp`, password = the printer's LAN access
  code; it listens to `device/<serial>/report`. The printer's certificate is self-signed, so it is not
  verified (as every LAN client does).
- **Camera:** JPEG snapshots on port 6000 (the P1/A1 protocol). Whether the P2S answers there is what
  `bstat probe` finds out; if not, the camera shows as unavailable and everything else works.
- **Errors in words:** from the error-description files Bambu Studio / OrcaSlicer keep on this Mac
  (`hms_en_*.json`), never fetched from the network. Without them an error shows its code and a link.

**On the P2S**, programs other than Bambu's may only be allowed in **LAN Only Mode + Developer Mode**
(printer: Settings > Network). In that mode Bambu Cloud and Handy stop working. Try `bstat probe` with
your current settings first: it says whether the printer answers.

## Install (in the project's .venv)

```bash
cd ~/Software/print3d_translator && .venv/bin/pip install -e status/
```

This adds the `bstat` command and paho-mqtt. Optional: `.venv/bin/pip install pillow` shows the camera
in the desktop window. The window needs Tk: with Homebrew's Python, `brew install python-tk@3.12`.

## Use

```bash
.venv/bin/bstat web --demo        # see the page with made-up data, no printer needed (also: tui, gui)
.venv/bin/bstat config --host 192.168.1.50 --serial 01P00A000000000 --code 12345678
.venv/bin/bstat probe             # one connection attempt, reported in plain words
.venv/bin/bstat web               # http://127.0.0.1:8765/ (add --lan to open it from a phone)
.venv/bin/bstat tui               # in the terminal
.venv/bin/bstat gui               # in a window
```

- `config` saves `~/.config/bstat/printer.json` (mode 600). The IP and the access code are on the
  printer under Settings > Network; the serial under Settings > Device. `BSTAT_HOST`, `BSTAT_SERIAL`,
  `BSTAT_CODE` override it.
- `probe` listens for 20 s and prints: connected or refused (and why), the fields the printer sent, what
  was decoded, and whether the camera answered. It records the messages to `~/p3dt_runs/bstat/probe_*.jsonl`
  (the serial number replaced by `<serial>`; the access code is never written), so the decoding can be
  checked against your printer.
- `--replay FILE` plays a recording back in any front-end; `--record FILE` records while watching.

## What it shows

| | |
|---|---|
| Print | state (idle, preparing, printing, paused, finished, failed), file, %, layer x of y, time left, finish time, speed mode |
| Temperatures and fans | nozzle and bed (current and target), chamber; part, aux, chamber and heatbreak fans |
| AMS | each tray: label (A1–A4), filament type and brand, colour, % left, the one in use; humidity and temperature; the external spool |
| Errors | HMS codes and the print error, in words when the local description files have them, with a link |
| Camera | the latest snapshot (web page; the window with Pillow) |

The fields follow the community documentation of the protocol (OpenBambuAPI, ha-bambulab). Newer firmware
reports temperatures in a `device` block with packed values; both forms are read.

## Files

```
bstat/   client.py (MQTT link) camera.py state.py (merge, decode) hms.py monitor.py (+ demo, replay)
         web.py + page.html, tui.py, gui.py, config.py, cli.py
tests/   test_bstat.py (decode, read-only rule, front-ends, config, HMS)
         test_mqtt_tls.py (a fake printer: MQTT over TLS with a self-signed certificate)
```

## Versions

| Version | Changes |
|---|---|
| 2026.11.14.1 | First version: probe, web page, terminal dashboard, desktop window; demo and replay; HMS text from local files; camera on port 6000. Tested against a fake printer (MQTT over TLS), not yet against the P2S. |
