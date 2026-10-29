"""geometry.py — the parts: load them (STEP, STL, 3MF, a build123d-translator goal or result), orient
them, and check them before slicing (fit, closed mesh, bed contact, unsupported regions, thin walls).
Version: 2026.10.29.1

Units mm, Z up. A loaded part is a dict:
  tri          (n, 3, 3) triangles, oriented, base on z = 0, centred on x = y = 0
  volume       mm3: exact from STEP/b3dt (OpenCascade), else the mesh's
  volume_from  'step' | 'b3dt' | 'mesh'
  area         mm2 (mesh)
  rotation_deg [rx, ry, rz] applied (X, then Y, then Z), or the 'flat' rotation as a matrix too
  closed       {closed, boundary_edges, nonmanifold_edges, inverted}
STEP and goals need build123d (pip install "print3d-translator[cad]"); goals also need
build123d-translator.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

from . import mesh as M

STEP_TOL = 0.01          # mm, tessellation of STEP/b3dt parts
STEP_ANG = 0.1           # rad


class GeometryError(ValueError):
    """A part could not be loaded; the message says what to change."""


# ---------------------------------------------------------------- loading

def load_part(part: dict, base_dir: str | Path | None = None) -> dict:
    """{'step'|'stl'|'3mf': path} | {'goal': b3dt goal} | {'b3dt': {'step', 'volume'}} -> triangles, volume."""
    def path_of(key):
        p = Path(str(part[key])).expanduser()
        if not p.is_absolute() and base_dir:
            p = Path(base_dir) / p
        if not p.is_file():
            raise GeometryError(f"part.{key}: file {p} not found")
        return p

    if "stl" in part:
        tri = M.read_stl(path_of("stl"))
        return {"tri": tri, "volume": abs(M.volume(tri)), "volume_from": "mesh"}
    if "3mf" in part:
        tri = read_3mf(path_of("3mf"))
        return {"tri": tri, "volume": abs(M.volume(tri)), "volume_from": "mesh"}
    if "step" in part:
        solid = _import_step(path_of("step"))
        return {**_from_solid(solid), "volume_from": "step"}
    if "b3dt" in part:
        r = part["b3dt"]
        if not isinstance(r, dict) or "step" not in r:
            raise GeometryError("part.b3dt: give the build123d-translator result's {'step': path, 'volume': mm3}")
        p = Path(str(r["step"])).expanduser()
        if not p.is_file():
            raise GeometryError(f"part.b3dt.step: file {p} not found (if b3dt ran on another machine, fetch it "
                                f"with its get_file tool first)")
        out = {**_from_solid(_import_step(p)), "volume_from": "b3dt"}
        if r.get("volume") is not None:
            v = float(r["volume"])
            if abs(v - out["volume"]) > 1e-3 * max(1.0, v):
                raise GeometryError(f"part.b3dt: volume {v} mm3 does not match the STEP's {out['volume']:.3f} mm3; "
                                    f"is it the same part?")
            out["volume"] = v
        return out
    if "goal" in part:
        try:
            from build123d_translator import translate as T
        except ImportError as exc:
            raise GeometryError("part.goal: build123d-translator is not installed here; give its result "
                                "({'b3dt': {'step', 'volume'}}) or a STEP file") from exc
        solid, _ = T.build(part["goal"])
        return {**_from_solid(solid), "volume_from": "b3dt"}
    raise GeometryError("part: give one of {'step': path}, {'stl': path}, {'3mf': path}, {'goal': b3dt goal}, "
                        "{'b3dt': {'step': path, 'volume': mm3}}")


def _import_step(p: Path):
    try:
        from build123d import import_step
    except ImportError as exc:
        raise GeometryError("STEP input needs build123d: pip install \"print3d-translator[cad]\" "
                            "(or give an STL/3MF)") from exc
    try:
        return import_step(str(p))
    except Exception as exc:                        # OpenCascade raises many kinds
        raise GeometryError(f"part.step: {p} could not be read ({exc})") from exc


def _from_solid(solid) -> dict:
    verts, faces = solid.tessellate(STEP_TOL, STEP_ANG)
    v = np.array([[p.X, p.Y, p.Z] for p in verts], dtype=np.float64)
    f = np.array(faces, dtype=np.int64)
    if not len(f):
        raise GeometryError("the part has no faces (empty STEP?)")
    return {"tri": v[f], "volume": float(solid.volume)}


_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
_P = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"


def _matrix(s: str | None) -> np.ndarray:
    """3MF 'm00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32' (row-vector convention: [x y z 1] . M)
    -> 4x4 column-vector matrix."""
    T = np.eye(4)
    if s:
        a = [float(x) for x in s.split()]
        if len(a) != 12:
            raise GeometryError(f"3MF transform '{s}': needs 12 numbers")
        T[:3, :3] = np.array(a[:9]).reshape(3, 3).T
        T[:3, 3] = a[9:12]
    return T


def read_3mf(path: str | Path) -> np.ndarray:
    """All build items of a 3MF as one triangle array (core spec + production extension components;
    units: the model's 'unit', converted to mm)."""
    scale = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
    with zipfile.ZipFile(path) as z:
        models = {}
        for n in z.namelist():
            if n.lower().endswith(".model"):
                models["/" + n.lstrip("/")] = ET.fromstring(z.read(n))
    main = models.get("/3D/3dmodel.model")
    if main is None:
        raise GeometryError(f"{path}: not a 3MF (no 3D/3dmodel.model)")
    unit = scale.get(main.get("unit", "millimeter"), 1.0)

    def objects(root):
        return {o.get("id"): o for o in root.iter(f"{{{_NS['m']}}}object")}

    objs = {k: objects(r) for k, r in models.items()}

    def tris_of(model_key, oid, T, depth=0):
        if depth > 10:
            raise GeometryError(f"{path}: components nested too deep")
        o = objs[model_key].get(oid)
        if o is None:
            raise GeometryError(f"{path}: object {oid} not found in {model_key}")
        out = []
        m = o.find("m:mesh", _NS)
        if m is not None:
            v = np.array([[float(e.get("x")), float(e.get("y")), float(e.get("z"))]
                          for e in m.find("m:vertices", _NS)], dtype=np.float64)
            t = np.array([[int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))]
                          for e in m.find("m:triangles", _NS)], dtype=np.int64)
            if len(t):
                vh = np.c_[v, np.ones(len(v))] @ T.T
                out.append(vh[:, :3][t])
        comps = o.find("m:components", _NS)
        if comps is not None:
            for c in comps:
                key = c.get(_P) or model_key
                out += tris_of(key, c.get("objectid"), T @ _matrix(c.get("transform")), depth + 1)
        return out

    parts = []
    build = main.find("m:build", _NS)
    for item in (build if build is not None else []):
        key = item.get(_P) or "/3D/3dmodel.model"
        parts += tris_of(key, item.get("objectid"), _matrix(item.get("transform")))
    if not parts:
        raise GeometryError(f"{path}: the 3MF has no build items with triangles")
    return np.concatenate(parts) * unit


# ---------------------------------------------------------------- mesh quality

def closedness(tri: np.ndarray, tol: float = 1e-4) -> dict:
    """Edges shared by exactly two triangles in opposite directions = closed and consistently oriented."""
    pts = np.round(tri.reshape(-1, 3) / tol).astype(np.int64)
    _, inv = np.unique(pts, axis=0, return_inverse=True)
    f = inv.reshape(-1, 3)
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    und = np.sort(e, axis=1)
    keys, counts = np.unique(und, axis=0, return_counts=True)
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    _, dcounts = np.unique(e, axis=0, return_counts=True)
    flipped = int((dcounts > 1).sum())
    vol = M.volume(tri)
    return {"closed": boundary == 0 and nonmanifold == 0 and flipped == 0, "boundary_edges": boundary,
            "nonmanifold_edges": nonmanifold, "inconsistent_edges": flipped, "inverted": vol < 0}


# ---------------------------------------------------------------- orientation

def orient(tri: np.ndarray, how) -> tuple[np.ndarray, list | None, np.ndarray]:
    """'as_is' | 'flat' (the largest flat area down) | {'rotate': [rx, ry, rz]} -> (triangles on z = 0,
    centred at x = y = 0; the rotation in degrees when given; the 3x3 matrix applied)."""
    R = np.eye(3)
    rot = None
    if how in (None, "as_is"):
        pass
    elif how == "flat":
        R = flat_rotation(tri)
    elif isinstance(how, dict) and "rotate" in how:
        rot = [float(a) for a in how["rotate"]]
        R = M.rotation(*rot)
    else:
        raise GeometryError(f"orient {how!r}: use 'as_is', 'flat' or {{'rotate': [rx, ry, rz]}} (degrees)")
    out = M.transform(tri, R)
    return M.place(out, (0.0, 0.0)), rot, R


def flat_rotation(tri: np.ndarray, angle_tol_deg: float = 1.0) -> np.ndarray:
    """The rotation that puts the largest set of coplanar, same-facing triangles down (-Z). Ties: the
    lower resulting height, then the normal's order (deterministic)."""
    n = M.normals(tri)
    a = np.linalg.norm(M.normals(tri, unit=False), axis=1) / 2
    ok = a > 1e-12
    q = np.round(n[ok] / math.radians(angle_tol_deg)).astype(np.int64)
    d = np.round(np.einsum("ij,ij->i", n[ok], tri[ok, 0]) / 0.05).astype(np.int64)
    key = np.c_[q, d]
    uk, inv = np.unique(key, axis=0, return_inverse=True)
    area = np.bincount(inv.reshape(-1), weights=a[ok])
    best, best_key = None, None
    for k in np.argsort(-area)[:8]:
        if area[k] < 0.98 * area.max():
            break
        m = inv.reshape(-1) == k
        normal = (n[ok][m] * a[ok][m, None]).sum(axis=0)
        normal /= np.linalg.norm(normal)
        R = _align(normal, np.array([0.0, 0.0, -1.0]))
        h = float(np.ptp((tri.reshape(-1, 3) @ R.T)[:, 2]))
        cand = (-round(area[k], 3), round(h, 4), tuple(np.round(normal, 6)))
        if best_key is None or cand < best_key:
            best, best_key = R, cand
    return best if best is not None else np.eye(3)


def _align(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation taking unit vector a onto b (Rodrigues)."""
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
        if c > 0:
            return np.eye(3)
        axis = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = axis - a * np.dot(axis, a)
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


# ---------------------------------------------------------------- checks before slicing

def _c(passed: bool, why: str, hard: bool = False, **kw) -> dict:
    return {"passed": bool(passed), "hard": hard, "why": why, **kw}


def bed_contact(tri: np.ndarray, tol: float = 0.05) -> float:
    """Area (mm2) of downward faces lying on the bed (z within tol of the lowest point)."""
    n = M.normals(tri)
    a = np.linalg.norm(M.normals(tri, unit=False), axis=1) / 2
    zmin = tri[:, :, 2].min()
    on = (n[:, 2] < -0.999) & (tri[:, :, 2].max(axis=1) <= zmin + tol)
    return float(a[on].sum())


def overhang_area(tri: np.ndarray, threshold_deg: float, tol: float = 0.05) -> float:
    """Area (mm2) of downward faces flatter than threshold_deg from horizontal, not on the bed
    (OrcaSlicer's support_threshold_angle: overhangs below this angle get support)."""
    n = M.normals(tri)
    a = np.linalg.norm(M.normals(tri, unit=False), axis=1) / 2
    zmin = tri[:, :, 2].min()
    steep = -n[:, 2] > math.cos(math.radians(threshold_deg))
    off_bed = tri[:, :, 2].max(axis=1) > zmin + tol
    return float(a[steep & off_bed].sum())


def silhouette_area(tri: np.ndarray, res: float = 0.25) -> float:
    from .check import Footprint
    fp = Footprint(tri, res=res, pad=1.0)
    return float(fp.mask.sum() * res * res)


def thin_walls(tri: np.ndarray, limit: float, samples: int = 1500, seed: int = 0) -> dict:
    """Wall thickness by casting a ray inward from points spread over the surface (area-weighted,
    fixed seed); reports the minimum and the area fraction thinner than `limit`."""
    rng = np.random.default_rng(seed)
    a = np.linalg.norm(M.normals(tri, unit=False), axis=1) / 2
    keep = a > 1e-12
    T = tri[keep]
    a = a[keep]
    n = M.normals(T)
    budget = max(200, min(samples, int(2e7 / max(1, len(T)))))
    idx = rng.choice(len(T), size=budget, p=a / a.sum())
    r1, r2 = rng.random(budget), rng.random(budget)
    s = np.sqrt(r1)
    P = T[idx, 0] * (1 - s)[:, None] + T[idx, 1] * (s * (1 - r2))[:, None] + T[idx, 2] * (s * r2)[:, None]
    D = -n[idx]
    O = P + D * 1e-4
    dist = np.full(budget, np.inf)
    e1, e2 = T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
    chunk = max(1, int(4e6 / max(1, len(T))))
    for i in range(0, budget, chunk):
        o, d = O[i:i + chunk, None, :], D[i:i + chunk, None, :]
        h = np.cross(d, e2[None])
        det = np.einsum("ijk,jk->ij", h, e1)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / det
            sv = o - T[None, :, 0]
            u = np.einsum("ijk,ijk->ij", sv, h) * inv
            qv = np.cross(sv, e1[None])
            v = np.einsum("ijk,ijk->ij", d, qv) * inv
            t = np.einsum("jk,ijk->ij", e2, qv) * inv
            hit = (np.abs(det) > 1e-12) & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6)
            t = np.where(hit, t, np.inf)
        dist[i:i + chunk] = t.min(axis=1)
    finite = dist[np.isfinite(dist)]
    thin = finite < limit
    return {"min_mm": round(float(finite.min()), 3) if len(finite) else None,
            "thin_fraction": round(float(thin.sum() / max(1, len(dist))), 4),
            "thin_area_mm2": round(float(thin.sum() / max(1, len(dist)) * a.sum()), 1), "samples": int(budget)}


def checks(part: dict, bed: np.ndarray, height: float, nozzle: float, support_angle: float, supports: str,
           area_name: str = "printable area", layer_height: float = 0.2, first_layer: float = 0.2,
           line_width: float | None = None) -> dict:
    """Checks before slicing for one oriented part (base on z = 0). bed: the (n, 2) area its extruder
    can print (rectangle). Hard: fits, closed. Soft: bed_contact, unsupported_regions, thin_walls.
    (The G-code check unsupported_extrusion decides; unsupported_regions warns before slicing.)"""
    tri = part["tri"]
    lo, hi = M.bbox(tri)
    size = hi - lo
    blo, bhi = bed.min(axis=0), bed.max(axis=0)
    room = bhi - blo
    fits = size[0] <= room[0] + 1e-6 and size[1] <= room[1] + 1e-6 and size[2] <= height + 1e-6
    out = {"fits": _c(fits, f"the part fits the {area_name} and the printable height", hard=True,
                      got=[round(float(v), 2) for v in size], expected=[round(float(room[0]), 2), round(float(room[1]), 2), height])}
    cl = closedness(tri)
    out["closed_mesh"] = _c(cl["closed"] and not cl["inverted"], "a closed, consistently oriented mesh (else the "
                            "slicer guesses what is inside)", hard=part.get("volume_from") == "mesh", got=cl)
    contact = bed_contact(tri)
    sil = silhouette_area(tri)
    slender = float(size[2]) / math.sqrt(max(contact, 1e-9))
    ok_contact = contact >= max(20.0, 0.05 * sil) and slender <= 10
    out["bed_contact"] = _c(ok_contact, "enough flat area on the bed (>= 20 mm2 and 5% of the footprint; height / "
                                        "sqrt(contact) <= 10); else use a brim, or another orientation",
                            got={"contact_mm2": round(contact, 1), "footprint_mm2": round(sil, 1),
                                 "height_over_sqrt_contact": round(slender, 2)})
    th = support_angle if support_angle and support_angle > 0 else 30.0
    ov = overhang_area(tri, th)
    from . import supports as S
    reg = S.regions(tri, first_layer, layer_height, line_width or 1.05 * nozzle, th)
    ok_reg, problems = S.verdict(reg, supports)
    out["unsupported_regions"] = _c(
        ok_reg, "nothing starts in mid-air (island) or reaches more than 1 mm past the layer below on one side "
                "(cantilever) without supports; fix: intent.supports 'tree', or another orientation",
        got={"supports": supports, "reach_mm": reg["reach_mm"], "grid_mm": reg["grid_mm"],
             "regions": reg["regions"][:5], "overhang_area_mm2": round(ov, 1)},
        problems=problems[:5])
    lw = 1.125 * nozzle
    tw = thin_walls(tri, lw)
    out["thin_walls"] = _c(tw["thin_fraction"] <= 0.01, f"walls at least one line ({lw:.2f} mm) thick; thinner "
                                                        f"ones may vanish or print weak", got=tw)
    return out
