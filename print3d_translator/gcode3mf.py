"""gcode3mf.py — the Bambu .gcode.3mf archive: read it, check its structure, compare the settings it
was sliced with against ours, and fill in what OrcaSlicer's CLI leaves out.
Version: 2026.10.24.9

Layout written by OrcaSlicer 2.4.2 (--export-3mf after --slice), as seen on the P2S and H2D:
  [Content_Types].xml, _rels/.rels, 3D/3dmodel.model (+ 3D/Objects/*.model without --min-save),
  Metadata/plate_<n>.gcode, plate_<n>.gcode.md5 (upper-case hex MD5 of the G-code bytes),
  slice_info.config (XML: plate metadata, objects, filaments with used_m/used_g, warnings, nozzles),
  model_settings.config (XML: objects with their 'extruder' = filament number, plate filament_maps),
  project_settings.config (JSON: every setting the plate was sliced with), plate_<n>.json,
  filament_sequence.json. No thumbnails (the 2.4.2 CLI cannot render them).

OrcaSlicer 2.4.2 leaves slice_info 'printer_model_id' empty (it looks for Bambu Studio's
profiles/BBL/machine_full/, which OrcaSlicer does not ship), and on the P2S writes an uninitialised
'first_layer_time' (3.07e32): patch_model_id() fixes both.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

FIRST_LAYER_TIME_MAX = 1e7     # s; above this slice_info's first_layer_time is garbage

REQUIRED = ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/slice_info.config",
            "Metadata/model_settings.config", "Metadata/project_settings.config")

# keys that name or identify profiles, or that OrcaSlicer rewrites by design: not compared
IDENTITY = {"name", "from", "inherits", "setting_id", "instantiation", "type", "compatible_printers",
            "compatible_prints", "compatible_printers_condition", "compatible_prints_condition", "filament_id",
            "description", "version", "renamed_from", "url", "print_settings_id", "printer_settings_id",
            "filament_settings_id", "inherits_group", "different_settings_to_system", "filament_ids"}


def read(path: str | Path, plate: int = 1) -> dict:
    """Everything in the archive the checks need: {names, gcode (bytes), md5_file, slice_info, model_settings,
    project, plate_json}. Missing parts are None."""
    out = {"path": str(path)}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        out["names"] = names
        g = f"Metadata/plate_{plate}.gcode"
        out["gcode"] = z.read(g) if g in names else None
        out["md5_file"] = z.read(g + ".md5").decode("ascii", "replace").strip() if g + ".md5" in names else None
        out["slice_info"] = slice_info(z.read("Metadata/slice_info.config")) if "Metadata/slice_info.config" in names else None
        out["model_settings"] = model_settings(z.read("Metadata/model_settings.config")) if "Metadata/model_settings.config" in names else None
        try:
            out["project"] = json.loads(z.read("Metadata/project_settings.config")) if "Metadata/project_settings.config" in names else None
        except ValueError:
            out["project"] = None
        pj = f"Metadata/plate_{plate}.json"
        try:
            out["plate_json"] = json.loads(z.read(pj)) if pj in names else None
        except ValueError:
            out["plate_json"] = None
    return out


def slice_info(data: bytes) -> dict:
    """slice_info.config -> {header, plates: [{metadata, objects, filaments, warnings, nozzles}]}"""
    root = ET.fromstring(data)
    out = {"header": {h.get("key"): h.get("value") for h in root.iter("header_item")}, "plates": []}
    for p in root.iter("plate"):
        out["plates"].append({
            "metadata": {m.get("key"): m.get("value") for m in p.findall("metadata")},
            "objects": [dict(o.attrib) for o in p.findall("object")],
            "filaments": [dict(f.attrib) for f in p.findall("filament")],
            "warnings": [dict(w.attrib) for w in p.findall("warning")],
            "nozzles": [dict(n.attrib) for n in p.findall("nozzle")]})
    return out


def model_settings(data: bytes) -> dict:
    """model_settings.config -> {objects: {name: {extruder, matrix}}, plates: [{metadata}]}"""
    root = ET.fromstring(data)
    objs = {}
    for o in root.findall("object"):
        md = {m.get("key"): m.get("value") for m in o.findall("metadata")}
        part = o.find("part")
        pmd = {m.get("key"): m.get("value") for m in part.findall("metadata")} if part is not None else {}
        objs[md.get("name")] = {"extruder": md.get("extruder"), "matrix": pmd.get("matrix"),
                                "source_file": pmd.get("source_file")}
    plates = [{m.get("key"): m.get("value") for m in p.findall("metadata")} for p in root.findall("plate")]
    return {"objects": objs, "plates": plates}


def md5_upper(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()


def check_archive(a: dict, plain_gcode: bytes | None, model_id: str | None, nozzle: float,
                  n_filaments: int, filament_map: list[int] | None = None) -> dict:
    """The structural checks: {name: {passed, got, expected, why}}."""
    c = {}
    missing = [n for n in REQUIRED if n not in a["names"]]
    if a["gcode"] is None:
        missing.append("Metadata/plate_1.gcode")
    thumbs_missing = [n for n in THUMBS.values() if n not in a["names"]]
    c["thumbnails"] = {"passed": not thumbs_missing, "hard": False, "missing": thumbs_missing,
                       "why": "plate pictures as OrcaSlicer's GUI writes them (preview and skip-object on the printer)"}
    c["archive_layout"] = {"passed": not missing, "got": sorted(a["names"]), "missing": missing,
                           "why": "the printer reads Metadata/plate_<n>.gcode with its md5 and slice_info.config"}
    if a["gcode"] is not None:
        md5 = md5_upper(a["gcode"])
        c["md5"] = {"passed": a["md5_file"] == md5, "got": a["md5_file"], "expected": md5,
                    "why": "plate_1.gcode.md5 must be the upper-case hex MD5 of plate_1.gcode"}
        if plain_gcode is not None:
            c["plain_gcode_identical"] = {"passed": plain_gcode == a["gcode"],
                                          "why": "the plain .gcode must be the archived one"}
    si = (a.get("slice_info") or {}).get("plates") or []
    if si:
        md = si[0]["metadata"]
        c["printer_model_id"] = {"passed": bool(model_id) and md.get("printer_model_id") == model_id,
                                 "got": md.get("printer_model_id"), "expected": model_id,
                                 "why": "the printer refuses a file for another model"}
        flt = md.get("first_layer_time")
        try:
            flt_v = float(flt) if flt is not None else 0.0
        except ValueError:
            flt_v = float("nan")
        c["slice_info_values"] = {"passed": 0 <= flt_v < FIRST_LAYER_TIME_MAX, "hard": False, "got": {"first_layer_time": flt},
                                  "why": "slice_info values are sane (OrcaSlicer 2.4.2 can write an uninitialised "
                                         "first_layer_time; the translator resets it)"}
        noz = [float(v) for v in re.split(r"[ ,]+", md.get("nozzle_diameters", "").strip()) if v]
        c["nozzle_diameters"] = {"passed": bool(noz) and all(abs(v - nozzle) < 1e-6 for v in noz),
                                 "got": noz, "expected": nozzle}
        fil = si[0]["filaments"]
        used = [int(f["id"]) for f in fil]
        c["slice_info_filaments"] = {"passed": bool(used) and max(used) <= n_filaments, "got": used,
                                     "expected": f"ids within 1..{n_filaments}"}
        if filament_map:
            got = [int(v) for v in md.get("filament_maps", "").split()]
            c["filament_map"] = {"passed": got == list(filament_map), "got": got, "expected": list(filament_map),
                                 "why": "each filament must go to the extruder the study asked for (1 left, 2 right)"}
    else:
        c["slice_info"] = {"passed": False, "why": "slice_info.config is missing or has no plate"}
    return c


# ---------------------------------------------------------------- settings round trip

_POINT = re.compile(r"^-?\d*\.?\d+\s*[xX]\s*-?\d*\.?\d+$")


def _norm(v):
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if v is None:
        return None
    s = str(v).strip()
    if _POINT.match(s):
        a, b = re.split(r"\s*[xX]\s*", s)
        return f"{float(a):g},{float(b):g}"
    t = s[:-1] if s.endswith("%") else s
    try:
        f = float(t)
        return f"{f:.9g}" + ("%" if s.endswith("%") else "")
    except ValueError:
        return s.replace("\r\n", "\n")


def _same(ours, theirs) -> bool:
    a, b = _norm(ours), _norm(theirs)
    if a in ([], "", None):                     # unset in our profile: OrcaSlicer's default applies
        return True
    if isinstance(a, list) and isinstance(b, list):
        # equal, or one value expanded per extruder variant (['0'] -> ['0', '0'])
        return a == b or (len(a) == 1 and len(b) >= 1 and all(x == a[0] for x in b))
    if isinstance(a, list):                     # a list we wrote, a scalar sliced: the variant used
        return b in a
    if isinstance(b, list):
        return len(b) >= 1 and all(x == a for x in b) or a in b
    if isinstance(a, str) and isinstance(b, str) and "," in b and _POINT.match(str(ours) or ""):
        return a == b
    return a == b


def roundtrip(machine: dict, process: dict, filaments: list[dict], project: dict, known: set) -> dict:
    """Compares our flattened profiles with project_settings.config. Only keys OrcaSlicer knows (known)
    and that it wrote are compared; identity keys are skipped. Filament keys in project_settings are
    per filament, or per filament and extruder variant (filament_self_index tells which entry is
    whose). Returns {passed, compared, differ: {key: [ours, sliced]}, skipped_unknown: n}."""
    differ, compared, unknown = {}, 0, 0
    for where, cfg in (("machine", machine), ("process", process)):
        for k, v in cfg.items():
            if k in IDENTITY:
                continue
            if k not in known:
                unknown += 1
                continue
            if k not in project:
                continue
            compared += 1
            if not _same(v, project[k]):
                differ[f"{where}.{k}"] = [v, project[k]]
    selfidx = [int(float(s)) for s in project.get("filament_self_index", [])]
    nf = len(filaments)
    for i, cfg in enumerate(filaments, start=1):
        for k, v in cfg.items():
            if k in IDENTITY:
                continue
            if k not in known:
                unknown += 1
                continue
            if k not in project:
                continue
            theirs = project[k]
            if isinstance(theirs, list) and selfidx and len(theirs) == len(selfidx) and len(selfidx) != nf:
                mine = [theirs[j] for j, s in enumerate(selfidx) if s == i]
            elif isinstance(theirs, list) and len(theirs) == nf:
                mine = theirs[i - 1]
            else:
                mine = theirs
            compared += 1
            if not _same(v, mine):
                differ[f"filament.{i}.{k}"] = [v, mine]
    return {"passed": not differ, "compared": compared, "differ": differ, "skipped_unknown": unknown}


# ---------------------------------------------------------------- patching

THUMBS = {"plate": "Metadata/plate_1.png", "plate_small": "Metadata/plate_1_small.png",
          "no_light": "Metadata/plate_no_light_1.png", "top": "Metadata/top_1.png", "pick": "Metadata/pick_1.png"}
_MS_REFS = (("thumbnail_file", "plate"), ("thumbnail_no_light_file", "no_light"), ("top_file", "top"),
            ("pick_file", "pick"))


def _set_meta(xml: str, key: str, value: str) -> str:
    """Sets <metadata key=".." value=".."/> in slice_info's first plate (added after 'index' if missing)."""
    pat = rf'(<metadata key="{re.escape(key)}" value=")[^"]*(")'
    if re.search(pat, xml):
        return re.sub(pat, lambda m: f"{m.group(1)}{value}{m.group(2)}", xml, count=1)
    return re.sub(r'(\n(\s*)<metadata key="index" value="[^"]*"/>)',
                  lambda m: f'{m.group(1)}\n{m.group(2)}<metadata key="{key}" value="{value}"/>', xml, count=1)


def finalize(path: str | Path, model_id: str | None, extruders: int = 1, first_layer_s: float | None = None,
             images: dict | None = None) -> dict:
    """Makes the CLI's archive match what OrcaSlicer's GUI exports ('Export plate sliced file', compared in
    spike S4 on the P2S and H2D), without touching the G-code or its md5:
      slice_info:     printer_model_id (the CLI leaves it empty); first_layer_time from the G-code header
                      (the CLI writes 0, or garbage such as 3.07e32 on the P2S); nozzle_volume_type with one
                      value per extruder (the CLI writes one value on the H2D, the GUI '0 0')
      model_settings: the plate's filament_maps, thumbnail_file, thumbnail_no_light_file, top_file,
                      pick_file and pattern_bbox_file entries
      plate_1.json:   first_layer_time
      the pictures:   plate_1.png, plate_1_small.png, plate_no_light_1.png, top_1.png, pick_1.png (thumbs.py)
    Returns {changed: [...]}. Rewrites the zip once."""
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    files = {i.filename: b for i, b in items}
    changed = []
    si = files.get("Metadata/slice_info.config", b"").decode("utf-8")
    si0 = si
    if model_id:
        si = _set_meta(si, "printer_model_id", model_id)
    flt = None
    m = re.search(r'<metadata key="first_layer_time" value="([^"]*)"', si)
    try:
        cur = float(m.group(1)) if m else 0.0
    except ValueError:
        cur = float("nan")
    if first_layer_s is not None and first_layer_s >= 0:
        flt = float(first_layer_s)
    elif not (0 <= cur < FIRST_LAYER_TIME_MAX):
        flt = 0.0
    if flt is not None:
        si = _set_meta(si, "first_layer_time", f"{flt:.6f}")
    m = re.search(r'<metadata key="nozzle_volume_type" value="([^"]*)"', si)
    if m and extruders > 1:
        vals = m.group(1).split()
        if 0 < len(vals) < extruders:
            si = _set_meta(si, "nozzle_volume_type", " ".join(vals + [vals[-1]] * (extruders - len(vals))))
    if si != si0:
        files["Metadata/slice_info.config"] = si.encode("utf-8")
        changed.append("slice_info.config")
    fm = re.search(r'<metadata key="filament_maps" value="([^"]*)"', si)
    ms = files.get("Metadata/model_settings.config", b"").decode("utf-8")
    if ms and "<plate>" in ms:
        ms0 = ms
        want = []
        if fm and 'key="filament_maps"' not in ms.split("<plate>", 1)[1]:
            want.append(("filament_maps", fm.group(1)))
        if images:
            for key, img in _MS_REFS:
                if f'key="{key}"' not in ms:
                    want.append((key, THUMBS[img]))
        if "Metadata/plate_1.json" in files and 'key="pattern_bbox_file"' not in ms:
            want.append(("pattern_bbox_file", "Metadata/plate_1.json"))
        if want:
            plate_open = ms.index("<plate>") + len("<plate>")
            gm = re.search(r'\n([ \t]*)<metadata key="gcode_file"[^\n]*', ms[plate_open:])
            if gm:
                at, indent = plate_open + gm.end(), gm.group(1)
            else:
                at, indent = plate_open, "    "
            ms = ms[:at] + "".join(f'\n{indent}<metadata key="{k}" value="{v}"/>' for k, v in want) + ms[at:]
        if ms != ms0:
            files["Metadata/model_settings.config"] = ms.encode("utf-8")
            changed.append("model_settings.config")
    if flt is not None and "Metadata/plate_1.json" in files:
        try:
            pj = json.loads(files["Metadata/plate_1.json"])
            if pj.get("first_layer_time") != flt:
                pj["first_layer_time"] = flt
                files["Metadata/plate_1.json"] = json.dumps(pj, separators=(",", ":")).encode()
                changed.append("plate_1.json")
        except ValueError:
            pass
    new_names = []
    for key, name in THUMBS.items():
        if images and key in images and name not in files:
            files[name] = images[key]
            new_names.append(name)
    if new_names:
        changed.append("thumbnails")
    if not changed:
        return {"changed": []}
    tmp = Path(tempfile.mkstemp(suffix=".3mf", dir=path.parent)[1])
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, _ in items:
            zout.writestr(info, files[info.filename])
        for name in new_names:
            zout.writestr(name, files[name])
    shutil.move(str(tmp), str(path))
    return {"changed": changed}


def patch_model_id(path: str | Path, model_id: str, plate: int = 1) -> bool:
    """printer_model_id and a sane first_layer_time only (see finalize). Returns True if rewritten."""
    return bool(finalize(path, model_id)["changed"])
