"""gcode3mf.py — the Bambu .gcode.3mf archive: read it, check its structure, compare the settings it
was sliced with against ours, and fill in what OrcaSlicer's CLI leaves out.
Version: 2026.10.24.3

Layout written by OrcaSlicer 2.4.2 (--export-3mf after --slice), as seen on the P2S and H2D:
  [Content_Types].xml, _rels/.rels, 3D/3dmodel.model (+ 3D/Objects/*.model without --min-save),
  Metadata/plate_<n>.gcode, plate_<n>.gcode.md5 (upper-case hex MD5 of the G-code bytes),
  slice_info.config (XML: plate metadata, objects, filaments with used_m/used_g, warnings, nozzles),
  model_settings.config (XML: objects with their 'extruder' = filament number, plate filament_maps),
  project_settings.config (JSON: every setting the plate was sliced with), plate_<n>.json,
  filament_sequence.json. No thumbnails (the 2.4.2 CLI cannot render them).

OrcaSlicer 2.4.2 leaves slice_info 'printer_model_id' empty (it looks for Bambu Studio's
profiles/BBL/machine_full/, which OrcaSlicer does not ship): patch_model_id() writes it.
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

def patch_model_id(path: str | Path, model_id: str, plate: int = 1) -> bool:
    """Writes printer_model_id into slice_info.config when it is empty or different; the G-code and its
    md5 are not touched. Returns True if the archive was rewritten."""
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        data = z.read("Metadata/slice_info.config").decode("utf-8")
    new = re.sub(r'(<metadata key="printer_model_id" value=")[^"]*(")', rf'\g<1>{model_id}\g<2>', data, count=1)
    if new == data:
        return False
    tmp = Path(tempfile.mkstemp(suffix=".3mf", dir=path.parent)[1])
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            buf = new.encode("utf-8") if item.filename == "Metadata/slice_info.config" else zin.read(item.filename)
            zout.writestr(item, buf)
    shutil.move(str(tmp), str(path))
    return True
