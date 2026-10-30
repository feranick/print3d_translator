"""support_corpus.py — run the unsupported-extrusion check over finished studies (their G-code and
profiles), to compare versions of supports.py on real slices.
Version: 2026.10.30.1

Usage:  python tools/support_corpus.py ~/p3dt_runs/run_*/*  [more study folders or globs]
Prints per study: unsupported and severe mm, seconds, the worst layers (z, severe mm, floating), and
the longest bridge.
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

from print3d_translator import check as C, gcode as G, supports as S


def study(d: Path) -> str | None:
    g = d / f"{d.name}.gcode"
    if not g.exists():
        return None
    try:
        proc = json.loads((d / "profiles" / "process.json").read_text())
        mach = json.loads((d / "profiles" / "machine.json").read_text())
    except (OSError, ValueError):
        proc, mach = {}, {}
    sp = C.support_params(lambda k, dflt=None: proc.get(k, dflt), C._f(mach.get("nozzle_diameter"), 0.4))
    P = G.parse(g.read_text(errors="replace"))
    t = time.monotonic()
    u = S.unsupported(P, sp["line_width"], sp["layer_height"], sp["angle_deg"], sp["support_spacing"])
    worst = [(w["z_mm"], w["severe_mm"], "floating" if w["floating"] else w["max_beyond_mm"]) for w in u["worst"][:2]]
    return (f"{d.name[:32]:32s} sup={'on ' if sp['supports'] else 'off'} unsup={u['unsupported_mm']:8.1f} "
            f"severe={u['severe_mm']:8.1f} {time.monotonic() - t:4.1f}s {worst} "
            f"bridge_max={u['long_bridges']['longest_mm']}")


def main(args: list[str]) -> None:
    folders = sorted({Path(p) for a in args for p in glob.glob(str(Path(a).expanduser()))})
    for d in folders:
        if d.is_dir():
            line = study(d)
            if line:
                print(line, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
