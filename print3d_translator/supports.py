"""supports.py — will the part print without supports? Two independent answers:
Version: 2026.10.29.2

1. From the geometry, before slicing (regions()): the part is voxelized at the real layer heights
   (vertical rays through a 0.25 mm grid, inside/outside from the mesh's crossings). On each layer,
   material farther than `reach` from the layer below has nothing to rest on, where
       reach = line_width / 2 + layer_height / tan(support_angle)
   (an overhang steeper than support_angle steps out at most layer_height / tan(angle) per layer, and
   a line may hang half its width over the edge). Such material is grouped into regions (3D connected)
   and classified on the layer where it starts:
       island      a piece of the layer with nothing at all within reach below: it starts in mid-air
       bridge      a ceiling touching supported material on two or more separate sides (prints as
                   a bridge; long spans sag)
       cantilever  a ceiling or shallow overhang held on one side only: it curls and fails
   Each region: area (mm2), how far it reaches beyond support (mm), its z range and centre.

2. From the G-code, after slicing (unsupported()): every extrusion of the part is checked against the
   extrusion of the layer below (and support material up to 3 layers below, for the support's top gap):
   a point farther than `reach` from any of it is unsupported. An unsupported stretch of a path with
   supported path on both sides, about straight between them, is a bridge: accepted (spans over
   BRIDGE_MAX listed: they sag).
   Returns the unsupported length per layer and a flag per segment (the
   3D view draws them red).
"""
from __future__ import annotations

import math

import numpy as np

from . import mesh as M

RES = 0.25          # mm, geometry grid
GRES = 0.2          # mm, G-code grid
MIN_AREA = 2.0      # mm2: smaller regions are noise (tessellation, chamfers)
MAX_VOXELS = 4e7    # geometry grid cells (memory: about 3 bytes each)
STRAIGHT = 1.25     # a bridge's length is at most this x the distance between its ends (+1 mm)
STEP = 0.4          # mm, sampling along extrusion paths
BRIDGE_MAX = 15.0   # mm, longer bridges are reported (they sag)


def reach(line_width: float, layer_height: float, angle_deg: float) -> float:
    a = math.radians(angle_deg if angle_deg and angle_deg > 0 else 30.0)
    return 0.5 * line_width + layer_height / math.tan(a)


def layer_centres(height: float, first: float, lh: float) -> np.ndarray:
    zs = [first / 2]
    top = first
    while top + 1e-9 < height:
        zs.append(top + lh / 2)
        top += lh
    return np.array(zs)


def voxelize(tri: np.ndarray, zc: np.ndarray, res: float = RES):
    """Occupancy (n_layers, ny, nx) bool at layer centres zc, grid origin, res. Vertical rays through
    pixel centres; a closed mesh is entered through faces with nz < 0 and left through nz > 0."""
    lo, hi = M.bbox(tri)
    off = res * 0.5 + 1.234e-4                  # keep pixel centres off triangle edges
    nx = int(math.ceil((hi[0] - lo[0]) / res)) + 1
    ny = int(math.ceil((hi[1] - lo[1]) / res)) + 1
    ox, oy = lo[0] - off, lo[1] - off
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    keep = np.abs(n[:, 2]) > 1e-12
    T, sgn = tri[keep], np.where(n[keep, 2] < 0, 1, -1)       # +1 entering, -1 leaving (going up)
    delta = np.zeros((len(zc) + 1, ny, nx), dtype=np.int8)
    for k in range(len(T)):
        p = T[k]
        x, y = (p[:, 0] - ox) / res, (p[:, 1] - oy) / res
        i0, i1 = int(math.ceil(x.min())), int(math.floor(x.max()))
        j0, j1 = int(math.ceil(y.min())), int(math.floor(y.max()))
        if i1 < i0 or j1 < j0:
            continue
        X, Y = np.meshgrid(np.arange(i0, i1 + 1, dtype=float), np.arange(j0, j1 + 1, dtype=float))
        d = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
        w0 = ((y[1] - y[2]) * (X - x[2]) + (x[2] - x[1]) * (Y - y[2])) / d
        w1 = ((y[2] - y[0]) * (X - x[2]) + (x[0] - x[2]) * (Y - y[2])) / d
        w2 = 1 - w0 - w1
        ins = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not ins.any():
            continue
        z = w0 * p[0, 2] + w1 * p[1, 2] + w2 * p[2, 2]
        kk = np.searchsorted(zc, z[ins])                 # first layer centre above the crossing
        jj, ii = (Y[ins] - 0).astype(int), (X[ins] - 0).astype(int)
        np.add.at(delta, (kk, jj, ii), sgn[k])
    occ = np.cumsum(delta, axis=0, dtype=np.int8)[:-1] > 0
    return occ, (ox + 0.0, oy + 0.0)


def regions(tri: np.ndarray, first: float, lh: float, line_width: float, angle_deg: float,
            res: float = RES, max_voxels: float = MAX_VOXELS) -> dict:
    """Unsupported regions of an oriented part (base on z = 0) printed without supports. The grid is
    res mm, coarser for big parts (at most max_voxels cells); grid_mm in the result."""
    from scipy import ndimage
    lo, hi = M.bbox(tri)
    zc = layer_centres(float(hi[2]), first, lh)
    cells = (float(hi[0] - lo[0]) + 1) * (float(hi[1] - lo[1]) + 1) * len(zc)
    res = max(res, math.sqrt(cells / max_voxels))
    occ, (ox, oy) = voxelize(tri, zc, res)
    r = reach(line_width, lh, angle_deg)
    unsup = np.zeros_like(occ)
    dist = np.zeros(occ.shape, dtype=np.float32)
    for k in range(1, len(zc)):
        if not occ[k].any():
            continue
        if occ[k - 1].any():
            d = ndimage.distance_transform_edt(~occ[k - 1]) * res
        else:
            d = np.full(occ[k].shape, np.inf)
        u = occ[k] & (d > r)
        unsup[k] = u
        dist[k][u] = np.minimum(d[u], 1e6)
    lab, n = ndimage.label(unsup, structure=np.ones((3, 3, 3), dtype=bool))
    out = []
    eight = np.ones((3, 3), dtype=bool)
    for idx, sl in enumerate(ndimage.find_objects(lab), start=1):
        m = lab[sl] == idx
        area = float(m.sum()) * res * res
        if area < MIN_AREA:
            continue
        k0 = sl[0].start + int(np.argmax(m.reshape(m.shape[0], -1).any(axis=1)))
        layer_u = lab[k0] == idx
        # the piece of layer k0 this region belongs to, and what of that piece is supported
        piece_lab, _ = ndimage.label(occ[k0], structure=eight)
        pieces = set(np.unique(piece_lab[layer_u])) - {0}
        piece = np.isin(piece_lab, list(pieces))
        supported = piece & ~unsup[k0]
        ring = ndimage.binary_dilation(layer_u, structure=eight, iterations=2) & supported
        contacts = ndimage.label(ring, structure=eight)[1]
        if not supported.any():
            kind = "island"
        elif contacts >= 2:
            kind = "bridge"
        else:
            kind = "cantilever"
        # how far the region reaches beyond its support (on its first layer), and a bridge's span
        far = ndimage.distance_transform_edt(~supported) * res if supported.any() else None
        beyond = float(far[layer_u].max()) if far is not None else float("inf")
        zz = zc[sl[0].start:sl[0].stop]
        jj, ii = np.nonzero(layer_u)
        out.append({"kind": kind, "area_mm2": round(area, 1),
                    "beyond_mm": round(beyond, 2) if math.isfinite(beyond) else None,
                    "span_mm": round(2 * beyond, 1) if kind == "bridge" else None,
                    "z_mm": [round(float(zz.min() - lh / 2), 2), round(float(zz.max() + lh / 2), 2)],
                    "center_xy": [round(float(ox + (ii.mean() + 0.5) * res), 1), round(float(oy + (jj.mean() + 0.5) * res), 1)],
                    "layers": int(sl[0].stop - sl[0].start)})
    out.sort(key=lambda g: (-{"island": 3, "cantilever": 2, "bridge": 1}[g["kind"]], -g["area_mm2"]))
    return {"reach_mm": round(r, 3), "grid_mm": round(res, 3), "layers": int(len(zc)), "regions": out,
            "volume_voxels_mm3": round(float(occ.sum()) * res * res * lh, 0)}


def verdict(reg: dict, supports: str, bridge_max: float = 25.0) -> tuple[bool, list[str]]:
    """(prints without failing, problems) for a regions() result with this support setting. Islands
    and cantilevers reaching more than 1 mm beyond support fail without supports; bridges longer than
    bridge_max mm are reported (they sag) but do not fail."""
    problems = []
    for g in reg["regions"]:
        where = f"z {g['z_mm'][0]}-{g['z_mm'][1]} mm, part xy {g['center_xy']}"
        if g["kind"] == "island":
            problems.append(f"island of {g['area_mm2']} mm2 starting in mid-air ({where})")
        elif g["kind"] == "cantilever" and (g["beyond_mm"] or 0) > 1.0:
            problems.append(f"cantilever of {g['area_mm2']} mm2 reaching {g['beyond_mm']} mm past support ({where})")
        elif g["kind"] == "bridge" and (g["span_mm"] or 0) > bridge_max:
            problems.append(f"bridge spanning {g['span_mm']} mm ({where}): it will sag")
    hard = [p for p in problems if not p.startswith("bridge")]
    ok = supports != "none" or not hard
    return ok, problems


# ---------------------------------------------------------------- after slicing

def _fill(g: np.ndarray) -> np.ndarray:
    """Lines plus everything they enclose: background regions not touching the border are holes."""
    from scipy import ndimage
    lab, n = ndimage.label(~g)
    if n == 0:
        return g
    keep = np.ones(n + 1, dtype=bool)
    keep[0] = False
    keep[np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))] = False
    return g | keep[lab]


def _ramp(n: np.ndarray):
    """For segments cut into n[k] pieces: the segment of each sample point and its t in 0..1 (ends included)."""
    rep = np.repeat(np.arange(len(n)), n + 1)
    first = np.r_[0, np.cumsum(n + 1)[:-1]]
    return rep, (np.arange(len(rep)) - first[rep]) / n[rep]


class _Mask:
    """A boolean mask on a window of the layer grid, starting at grid cell (i0, j0)."""

    def __init__(self, i0, j0, mask):
        self.i0, self.j0, self.mask = i0, j0, mask

    def __getitem__(self, ij):
        """Mask values at grid cells (i, j) arrays (False outside the window)."""
        i, j = ij
        ii, jj = i - self.i0, j - self.j0
        ok = (ii >= 0) & (jj >= 0) & (ii < self.mask.shape[1]) & (jj < self.mask.shape[0])
        v = np.zeros(len(i), dtype=bool)
        v[ok] = self.mask[jj[ok], ii[ok]]
        return v

    def window(self, i0, j0, ny, nx):
        """The mask on another window (False outside its own)."""
        out = np.zeros((ny, nx), dtype=bool)
        a0, a1 = max(i0, self.i0), min(i0 + nx, self.i0 + self.mask.shape[1])
        b0, b1 = max(j0, self.j0), min(j0 + ny, self.j0 + self.mask.shape[0])
        if a1 > a0 and b1 > b0:
            out[b0 - j0:b1 - j0, a0 - i0:a1 - i0] = self.mask[b0 - self.j0:b1 - self.j0, a0 - self.i0:a1 - self.i0]
        return out


def unsupported(P, line_width: float, layer_height: float, angle_deg: float, support_spacing: float = 2.5,
                bridge_max: float = BRIDGE_MAX, res: float = GRES) -> dict:
    """Extrusion of the part farther than `reach` from what the layer below printed. What a layer offers
    the next is an area: its perimeters filled (sparse infill touches the layer above only at crossings),
    and its support lines with their spacing filled (a part's first layer spans the support's lines like
    a short bridge; support up to 3 layers below counts, for the support's top gap). Each extrusion path
    (segments continuing one another without a travel) is sampled every STEP mm; an unsupported stretch
    with supported path on both sides, about straight between them, is a bridge: accepted, but spans over
    bridge_max mm are listed in long_bridges (they sag).
    Returns {reach_mm, unsupported_mm (past reach), severe_mm (more than 1 mm past reach), by_layer,
    worst, layers_affected, long_bridges, flags (bool per segment of P: severe)}. worst[].floating: a
    whole extrusion path with nothing below (it starts in mid-air). Distances are exact up to reach +
    support_spacing / 2 + 2 mm; max_beyond_mm stops there (beyond_at_least: true)."""
    from scipy import ndimage
    from .check import category
    seg, info = P.segments, P.seg_info
    flags = np.zeros(len(seg), dtype=bool)
    r = reach(line_width, layer_height, angle_deg)
    gap = 0.5 * max(support_spacing, 0.0)
    margin = r + 1.0 + gap + 1.0                       # mm: farther than this is severe whatever the exact distance
    out = {"reach_mm": round(r, 3), "unsupported_mm": 0.0, "severe_mm": 0.0, "by_layer": {}, "worst": [],
           "layers_affected": 0, "long_bridges": []}
    m = info[:, 0] == 1 if len(info) else np.zeros(0, bool)
    if not m.any():
        out["flags"] = flags
        return out
    cats = np.array([category(f) for f in P.features])
    cat = cats[info[:, 2]]
    lo = np.vstack([seg[m][:, 0:2], seg[m][:, 2:4]]).min(axis=0) - 5
    order = np.argsort(info[:, 4], kind="stable")
    order = order[m[order]]
    lays = info[order, 4]
    bounds = np.r_[0, np.nonzero(np.diff(lays))[0] + 1, len(order)]

    def samples(s, step):
        L = np.hypot(s[:, 2] - s[:, 0], s[:, 3] - s[:, 1])
        n = np.maximum(1, np.ceil(L / step).astype(int))
        rep, t = _ramp(n)
        return s[rep, 0] + (s[rep, 2] - s[rep, 0]) * t, s[rep, 1] + (s[rep, 3] - s[rep, 1]) * t

    def grid_of(ids):
        """(i0, j0, mask) of the lines of these segments, 2 px thick, on their own window."""
        if not len(ids):
            return None
        x, y = samples(seg[ids], res * 0.5)
        i = ((x - lo[0]) / res).astype(int)
        j = ((y - lo[1]) / res).astype(int)
        i0, j0 = i.min() - 2, j.min() - 2
        g = np.zeros((j.max() - j0 + 3, i.max() - i0 + 3), dtype=bool)
        g[j - j0, i - i0] = g[j - j0 + 1, i - i0] = g[j - j0, i - i0 + 1] = g[j - j0 + 1, i - i0 + 1] = True
        return i0, j0, g

    layers = []                                       # (layer, segment ids, area _Mask, support _Mask)
    for a, b in zip(bounds[:-1], bounds[1:]):
        ids = order[a:b]
        part = grid_of(ids[cat[ids] != "support"])
        sup = grid_of(ids[cat[ids] == "support"])
        if part is None and sup is None:
            continue
        layers.append((int(lays[a]), ids, _Mask(part[0], part[1], _fill(part[2])) if part else None,
                       _Mask(*sup) if sup else None))

    total = severe = 0.0
    floating, where = {}, {}
    for pos in range(1, len(layers)):
        L, ids, _, _ = layers[pos]
        below = layers[pos - 1][2]
        obj = ids[np.isin(cat[ids], ["object", "other"])]
        if not len(obj):
            continue
        s = seg[obj]
        Ls = np.hypot(s[:, 2] - s[:, 0], s[:, 3] - s[:, 1])
        # paths: segments that continue one another without a travel (a wall loop, a bridge zigzag; the
        # slicer may switch feature mid-path, e.g. outer wall -> overhang wall over a gap)
        cont = np.r_[False, np.hypot(s[1:, 0] - s[:-1, 2], s[1:, 1] - s[:-1, 3]) < 1e-3]
        path = np.cumsum(~cont)
        n = np.maximum(1, np.ceil(Ls / STEP).astype(int))
        rep, t = _ramp(n)
        x = s[rep, 0] + (s[rep, 2] - s[rep, 0]) * t
        y = s[rep, 1] + (s[rep, 3] - s[rep, 1]) * t
        w = np.where(t > 0, Ls[rep] / n[rep], 0.0)          # length each sample closes
        I = ((x - lo[0]) / res).astype(int)
        J = ((y - lo[1]) / res).astype(int)
        inside = below[I, J] if below is not None else np.zeros(len(x), dtype=bool)
        if inside.all():
            continue
        # distances only around the samples outside the area below (exact up to `margin`)
        o = ~inside
        mpx = int(math.ceil(margin / res))
        i0, j0 = I[o].min() - mpx, J[o].min() - mpx
        nxw, nyw = I[o].max() - i0 + mpx + 1, J[o].max() - j0 + mpx + 1
        base = below.window(i0, j0, nyw, nxw) if below is not None else np.zeros((nyw, nxw), dtype=bool)
        d = ndimage.distance_transform_edt(~base) * res if base.any() else np.full((nyw, nxw), np.inf)
        sb = np.zeros((nyw, nxw), dtype=bool)
        for back in range(max(0, pos - 4), pos):
            sl = layers[back][3]
            if sl is not None:
                sb |= sl.window(i0, j0, nyw, nxw)
        if sb.any():
            d = np.minimum(d, np.maximum(ndimage.distance_transform_edt(~sb) * res - gap, 0.0))
        D = np.zeros(len(x))
        D[o] = np.minimum(d[J[o] - j0, I[o] - i0], margin)
        bad = D > r
        if not bad.any():
            continue
        # runs of unsupported samples along a path
        pid = path[rep]
        same_prev = np.r_[False, pid[1:] == pid[:-1]]
        same_next = np.r_[pid[1:] == pid[:-1], False]
        starts = np.nonzero(bad & ~(np.r_[False, bad[:-1]] & same_prev))[0]
        ends = np.nonzero(bad & ~(np.r_[bad[1:], False] & same_next))[0]
        csum = np.cumsum(w)
        length = csum[ends] - csum[starts] + np.where(t[starts] > 0, w[starts], 0.0)
        dmax = np.maximum.reduceat(np.where(bad, D, 0.0), starts)
        # a bridge: supported path on both sides, and about straight between them (a loop around an
        # overhang also leaves and comes back to supported path, but much longer than the chord)
        anchored = same_prev[starts] & same_next[ends]
        a0, a1 = np.maximum(starts - 1, 0), np.minimum(ends + 1, len(x) - 1)
        chord = np.hypot(x[a1] - x[a0], y[a1] - y[a0])
        bridge = anchored & (length <= STRAIGHT * chord + 1.0)
        long = bridge & (length > bridge_max)
        if long.any():
            out["long_bridges"].append([L, round(float(length[long].max()), 1), int(long.sum())])
        count = ~bridge
        sev = count & (dmax > r + 1.0)
        if not count.any():
            continue
        mm, smm = float(length[count].sum()), float(length[sev].sum())
        total += mm
        severe += smm
        def samples_of(runs):
            mark = np.zeros(len(x) + 1, dtype=int)
            np.add.at(mark, starts[runs], 1)
            np.add.at(mark, ends[runs] + 1, -1)
            return np.cumsum(mark[:-1]) > 0

        if sev.any():
            flags[obj[np.unique(rep[samples_of(sev)])]] = True
        sm = samples_of(sev if sev.any() else count)
        names = sorted({P.objects[o] for o in info[obj[np.unique(rep[sm])], 3] if 0 <= o < len(P.objects)})
        where[L] = ([round(float(x[sm].mean()), 1), round(float(y[sm].mean()), 1)], names)
        top = float(dmax[count].max())
        loose = count & ~same_prev[starts] & ~same_next[ends]      # a whole path with nothing below
        floating[L] = bool((loose & sev).any() if sev.any() else loose.any())
        out["by_layer"][L] = [round(mm, 1), round(smm, 1), round(top, 2)]
    out["unsupported_mm"] = round(total, 1)
    out["severe_mm"] = round(severe, 1)
    lz = {i: l.get("z_height") for i, l in enumerate(P.layers)}
    worst = sorted(out["by_layer"].items(), key=lambda kv: (-kv[1][1], -kv[1][0]))[:5]
    out["worst"] = [{"layer": L + 1, "z_mm": lz.get(L), "mm": v[0], "severe_mm": v[1],
                     "max_beyond_mm": round(v[2] - r, 2), "beyond_at_least": bool(v[2] >= margin - 1e-6),
                     "floating": bool(floating.get(L)),
                     "xy": where[L][0], "objects": where[L][1]} for L, v in worst]
    out["layers_affected"] = len(out["by_layer"])
    lb = sorted(out["long_bridges"], key=lambda v: -v[1])
    out["long_bridges"] = {"layers": len(lb), "longest_mm": lb[0][1] if lb else 0.0,
                           "worst": [{"layer": L + 1, "z_mm": lz.get(L), "span_mm": v, "count": c} for L, v, c in lb[:3]]}
    out["flags"] = flags
    return out
