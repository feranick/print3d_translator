"""threemf.py — reads 3MF files: plain ones (core spec + production extension components) and Bambu Studio /
OrcaSlicer projects (Metadata/model_settings.config: object and part names, part types, filaments,
per-object settings, plates).
Version: 2026.11.11.1

What is read:
  meshes          every build item, its components (parts) with their transforms, in mm ('unit' converted)
  part types      Bambu/Orca 'subtype': only normal parts are solids; modifiers, negative parts, support
                  enforcers / blockers are listed and left out of the geometry
  filaments       the part's 'extruder', else its object's (1-based, as in the project's filament list)
  settings        per-object metadata that are OrcaSlicer process settings (layer_height, wall_loops,
                  enable_support, brim_type, ...): returned as 'settings'
  plates          plate number, name and the objects on it; plate-local positions (Bambu's plate grid)
What is not read (reported in 'ignored'): variable layer height profiles (layer_heights_profile.txt),
height-range modifiers (layer_config_ranges.xml), painted supports / seams / colours, per-part settings.

Meshes are parsed with regular expressions (vertex x y z and triangle v1 v2 v3 in that order, as every
slicer writes them; an object that does not match falls back to the XML parser): about 1 s per 10 MB.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
from itertools import chain
from pathlib import Path

import numpy as np

UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
P_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
SOLID = "normal_part"
NOT_SETTINGS = {"name", "extruder", "matrix", "source_file", "source_object_id", "source_volume_id",
                "source_offset_x", "source_offset_y", "source_offset_z", "mesh_stat", "face_count"}
PLATE_GAP = 0.2         # Bambu Studio lays plates out in a grid, 1.2 x the bed apart

_OBJ = re.compile(rb"<object\b([^>]*)>(.*?)</object>", re.S)
_ATTR = re.compile(rb'([\w:]+)="([^"]*)"')
_VERT = re.compile(rb'<vertex\s+x="([^"]+)"\s+y="([^"]+)"\s+z="([^"]+)"')
_TRI = re.compile(rb'<triangle\s+v1="(\d+)"\s+v2="(\d+)"\s+v3="(\d+)"')
_COMP = re.compile(rb"<component\b([^>]*)/?>")
_VERT_ANY = re.compile(rb"<vertex\s")
_TRI_ANY = re.compile(rb"<triangle\s")


class ThreeMFError(ValueError):
    pass


def _matrix(s: str | None) -> np.ndarray:
    T = np.eye(4)
    if s:
        v = [float(x) for x in s.split()]
        if len(v) == 12:
            T[:3, :3] = np.array(v[:9]).reshape(3, 3).T
            T[:3, 3] = v[9:12]
    return T


def _attrs(b: bytes) -> dict:
    return {k.decode(): v.decode("utf-8", "replace") for k, v in _ATTR.findall(b)}


def _numbers(groups, dtype) -> np.ndarray:
    flat = b" ".join(chain.from_iterable(groups))
    return np.array(flat.split(), dtype=dtype) if flat else np.zeros(0, dtype=dtype)


class _Models:
    """The .model files of the archive, parsed lazily: objects by (file, id) -> (attrs, body)."""

    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.names = {"/" + n.lstrip("/"): n for n in z.namelist() if n.lower().endswith(".model")}
        self.cache: dict[str, dict] = {}

    def objects(self, key: str) -> dict:
        if key not in self.cache:
            name = self.names.get(key)
            if name is None:
                raise ThreeMFError(f"model file {key} not found in the 3MF")
            data = self.z.read(name)
            self.cache[key] = {_attrs(a).get("id"): (a, body) for a, body in _OBJ.findall(data)}
        return self.cache[key]

    def mesh(self, key: str, oid: str) -> np.ndarray | None:
        """(n, 3, 3) triangles of object oid's own mesh, or None."""
        a, body = self.objects(key)[oid]
        if b"<mesh" not in body:
            return None
        V = _VERT.findall(body)
        T = _TRI.findall(body)
        if len(V) != len(_VERT_ANY.findall(body)) or len(T) != len(_TRI_ANY.findall(body)):
            return _mesh_xml(body)
        v = _numbers(V, np.float64).reshape(-1, 3)
        t = _numbers(T, np.int64).reshape(-1, 3)
        return v[t] if len(t) else np.zeros((0, 3, 3))


def _mesh_xml(body: bytes) -> np.ndarray:
    root = ET.fromstring(b'<object xmlns="' + NS["m"].encode() + b'">' + body + b"</object>")
    m = root.find("m:mesh", NS)
    v = np.array([[float(e.get("x")), float(e.get("y")), float(e.get("z"))] for e in m.find("m:vertices", NS)])
    t = np.array([[int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))] for e in m.find("m:triangles", NS)],
                 dtype=np.int64)
    return v[t] if len(t) else np.zeros((0, 3, 3))


def _apply(tri: np.ndarray, T: np.ndarray) -> np.ndarray:
    if not len(tri):
        return tri
    return tri @ T[:3, :3].T + T[:3, 3]


def _settings_xml(z: zipfile.ZipFile) -> tuple[dict, list]:
    """model_settings.config -> ({object id: {name, extruder, settings, parts: {part id: {...}}}}, plates)."""
    try:
        root = ET.fromstring(z.read("Metadata/model_settings.config"))
    except KeyError:
        return {}, []
    objs = {}
    for o in root.findall("object"):
        md = {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")}
        parts = {}
        for p in o.findall("part"):
            pm = {m.get("key"): m.get("value") for m in p.findall("metadata") if m.get("key")}
            parts[p.get("id")] = {"name": pm.get("name"), "subtype": p.get("subtype", SOLID),
                                  "extruder": int(pm["extruder"]) if pm.get("extruder", "").isdigit() else None,
                                  "settings": {k: v for k, v in pm.items() if k not in NOT_SETTINGS}}
        objs[o.get("id")] = {"name": md.get("name"),
                             "extruder": int(md["extruder"]) if md.get("extruder", "").isdigit() else None,
                             "settings": {k: v for k, v in md.items() if k not in NOT_SETTINGS}, "parts": parts}
    plates = []
    for pl in root.findall("plate"):
        md = {m.get("key"): m.get("value") for m in pl.findall("metadata") if m.get("key")}
        ids = []
        for mi in pl.findall("model_instance"):
            mm = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
            ids.append((mm.get("object_id"), int(mm.get("instance_id") or 0)))
        plates.append({"plate": int(md.get("plater_id") or len(plates) + 1), "name": md.get("plater_name") or "",
                       "instances": ids})
    return objs, plates


def _bed(z: zipfile.ZipFile) -> tuple[float, float] | None:
    import json
    try:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    except (KeyError, ValueError):
        return None
    pts = cfg.get("printable_area")
    try:
        xy = np.array([[float(a) for a in p.split("x")] for p in pts])
        return float(np.ptp(xy[:, 0])), float(np.ptp(xy[:, 1]))
    except (TypeError, ValueError, AttributeError):
        return None


def summary(path: str | Path) -> dict:
    """Without meshes: plates, objects (names, filaments, settings, part types) and what is ignored."""
    return read(path, meshes=False)


_CACHE: dict = {}
_CACHE_MAX = 4


def read(path: str | Path, plate: int | None = None, names: list[str] | None = None, meshes: bool = True) -> dict:
    """Cached by (path, size, mtime, plate, names, meshes): a study's objects from one 3MF read it once.
    Callers must not modify the arrays."""
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_size, st.st_mtime_ns, plate, tuple(names or ()), meshes)
    except OSError:
        key = None
    if key is not None and key in _CACHE:
        return _CACHE[key]
    out = _read(p, plate, names, meshes)
    if key is not None:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = out
    return out


def _read(path: str | Path, plate: int | None = None, names: list[str] | None = None, meshes: bool = True) -> dict:
    """{'objects': [{id, instance, name, filament, settings, plate, parts: [{id, name, subtype, filament, tri}],
    tri (normal parts, in plate coordinates when the plate is known), plate_xy}], 'plates', 'bed', 'ignored',
    'unit'}. plate: only that plate's objects; names: only objects with these names (or 'o<id>')."""
    path = Path(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ThreeMFError(f"{path}: not a 3MF ({e})") from e
    with z:
        models = _Models(z)
        if "/3D/3dmodel.model" not in models.names:
            raise ThreeMFError(f"{path}: not a 3MF (no 3D/3dmodel.model)")
        main = z.read(models.names["/3D/3dmodel.model"])
        head = re.search(rb"<model\b([^>]*)>", main)
        unit = UNITS.get(_attrs(head.group(1)).get("unit", "millimeter") if head else "millimeter", 1.0)
        settings, plates = _settings_xml(z)
        bed = _bed(z)
        ignored = []
        for f, what in (("Metadata/layer_heights_profile.txt", "variable layer height profiles"),
                        ("Metadata/layer_config_ranges.xml", "height-range modifiers")):
            if f in z.namelist():
                ignored.append(what)
        build = re.search(rb"<build\b[^>]*>(.*?)</build>", main, re.S)
        items = [_attrs(a) for a in re.findall(rb"<item\b([^>]*)/?>", build.group(1))] if build else []
        if not items:
            raise ThreeMFError(f"{path}: the 3MF has no build items")
        on_plate = {}
        for pl in plates:
            for oid, inst in pl["instances"]:
                on_plate[(oid, inst)] = pl["plate"]
        if plate is not None and plates and plate not in {p["plate"] for p in plates}:
            raise ThreeMFError(f"{path}: no plate {plate}; plates {[p['plate'] for p in plates]}")
        n_plates = max(1, len(plates))
        cols = _plate_cols(n_plates)
        seen: dict[str, int] = {}
        out = []
        for it in items:
            oid = it.get("objectid")
            inst = seen.get(oid, 0)
            seen[oid] = inst + 1
            ms = settings.get(oid, {})
            name = ms.get("name") or f"o{oid}"
            pl = on_plate.get((oid, inst))
            if plate is not None and plates and pl != plate:
                continue
            if names and name not in names and f"o{oid}" not in names:
                continue
            T = _matrix(it.get("transform"))
            key = it.get(P_PATH) or "/3D/3dmodel.model"
            parts = []
            _collect(models, key, oid, T, parts, ms.get("parts", {}), top=True, meshes=meshes)
            obj_fil = ms.get("extruder")
            for p in parts:
                if p["filament"] is None:
                    p["filament"] = obj_fil
            solid = [p for p in parts if p["subtype"] == SOLID]
            tri = np.concatenate([p["tri"] for p in solid]) * unit if meshes and solid else np.zeros((0, 3, 3))
            for p in parts:
                if meshes:
                    p["tri"] = p["tri"] * unit
                else:
                    p.pop("tri", None)
            origin = None
            if pl is not None and bed and len(plates) > 1:
                k = pl - 1
                origin = ((k % cols) * bed[0] * (1 + PLATE_GAP), -(k // cols) * bed[1] * (1 + PLATE_GAP))
            if origin and meshes and len(tri):
                shift = np.array([origin[0], origin[1], 0.0])
                tri = tri - shift
                for p in parts:
                    p["tri"] = p["tri"] - shift
            out.append({"id": oid, "instance": inst, "name": name, "filament": obj_fil,
                        "settings": ms.get("settings", {}), "plate": pl, "parts": parts, "tri": tri})
        if not out:
            raise ThreeMFError(f"{path}: nothing selected (plate {plate}, objects {names}); "
                               f"objects: {[o.get('name') for o in settings.values()][:40]}")
        return {"objects": out, "plates": [{"plate": p["plate"], "name": p["name"],
                                            "objects": len(p["instances"])} for p in plates],
                "bed": bed, "ignored": ignored, "unit": unit}


def _plate_cols(n: int) -> int:
    """Bambu Studio's plate grid: columns = round(sqrt(n)), one more if sqrt(n) is above it."""
    v = math.sqrt(n)
    r = round(v)
    return int(r + 1 if v > r else r)


def _collect(models: _Models, key: str, oid: str, T: np.ndarray, parts: list, part_settings: dict,
             top: bool, depth: int = 0, pid: str | None = None, meshes: bool = True) -> None:
    if depth > 10:
        raise ThreeMFError("components nested too deep")
    objs = models.objects(key)
    if oid not in objs:
        raise ThreeMFError(f"object {oid} not found in {key}")
    a, body = objs[oid]
    tri = models.mesh(key, oid) if meshes else (np.zeros((1, 3, 3)) if b"<mesh" in body else None)
    if tri is not None and len(tri):
        ps = part_settings.get(pid or oid, {})
        parts.append({"id": pid or oid, "name": ps.get("name") or f"part{len(parts) + 1}",
                      "subtype": ps.get("subtype", SOLID), "filament": ps.get("extruder"),
                      "tri": _apply(tri, T)})
    for c in _COMP.findall(body):
        ca = _attrs(c)
        ckey = ca.get("p:path") or key
        cid = ca.get("objectid")
        _collect(models, ckey, cid, T @ _matrix(ca.get("transform")), parts, part_settings, False, depth + 1,
                 pid=cid if top else pid, meshes=meshes)
