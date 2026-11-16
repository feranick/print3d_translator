"""threemf.py — reads 3MF files: plain ones (core spec + production extension components) and Bambu Studio /
OrcaSlicer projects (Metadata/model_settings.config: object and part names, part types, filaments,
per-object settings, plates).
Version: 2026.11.16.2

What is read:
  meshes          every build item, its components (parts) with their transforms, in mm ('unit' converted)
  part types      Bambu/Orca 'subtype': only normal parts are solids; modifiers, negative parts, support
                  enforcers / blockers are listed and left out of the geometry
  filaments       the part's 'extruder', else its object's (1-based, as in the project's filament list)
  settings        per-object metadata that are OrcaSlicer process settings (layer_height, wall_loops,
                  enable_support, brim_type, ...): returned as 'settings'
  plates          plate number, name and the objects on it; plate-local positions (Bambu's plate grid)
  paint           per object: colour-painted triangles (MMU segmentation, 'paint_color' or PrusaSlicer's
                  'slic3rpe:mmu_segmentation'), the filaments the paint uses (decoded), painted supports and
                  seams. The paint itself is not geometry: format.expand decides to slice the 3MF directly
                  (paint 'keep', write_trimmed) or to drop it with a warning.
What is not read (reported in 'ignored'): variable layer height profiles (layer_heights_profile.txt),
height-range modifiers (layer_config_ranges.xml), per-part settings.

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


# ---------------------------------------------------------------- paint (TriangleSelector strings)

_PAINT = {"color": re.compile(rb'(?:paint_color|slic3rpe:mmu_segmentation)="([0-9A-Fa-f]+)"'),
          "supports": re.compile(rb'(?:paint_supports|slic3rpe:custom_supports)="([0-9A-Fa-f]+)"'),
          "seam": re.compile(rb'(?:paint_seam|slic3rpe:custom_seam)="([0-9A-Fa-f]+)"')}


def _nibbles(code: str) -> list[int]:
    """A painted triangle's string: hex digits read from the end, one 4-bit node each (PrusaSlicer's
    FacetsAnnotation::set_triangle_from_string)."""
    return [int(c, 16) for c in reversed(code)]


def paint_leaves(code: str) -> list[int]:
    """The states of a painted triangle's leaves. Each node: low 2 bits = number of split sides (0 = leaf);
    a leaf's high 2 bits are its state, 3 meaning 'the next nibble + 3'; a split node has split + 1 children.
    For colour paint, state n is filament n (1-based) and 0 the object's own filament."""
    nb, leaves, i, stack = _nibbles(code), [], 0, 1
    while stack and i < len(nb):
        c = nb[i]
        i += 1
        stack -= 1
        split = c & 3
        if split:
            stack += split + 1
        else:
            st = c >> 2
            if st == 3:
                st = nb[i] + 3
                i += 1
            leaves.append(st)
    return leaves


def paint_remap(code: str, mapping: dict) -> str:
    """The same painted triangle with leaf states renamed (mapping {old: new}); unchanged elsewhere, so
    an identity mapping returns the string as read (upper case)."""
    nb, out, i, stack = _nibbles(code), [], 0, 1
    while stack and i < len(nb):
        c = nb[i]
        i += 1
        stack -= 1
        split = c & 3
        if split:
            stack += split + 1
            out.append(c)
            continue
        st = c >> 2
        if st == 3:
            st = nb[i] + 3
            i += 1
        st = mapping.get(st, st)
        out.extend([st << 2] if st < 3 else [0b1100, st - 3])
    out.extend(nb[i:])
    return "".join("%X" % n for n in reversed(out))


def paint_stats(body: bytes) -> dict:
    """{color: painted triangles, supports, seam, states: {state: leaves}} of one mesh object's XML."""
    out = {k: 0 for k in _PAINT}
    states: dict[int, int] = {}
    for k, rx in _PAINT.items():
        codes = rx.findall(body)
        out[k] = len(codes)
        if k == "color":
            for c in codes:
                for st in paint_leaves(c.decode()):
                    states[st] = states.get(st, 0) + 1
    out["states"] = dict(sorted(states.items()))
    return out


def _add_paint(a: dict, b: dict) -> dict:
    out = {k: a.get(k, 0) + b.get(k, 0) for k in _PAINT}
    st = dict(a.get("states") or {})
    for k, v in (b.get("states") or {}).items():
        st[k] = st.get(k, 0) + v
    out["states"] = dict(sorted(st.items()))
    return out


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


def project_flush(path: str | Path) -> dict | None:
    """The project's purge settings (Bambu Studio / OrcaSlicer project_settings.config): {colors, matrix
    (effective mm3 = flush_volumes_matrix x flush_multiplier, rows = from), multiplier} or None."""
    import json
    try:
        with zipfile.ZipFile(path) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
    except (KeyError, ValueError, OSError, zipfile.BadZipFile):
        return None
    cols = cfg.get("filament_colour") or []
    m = cfg.get("flush_volumes_matrix") or []
    n = len(cols)
    if n < 2 or len(m) < n * n:
        return None
    try:
        mult = float((cfg.get("flush_multiplier") or ["1"])[0])
        vals = [float(v) for v in m[:n * n]]
    except (TypeError, ValueError):
        return None
    return {"colors": cols, "multiplier": mult,
            "matrix": [[round(vals[i * n + j] * mult, 1) for j in range(n)] for i in range(n)]}


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
            paint = {"color": 0, "supports": 0, "seam": 0, "states": {}}
            for p in parts:
                if p["filament"] is None:
                    p["filament"] = obj_fil
                paint = _add_paint(paint, p.get("paint") or {})
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
                        "settings": ms.get("settings", {}), "plate": pl, "parts": parts, "tri": tri,
                        "paint": paint, "plate_origin": origin})
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
                      "tri": _apply(tri, T), "paint": paint_stats(body) if b"paint_" in body or b"slic3rpe:" in body
                      else {"color": 0, "supports": 0, "seam": 0, "states": {}}, "file": key, "file_object": oid})
    for c in _COMP.findall(body):
        ca = _attrs(c)
        ckey = ca.get("p:path") or key
        cid = ca.get("objectid")
        _collect(models, ckey, cid, T @ _matrix(ca.get("transform")), parts, part_settings, False, depth + 1,
                 pid=cid if top else pid, meshes=meshes)


# ---------------------------------------------------------------- a trimmed copy to slice directly (paint 'keep')

DROP_FILES = ("Metadata/layer_heights_profile.txt", "Metadata/layer_config_ranges.xml", "Metadata/project_settings.config",
              "Metadata/slice_info.config", "Metadata/filament_sequence.json", "Metadata/cut_information.xml")


def write_trimmed(src: str | Path, dst: str | Path, plate: int | None, select: list[tuple[str, int]],
                  names: dict, fmap: dict | None = None, settings: str = "keep", project: dict | None = None) -> dict:
    """A copy of a Bambu Studio / OrcaSlicer project that OrcaSlicer's CLI slices as it is, paint included:
    only the selected build items (select [(object id, instance)], their plate moved to plate 1's place),
    one plate, each object renamed (names {object id: name}, the names the checks expect in the G-code).
    fmap {3MF filament: study filament}: object / part 'extruder' and the colour paint's filament states
    renamed. settings 'drop': the objects' and parts' own settings removed. project: the project settings
    written instead of the source's (the bed the plate was laid out for: without it OrcaSlicer assumes a
    200 x 200 bed and moves everything by the difference; the filament colours). Left out: other plates' objects
    and model files, the project's settings (the study's profiles are loaded by the CLI), variable layer
    heights, height-range modifiers, the slicer's cached results and pictures. Returns {objects, files,
    remapped_paint (triangles), left_out}."""
    src, dst = Path(src), Path(dst)
    fmap = {int(a): int(b) for a, b in (fmap or {}).items() if int(a) != int(b)}
    sel = {(str(o), int(i)) for o, i in select}
    sel_ids = {o for o, _ in sel}
    with zipfile.ZipFile(src) as z:
        names_in = z.namelist()
        models = _Models(z)
        main = z.read(models.names["/3D/3dmodel.model"])
        settings_xml, plates = _settings_xml(z)
        bed = _bed(z)
        origin = (0.0, 0.0)
        if plate is not None and bed and len(plates) > 1:
            k = plate - 1
            cols = _plate_cols(len(plates))
            origin = ((k % cols) * bed[0] * (1 + PLATE_GAP), -(k // cols) * bed[1] * (1 + PLATE_GAP))
        # build items: the selected instances, moved by the plate origin
        seen: dict[str, int] = {}

        def item(m):
            a = _attrs(m.group(1))
            oid = a.get("objectid")
            inst = seen.get(oid, 0)
            seen[oid] = inst + 1
            if (oid, inst) not in sel:
                return b""
            t = m.group(0)
            tr = a.get("transform")
            if tr and (origin[0] or origin[1]):
                v = [float(x) for x in tr.split()]
                v[9] -= origin[0]
                v[10] -= origin[1]
                t = t.replace(f'transform="{tr}"'.encode(), ('transform="' + " ".join(f"{x:.9g}" for x in v) + '"').encode())
            return t
        build = re.search(rb"(<build\b[^>]*>)(.*?)(</build>)", main, re.S)
        items = re.sub(rb"<item\b([^>]*)/?>", item, build.group(2))
        main = main[:build.start(2)] + items + main[build.end(2):]
        # top-level objects: only the selected ones; the model files their components use
        files = set()

        def obj(m):
            a = _attrs(m.group(1))
            if a.get("id") not in sel_ids:
                return b""
            for c in _COMP.findall(m.group(2)):
                ca = _attrs(c)
                files.add(ca.get("p:path") or "/3D/3dmodel.model")
            return m.group(0)
        res = re.search(rb"(<resources>)(.*?)(</resources>)", main, re.S)
        kept = _OBJ.sub(obj, res.group(2))
        main = main[:res.start(2)] + kept + main[res.end(2):]
        files.discard("/3D/3dmodel.model")
        # model_settings.config
        root = ET.fromstring(z.read("Metadata/model_settings.config"))
        for o in list(root.findall("object")):
            if o.get("id") not in sel_ids:
                root.remove(o)
                continue
            for md in o.findall("metadata"):
                k = md.get("key")
                if k == "name":
                    md.set("value", names.get(o.get("id"), md.get("value")))
                elif k == "extruder" and md.get("value", "").isdigit():
                    md.set("value", str(fmap.get(int(md.get("value")), int(md.get("value")))))
                elif settings == "drop" and k and k not in NOT_SETTINGS:
                    o.remove(md)
            for p in o.findall("part"):
                for md in p.findall("metadata"):
                    k = md.get("key")
                    if k == "extruder" and md.get("value", "").isdigit():
                        md.set("value", str(fmap.get(int(md.get("value")), int(md.get("value")))))
                    elif settings == "drop" and k and k not in NOT_SETTINGS:
                        p.remove(md)
        for pl in list(root.findall("plate")):
            md = {m.get("key"): m for m in pl.findall("metadata")}
            pid = int(md["plater_id"].get("value")) if "plater_id" in md else None
            if plates and len(plates) > 1 and plate is not None and pid != plate:
                root.remove(pl)
                continue
            if "plater_id" in md:
                md["plater_id"].set("value", "1")
            for k in ("thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file", "pattern_bbox_file"):
                if k in md:
                    pl.remove(md[k])
            for mi in list(pl.findall("model_instance")):
                mm = {m.get("key"): m.get("value") for m in mi.findall("metadata")}
                if (mm.get("object_id"), int(mm.get("instance_id") or 0)) not in sel:
                    pl.remove(mi)
        for asm in root.findall("assemble"):
            for it in list(asm):
                if (it.get("object_id"), int(it.get("instance_id") or 0)) not in sel:
                    asm.remove(it)
        ms = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        # write
        remapped = 0
        out_names = []
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
            if project is not None:
                import json
                zo.writestr("Metadata/project_settings.config", json.dumps(project, indent=4))
                out_names.append("Metadata/project_settings.config")
            for n in names_in:
                key = "/" + n.lstrip("/")
                if n in DROP_FILES or n.startswith("Auxiliaries/") or re.match(r"Metadata/(plate|top|pick)_.*", n):
                    continue
                if key.lower().endswith(".model") and key != "/3D/3dmodel.model" and key not in files:
                    continue
                if key == "/3D/3dmodel.model":
                    data = main
                elif n == "Metadata/model_settings.config":
                    data = ms
                elif n == "3D/_rels/3dmodel.model.rels":
                    data = re.sub(rb'<Relationship\b[^>]*Target="([^"]+)"[^>]*/>',
                                  lambda m: m.group(0) if m.group(1).decode() in files else b"", z.read(n))
                elif key in files and fmap:
                    data = z.read(n)
                    cnt = [0]

                    def rp(m):
                        cnt[0] += 1
                        return m.group(1) + paint_remap(m.group(2).decode(), fmap).encode() + b'"'
                    data = re.sub(rb'((?:paint_color|slic3rpe:mmu_segmentation)=")([0-9A-Fa-f]+)"', rp, data)
                    remapped += cnt[0]
                else:
                    data = z.read(n)
                zo.writestr(n, data)
                out_names.append(n)
    return {"objects": sorted(sel), "files": out_names, "remapped_paint": remapped, "origin": origin,
            "left_out": [n for n in names_in if n not in out_names]}
