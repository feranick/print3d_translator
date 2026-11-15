"""tui.py — the status as a live dashboard in the terminal (redrawn every second; Ctrl-C to quit).
Version: 2026.11.14.1

Plain ANSI escapes: 24-bit colour swatches for the AMS (macOS Terminal and iTerm show them), no curses.
"""
from __future__ import annotations

import shutil
import sys
import time

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, YELLOW, RED, BLUE = "\033[32m", "\033[33m", "\033[31m", "\033[34m"
STATE_COL = {"printing": GREEN, "preparing": GREEN, "finished": GREEN, "paused": YELLOW, "failed": RED}


def _sw(hexcol: str | None, empty: bool) -> str:
    if empty or not hexcol:
        return DIM + "░░░░" + RESET
    r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
    return f"\033[48;2;{r};{g};{b}m    {RESET}"


def _bar(pct, width: int) -> str:
    pct = max(0, min(100, pct or 0))
    n = round(width * pct / 100)
    return GREEN + "█" * n + RESET + DIM + "░" * (width - n) + RESET


def _t(pair) -> str:
    c, t = (pair or [None, None])[:2]
    if c is None:
        return "—"
    return f"{c:.0f}°" + (f" → {t:.0f}°" if t else "")


def _hm(m) -> str:
    if m is None:
        return "—"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _ago(t, now) -> str:
    if not t:
        return "never"
    s = max(0, int(now - t))
    return f"{s} s ago" if s < 60 else f"{s // 60} min ago"


def render(s: dict, width: int = 80) -> str:
    p, c, now = s["printer"], s["connection"], s["now"]
    w = max(50, min(width, 110))
    out = []
    st = p.get("state") or "unknown"
    if s["source"] != "printer":
        conn = f"{s['source']}: {c.get('detail') or ''}"
    elif c.get("conn") == "connected":
        conn = f"connected · last update {_ago(c.get('last'), now)}"
    else:
        conn = f"{c.get('conn')}: {c.get('detail') or ''}"
    out.append(f"{BOLD}Printer status{RESET}  {STATE_COL.get(st, '')}{BOLD}● {st}{RESET}   {DIM}{conn}{RESET}")
    out.append("─" * w)
    out.append(f"{BOLD}{p.get('file') or 'no file'}{RESET}")
    pct = p.get("percent")
    out.append(f"{_bar(pct, w - 8)} {('—' if pct is None else str(pct) + '%'):>6}")
    layer = p.get("layer") or [None, None]
    eta = time.strftime("%H:%M", time.localtime(p["eta"])) if p.get("eta") else "—"
    out.append(f"layer {layer[0] if layer[0] is not None else '—'} of {layer[1] if layer[1] else '—'}   "
               f"time left {_hm(p.get('remaining_min'))}   finishes {eta}   speed {p.get('speed') or '—'}")
    out.append("")
    tp = p.get("temps") or {}
    out.append(f"{BOLD}Temperatures{RESET}  nozzle {_t(tp.get('nozzle'))}   bed {_t(tp.get('bed'))}   "
               f"chamber {_t([tp.get('chamber'), None])}")
    fans = p.get("fans") or {}
    out.append(f"{BOLD}Fans{RESET}          " + "   ".join(f"{k} {('—' if v is None else str(v) + '%')}" for k, v in fans.items()))
    out.append("")
    ams = p.get("ams") or {}
    out.append(f"{BOLD}AMS{RESET}  in use: {ams.get('active') or '—'}")
    for u in ams.get("units") or []:
        hum = f"{u['humidity_pct']:.0f}%" if u.get("humidity_pct") is not None else \
            (f"level {u['humidity_level']}/5" if u.get("humidity_level") is not None else "—")
        tmp = f"{u['temp']:.1f}°C" if u.get("temp") is not None else ""
        out.append(f"  {u.get('name')}  {DIM}humidity {hum}  {tmp}{RESET}")
        for t in u.get("trays") or []:
            mark = f"{GREEN}◀ in use{RESET}" if t.get("active") else ""
            if t.get("empty"):
                out.append(f"   {t.get('label')}  {_sw(None, True)}  {DIM}empty{RESET}")
            else:
                rem = "—" if t.get("remain") is None else f"{t['remain']}%"
                out.append(f"   {t.get('label')}  {_sw(t.get('color'), False)}  {(t.get('type') or ''):6s} "
                           f"{(t.get('brand') or ''):14s} {rem:>5} left  {mark}")
    ext = ams.get("external")
    if ext and not ext.get("empty"):
        out.append(f"   Ext {_sw(ext.get('color'), False)}  {ext.get('type') or ''}  "
                   + (f"{GREEN}◀ in use{RESET}" if ext.get("active") else ""))
    out.append("")
    errs = p.get("errors") or []
    out.append(f"{BOLD}Errors{RESET}  " + ("none" if not errs else ""))
    for e in errs:
        out.append(f"  {RED}●{RESET} {e.get('text') or 'no local description'}  {DIM}{e['code']}  {e.get('link')}{RESET}")
    cam = s.get("camera") or {}
    out.append(f"{BOLD}Camera{RESET}  " + ({"ok": f"snapshot {_ago(cam.get('time'), now)} (see the web page)",
                                             "off": "off"}.get(cam.get("camera"), f"not available {DIM}{cam.get('detail') or ''}{RESET}")))
    out.append("─" * w)
    out.append(f"{DIM}bstat {s['version']} · read-only · Ctrl-C to quit{RESET}")
    return "\n".join(out)


def run(mon, period: float = 1.0):
    sys.stdout.write("\033[?25l")           # hide the cursor
    try:
        while True:
            width = shutil.get_terminal_size((90, 30)).columns
            sys.stdout.write("\033[H\033[2J" + render(mon.snapshot(), width) + "\n")
            sys.stdout.flush()
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h" + RESET + "\n")
        sys.stdout.flush()
