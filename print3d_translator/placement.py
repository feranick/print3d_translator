"""placement.py — puts the oriented parts and the prime tower on the plate, deterministically.
Version: 2026.11.11.1

OrcaSlicer's arrange is a heuristic and its default prime-tower position (165, 250) is off the P2S
bed, so we place everything ourselves and lock the plate (need_arrange false).

What a part takes on the plate (its "stamp"), on a 0.5 mm grid:
  layout.overlap "footprint" (default)  its silhouette seen from above
  layout.overlap "first_layer"          its first-layer outline only; parts may then interleave above
                                        the bed, and a 3D check makes sure they never touch (1 mm
                                        clearance, 1 mm voxels): for nested layouts
grown by its halo = half the gap between parts (gap / 2, default 3 mm) + its brim width (unless
layout.brims_may_overlap) + 3 mm with supports on (supports spread past the silhouette; OrcaSlicer
stops with "G-code paths conflict" when they reach another part). Two stamps may not overlap: two
parts end at least halo_a + halo_b apart. Stamps must lie inside the area their extruder prints (H2D:
left 0-325, right 25-350 mm) and outside the machine's wrapping-detection zone.

'at' [x, y] is the centre of the part's bounding box, in bed coordinates (mm). Parts with 'at' go there
first (an overlap names the other part). The others go largest first by one of these strategies, the
first that places them all: nearest the centre of their area; bottom-left (rows from the front-left);
shelves (rows of bounding rectangles, tallest first). With two or more filaments the prime tower is
reserved first, in a back corner reachable by every extruder used; process wipe_tower_x/y is its corner.
Positions are rounded to 0.1 mm.
"""
from __future__ import annotations

import numpy as np

from . import mesh as M

RES = 0.5               # mm, placement grid
SUPPORT_HALO = 3.0      # mm added around parts when supports are on
CLEAR_3D = 1.0          # mm, the 3D check's clearance (layout.overlap 'first_layer')


class PlacementError(ValueError):
    """The parts do not fit; the message says what to change."""


def _raster(tri: np.ndarray, lo: np.ndarray, shape: tuple[int, int], res: float = RES) -> np.ndarray:
    """Silhouette of triangles (xy) on a grid [y, x] with cell (0, 0) centred at lo."""
    mask = np.zeros(shape, dtype=bool)
    xy = tri[:, :, :2]
    a = xy[:, 1] - xy[:, 0]
    b = xy[:, 2] - xy[:, 0]
    area2 = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    I0 = np.ceil((xy[:, :, 0].min(axis=1) - lo[0]) / res - 1e-9).astype(int)
    I1 = np.floor((xy[:, :, 0].max(axis=1) - lo[0]) / res + 1e-9).astype(int)
    J0 = np.ceil((xy[:, :, 1].min(axis=1) - lo[1]) / res - 1e-9).astype(int)
    J1 = np.floor((xy[:, :, 1].max(axis=1) - lo[1]) / res + 1e-9).astype(int)
    real = np.abs(area2) > 1e-12
    small = real & (I1 - I0 <= 1) & (J1 - J0 <= 1)
    # triangles over at most 2 x 2 cell centres, all at once; tiny ones are also marked by their vertices
    # and centroid (below); larger ones one by one
    sx, sy, d_ = xy[small, :, 0], xy[small, :, 1], area2[small]
    for di in (0, 1):
        for dj in (0, 1):
            X, Y = I0[small] + di, J0[small] + dj
            ok = (X <= I1[small]) & (Y <= J1[small]) & (X >= 0) & (Y >= 0) & (X < shape[1]) & (Y < shape[0])
            px, py = lo[0] + X * res, lo[1] + Y * res
            w0 = ((sx[:, 1] - px) * (sy[:, 2] - py) - (sy[:, 1] - py) * (sx[:, 2] - px)) / d_
            w1 = ((sx[:, 2] - px) * (sy[:, 0] - py) - (sy[:, 2] - py) * (sx[:, 0] - px)) / d_
            e = 1e-9
            hit = ok & (w0 >= -e) & (w1 >= -e) & (1 - w0 - w1 >= -e)
            mask[Y[hit], X[hit]] = True
    for t in np.nonzero(real & ~small)[0]:
        p = xy[t]
        i0, j0 = np.floor((p.min(axis=0) - lo) / res).astype(int)
        i1, j1 = np.ceil((p.max(axis=0) - lo) / res).astype(int)
        i0, j0 = max(i0, 0), max(j0, 0)
        i1, j1 = min(i1, shape[1] - 1), min(j1, shape[0] - 1)
        if i1 < i0 or j1 < j0:
            continue
        X, Y = np.meshgrid(lo[0] + np.arange(i0, i1 + 1) * res, lo[1] + np.arange(j0, j1 + 1) * res)
        d = area2[t]
        w0 = ((p[1, 0] - X) * (p[2, 1] - Y) - (p[1, 1] - Y) * (p[2, 0] - X)) / d
        w1 = ((p[2, 0] - X) * (p[0, 1] - Y) - (p[2, 1] - Y) * (p[0, 0] - X)) / d
        e = 1e-9
        mask[j0:j1 + 1, i0:i1 + 1] |= (w0 >= -e) & (w1 >= -e) & (1 - w0 - w1 >= -e)
    # every vertex and centroid: small triangles, and thin ones (edge-on walls) with no cell centre inside
    ij = np.round((np.vstack([xy.reshape(-1, 2), xy.mean(axis=1)]) - lo) / res).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 1] >= 0) & (ij[:, 0] < shape[1]) & (ij[:, 1] < shape[0])
    mask[ij[ok, 1], ij[ok, 0]] = True
    return mask


def first_layer_tri(tri: np.ndarray, z: float) -> np.ndarray:
    """The triangles' cross-section at height z as small xy triangles (for rasterizing): every triangle
    crossing z contributes the segment where it does, thickened to a sliver, and triangles lying below z
    (a flat bottom) contribute themselves."""
    zs = tri[:, :, 2]
    flat = zs.max(axis=1) <= z
    out = [tri[flat]]
    cross = (zs.min(axis=1) < z) & (zs.max(axis=1) > z)
    for t in tri[cross]:
        pts = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            za, zb = t[a, 2], t[b, 2]
            if (za - z) * (zb - z) < 0:
                s = (z - za) / (zb - za)
                pts.append(t[a] + s * (t[b] - t[a]))
        if len(pts) == 2:
            out.append(np.array([[pts[0], pts[1], pts[1] + 1e-6]]))
    return np.concatenate(out) if len(out) > 1 else out[0]


def stamp(tri: np.ndarray, halo: float, base: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(mask [y, x], offset of cell (0, 0) from the bounding-box centre): the part's outline (base: the
    triangles to outline, default all), grown by halo, on the placement grid."""
    from scipy.ndimage import distance_transform_edt
    lo, hi = M.bbox(tri)
    c = (lo[:2] + hi[:2]) / 2
    src = tri if base is None else base
    slo = src[:, :, :2].reshape(-1, 2).min(axis=0) - halo - RES
    shi = src[:, :, :2].reshape(-1, 2).max(axis=0) + halo + RES
    slo = c + np.floor((slo - c) / RES) * RES
    shape = (int(np.ceil((shi[1] - slo[1]) / RES)) + 1, int(np.ceil((shi[0] - slo[0]) / RES)) + 1)
    m = _raster(src, slo, shape)
    if halo > 0:
        m = distance_transform_edt(~m) * RES <= halo + 1e-9
    return m, slo - c


def place(items: list[dict], bed: np.ndarray, areas: list[np.ndarray] | None = None,
          tower: dict | None = None, exclude: np.ndarray | None = None, overlap: str = "footprint",
          first_layer: float = 0.2) -> dict:
    """items: [{id, tri (oriented, bounding box centred at 0, 0), halo (mm), extruders: [1-based] or None
    (any), at: [x, y] or None}]. bed: (n, 2) rectangle. areas: per-extruder rectangles (H2D) or None.
    tower: {w, d, halo, extruders} or None. exclude: a rectangle to keep clear (wrapping zone).
    Returns {placed: {id: {center, tri}}, tower: {x, y, rect} or None, strategy, collisions_checked}."""
    blo, bhi = bed.min(axis=0), bed.max(axis=0)
    shape = (int(np.floor((bhi[1] - blo[1]) / RES)) + 1, int(np.floor((bhi[0] - blo[0]) / RES)) + 1)
    stamps = {}
    for it in items:
        base = first_layer_tri(it["tri"], first_layer) if overlap == "first_layer" else None
        if base is not None and not len(base):
            base = None
        stamps[it["id"]] = stamp(it["tri"], it["halo"], base)
    last_err = None
    for strategy in ("centre", "bottom-left", "shelves"):
        try:
            out = _place(items, stamps, bed, blo, bhi, shape, areas, tower, exclude, strategy)
        except PlacementError as e:
            if "no room left" not in str(e):
                raise
            last_err = e
            continue
        out["strategy"] = strategy
        if overlap == "first_layer":
            clashes = collisions([{"id": k, "tri": v["tri"]} for k, v in out["placed"].items()])
            if clashes:
                a, b, z = clashes[0]
                raise PlacementError(f"objects {a} and {b} come within {CLEAR_3D:g} mm of each other at z {z:g} mm "
                                     f"(layout.overlap 'first_layer' lets parts interleave above the bed, never touch): "
                                     f"move one of them")
            out["collisions_checked"] = True
        return out
    raise last_err


def _place(items, stamps, bed, blo, bhi, shape, areas, tower, exclude, strategy) -> dict:
    from scipy.signal import fftconvolve
    owner = np.full(shape, -1, dtype=np.int32)          # index of the item (or -2 tower, -3 keep-out)
    if exclude is not None and len(exclude):
        owner[_rect(exclude.min(axis=0), exclude.max(axis=0), blo, shape)] = -3
    names = [it["id"] for it in items]

    def allowed(extruders):
        lo, hi = area_of(extruders)
        return _rect(lo, hi, blo, shape)

    def area_of(extruders):
        lo, hi = blo.copy(), bhi.copy()
        if areas and extruders:
            for e in extruders:
                lo = np.maximum(lo, areas[e - 1].min(axis=0))
                hi = np.minimum(hi, areas[e - 1].max(axis=0))
        return lo, hi

    out = {"placed": {}, "tower": None}

    def stamp_at(m, off, c):
        """Grid slices of a stamp whose bounding-box centre is c; None if it leaves the grid."""
        i0 = int(round((c[0] + off[0] - blo[0]) / RES))
        j0 = int(round((c[1] + off[1] - blo[1]) / RES))
        if i0 < 0 or j0 < 0 or i0 + m.shape[1] > shape[1] or j0 + m.shape[0] > shape[0]:
            return None
        return slice(j0, j0 + m.shape[0]), slice(i0, i0 + m.shape[1])

    def free_centres(m, off, ok):
        blocked = ((owner != -1) | ~ok).astype(np.float32)
        if m.shape[0] > shape[0] or m.shape[1] > shape[1]:
            return np.zeros(0), np.zeros(0)
        hits = fftconvolve(blocked, m[::-1, ::-1].astype(np.float32), mode="valid")
        jj, ii = np.nonzero(hits < 0.5)
        return blo[0] + ii * RES - off[0], blo[1] + jj * RES - off[1]

    # fixed parts first: inside their area, and not on anything
    k_of = {it["id"]: k for k, it in enumerate(items)}
    for it in [it for it in items if it.get("at") is not None]:
        m, off = stamps[it["id"]]
        c = np.asarray(it["at"], dtype=float)
        cl = [round(float(v), 1) for v in c]
        alo, ahi = area_of(it.get("extruders"))
        sl = stamp_at(m, off, c)
        lo_s, hi_s = c + off, c + off + np.array([m.shape[1], m.shape[0]]) * RES
        inside = sl is not None and not (m & ~allowed(it.get("extruders"))[sl]).any()
        if not inside:
            raise PlacementError(f"object {it['id']}: at {cl} puts it (with {it['halo']:g} mm around for brim and gap) "
                                 f"outside the area its extruder prints, x {alo[0]:g}-{ahi[0]:g}, y {alo[1]:g}-{ahi[1]:g} "
                                 f"(its outline spans x {lo_s[0]:.1f}-{hi_s[0]:.1f}, y {lo_s[1]:.1f}-{hi_s[1]:.1f})")
        hit = owner[sl][m]
        hit = hit[hit != -1]
        if len(hit):
            who = int(np.bincount(hit - hit.min()).argmax() + hit.min())
            other = "the prime tower" if who == -2 else "the wrapping-detection zone" if who == -3 else f"object {names[who]}"
            raise PlacementError(f"object {it['id']}: at {cl} overlaps {other} (each part keeps its outline + "
                                 f"{it['halo']:g} mm for brim and half the gap; see layout.overlap / brims_may_overlap); "
                                 f"move it")
        owner[sl][m] = k_of[it["id"]]
        out["placed"][it["id"]] = {"center": cl, "tri": M.transform(it["tri"], None, (c[0], c[1], 0.0))}

    # the prime tower: a back corner reachable by every extruder used
    if tower:
        w = tower["w"] + 2 * tower["halo"]
        d = tower["d"] + 2 * tower["halo"]
        m = np.ones((int(np.ceil(d / RES)), int(np.ceil(w / RES))), dtype=bool)
        alo, ahi = area_of(tower.get("extruders"))
        cx, cy = free_centres(m, np.array([-w / 2, -d / 2]), allowed(tower.get("extruders")))
        if not len(cx):
            raise PlacementError("no room for the prime tower: fewer objects, or one filament")
        mid = (alo[0] + ahi[0]) / 2
        key = (ahi[1] - cy) * 1000 + np.abs(np.abs(cx - mid) - (ahi[0] - alo[0]) / 2)
        k = np.lexsort((cx, cy, np.round(key, 6)))[0]
        sl = stamp_at(m, np.array([-w / 2, -d / 2]), (cx[k], cy[k]))
        owner[sl][m] = -2
        x, y = cx[k] - w / 2 + tower["halo"], cy[k] - d / 2 + tower["halo"]
        out["tower"] = {"x": round(x, 1), "y": round(y, 1), "rect": [round(x, 1), round(y, 1),
                                                                   round(x + tower["w"], 1), round(y + tower["d"], 1)]}

    free_items = [it for it in items if it.get("at") is None]

    def size(it):
        lo, hi = M.bbox(it["tri"])
        return hi - lo

    if strategy == "shelves":
        order = sorted(free_items, key=lambda it: (-(size(it)[1] + 2 * it["halo"]), -size(it)[0], it["id"]))
    else:
        order = sorted(free_items, key=lambda it: (-size(it)[0] * size(it)[1], it["id"]))
    shelf = {"y": None, "x": None, "h": 0.0}
    for it in order:
        m, off = stamps[it["id"]]
        sz = size(it)
        alo, ahi = area_of(it.get("extruders"))
        if sz[0] + 2 * it["halo"] > ahi[0] - alo[0] + 1e-6 or sz[1] + 2 * it["halo"] > ahi[1] - alo[1] + 1e-6:
            raise PlacementError(f"object {it['id']}: {sz[0]:.1f} x {sz[1]:.1f} mm (+ {it['halo']:g} mm around) does not "
                                 f"fit the area its extruder prints ({ahi[0] - alo[0]:g} x {ahi[1] - alo[1]:g} mm): "
                                 f"rotate it, scale it, or use a larger printer")
        cx, cy = free_centres(m, off, allowed(it.get("extruders")))
        if not len(cx):
            raise PlacementError(f"no room left on the plate for object {it['id']}: fewer copies, smaller parts, "
                                 f"layout.overlap 'first_layer' for nested parts, or a second plate (a second study)")
        if strategy == "centre":
            key = (cx - (alo[0] + ahi[0]) / 2) ** 2 + (cy - (alo[1] + ahi[1]) / 2) ** 2
            k = np.lexsort((cx, cy, np.round(key, 6)))[0]
        elif strategy == "bottom-left":
            k = np.lexsort((cx, np.round(cy - sz[1] / 2, 1)))[0]
        else:
            # shelves: fill a row left to right; start a new row above the tallest of the current one
            lo_y = cy - sz[1] / 2 - it["halo"]
            if shelf["y"] is None:
                cand = np.nonzero(np.isclose(lo_y, lo_y.min(), atol=RES))[0]
            else:
                cand = np.nonzero((lo_y >= shelf["y"] - 1e-6) & (lo_y <= shelf["y"] + RES + 1e-6))[0]
                if not len(cand):
                    ny = shelf["y"] + shelf["h"]
                    cand = np.nonzero(lo_y >= ny - 1e-6)[0]
                    if len(cand):
                        cand = cand[np.isclose(lo_y[cand], lo_y[cand].min(), atol=RES)]
                        shelf.update(y=None, h=0.0)
            if not len(cand):
                raise PlacementError(f"no room left on the plate for object {it['id']}")
            k = cand[np.lexsort((cx[cand],))[0]]
            if shelf["y"] is None:
                shelf["y"] = float(lo_y[k])
            shelf["h"] = max(shelf["h"], float(sz[1] + 2 * it["halo"]))
        c = (round(float(cx[k]), 1), round(float(cy[k]), 1))
        sl = stamp_at(m, off, c)
        if sl is None or (owner[sl][m] != -1).any():          # rounding to 0.1 mm moved it onto something
            c = (float(cx[k]), float(cy[k]))
            sl = stamp_at(m, off, c)
        owner[sl][m] = k_of[it["id"]]
        out["placed"][it["id"]] = {"center": [round(c[0], 3), round(c[1], 3)],
                                   "tri": M.transform(it["tri"], None, (c[0], c[1], 0.0))}
    return out


def _rect(lo, hi, origin, shape) -> np.ndarray:
    """Cells whose centres lie in the rectangle [lo, hi]."""
    m = np.zeros(shape, dtype=bool)
    i0 = int(np.ceil((lo[0] - origin[0]) / RES - 1e-9))
    j0 = int(np.ceil((lo[1] - origin[1]) / RES - 1e-9))
    i1 = int(np.floor((hi[0] - origin[0]) / RES + 1e-9))
    j1 = int(np.floor((hi[1] - origin[1]) / RES + 1e-9))
    if i1 >= i0 and j1 >= j0:
        m[max(j0, 0):max(j1 + 1, 0), max(i0, 0):max(i1 + 1, 0)] = True
    return m


def collisions(placed: list[dict], res: float = 1.0, clearance: float = CLEAR_3D) -> list[tuple[str, str, float]]:
    """Pairs of placed parts [{id, tri}] closer than clearance in 3D (voxels of res mm): [(a, b, z)]."""
    from scipy.ndimage import binary_dilation
    from .supports import voxelize
    boxes = {p["id"]: M.bbox(p["tri"]) for p in placed}
    vox = {}
    out = []
    pad = clearance + res
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            (alo, ahi), (blo, bhi) = boxes[a["id"]], boxes[b["id"]]
            if np.any(alo[:2] - pad > bhi[:2]) or np.any(blo[:2] - pad > ahi[:2]):
                continue
            for p in (a, b):
                if p["id"] not in vox:
                    lo, hi = boxes[p["id"]]
                    zc = np.arange(res / 2, hi[2], res)
                    occ, org = voxelize(p["tri"], zc, res)
                    vox[p["id"]] = (occ, np.array(org))
            (oa, ga), (ob, gb) = vox[a["id"]], vox[b["id"]]
            r = max(1, int(np.ceil(clearance / res)))
            st = np.zeros((1, 2 * r + 1, 2 * r + 1), dtype=bool)
            st[0] = True
            da = binary_dilation(oa, structure=st)
            di = int(round((gb[0] - ga[0]) / res))
            dj = int(round((gb[1] - ga[1]) / res))
            nz = min(da.shape[0], ob.shape[0])
            # overlap of b's grid inside a's: a[j, i] <-> b[j - dj, i - di]
            ja0, ja1 = max(0, dj), min(da.shape[1], dj + ob.shape[1])
            ia0, ia1 = max(0, di), min(da.shape[2], di + ob.shape[2])
            if ja1 <= ja0 or ia1 <= ia0:
                continue
            both = da[:nz, ja0:ja1, ia0:ia1] & ob[:nz, ja0 - dj:ja1 - dj, ia0 - di:ia1 - di]
            if both.any():
                k = int(np.nonzero(both.any(axis=(1, 2)))[0][0])
                out.append((a["id"], b["id"], round((k + 0.5) * res, 1)))
    return out
