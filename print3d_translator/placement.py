"""placement.py — puts the oriented parts and the prime tower on the plate, deterministically.
Version: 2026.10.24.6

OrcaSlicer's arrange is a heuristic and its default prime-tower position (165, 250) is off the P2S
bed, so we place everything ourselves and lock the plate (need_arrange false):

- Each part takes its bounding rectangle plus a halo (room for its brim and a gap to its neighbours).
  It must lie inside the area its extruder can print (H2D: left 0-325, right 25-350 mm; with an
  automatic filament map, the area both reach) and outside the machine's wrapping-detection zone.
- Parts with 'at' [x, y] (centre) go there; the others go, largest first, at the free spot nearest
  the centre of their area (ties: lower y, then lower x).
- With two or more filaments the prime tower is reserved first, in a back corner reachable by every
  extruder used; process wipe_tower_x/y is then its corner.
Grid: 1 mm. Positions are rounded to 0.1 mm.
"""
from __future__ import annotations

import numpy as np

from . import mesh as M

RES = 1.0


class PlacementError(ValueError):
    """The parts do not fit; the message says what to change."""


def _rect_mask(lo, hi, origin, shape) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)                         # [y, x]
    i0 = int(np.ceil((lo[0] - origin[0]) / RES))
    j0 = int(np.ceil((lo[1] - origin[1]) / RES))
    i1 = int(np.floor((hi[0] - origin[0]) / RES))
    j1 = int(np.floor((hi[1] - origin[1]) / RES))
    if i1 >= i0 and j1 >= j0:
        m[max(j0, 0):max(j1 + 1, 0), max(i0, 0):max(i1 + 1, 0)] = True
    return m


def _free_positions(blocked: np.ndarray, w: int, h: int) -> np.ndarray:
    """(ny, nx) bool: a w x h cell rectangle with its lower-left cell at (j, i) touches no blocked cell."""
    ny, nx = blocked.shape
    if w > nx or h > ny:
        return np.zeros((0, 0), dtype=bool)
    S = np.zeros((ny + 1, nx + 1), dtype=np.int64)
    S[1:, 1:] = blocked.astype(np.int64).cumsum(0).cumsum(1)
    tot = S[h:, w:] - S[:-h, w:] - S[h:, :-w] + S[:-h, :-w]
    return tot == 0


def place(items: list[dict], bed: np.ndarray, areas: list[np.ndarray] | None = None,
          tower: dict | None = None, exclude: np.ndarray | None = None) -> dict:
    """items: [{id, tri (oriented, centred at 0,0), halo (mm), extruders: [1-based] or None (any),
    at: [x, y] or None}]. bed: (n, 2) rectangle. areas: per-extruder rectangles (H2D) or None.
    tower: {w, d, halo, extruders} or None. exclude: a rectangle to keep clear (wrapping zone).
    Returns {placed: {id: {center, tri}}, tower: {x, y, rect} or None}."""
    blo, bhi = bed.min(axis=0), bed.max(axis=0)
    origin = blo
    shape = (int(np.floor((bhi[1] - blo[1]) / RES)) + 1, int(np.floor((bhi[0] - blo[0]) / RES)) + 1)
    occupied = np.zeros(shape, dtype=bool)
    if exclude is not None and len(exclude):
        occupied |= _rect_mask(exclude.min(axis=0), exclude.max(axis=0), origin, shape)

    def allowed(extruders):
        m = _rect_mask(blo, bhi, origin, shape)
        if areas and extruders:
            for e in extruders:
                a = areas[e - 1]
                m &= _rect_mask(a.min(axis=0), a.max(axis=0), origin, shape)
        return m

    def area_of(extruders):
        lo, hi = blo.copy(), bhi.copy()
        if areas and extruders:
            for e in extruders:
                lo = np.maximum(lo, areas[e - 1].min(axis=0))
                hi = np.minimum(hi, areas[e - 1].max(axis=0))
        return lo, hi

    out = {"placed": {}, "tower": None}

    def put(ident, w, h, ok_mask, target, prefer=None):
        """Lower-left (x, y) of a free w x h (mm) rectangle nearest target; None if none."""
        wc, hc = int(np.ceil(w / RES)), int(np.ceil(h / RES))
        free = _free_positions(~ok_mask | occupied, wc, hc)
        if free.size == 0 or not free.any():
            return None
        jj, ii = np.nonzero(free)
        cx = origin[0] + ii * RES + w / 2
        cy = origin[1] + jj * RES + h / 2
        if prefer is not None:
            key = prefer(cx, cy)
        else:
            key = (cx - target[0]) ** 2 + (cy - target[1]) ** 2
        k = np.lexsort((ii, jj, np.round(key, 6)))[0]
        x0, y0 = origin[0] + ii[k] * RES, origin[1] + jj[k] * RES
        occupied[jj[k]:jj[k] + hc, ii[k]:ii[k] + wc] = True
        return float(x0), float(y0)

    # fixed parts first: they must be free and inside their area
    fixed = [it for it in items if it.get("at") is not None]
    free_items = [it for it in items if it.get("at") is None]
    for it in fixed:
        lo, hi = M.bbox(it["tri"])
        half = (hi[:2] - lo[:2]) / 2 + it["halo"]
        c = np.asarray(it["at"], dtype=float)
        cl = [round(float(v), 1) for v in c]
        ok = allowed(it.get("extruders"))
        m = _rect_mask(c - half, c + half, origin, shape)
        alo, ahi = area_of(it.get("extruders"))
        if np.any(c - half < alo - 1e-6) or np.any(c + half > ahi + 1e-6):
            raise PlacementError(f"object {it['id']}: at {cl} puts it (with {it['halo']} mm for brim and gap) "
                                 f"outside the area its extruder prints, x {alo[0]:g}-{ahi[0]:g}, y {alo[1]:g}-{ahi[1]:g}")
        if (m & (occupied | ~ok)).any():
            raise PlacementError(f"object {it['id']}: at {cl} overlaps another object or a keep-out zone; move it")
        occupied |= m
        out["placed"][it["id"]] = {"center": [round(float(c[0]), 1), round(float(c[1]), 1)],
                                   "tri": M.transform(it["tri"], None, (c[0], c[1], 0.0))}

    # the prime tower: a back corner reachable by every extruder used
    if tower:
        w = tower["w"] + 2 * tower["halo"]
        d = tower["d"] + 2 * tower["halo"]
        ok = allowed(tower.get("extruders"))
        alo, ahi = area_of(tower.get("extruders"))
        mid = (alo[0] + ahi[0]) / 2
        pos = put("prime tower", w, d, ok, None,
                  prefer=lambda cx, cy: (ahi[1] - cy) * 1000 + np.abs(np.abs(cx - mid) - (ahi[0] - alo[0]) / 2))
        if pos is None:
            raise PlacementError("no room for the prime tower: fewer objects, or one filament")
        x, y = pos[0] + tower["halo"], pos[1] + tower["halo"]
        out["tower"] = {"x": round(x, 1), "y": round(y, 1), "rect": [round(x, 1), round(y, 1),
                                                                   round(x + tower["w"], 1), round(y + tower["d"], 1)]}

    # the other parts, largest footprint first (ties by id)
    def footprint(it):
        lo, hi = M.bbox(it["tri"])
        return -(hi[0] - lo[0]) * (hi[1] - lo[1]), it["id"]

    for it in sorted(free_items, key=footprint):
        lo, hi = M.bbox(it["tri"])
        w = hi[0] - lo[0] + 2 * it["halo"]
        h = hi[1] - lo[1] + 2 * it["halo"]
        alo, ahi = area_of(it.get("extruders"))
        if w > ahi[0] - alo[0] + 1e-6 or h > ahi[1] - alo[1] + 1e-6:
            raise PlacementError(f"object {it['id']}: {hi[0] - lo[0]:.1f} x {hi[1] - lo[1]:.1f} mm (+ {it['halo']} mm "
                                 f"around) does not fit the area its extruder prints ({ahi[0] - alo[0]:g} x "
                                 f"{ahi[1] - alo[1]:g} mm): rotate it, scale it, or use a larger printer")
        pos = put(it["id"], w, h, allowed(it.get("extruders")), ((alo[0] + ahi[0]) / 2, (alo[1] + ahi[1]) / 2))
        if pos is None:
            raise PlacementError(f"no room left on the plate for object {it['id']}: fewer copies, smaller parts, "
                                 f"or a second plate (a second study)")
        c = (pos[0] + w / 2, pos[1] + h / 2)
        c = (round(float(c[0]), 1), round(float(c[1]), 1))
        out["placed"][it["id"]] = {"center": [c[0], c[1]], "tri": M.transform(it["tri"], None, (c[0], c[1], 0.0))}
    return out
