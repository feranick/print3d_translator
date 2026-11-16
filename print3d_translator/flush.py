"""flush.py — how much filament to purge on a filament change, from the two filaments' colours.
Version: 2026.11.16.1

Bambu Studio's colour-based flush model (FlushVolCalculator::calc_flush_vol), which OrcaSlicer's CLI also
runs: a hue/saturation distance and a luminance step (dark -> light costs much more than light -> dark),
combined as two sides of a triangle at 120 degrees, at least 60 mm3, plus the printer's extra flush (its
nozzle volume, 110 mm3 on the P2S), at most 800 mm3. Checked against OrcaSlicer 2.5.1 on the P2S:
black -> white 670, white -> black 190, white -> #1E88E5 315, #1E88E5 -> white 594 mm3.

OrcaSlicer then multiplies the matrix by flush_multiplier, whose default is 0.3 (Bambu Studio's is 1.0):
a black -> white change purged 201 mm3 instead of 670, and the white part came out grey. The translator
therefore writes the matrix and a multiplier of 1 itself (profiles.apply_flush).

minimum(): what the flush_adequacy check requires per change: MIN_FRACTION of the model's volume
(black -> white: 603 mm3); below HARD_FRACTION of that minimum the check fails hard.
"""
from __future__ import annotations

import math

MAX_FLUSH = 800.0
MIN_FLUSH = 60.0
MIN_FRACTION = 0.9
HARD_FRACTION = 0.5


def rgba(c) -> tuple[float, float, float, float]:
    """'#RRGGBB' or '#RRGGBBAA' -> (r, g, b, a) in 0..1. Fully transparent counts as white (as Bambu does)."""
    s = str(c or "#FFFFFF").strip().lstrip("#")
    if len(s) < 6:
        s = "FFFFFF"
    r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    a = int(s[6:8], 16) / 255.0 if len(s) >= 8 else 1.0
    if a == 0:
        r = g = b = 1.0
    return r, g, b, a


def _hsv(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = 60.0 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60.0 * ((b - r) / d + 2)
    else:
        h = 60.0 * ((r - g) / d + 4)
    s = 0.0 if mx == 0 else d / mx
    return h, s, mx


def _delta_hs(h1, s1, v1, h2, s2, v2) -> float:
    a1, a2 = math.radians(h1), math.radians(h2)
    dx = math.cos(a1) * s1 * v1 - math.cos(a2) * s2 * v2
    dy = math.sin(a1) * s1 * v1 - math.sin(a2) * s2 * v2
    return min(1.2, math.hypot(dx, dy))


def _lumi(r, g, b) -> float:
    return r * 0.3 + g * 0.59 + b * 0.11


def volume(src, dst, extra: float = 0.0, cap: float = MAX_FLUSH) -> float:
    """mm3 to purge when changing from colour src to colour dst (Bambu's model, extra included)."""
    sr, sg, sb, _ = rgba(src)
    dr, dg, db, _ = rgba(dst)
    fh, fs, fv = _hsv(sr, sg, sb)
    th, ts, tv = _hsv(dr, dg, db)
    hs = _delta_hs(fh, fs, fv, th, ts, tv)
    fl, tl = _lumi(sr, sg, sb), _lumi(dr, dg, db)
    if tl >= fl:
        lumi = (tl - fl) ** 0.7 * 560.0
    else:
        lumi = (fl - tl) * 80.0
        hs = min(0.67 * tv + 0.33 * fv, hs)
    hsf = 230.0 * hs
    v = math.sqrt(hsf ** 2 + lumi ** 2 - 2 * hsf * lumi * math.cos(math.radians(120.0)))
    v = max(v, MIN_FLUSH) + extra
    return float(min(int(v + 1e-4), cap))      # float noise: white is 0.3 + 0.59 + 0.11 = 0.99999...


def matrix(colors: list, extra: float = 0.0) -> list[list[float]]:
    """n x n, row = from, column = to; 0 on the diagonal."""
    return [[0.0 if i == j else volume(a, b, extra) for j, b in enumerate(colors)] for i, a in enumerate(colors)]


def minimum(src, dst, extra: float = 0.0) -> float:
    return round(MIN_FRACTION * volume(src, dst, extra), 1)


def flat(m: list[list[float]]) -> list[str]:
    """OrcaSlicer's flush_volumes_matrix: the rows one after another, as strings."""
    return [f"{v:g}" for row in m for v in row]


def unflat(values, n: int) -> list[list[float]] | None:
    vals = [float(v) for v in (values or [])]
    if len(vals) < n * n:
        return None
    return [vals[i * n:(i + 1) * n] for i in range(n)]
