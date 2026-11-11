"""check.py — independent checks of a sliced plate: the G-code (as gcode.py reads it) against the study,
our flattened profiles and the placed geometry. Nothing here trusts OrcaSlicer's own summary; its
estimates are only compared with ours.
Version: 2026.11.11.2

Each check is {passed, hard, why, got, expected, ...}. A failed hard check makes the result
unreliable. The checks:

  objects_present   every placed object is printed (object markers), and nothing else
  bounds            every extrusion of the print is inside the printable area of the extruder that
                    prints it (H2D: per-extruder areas), with 0 < z <= printable height
  travel            travel moves of the print stay within the bed, 1 mm margin (soft)
  layers            layer count and heights: first layer = initial_layer_print_height, then
                    layer_height; the top at the part height (+- half a layer). Support-only layers
                    (support at its own layer heights) are counted apart
  first_layer       the first extrusion is at the first layer height, and every object has some
  tools             each object is printed with its own filament; only the study's filaments are used
  temperatures      nozzle and bed targets = the filament's (first layer, then others), within its range
  unlocated         no extrusion of the print starts from an unknown position
  extrusion_volume  per object, extruded volume vs a model from the exact volume, the surface areas,
                    walls, top/bottom shells and infill (+-25%), and never more than 1.1 x the volume
  footprint         no extrusion outside its allowed region: the object's silhouette + half a line
                    (object features), + brim, + support margin, the prime tower's own rectangle
  wrapping_area     no extrusion in the machine's wrapping-detection zone (soft)
  shared_nozzle     (multi-extruder) filaments with different temperatures not on one nozzle (soft)
  bed_vs_filaments  the bed not more than 10 C above a used filament's own bed temperature (soft)
  unsupported_extrusion  every extrusion of the parts rests on the layer below or on support: no
                    more than SEVERE_MAX mm farther than reach + 1 mm (supports.unsupported)
  long_bridges      bridges (unsupported stretches held at both ends) up to BRIDGE_MAX mm (soft)
  estimates         OrcaSlicer's filament lengths and weights = ours (soft)
"""
from __future__ import annotations

import math

import numpy as np

from . import gcode as G
from . import mesh as M

TOL_Z = 0.005           # mm
TOL_XY = 0.05           # mm, bounds
SUPPORT_MARGIN = 5.0    # mm beyond the silhouette that supports may reach
AUTO_BRIM_MAX = 10.0    # mm, allowance for brim_type auto_brim (assumption; see README)
SEVERE_MAX = 5.0        # mm of extrusion more than 1 mm beyond support (noise allowance)


def _c(passed: bool, why: str, hard: bool = True, **kw) -> dict:
    return {"passed": bool(passed), "hard": hard, "why": why, **kw}


def _first(v, default=None):
    if isinstance(v, list):
        v = v[0] if v else default
    if isinstance(v, str) and ("," in v) and not v.replace(",", "").replace(".", "").replace("-", "").isalpha():
        v = v.split(",")[0]
    return default if v in (None, "") else v


def _f(v, default=0.0) -> float:
    v = _first(v, default)
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return float(default)


def _list(v) -> list:
    if isinstance(v, list):
        return v
    if v is None or v == "":
        return []
    return str(v).split(",")


def _poly(pts) -> np.ndarray:
    """['0x0', '350x0', ...] or '0x0,350x0,...' -> (n, 2) array."""
    if isinstance(pts, str):
        pts = pts.split(",")
    return np.array([[float(a) for a in p.lower().split("x")] for p in pts if p], dtype=float)


def inside(points: np.ndarray, poly: np.ndarray, tol: float = 0.0) -> np.ndarray:
    """Points (n, 2) inside a polygon; tol > 0 also accepts points within tol of an axis-aligned
    rectangle's edges (the BBL beds are rectangles)."""
    x, y = points[:, 0], points[:, 1]
    lo, hi = poly.min(axis=0), poly.max(axis=0)
    is_rect = len(poly) == 4 and all((abs(p[0] - lo[0]) < 1e-9 or abs(p[0] - hi[0]) < 1e-9) and
                                     (abs(p[1] - lo[1]) < 1e-9 or abs(p[1] - hi[1]) < 1e-9) for p in poly)
    if is_rect:
        return (x >= lo[0] - tol) & (x <= hi[0] + tol) & (y >= lo[1] - tol) & (y <= hi[1] + tol)
    res = np.zeros(len(points), dtype=bool)
    n = len(poly)
    for k in range(n):
        x1, y1 = poly[k]
        x2, y2 = poly[(k + 1) % n]
        cond = (y1 > y) != (y2 > y)
        xi = (x2 - x1) * (y - y1) / ((y2 - y1) if y2 != y1 else 1e-12) + x1
        res ^= cond & (x < xi)
    return res


# ---------------------------------------------------------------- footprints

class Footprint:
    """An object's XY silhouette as a raster, with the distance (mm) from it of any point."""

    def __init__(self, tri: np.ndarray, res: float = 0.1, pad: float = 30.0):
        from scipy.ndimage import distance_transform_edt
        xy = tri[:, :, :2]
        lo = xy.reshape(-1, 2).min(axis=0) - pad
        hi = xy.reshape(-1, 2).max(axis=0) + pad
        self.res, self.lo = res, lo
        shape = np.ceil((hi - lo) / res).astype(int) + 1
        from .placement import _raster
        mask = _raster(tri, lo, (shape[1], shape[0]), res)            # [row = y, col = x]
        self.mask = mask
        self.dist = distance_transform_edt(~mask) * res               # 0 inside, mm outside
        self.shape = shape

    def distance(self, pts: np.ndarray) -> np.ndarray:
        ij = np.round((pts - self.lo) / self.res).astype(int)
        ok = (ij[:, 0] >= 0) & (ij[:, 0] < self.shape[0]) & (ij[:, 1] >= 0) & (ij[:, 1] < self.shape[1])
        out = np.full(len(pts), np.inf)
        out[ok] = self.dist[ij[ok, 1], ij[ok, 0]]
        return out


def section_width(tri: np.ndarray, z: float, res: float = 0.05) -> float:
    """The widest feature (mm: twice the largest inscribed radius) of the mesh's cross-section at z;
    0 when the mesh does not reach z."""
    from scipy.ndimage import distance_transform_edt
    from .supports import voxelize
    if z >= float(tri[:, :, 2].max()):
        return 0.0
    cross = tri[(tri[:, :, 2].max(axis=1) > z)]
    lo, hi = cross[:, :, :2].reshape(-1, 2).min(axis=0), cross[:, :, :2].reshape(-1, 2).max(axis=0)
    txy_lo, txy_hi = tri[:, :, :2].min(axis=1), tri[:, :, :2].max(axis=1)
    keep = tri[np.all(txy_hi >= lo, axis=1) & np.all(txy_lo <= hi, axis=1)]
    res = max(res, float(np.max(hi - lo)) / 2000)
    occ, (ox, oy) = voxelize(keep, np.array([z]), res)
    # inside / outside is right only in the columns whose triangles were all kept: [lo, hi]
    i0, i1 = int(np.floor((lo[0] - ox) / res)), int(np.ceil((hi[0] - ox) / res)) + 1
    j0, j1 = int(np.floor((lo[1] - oy) / res)), int(np.ceil((hi[1] - oy) / res)) + 1
    win = occ[0, max(j0, 0):j1, max(i0, 0):i1]
    if not win.any():
        return 0.0
    return float(2 * distance_transform_edt(np.pad(win, 1)).max() * res)


def sample(seg: np.ndarray, step: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Points every `step` mm along segments (both ends included) and their segment index."""
    if not len(seg):
        return np.zeros((0, 2)), np.zeros(0, dtype=int)
    L = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    n = np.maximum(1, np.ceil(L / step).astype(int))
    idx = np.repeat(np.arange(len(seg)), n + 1)
    k = np.concatenate([np.arange(m + 1) / m for m in n])
    pts = np.column_stack([seg[idx, 0] + (seg[idx, 2] - seg[idx, 0]) * k,
                           seg[idx, 1] + (seg[idx, 3] - seg[idx, 1]) * k])
    return pts, idx


def category(feature: str) -> str:
    f = (feature or "").lower()
    if "prime tower" in f or "wipe tower" in f:
        return "tower"
    if "support" in f:
        return "support"
    if "brim" in f:
        return "brim"
    if "skirt" in f:
        return "skirt"
    if f in ("custom", ""):
        return "other"
    return "object"


# ---------------------------------------------------------------- the checks

def check_gcode(P: G.Parsed, resolved: dict, objects: list[dict], tower_bbox: list | None = None,
                filament_map: list[int] | None = None, color_changes: list[dict] | None = None) -> dict:
    """objects: [{name (the G-code marker, e.g. 'part_1' or 'assemble_1'), filament (1-based), filaments (all
    of its parts', optional), tri (placed mesh), volume (mm3, exact when known), settings (its own process
    settings, optional)}]. color_changes: [{z, filament}]. Returns {checks, gcode (summary), estimate}."""
    machine, process, fils = resolved["machine"], resolved["process"], resolved["filaments"]
    cfg = P.config

    def pv(key, default=None):
        return process.get(key, cfg.get(key, default))

    def mv(key, default=None):
        return machine.get(key, cfg.get(key, default))

    diam = [_f(f.get("filament_diameter"), 1.75) for f in fils]
    seg, info = P.segments, P.seg_info
    printing = info[:, 0] == 1 if len(info) else np.zeros(0, dtype=bool)
    cats = np.array([category(P.features[f]) for f in info[:, 2]]) if len(info) else np.array([])
    vol = seg[:, 5] * np.array([G.filament_area(diam[t] if 0 <= t < len(diam) else 1.75) for t in info[:, 1]]) \
        if len(seg) else np.zeros(0)
    names = {o["name"]: k for k, o in enumerate(objects)}
    obj_of_seg = np.array([names.get(P.objects[o], -1) if o >= 0 else -1 for o in info[:, 3]]) if len(info) else np.array([])
    checks = {}

    # objects_present
    printed = set(P.objects)
    want = set(names)
    checks["objects_present"] = _c(printed == want, "every placed object is printed, and nothing else",
                                   got=sorted(printed), expected=sorted(want))

    # bounds (per extruder on multi-extruder printers)
    bed = _poly(mv("printable_area"))
    height = _f(mv("printable_height"), 1e9)
    ext_areas = [_poly(a) for a in _list(mv("extruder_printable_area"))] if mv("extruder_printable_area") else []
    fmap = filament_map or [int(_f(v)) for v in _list(cfg.get("filament_map"))] or None
    if fmap and len(fmap) < len(fils):
        # OrcaSlicer 2.4.2 may write one value for several filaments (auto map); its CLI gives the
        # missing filaments the first value ("set filament_map of filament N to first value")
        fmap = list(fmap) + [fmap[0]] * (len(fils) - len(fmap))
    bad_pts, worst = 0, None
    if printing.any():
        for t in np.unique(info[printing, 1]):
            m = printing & (info[:, 1] == t)
            area = bed
            if ext_areas and fmap and 0 <= t < len(fmap) and 1 <= fmap[t] <= len(ext_areas):
                area = ext_areas[fmap[t] - 1]
            pts = np.vstack([seg[m][:, 0:2], seg[m][:, 2:4]])
            ok = inside(pts, area, TOL_XY)
            if (~ok).any():
                bad_pts += int((~ok).sum())
                worst = pts[~ok][0].round(3).tolist()
        zs = seg[printing, 4]
        zbad = int(((zs <= 0) | (zs > height + TOL_Z)).sum())
    else:
        zbad = 0
    checks["bounds"] = _c(bad_pts == 0 and zbad == 0,
                          "every extrusion of the print inside the printable area of its extruder, 0 < z <= height",
                          got={"points_outside": bad_pts, "first_outside": worst, "z_outside": zbad},
                          expected={"area": bed.tolist(), "height": height,
                                    "extruder_areas": [a.tolist() for a in ext_areas] or None})
    tb = P.travel_bbox.get("print")
    if tb:
        lo, hi = bed.min(axis=0), bed.max(axis=0)
        okt = tb[0] >= lo[0] - 1 and tb[1] >= lo[1] - 1 and tb[2] <= hi[0] + 1 and tb[3] <= hi[1] + 1
        checks["travel"] = _c(okt, "travel moves of the print stay on the bed (1 mm margin)", hard=False,
                              got=tb, expected=[lo[0], lo[1], hi[0], hi[1]])

    # each object's own settings (objects[].settings: OrcaSlicer per-object print_params) over the plate's
    def opv(k):
        own = objects[k].get("settings") or {}
        return lambda key, default=None: own.get(key, pv(key, default))

    # layers: per object (objects may have their own layer heights); support printed at its own layer
    # heights (independent_support_layer_height, normal supports) adds support-only layers
    h1 = _f(pv("initial_layer_print_height"), 0.2)
    lh = _f(pv("layer_height"), 0.2)
    part = printing & (cats != "support") if len(seg) else np.zeros(0, dtype=bool)
    zl = np.unique(np.round(seg[part, 4], 3)) if part.any() else np.array([])
    sup_only = sorted(set(info[printing, 4].tolist()) - set(info[part, 4].tolist())) if printing.any() else []
    first_ok = bool(len(zl) and abs(zl[0] - h1) <= TOL_Z)
    per_layers, layers_ok, slivers = {}, first_ok, {}
    for k, o in enumerate(objects):
        lh_k = _f(opv(k)("layer_height"), lh)
        H = float(M.bbox(o["tri"])[1][2])
        zk = np.unique(np.round(seg[part & (obj_of_seg == k), 4], 3)) if part.any() else np.array([])
        dz = np.diff(zk) if len(zk) > 1 else np.array([])
        steps_ok = bool(len(dz) == 0 or (np.all(np.abs(dz[:-1] - lh_k) <= TOL_Z) and 0 < dz[-1] <= lh_k + TOL_Z))
        top = float(zk[-1]) if len(zk) else None
        top_ok = top is not None and abs(top - H) <= lh_k / 2 + TOL_Z
        if top is not None and not top_ok and top < H:
            lw_k = max(_f(opv(k)("outer_wall_line_width"), 0.42), _f(opv(k)("line_width"), 0.42))
            width = section_width(o["tri"], top + lh_k / 2)
            if width < lw_k:                    # the next layer would be thinner than a line: not printable
                slivers[o["name"]] = {"not_printed_mm": round(H - top, 3), "next_layer_width_mm": round(width, 3),
                                      "line_width_mm": lw_k}
                top_ok = True
        n_exp = 1 + max(0, int(math.floor(((top if top_ok and top is not None else H) - h1) / lh_k + 0.5)))
        count_ok = len(zk) > 0 and abs(len(zk) - n_exp) <= 1
        ok_k = bool(steps_ok and top_ok and count_ok and len(zk) and abs(zk[0] - h1) <= TOL_Z)
        layers_ok &= ok_k
        per_layers[o["name"]] = {"levels": int(len(zk)), "expected_levels": n_exp, "first": float(zk[0]) if len(zk) else None,
                                 "top": top, "part_top": round(H, 4), "layer_height": lh_k,
                                 "steps": sorted({round(float(v), 4) for v in dz})[:6], "passed": ok_k}
    plate_ok = len(zl) == len(P.layers) - len(sup_only) and (P.header.get("layers") in (None, len(P.layers)))
    checks["layers"] = _c(layers_ok and plate_ok,
                          "per object: first layer, then its layer_height, its top at its height (+- half a layer; a "
                          "top thinner than a line above the last layer, which the slicer cannot print, is reported "
                          "in top_slivers, not failed); the plate's layer markers match",
                          got={"levels": int(len(zl)), "layer_markers": len(P.layers), "support_only_layers": len(sup_only),
                               "header": P.header.get("layers"), "first": float(zl[0]) if len(zl) else None,
                               "top": float(zl[-1]) if len(zl) else None, "by_object": per_layers,
                               **({"slivers_not_printed": slivers} if slivers else {})},
                          expected={"first": h1, "layer_height": lh})
    if slivers:
        checks["top_slivers"] = _c(False, "the top of these objects is thinner than a printable layer and is not printed "
                                          "(trim it, or accept it)", hard=False, got=slivers,
                                   problems=[f"{n}: top {v['not_printed_mm']} mm not printed (the next layer would be "
                                             f"{v['next_layer_width_mm']} mm wide, less than a line)" for n, v in slivers.items()])

    # first_layer
    on_first = {}
    if printing.any() and len(zl):
        m0 = printing & (np.abs(seg[:, 4] - zl[0]) <= TOL_Z) & (cats == "object")
        on_first = {objects[k]["name"]: bool((m0 & (obj_of_seg == k)).any()) for k in range(len(objects))}
    checks["first_layer"] = _c(first_ok and all(on_first.values()) and bool(on_first),
                               "the first extrusion is at the first layer height, and every object starts on the bed",
                               got={"z": float(zl[0]) if len(zl) else None, "objects_on_first_layer": on_first},
                               expected={"z": h1})

    # tools: each object with its parts' filaments; from each color change's height up, with that filament
    used = sorted({int(t) for t in info[printing, 1]}) if printing.any() else []
    changes = sorted(color_changes or [], key=lambda c: c["z"])
    per_obj, tools_ok, wrong = {}, all(0 <= t < len(fils) for t in used), {}
    for k, o in enumerate(objects):
        m = printing & (obj_of_seg == k) & (cats == "object")
        per_obj[o["name"]] = sorted({int(t) + 1 for t in info[m, 1]})
        allowed = set(o.get("filaments") or [o["filament"]])
        z = seg[:, 4]
        t1 = info[:, 1] + 1
        below = m & (z < changes[0]["z"] - TOL_Z) if changes else m
        bad = int((below & ~np.isin(t1, sorted(allowed))).sum())
        for i, c in enumerate(changes):
            top = changes[i + 1]["z"] if i + 1 < len(changes) else 1e9
            rng = m & (z >= c["z"] - TOL_Z) & (z < top - TOL_Z)
            bad += int((rng & (t1 != c["filament"])).sum())
        if bad:
            wrong[o["name"]] = bad
        tools_ok &= not bad and bool(per_obj[o["name"]])
    checks["tools"] = _c(tools_ok, "each object printed with its parts' filaments (and from each color change's height "
                                   "up, with that filament); only the study's filaments used",
                         got={"by_object": per_obj, "filaments_used": [t + 1 for t in used],
                              **({"segments_with_wrong_filament": wrong} if wrong else {})},
                         expected={"by_object": {o["name"]: sorted(set(o.get("filaments") or [o["filament"]])) for o in objects},
                                   **({"color_changes": changes} if changes else {})})

    # temperatures
    checks["temperatures"] = _temperatures(P, resolved, used)
    checks.update(_compromises(resolved, used, fmap, ext_areas, checks["temperatures"]))

    # unlocated extrusion in the print
    unl = {k: round(v, 3) for k, v in P.unlocated_by_region.items()}
    checks["unlocated"] = _c(unl.get("print", 0.0) <= 0.01,
                             "no extrusion of the print from an unknown position (it could not be checked)",
                             got={"by_region_mm": unl})

    # extrusion_volume
    checks["extrusion_volume"] = _volume(objects, vol, printing, obj_of_seg, cats, opv, h1)

    # footprint
    checks["footprint"] = _footprint(objects, seg, info, printing, obj_of_seg, cats, vol, pv, opv, tower_bbox)

    # wrapping-detection zone
    wz = mv("wrapping_exclude_area")
    if wz and printing.any():
        poly = _poly(wz)
        if len(poly) >= 3:
            n_in = int(inside(np.vstack([seg[printing][:, 0:2], seg[printing][:, 2:4]]), poly).sum())
            checks["wrapping_area"] = _c(n_in == 0, "no extrusion in the machine's wrapping-detection zone",
                                         hard=False, got={"points_inside": n_in}, expected={"zone": poly.tolist()})

    # extrusion with nothing below it
    if printing.any():
        checks["unsupported_extrusion"], checks["long_bridges"] = _support(P, pv, mv, objects, obj_of_seg, opv)

    # estimates (OrcaSlicer's vs ours)
    checks["estimates"], estimate = _estimates(P, fils, diam)
    return {"checks": checks, "gcode": G.summary(P, diam), "estimate": estimate}


def _temperatures(P: G.Parsed, resolved: dict, used: list[int]) -> dict:
    fils = resolved["filaments"]
    plate = resolved["plate"]
    first_line = P.marks.get("first_layer") or 0
    exp = []
    for f in fils:
        exp.append({"first": _f(f.get("nozzle_temperature_initial_layer")), "other": _f(f.get("nozzle_temperature")),
                    "low": _f(f.get("nozzle_temperature_range_low"), 0), "high": _f(f.get("nozzle_temperature_range_high"), 1e9),
                    "bed_first": _f(f.get(plate["temp_key"] + "_initial_layer"), _f(f.get(plate["temp_key"]))),
                    "bed_other": _f(f.get(plate["temp_key"]))})
    problems = []
    unc = [t for t in P.temps if not t["conditional"]]
    nozzle_before = [t for t in unc if t["kind"] == "nozzle" and t["line"] < first_line and t["value"] > 0
                     and "A" not in t["flags"]]
    first_tool = next((c["tool"] for c in P.tool_changes if not c["conditional"] and c["line"] < first_line), None)
    if first_tool is None and used:
        first_tool = used[0]
    got_first = nozzle_before[-1]["value"] if nozzle_before else None
    if first_tool is not None and 0 <= first_tool < len(exp):
        if got_first != exp[first_tool]["first"]:
            problems.append(f"nozzle at the start of the print {got_first}, filament {first_tool + 1} wants "
                            f"{exp[first_tool]['first']} (first layer)")
    in_print = [t for t in unc if t["region"] == "print" and t["value"] > 0 and t["kind"] in ("nozzle", "nozzle_change")
                and "A" not in [f for f in t["flags"] if f == "A"]]
    seen = {}
    for t in in_print:
        tool = t["tool"]
        if tool is None or not (0 <= tool < len(exp)):
            continue
        e = exp[tool]
        seen.setdefault(tool + 1, set()).add(t["value"])
        if t["value"] not in (e["first"], e["other"]):
            problems.append(f"line {t['line'] + 1}: nozzle {t['value']} for filament {tool + 1}, expected "
                            f"{e['first']} or {e['other']}")
        if not e["low"] <= t["value"] <= e["high"]:
            problems.append(f"line {t['line'] + 1}: nozzle {t['value']} outside filament {tool + 1}'s range "
                            f"{e['low']}-{e['high']}")
    # bed: by_first_filament or by_highest_temp
    formula = str(P.config.get("bed_temperature_formula", "by_first_filament"))
    pool = [exp[t] for t in used if 0 <= t < len(exp)] or exp
    if formula == "by_highest_temp":
        bed_first, bed_other = max(e["bed_first"] for e in pool), max(e["bed_other"] for e in pool)
    else:
        e0 = exp[first_tool] if first_tool is not None and 0 <= first_tool < len(exp) else exp[0]
        bed_first, bed_other = e0["bed_first"], e0["bed_other"]
    beds_before = [t for t in unc if t["kind"] == "bed" and t["line"] < first_line]
    got_bed = beds_before[-1]["value"] if beds_before else None
    if got_bed != bed_first:
        problems.append(f"bed at the start of the print {got_bed}, expected {bed_first} ({plate['bed_type']}, {formula})")
    # by_first_filament: the bed follows the filament active at each layer change (OrcaSlicer 2.4.2 P2S:
    # 55 C under PLA, 70 C once a layer starts with PETG); by_highest_temp: the hottest used filament's
    allowed = {bed_first, bed_other} if formula == "by_highest_temp" else \
        {bed_first, bed_other} | {e["bed_other"] for e in pool} | {e["bed_first"] for e in pool}
    for t in unc:
        if t["kind"] == "bed" and t["region"] == "print" and t["value"] not in allowed:
            problems.append(f"line {t['line'] + 1}: bed {t['value']}, expected one of {sorted(allowed)} ({formula})")
    return _c(not problems, "nozzle and bed temperatures are the filaments' (first layer, then the rest), within range",
              got={"nozzle_start": got_first, "nozzle_in_print": {k: sorted(v) for k, v in seen.items()},
                   "bed_start": got_bed, "bed_max": max([got_bed or 0] + [t["value"] for t in unc if t["kind"] == "bed"
                                                                          and t["region"] == "print"])}, expected={"filaments": exp, "bed": [bed_first, bed_other]},
              problems=problems[:20])


def _compromises(resolved: dict, used: list[int], fmap: list[int] | None, ext_areas: list, temps: dict) -> dict:
    """Soft checks for prints that slice but compromise a filament:
    shared_nozzle  (multi-extruder) two used filaments on one nozzle whose print temperatures differ
                   by more than 15 C: the nozzle keeps reheating between them
    bed_vs_filaments  the bed (one temperature for all) more than 10 C above a used filament's own bed
                   temperature for this plate (OrcaSlicer's 'bed_temperature_too_high_than_filament')"""
    out = {}
    exp = temps.get("expected", {}).get("filaments", [])
    if ext_areas and fmap and len(used) > 1:
        groups = {}
        for t in used:
            if t < len(fmap):
                groups.setdefault(fmap[t], []).append(t)
        problems = []
        for ext, ts in groups.items():
            temps_t = [exp[t]["other"] for t in ts if t < len(exp)]
            if len(temps_t) > 1 and max(temps_t) - min(temps_t) > 15:
                problems.append(f"filaments {[t + 1 for t in ts]} share extruder {ext} "
                                f"({'left' if ext == 1 else 'right'}) at {temps_t} C")
        out["shared_nozzle"] = _c(not problems, "filaments with different print temperatures on separate nozzles "
                                  "(set filaments[].extruder 'left'/'right')", hard=False,
                                  got={"by_extruder": {str(k): [t + 1 for t in v] for k, v in groups.items()}},
                                  problems=problems)
    bed = temps.get("got", {}).get("bed_max") or temps.get("got", {}).get("bed_start")
    if bed is not None and used:
        over = [f"filament {t + 1}: bed {bed} C, its own {exp[t]['bed_first']} C" for t in used
                if t < len(exp) and bed > exp[t]["bed_first"] + 10]
        out["bed_vs_filaments"] = _c(not over, "the bed is not much hotter than any filament wants "
                                     "(PLA on a PETG bed softens: elephant foot, warping)", hard=False,
                                     got={"bed": bed}, problems=over)
    return out


def _volume(objects, vol, printing, obj_of_seg, cats, opv, h1) -> dict:
    rows, ok = {}, True
    model = {}
    for k, o in enumerate(objects):
        pv = opv(k)
        lh = _f(pv("layer_height"), 0.2)
        walls = _f(pv("wall_loops"), 2)
        outer = _f(pv("outer_wall_line_width"), 0.42)
        inner = _f(pv("inner_wall_line_width"), 0.45)
        t_wall = outer + max(0.0, walls - 1) * inner
        t_top = max(_f(pv("top_shell_layers"), 4) * lh, _f(pv("top_shell_thickness"), 0))
        nb = _f(pv("bottom_shell_layers"), 3)
        t_bot = max(h1 + (nb - 1) * lh if nb >= 1 else 0.0, _f(pv("bottom_shell_thickness"), 0))
        dens = _f(pv("sparse_infill_density"), 15) / 100.0
        model[o["name"]] = {"wall_mm": round(t_wall, 3), "top_mm": round(t_top, 3), "bottom_mm": round(t_bot, 3),
                            "infill": dens}
        tri = o["tri"]
        n = M.normals(tri)
        a = np.linalg.norm(M.normals(tri, unit=False), axis=1) / 2
        a_top = float(a[n[:, 2] > 0.7].sum())
        a_bot = float(a[n[:, 2] < -0.7].sum())
        a_side = float(a[np.abs(n[:, 2]) <= 0.7].sum())
        inner = _internal_area([t for _, _, t in o.get("parts") or []])
        a_top, a_bot = max(0.0, a_top - inner), max(0.0, a_bot - inner)
        V = float(o.get("volume") or M.volume(tri))
        shell = min(V, a_side * t_wall + a_top * t_top + a_bot * t_bot)
        expected = shell + dens * (V - shell)
        got = float(vol[printing & (obj_of_seg == k) & (cats == "object")].sum())
        ratio = got / expected if expected > 0 else None
        good = ratio is not None and 0.75 <= ratio <= 1.25 and got <= 1.1 * V
        ok &= good
        rows[o["name"]] = {"got_mm3": round(got, 1), "model_mm3": round(expected, 1), "ratio": round(ratio, 3) if ratio else None,
                           "volume_mm3": round(V, 1), "shell_mm3": round(shell, 1), "passed": good}
    first = next(iter(model.values()), {})
    return _c(ok, "extruded volume per object vs walls + shells + infill of its exact volume (+-25%), "
                  "never above 1.1 x the volume", got=rows,
              expected=first if all(v == first for v in model.values()) else model)


def _internal_area(parts: list) -> float:
    """Horizontal area where two parts of one object meet (one's top on another's bottom, same height):
    the slicer prints the union, so neither is a top or bottom surface. Per height, the smaller of the
    up-facing area of one part and the down-facing area of the others."""
    if len(parts) < 2:
        return 0.0
    ups, downs = [], []
    for t in parts:
        n = M.normals(t)
        a = np.linalg.norm(M.normals(t, unit=False), axis=1) / 2
        z = np.round(t[:, :, 2].mean(axis=1), 2)
        flat = np.ptp(t[:, :, 2], axis=1) < 1e-3
        u, d = {}, {}
        for zz, aa in zip(z[flat & (n[:, 2] > 0.99)], a[flat & (n[:, 2] > 0.99)]):
            u[zz] = u.get(zz, 0.0) + aa
        for zz, aa in zip(z[flat & (n[:, 2] < -0.99)], a[flat & (n[:, 2] < -0.99)]):
            d[zz] = d.get(zz, 0.0) + aa
        ups.append(u)
        downs.append(d)
    total = 0.0
    for i, u in enumerate(ups):
        for zz, au in u.items():
            ad = sum(d.get(zz, 0.0) for j, d in enumerate(downs) if j != i)
            total += min(au, ad)
    return total


def _footprint(objects, seg, info, printing, obj_of_seg, cats, vol, pv, opv, tower_bbox) -> dict:
    if not printing.any() or not objects:
        return _c(False, "nothing to check: no extrusion or no objects")

    def allowances(p):
        lw = max(_f(p("outer_wall_line_width"), 0.42), _f(p("inner_wall_line_width"), 0.45),
                 _f(p("initial_layer_line_width"), 0.5), _f(p("line_width"), 0.42))
        brim_type = str(_first(p("brim_type"), "auto_brim"))
        bw = _f(p("brim_width"), 5)
        brim = 0.0 if brim_type == "no_brim" else (max(bw, AUTO_BRIM_MAX) if brim_type == "auto_brim" else bw)
        brim += _f(p("brim_object_gap"), 0.1) + lw
        skirt = _f(p("skirt_distance"), 2) + max(1.0, _f(p("skirt_loops"), 0)) * lw + brim
        return lw, {"object": lw / 2 + 0.25, "brim": brim + 0.25, "skirt": skirt + 0.5,
                    "support": SUPPORT_MARGIN + brim, "other": lw / 2 + 0.25}

    lw, allow = allowances(pv)
    per_obj = [allowances(opv(k))[1] for k in range(len(objects))]
    fps = [Footprint(o["tri"]) for o in objects]
    m_all = printing & (cats != "tower")
    s = seg[m_all]
    pts, idx = sample(s)
    sel = np.nonzero(m_all)[0][idx]
    d_own = np.full(len(pts), np.inf)
    d_any = np.full(len(pts), np.inf)
    for k, fp in enumerate(fps):
        d = fp.distance(pts)
        d_any = np.minimum(d_any, d)
        own = obj_of_seg[sel] == k
        d_own[own] = d[own]
    # support: the nearest object (OrcaSlicer may print several objects' supports in one object's block)
    use = np.where((obj_of_seg[sel] >= 0) & (cats[sel] != "support"), d_own, d_any)
    lim = np.array([(per_obj[k] if k >= 0 else allow).get(c, allow["object"]) for k, c in zip(obj_of_seg[sel], cats[sel])])
    out = use > lim
    by_cat = {}
    for c in np.unique(cats[sel][out]) if out.any() else []:
        mm = out & (cats[sel] == c)
        fin = use[mm][np.isfinite(use[mm])]
        by_cat[str(c)] = {"points": int(mm.sum()), "max_distance_mm": round(float(fin.max()), 2) if len(fin) else
                          "more than 30 mm from every object"}
    # support spreading past the margin (tall tree supports lean out) is reported, not failed: it is no
    # placement error, and OrcaSlicer stops (-101) if it reaches another part
    sup_far = out & (cats[sel] == "support")
    out = out & (cats[sel] != "support")
    vol_out = float(vol[np.unique(sel[out])].sum()) if out.any() else 0.0
    tower = {}
    mt = printing & (cats == "tower")
    if mt.any():
        tp, _ = sample(seg[mt])
        dt = np.full(len(tp), np.inf)
        for fp in fps:
            dt = np.minimum(dt, fp.distance(tp))
        tower["min_distance_to_objects_mm"] = round(float(dt.min()), 2)
        tower["bbox"] = [round(float(v), 2) for v in (*tp.min(axis=0), *tp.max(axis=0))]
        okt = dt.min() > lw
        if tower_bbox:
            tb = np.array(tower_bbox, dtype=float)
            outside = (tp[:, 0] < tb[0] - 1) | (tp[:, 1] < tb[1] - 1) | (tp[:, 0] > tb[2] + 1) | (tp[:, 1] > tb[3] + 1)
            tower["outside_declared_bbox"] = int(outside.sum())
            tower["declared_bbox"] = list(tower_bbox)
            okt = okt and not outside.any()
        tower["passed"] = bool(okt)
    passed = (not out.any() or vol_out <= 0.5) and tower.get("passed", True)
    return _c(passed, "no extrusion outside its allowed region (object silhouette + half a line; + brim; "
                      "+ support margin; the prime tower apart from the objects)",
              got={"outside": {k: v for k, v in by_cat.items() if k != "support"}, "volume_outside_mm3": round(vol_out, 2),
                   "tower": tower or None,
                   **({"support_beyond_margin": by_cat.get("support")} if sup_far.any() else {})},
              expected={"allowance_mm": {k: round(v, 2) for k, v in allow.items()},
                        "brim_type": str(_first(pv("brim_type"), "auto_brim"))})


def _estimates(P: G.Parsed, fils: list[dict], diam: list[float]) -> tuple[dict, dict]:
    """OrcaSlicer's filament totals vs ours: extrusion (located or not) + firmware flushes ('VG1' in VFLUSH
    blocks). The totals must agree (0.5% in length, 2% in weight); OrcaSlicer splits flushes between the
    two filaments of a change differently, so the per-filament rows are for information."""
    ours = {}
    if len(P.segments):
        for t in np.unique(P.seg_info[:, 1]):
            ours[int(t)] = float(P.segments[P.seg_info[:, 1] == t, 5].sum())
    for src in (P.e_unlocated, P.e_flush):
        for t, v in src.items():
            if isinstance(t, (int, np.integer)):
                ours[int(t)] = ours.get(int(t), 0.0) + v
    foot_mm = P.footer.get("filament used [mm]", [])
    foot_g = P.footer.get("filament used [g]", [])
    rows = []
    g_ours_total = 0.0
    for i, mm in enumerate(foot_mm):
        o = ours.get(i, 0.0)
        dens = _f(fils[i].get("filament_density"), 1.24) if i < len(fils) else 1.24
        g_ours = o * G.filament_area(diam[i] if i < len(diam) else 1.75) * dens / 1000.0
        g_ours_total += g_ours
        rows.append({"filament": i + 1, "orca_mm": mm, "ours_mm": round(o, 2), "flush_mm": round(P.e_flush.get(i, 0.0), 2),
                     "orca_g": foot_g[i] if i < len(foot_g) else None, "ours_g": round(g_ours, 2)})
    tot_orca, tot_ours = sum(foot_mm), sum(ours.get(i, 0.0) for i in range(len(foot_mm)))
    g_orca = sum(foot_g) if foot_g else None
    ok = bool(rows) and ((tot_orca == 0 and tot_ours < 0.01) or (tot_orca > 0 and abs(tot_ours - tot_orca) / tot_orca <= 0.005))
    ok &= g_orca is None or abs(g_ours_total - g_orca) <= max(0.05, 0.02 * g_orca)
    est = {"time_s": P.header.get("total_time_s"), "model_time_s": P.header.get("model_time_s"),
           "filament_m": round(tot_orca / 1000.0, 3) if foot_mm else None,
           "filament_g": round(g_orca, 2) if g_orca is not None else None,
           "flush_m": round(sum(P.e_flush.values()) / 1000.0, 3),
           "by_filament": [{"n": r["filament"], "m": round(r["orca_mm"] / 1000.0, 3), "g": r["orca_g"]} for r in rows]}
    if est["time_s"] is not None:
        s = int(est["time_s"])
        est["time"] = f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"
    return _c(ok, "OrcaSlicer's filament totals equal ours, flushes included (0.5% length, 2% weight)", hard=False,
              got={"total_mm": [round(tot_orca, 2), round(tot_ours, 2)], "total_g": [g_orca, round(g_ours_total, 2)],
                   "by_filament": rows}), est


def support_params(pv, nozzle: float) -> dict:
    """The support check's inputs from the process settings (pv(key, default))."""
    def width(key):
        v = _first(pv(key), "")
        if str(v).endswith("%"):
            return _f(v) / 100 * nozzle
        return _f(v, 0.0)
    lw = max(width("outer_wall_line_width"), width("line_width")) or 1.05 * nozzle
    on = str(_first(pv("enable_support"), "0")).lower() not in ("0", "false")
    return {"line_width": lw, "layer_height": _f(pv("layer_height"), 0.2),
            "angle_deg": _f(pv("support_threshold_angle"), 30) or 30.0,
            "support_spacing": _f(pv("support_base_pattern_spacing"), 2.5), "supports": on}


def _support(P: G.Parsed, pv, mv, objects=None, obj_of_seg=None, opv=None) -> tuple[dict, dict]:
    """unsupported_extrusion (hard) and long_bridges (soft), object by object with each object's settings."""
    from . import supports as S
    nozzle = _f(mv("nozzle_diameter"), 0.4)
    sp = support_params(pv, nozzle)
    if objects and obj_of_seg is not None and len(obj_of_seg) == len(P.segments):
        groups = []
        for k, o in enumerate(objects):
            q = support_params(opv(k), nozzle)
            groups.append((o["name"], obj_of_seg == k, (q["line_width"], q["layer_height"], q["angle_deg"],
                                                        q["support_spacing"])))
        u = S.unsupported_groups(P, groups)
    else:
        u = S.unsupported(P, sp["line_width"], sp["layer_height"], sp["angle_deg"], sp["support_spacing"])
    problems = []
    for w in u["worst"]:
        if w["severe_mm"] <= 0:
            continue
        what = "starts in mid-air" if w["floating"] else \
            f"reaches {'more than ' if w['beyond_at_least'] else ''}{w['max_beyond_mm']} mm past what is below"
        problems.append(f"z {w['z_mm']} mm at {w['xy']} ({', '.join(w['objects'])}): {w['severe_mm']} mm of "
                        f"extrusion {what}")
    fix = "enable supports (intent.supports 'tree' or 'needed'), orient the part so it grows from what is below, " \
          "or accept it (accept: [{check: 'unsupported_extrusion', object, z_range, reason}])" \
        if not sp["supports"] else "supports do not reach it: lower support_angle, or orient the part differently"
    passed = u["severe_mm"] <= SEVERE_MAX
    got = {k: u[k] for k in ("reach_mm", "unsupported_mm", "severe_mm", "layers_affected", "worst") if k in u}
    if u.get("by_object"):
        got["by_object"] = {n: {kk: vv for kk, vv in v.items() if kk != "layers"} | {"severe_layers": v["layers"][:20]}
                            for n, v in u["by_object"].items() if v["unsupported_mm"] > 0}
    unsup = _c(passed, f"every extrusion of the parts rests on the layer below or on support (within reach "
                       f"{u['reach_mm']} mm = half a line + layer_height / tan(support_angle); at most "
                       f"{SEVERE_MAX:g} mm more than 1 mm beyond it)" + ("" if passed else f"; fix: {fix}"),
               got=got, expected={"severe_mm_max": SEVERE_MAX, "supports": sp["supports"]}, problems=problems)
    unsup["_findings"] = [{"object": n, "z": L[0], "severe_mm": L[1]} for n, v in (u.get("by_object") or {}).items()
                          for L in v["layers"]]
    lb = u["long_bridges"]
    bridges = _c(lb["longest_mm"] <= S.BRIDGE_MAX, f"bridges up to {S.BRIDGE_MAX:g} mm (longer ones sag)", hard=False,
                 got=lb, problems=[f"z {w['z_mm']} mm{' (' + w['object'] + ')' if w.get('object') else ''}: bridge of "
                                   f"{w['span_mm']} mm" for w in lb["worst"]])
    return unsup, bridges


def check_pauses(text: str, pauses: list[dict], pause_gcode: str) -> dict:
    """Each pause once, at the start of the first layer above its height (before that layer's first
    extrusion), followed by the machine's pause G-code."""
    want = sorted(p["z"] for p in pauses)
    cmds = [c.strip() for c in pause_gcode.strip().split("\n") if c.strip()]
    found, layer_z, extruded, lines = [], None, False, text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("; CHANGE_LAYER"):
            layer_z, extruded = None, False
        elif line.startswith("; Z_HEIGHT:") and layer_z is None:
            layer_z = float(line.split(":", 1)[1])
        elif line.startswith(("G1 ", "G2 ", "G3 ")) and " E" in line and layer_z is not None:
            try:
                extruded |= float(line.split(" E", 1)[1].split()[0]) > 0
            except ValueError:
                pass
        elif line.startswith("; PAUSE_PRINTING"):
            follow = [l_.strip() for l_ in lines[i + 1:i + 2 + len(cmds)]]
            found.append({"layer_z": layer_z, "before_extrusion": not extruded, "line": i + 1,
                          "gcode_follows": all(c in follow for c in cmds)})
    layers = sorted({float(l_.split(":", 1)[1]) for l_ in lines if l_.startswith("; Z_HEIGHT:")})
    expected = [next((z for z in layers if z > w + 1e-6), None) for w in want]
    ok = len(found) == len(want) and all(f["layer_z"] is not None and abs(f["layer_z"] - e) < 1e-4 and f["before_extrusion"]
                                         and f["gcode_follows"] for f, e in zip(found, expected))
    return _c(ok, f"each pause once, before the first extrusion of the first layer above its height, with the "
                  f"machine's pause G-code ({' / '.join(cmds)})", got=found,
              expected=[{"z": w, "layer_z": e} for w, e in zip(want, expected)])


def verdict(checks: dict) -> tuple[bool, list[str]]:
    """(reliable, unreliable_because) from a {name: check} dict."""
    bad = [f"{k}: {v.get('why', '')}" for k, v in checks.items() if v.get("hard", True) and not v.get("passed")]
    return not bad, bad
