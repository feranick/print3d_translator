"""gui.py — the status in a desktop window (Tk), refreshed every second.
Version: 2026.11.14.1

Tk comes with python.org's Python; Homebrew's needs 'brew install python-tk@3.12' (the message says so).
The camera snapshot is shown when Pillow is installed (Tk cannot decode JPEG by itself); otherwise the
window says to use the web page for the camera.
"""
from __future__ import annotations

import io
import sys
import time

BG, CARD, FG, MUTED, ACCENT, WARN, BAD, TRACK = "#f5f5f2", "#ffffff", "#1c1c1a", "#6d6c66", "#2f7d4f", "#b07a00", "#c0392b", "#ecebe5"
STATE_COL = {"printing": ACCENT, "preparing": ACCENT, "finished": ACCENT, "paused": WARN, "failed": BAD}


def _need_tk():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("bstat gui: this Python has no Tk. With Homebrew: brew install python-tk@3.12 "
                 "(or use 'bstat web' / 'bstat tui')")


def _hm(m):
    if m is None:
        return "—"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _t(pair):
    c, t = (pair or [None, None])[:2]
    return "—" if c is None else f"{c:.0f}°" + (f"  (target {t:.0f}°)" if t else "")


class Window:
    def __init__(self, mon):
        _need_tk()
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.mon = tk, mon
        self.root = tk.Tk()
        self.root.title("Printer status")
        self.root.configure(bg=BG)
        self.root.minsize(560, 520)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("P.Horizontal.TProgressbar", troughcolor=TRACK, background=ACCENT, bordercolor=TRACK,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=12)
        f = tk.Frame(self.root, bg=BG, padx=16, pady=14)
        f.pack(fill="both", expand=True)
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Printer status", font=("Helvetica", 18, "bold"), bg=BG, fg=FG).pack(side="left")
        self.state = tk.Label(head, text="—", font=("Helvetica", 13, "bold"), bg=BG, fg=MUTED, padx=12)
        self.state.pack(side="left")
        self.conn = tk.Label(f, text="connecting…", font=("Helvetica", 11), bg=BG, fg=MUTED, anchor="w")
        self.conn.pack(fill="x", pady=(2, 10))

        def card(title):
            c = tk.Frame(f, bg=CARD, highlightbackground="#e2e1db", highlightthickness=1, padx=12, pady=10)
            c.pack(fill="x", pady=5)
            tk.Label(c, text=title.upper(), font=("Helvetica", 10, "bold"), bg=CARD, fg=MUTED, anchor="w").pack(fill="x")
            return c
        c = card("Print")
        self.file = tk.Label(c, text="—", font=("Helvetica", 13, "bold"), bg=CARD, fg=FG, anchor="w")
        self.file.pack(fill="x")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", pady=6)
        self.bar = ttk.Progressbar(row, style="P.Horizontal.TProgressbar", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True)
        self.pct = tk.Label(row, text="—", font=("Helvetica", 20, "bold"), bg=CARD, fg=FG, width=5, anchor="e")
        self.pct.pack(side="right")
        self.detail = tk.Label(c, text="", font=("Helvetica", 11), bg=CARD, fg=FG, anchor="w", justify="left")
        self.detail.pack(fill="x")
        c = card("Temperatures and fans")
        self.temps = tk.Label(c, text="", font=("Helvetica", 12), bg=CARD, fg=FG, anchor="w", justify="left")
        self.temps.pack(fill="x")
        self.fans = tk.Label(c, text="", font=("Helvetica", 11), bg=CARD, fg=MUTED, anchor="w", justify="left")
        self.fans.pack(fill="x")
        c = card("AMS and filaments")
        self.ams = tk.Canvas(c, bg=CARD, height=120, highlightthickness=0)
        self.ams.pack(fill="x")
        c = card("Errors")
        self.errors = tk.Label(c, text="none", font=("Helvetica", 11), bg=CARD, fg=MUTED, anchor="w", justify="left", wraplength=520)
        self.errors.pack(fill="x")
        c = card("Camera")
        self.cam = tk.Label(c, text="—", font=("Helvetica", 11), bg=CARD, fg=MUTED, anchor="w")
        self.cam.pack(fill="x")
        self._cam_t = None
        self._photo = None
        self.root.after(200, self.refresh)

    def refresh(self):
        try:
            self._draw(self.mon.snapshot())
        finally:
            self.root.after(1000, self.refresh)

    def _draw(self, s):
        p, c, now = s["printer"], s["connection"], s["now"]
        st = p.get("state") or "unknown"
        self.state.config(text=f"● {st}", fg=STATE_COL.get(st, MUTED))
        if s["source"] != "printer":
            conn = f"{s['source']}: {c.get('detail') or ''}"
        elif c.get("conn") == "connected":
            last = c.get("last")
            conn = "connected · last update " + (f"{int(now - last)} s ago" if last else "never")
        else:
            conn = f"{c.get('conn')}: {c.get('detail') or ''}"
        self.conn.config(text=conn, fg=BAD if c.get("conn") == "refused" else MUTED)
        self.file.config(text=p.get("file") or "no file")
        pct = p.get("percent")
        self.bar["value"] = pct or 0
        self.pct.config(text="—" if pct is None else f"{pct}%")
        layer = p.get("layer") or [None, None]
        eta = time.strftime("%H:%M", time.localtime(p["eta"])) if p.get("eta") else "—"
        self.detail.config(text=f"Layer {layer[0] if layer[0] is not None else '—'} of {layer[1] or '—'}     "
                                f"Time left {_hm(p.get('remaining_min'))}     Finishes {eta}     Speed {p.get('speed') or '—'}")
        tp = p.get("temps") or {}
        self.temps.config(text=f"Nozzle {_t(tp.get('nozzle'))}     Bed {_t(tp.get('bed'))}     "
                               f"Chamber {_t([tp.get('chamber'), None])}")
        self.fans.config(text="Fans: " + "   ".join(f"{k} {'—' if v is None else str(v) + '%'}"
                                                     for k, v in (p.get("fans") or {}).items()))
        self._draw_ams(p.get("ams") or {})
        errs = p.get("errors") or []
        self.errors.config(text="\n".join(f"● {e.get('text') or 'no local description'}  [{e['code']}]" for e in errs) or "none",
                           fg=BAD if errs else MUTED)
        cam = s.get("camera") or {}
        if cam.get("camera") == "ok":
            self._show_camera(cam.get("time"))
        else:
            self.cam.config(text="off" if cam.get("camera") == "off" else f"not available  {cam.get('detail') or ''}", image="")

    def _draw_ams(self, ams):
        cv = self.ams
        cv.delete("all")
        x0, y = 4, 6
        rows = [(u["name"], u.get("trays") or [], u) for u in ams.get("units") or []]
        if ams.get("external") and not ams["external"].get("empty"):
            rows.append(("External", [ams["external"]], {}))
        if not rows:
            cv.create_text(x0, y + 8, text="no AMS reported", anchor="w", fill=MUTED, font=("Helvetica", 11))
            cv.config(height=30)
            return
        for name, trays, u in rows:
            hum = u.get("humidity_pct")
            sub = (f"humidity {hum:.0f}%" if hum is not None else
                   f"humidity level {u['humidity_level']}/5" if u.get("humidity_level") is not None else "")
            cv.create_text(x0, y + 8, text=f"{name}   {sub}", anchor="w", fill=FG, font=("Helvetica", 11, "bold"))
            y += 22
            for i, t in enumerate(trays):
                x = x0 + i * 130
                col = t.get("color") or TRACK
                outline = ACCENT if t.get("active") else "#d0cfc8"
                cv.create_rectangle(x, y, x + 120, y + 26, fill=TRACK if t.get("empty") else col, outline=outline,
                                    width=3 if t.get("active") else 1)
                label = f"{t.get('label')}  " + ("empty" if t.get("empty") else f"{t.get('type') or ''}  "
                                                 + ("—" if t.get("remain") is None else f"{t['remain']}%"))
                cv.create_text(x, y + 40, text=label, anchor="w", fill=FG, font=("Helvetica", 10))
                if t.get("active"):
                    cv.create_text(x, y + 54, text="in use", anchor="w", fill=ACCENT, font=("Helvetica", 10, "bold"))
            y += 66
        cv.config(height=y)

    def _show_camera(self, t):
        if t == self._cam_t:
            return
        self._cam_t = t
        jpeg = self.mon.camera_jpeg()
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.cam.config(text="snapshot available: open 'bstat web' to see it (or pip install pillow)", image="")
            return
        img = Image.open(io.BytesIO(jpeg))
        img.thumbnail((520, 300))
        self._photo = ImageTk.PhotoImage(img)
        self.cam.config(image=self._photo, text="")

    def run(self):
        self.root.mainloop()


def run(mon):
    Window(mon).run()
