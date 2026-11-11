"""format.py — the study format (study_format) and static validation (validate): no slicing.
Version: 2026.11.11.2

A study is one plate: a Bambu Lab printer, its filaments, the objects on the plate and the intent.
Validation checks keys, types, files and (when OrcaSlicer's profiles are found) resolves the
profiles, so a bad filament name or setting is reported with its fix before anything runs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import profiles as P

STUDY_KEYS = {"printer", "filaments", "objects", "intent", "overrides", "allow_mix_temp", "expect", "run",
              "description", "view", "layout", "accept"}
OBJECT_KEYS = {"name", "part", "parts", "filament", "orient", "copies", "at", "settings", "from_3mf"}
FROM_3MF_KEYS = {"path", "plate", "objects", "filament_map", "layout", "settings"}
LAYOUT_KEYS = {"overlap", "gap", "brims_may_overlap"}
ACCEPT_KEYS = {"check", "object", "z_range", "reason"}
ACCEPTABLE = {"unsupported_extrusion", "long_bridges", "extrusion_volume", "layers", "footprint", "bed_vs_filaments",
              "shared_nozzle", "estimates", "thin_walls", "bed_contact", "unsupported_regions", "closed_mesh"}
PART_KEYS = {"name", "part", "filament"}
PART_FORMS = ("step", "stl", "3mf", "goal", "b3dt")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
MAX_COPIES = 50

CONVENTIONS = [
    "Units: mm, degrees C, %, mm/s; Z up. One study = one plate of one printer. Nothing is ever sent to a printer: "
    "the result is a .gcode.3mf (and .gcode) to print from the printer's SD card, Bambu Studio or Bambu Handy.",
    "Profiles are OrcaSlicer's system profiles, flattened; intent and overrides change copies only.",
    "Objects are oriented ('as_is', 'flat' = largest flat face down, or {'rotate': [rx, ry, rz]} degrees, X then Y "
    "then Z) and placed by the translator (deterministic; 'at' [x, y] fixes a centre). With 2+ filaments the "
    "translator also places the prime tower.",
    "H2D: filaments[].extruder 'left' | 'right' | 'auto'. Filaments that print at different temperatures belong "
    "on different nozzles; on one-nozzle printers they need allow_mix_temp: true (one bed temperature for all).",
    "Check 'reliable', 'checks' and 'warnings' before using a result; if reliable is false, do not print it.",
]

EXAMPLE = {"bracket_petg": {
    "printer": {"model": "P2S", "nozzle": 0.4, "plate": "textured_pei"},
    "filaments": [{"profile": "Bambu PETG HF", "color": "#202020"}],
    "objects": [{"name": "bracket", "part": {"step": "bracket.step"}, "orient": "flat", "copies": 2}],
    "intent": {"preset": "strength", "walls": 4, "infill": 30, "infill_pattern": "gyroid", "supports": "none",
               "brim": "auto"},
    "expect": [{"quantity": "estimate.filament_g", "value": 40, "tol": 0.3}]}}


def study_format(status: dict | None = None) -> dict:
    return {
        "format": "print study (print3d_translator)",
        "conventions": CONVENTIONS,
        "study_keys": {
            "printer": "{model: H2D|H2S|P2S (other Bambu Lab models resolve untested), nozzle: 0.2|0.4|0.6|0.8, "
                       f"plate: {'|'.join(sorted(P.PLATES))} (default textured_pei)}}",
            "filaments": "[{profile: 'Bambu PLA Basic'} or {type: 'PETG', brand: 'Bambu'|'Generic', variant: 'HF'}, "
                         "color: '#RRGGBB', extruder: left|right|auto (H2D)}]; filament n = position n (1-based)",
            "objects": "[{name, part, filament (1), orient ('as_is'), copies (1), at: [x, y] mm, settings}] or, for an "
                       "object made of several solids with their own filaments (one object, one brim): "
                       "{name, parts: [{name, part, filament}], orient, copies, at, settings}; or "
                       "{from_3mf: {path, plate, objects, filament_map, layout, settings}} (one study object per 3MF object)",
            "at": "[x, y]: the centre of the object's bounding box on the bed (mm, bed coordinates)",
            "settings": "{key: value}: OrcaSlicer process settings for this object only (layer_height, wall_loops, "
                        "sparse_infill_density, enable_support, brim_type, brim_width, line widths, ...)",
            "part": "{step: path} | {stl: path} | {3mf: path, plate: n, objects: [names]} | {goal: build123d-translator "
                    "goal} | {b3dt: {step: path, volume: mm3}} (a build123d-translator result)",
            "3mf": "Read: build items and their components (parts) with transforms and units; Bambu Studio / "
                   "OrcaSlicer project metadata (Metadata/model_settings.config): object and part names, part types "
                   "(only normal parts are solids; modifiers, negative parts and support enforcers / blockers are "
                   "left out), each part's filament ('extruder'; kept when the parts differ, else 'filament' applies), "
                   "per-object settings, plates. Ignored (listed in warnings): variable layer height, height-range "
                   "modifiers, painted supports / seams / colours, per-part settings, the project's process and "
                   "filament profiles (the study's apply).",
            "from_3mf": "{path, plate: n (only that plate), objects: [names or 'o<id>'], filament_map: {'2': 1} "
                        "(3MF filament -> study filament), layout: 'keep' (default: the plate positions) | 'auto', "
                        "settings: 'keep' (default: per-object settings that OrcaSlicer knows) | 'drop'}",
            "layout": "{overlap: 'footprint' (default: silhouettes may not overlap) | 'first_layer' (first-layer outlines "
                      "may not overlap; parts may interleave above the bed and a 3D check keeps them apart), gap: mm "
                      "between parts (6), brims_may_overlap: false}. Each part keeps outline + gap/2 + its brim (+3 mm "
                      "with supports when placed automatically).",
            "accept": "[{check, object, z_range: [z0, z1], reason}]: a known finding accepted on purpose (e.g. "
                      "unsupported_extrusion of a bridge that cannot have supports): the result stays reliable and "
                      "lists it in 'accepted'. object and z_range narrow it (unsupported_extrusion); reason is required.",
            "intent": {"preset": f"{'|'.join(P.PRESETS)} (picks the process profile; default standard)",
                       "process": "an exact process profile name instead of preset",
                       "layer_height": "mm", "first_layer_height": "mm", "walls": "wall loops",
                       "top_layers": "n", "bottom_layers": "n", "infill": "%", "infill_pattern":
                       "gyroid|grid|cubic|rectilinear|honeycomb|lightning|crosshatch|...",
                       "supports": "none|auto|tree|needed (sliced without supports; again with tree supports only if "
                                   "something has nothing below it)", "support_angle": "degrees from horizontal (overhangs flatter "
                       "than this get support)", "brim": "none|auto|<width mm>", "seam": "aligned|back|nearest|random",
                       "color_changes": "[{z, filament}]: from height z (mm) up, every object prints with that filament "
                                        "(an AMS filament change; as OrcaSlicer height-range modifiers)",
                       "pauses": "[{z, note}]: pause the printer before the layer at height z (the machine's pause "
                                 "G-code, e.g. M400 U1), e.g. to insert magnets or change filament by hand"},
            "overrides": "{process: {key: value}, machine: {...}, filament: {'all' | '1' | '2': {...}}}: raw "
                         "OrcaSlicer keys (validated against OrcaSlicer's own keys)",
            "allow_mix_temp": "true: allow filaments with different temperatures on one nozzle (one bed temperature)",
            "expect": "[{quantity: dotted result path (estimate.filament_g, estimate.time_s, gcode.layers, "
                      "objects.<name>.volume_mm3), value, tol (relative, default 0.05)}]",
            "run": "{timeout_s}: OrcaSlicer's time limit for this study (default P3DT_TIMEOUT, 600)",
            "view": "true: also write <name>.view.html, a self-contained 3D view (parts, toolpaths, layer slider)"},
        "results": {
            "reliable": "false when a hard check failed (unreliable_because says which)",
            "files": "gcode_3mf (the file to print), gcode, profiles (the flattened settings), log",
            "estimate": "time_s, time, filament_g, filament_m, by_filament (OrcaSlicer's, checked against ours)",
            "objects": "per object: volume_mm3 (exact for STEP/b3dt), orientation, center, checks before slicing",
            "checks": "archive, md5, printer_model_id, settings_roundtrip, bounds, layers, first_layer, tools, "
                      "temperatures, extrusion_volume, footprint, ... each {passed, hard, why, got, expected}",
            "profiles": "machine, process, filaments, set (every value changed: [old, new]), sha256"},
        "presets": {k: v["words"] for k, v in P.PRESETS.items()},
        "example": EXAMPLE,
        "status": status or {},
    }


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def object_names(study: dict) -> list[str]:
    out = []
    for k, o in enumerate(study.get("objects") or [], start=1):
        out.append(str(o.get("name") or f"part{k}") if isinstance(o, dict) else f"part{k}")
    return out


def _slug(name: str, oid: str, inst: int) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", re.sub(r"\.(stl|step|stp|obj|3mf)$", "", str(name), flags=re.I)).strip("_-")
    tail = f"_{oid}" + (f"_{inst + 1}" if inst else "")
    return ((base[:40 - len(tail)] if base else "o") + tail).lstrip("_-") or f"o{oid}"


def expand(study: dict, base_dir=None) -> tuple[dict, list[str]]:
    """from_3mf entries -> one object per 3MF object (its solid parts with their filaments, its settings,
    its plate position). Returns (a copy of the study with objects expanded, notes). Raises ValueError."""
    import copy
    from . import mesh as M
    from . import threemf
    objs = study.get("objects")
    if not isinstance(objs, list) or not any(isinstance(o, dict) and "from_3mf" in o for o in objs):
        return study, []
    out, notes = [], []
    for k, o in enumerate(objs):
        if not (isinstance(o, dict) and "from_3mf" in o):
            out.append(o)
            continue
        spec = o["from_3mf"]
        where = f"objects[{k}].from_3mf"
        if not isinstance(spec, dict) or not spec.get("path"):
            raise ValueError(f"{where}: {{path, plate, objects, filament_map, layout, settings}}")
        bad = (set(spec) - FROM_3MF_KEYS) | (set(o) - {"from_3mf"})
        if bad:
            raise ValueError(f"{where}: unknown keys {sorted(bad)}; known {sorted(FROM_3MF_KEYS)} (and nothing beside from_3mf)")
        path = Path(str(spec["path"])).expanduser()
        if not path.is_absolute() and base_dir:
            path = Path(base_dir) / path
        if not path.is_file():
            raise ValueError(f"{where}.path: file {path} not found")
        layout = spec.get("layout", "keep")
        if layout not in ("keep", "auto"):
            raise ValueError(f"{where}.layout: 'keep' (the plate positions) or 'auto'")
        fmap = spec.get("filament_map") or {}
        if not isinstance(fmap, dict) or not all(str(a).isdigit() and isinstance(b, int) for a, b in fmap.items()):
            raise ValueError(f"{where}.filament_map: {{'3MF filament': study filament}}, e.g. {{'2': 1}}")
        try:
            r = threemf.read(path, plate=spec.get("plate"), names=spec.get("objects"), meshes=(layout == "keep"))
        except threemf.ThreeMFError as e:
            raise ValueError(f"{where}: {e}") from e
        if r["ignored"]:
            notes.append(f"{path.name}: not read: {', '.join(r['ignored'])}")
        multi_plate = len(r["plates"]) > 1
        if multi_plate and spec.get("plate") is None:
            notes.append(f"{path.name} has {len(r['plates'])} plates; without 'plate' all objects go on one plate")
        for ob in r["objects"]:
            solid = [q for q in ob["parts"] if q["subtype"] == threemf.SOLID]
            if not solid:
                continue
            e = {"name": _slug(ob["name"], ob["id"], ob["instance"]),
                 "part": {"3mf": str(path), "object_id": ob["id"], "instance": ob["instance"],
                          **({"plate": spec["plate"]} if spec.get("plate") is not None else {}),
                          **({"filament_map": {str(a): int(b) for a, b in fmap.items()}} if fmap else {})},
                 "orient": "as_is"}
            if layout == "keep" and len(ob["tri"]):
                lo, hi = M.bbox(ob["tri"])
                e["at"] = [round(float(lo[0] + hi[0]) / 2, 3), round(float(lo[1] + hi[1]) / 2, 3)]
            if spec.get("settings", "keep") == "keep" and ob["settings"]:
                e["settings"] = dict(ob["settings"])
                e["_settings_from_3mf"] = True
            e["_from_3mf"] = {"name": ob["name"], "id": ob["id"], "plate": ob["plate"]}
            out.append(e)
    st = copy.copy(study)
    st["objects"] = out
    return st, notes


def _check_part(where: str, part, base_dir) -> tuple[list[str], list[str]]:
    """Errors and warnings for one part form ({'stl': path}, {'step': path}, ...)."""
    errors, warnings = [], []
    forms = [f for f in PART_FORMS if isinstance(part, dict) and f in part]
    if len(forms) != 1:
        return [f"{where}: exactly one of {{{', '.join(PART_FORMS)}}}"], []
    form = forms[0]
    if form in ("step", "stl", "3mf"):
        p = Path(str(part[form])).expanduser()
        if not p.is_absolute() and base_dir:
            p = Path(base_dir) / p
        if not p.is_file():
            errors.append(f"{where}.{form}: file {p} not found")
        elif not p.name.lower().endswith({"step": (".step", ".stp"), "stl": (".stl",), "3mf": (".3mf",)}[form]):
            warnings.append(f"{where}.{form}: {p.name} does not have a .{form} extension")
    if form == "3mf":
        bad = set(part) - {"3mf", "plate", "objects", "object_id", "instance", "filament_map"}
        if bad:
            errors.append(f"{where}: unknown keys {sorted(bad)}; a 3MF part takes 'plate' (a number) and 'objects' (names)")
        if part.get("plate") is not None and not (isinstance(part["plate"], int) and part["plate"] >= 1):
            errors.append(f"{where}.plate: a plate number (1, 2, ...)")
        if part.get("objects") is not None and not (isinstance(part["objects"], list) and
                                                    all(isinstance(n, str) for n in part["objects"])):
            errors.append(f"{where}.objects: a list of object names (or 'o<id>')")
    elif len(part) != 1:
        errors.append(f"{where}: only the key '{form}'")
    if form in ("step", "b3dt", "goal"):
        try:
            import build123d  # noqa: F401
        except ImportError:
            errors.append(f"{where}.{form}: needs build123d here (pip install \"print3d-translator[cad]\"), "
                          f"or give an STL/3MF")
    if form == "b3dt" and not (isinstance(part["b3dt"], dict) and part["b3dt"].get("step")):
        errors.append(f"{where}.b3dt: the build123d-translator result's {{'step': path, 'volume': mm3}}")
    if form == "goal":
        try:
            from build123d_translator.goalformat import validate_goal
            v = validate_goal(part["goal"])
            errors += [f"{where}.goal: {e}" for e in v.get("errors", [])]
        except ImportError:
            errors.append(f"{where}.goal: build123d-translator is not installed here; give its result "
                          f"({{'b3dt': {{'step', 'volume'}}}}) instead")
    return errors, warnings


def object_filaments(o: dict) -> list[int]:
    """The filaments an object entry names (its parts' or its own; 3MF part assignments come later)."""
    if isinstance(o.get("parts"), list):
        return sorted({int(q.get("filament", 1)) for q in o["parts"] if isinstance(q, dict)})
    return [int(o.get("filament", 1))]


def validate_study(st, base_dir: str | Path | None = None, vendor: P.Vendor | None = None) -> dict:
    """{ok, errors, warnings, estimate}. Each error says what to change. With `vendor` (OrcaSlicer's
    profiles) the profiles are resolved too."""
    errors, warnings = [], []
    if not isinstance(st, dict):
        return {"ok": False, "errors": ["a study is a JSON object"], "warnings": [], "estimate": None}
    try:
        st, notes = expand(st, base_dir)
        warnings += notes
    except ValueError as e:
        return {"ok": False, "errors": [str(e)], "warnings": warnings, "estimate": None}
    unknown = set(st) - STUDY_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}; a study has {sorted(STUDY_KEYS)}")
    pr = st.get("printer")
    if not isinstance(pr, dict) or not pr.get("model"):
        errors.append("printer: give {'model': 'H2D'|'H2S'|'P2S', 'nozzle': 0.4}")
    elif "nozzle" in pr and not _num(pr["nozzle"]):
        errors.append("printer.nozzle: a number in mm (0.2, 0.4, 0.6, 0.8)")
    fils = st.get("filaments")
    if not isinstance(fils, list) or not fils or not all(isinstance(f, dict) for f in fils):
        errors.append("filaments: a list of {'profile': 'Bambu PLA Basic'} or {'type': 'PLA', ...}")
        fils = []
    for i, f in enumerate(fils):
        bad = set(f) - {"profile", "type", "brand", "variant", "color", "extruder"}
        if bad:
            errors.append(f"filaments[{i}]: unknown keys {sorted(bad)}")
        if f.get("color") and not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", str(f["color"])):
            errors.append(f"filaments[{i}].color: '#RRGGBB'")
        if f.get("extruder", "auto") not in ("left", "right", "auto"):
            errors.append(f"filaments[{i}].extruder: 'left', 'right' or 'auto'")
    objs = st.get("objects")
    if not isinstance(objs, list) or not objs:
        errors.append("objects: a list of {'part': {'step': path}, 'filament': 1, ...}")
        objs = []
    names = object_names(st)
    if len(set(names)) != len(names):
        errors.append(f"objects: names must be unique, got {names}")
    n_parts = 0
    for k, o in enumerate(objs):
        where = f"objects[{k}]"
        if not isinstance(o, dict):
            errors.append(f"{where}: an object")
            continue
        bad = set(o) - OBJECT_KEYS - {"_from_3mf", "_settings_from_3mf"}
        if bad:
            errors.append(f"{where}: unknown keys {sorted(bad)}; known {sorted(OBJECT_KEYS)}")
        if not NAME.match(names[k]):
            errors.append(f"{where}.name '{names[k]}' ({len(names[k])} characters): letters, digits, '_' and '-', "
                          f"at most 40, starting with a letter or digit")
        if ("part" in o) == ("parts" in o):
            errors.append(f"{where}: give 'part' (one solid) or 'parts' (several solids printed as one object, "
                          f"each with its own filament), not both")
        sub = [(f"{where}.part", o.get("part"))] if "part" in o else []
        if "parts" in o:
            if not isinstance(o["parts"], list) or not o["parts"] or not all(isinstance(q, dict) for q in o["parts"]):
                errors.append(f"{where}.parts: [{{'part': {{'stl': path}}, 'filament': 1}}, ...]")
            else:
                if "filament" in o:
                    errors.append(f"{where}: with 'parts', give each part its 'filament'")
                pn = [str(q.get("name") or f"part{i + 1}") for i, q in enumerate(o["parts"])]
                if len(set(pn)) != len(pn):
                    errors.append(f"{where}.parts: names must be unique, got {pn}")
                for i, q in enumerate(o["parts"]):
                    bad = set(q) - PART_KEYS
                    if bad:
                        errors.append(f"{where}.parts[{i}]: unknown keys {sorted(bad)}; known {sorted(PART_KEYS)}")
                    fq = q.get("filament", 1)
                    if not isinstance(fq, int) or isinstance(fq, bool) or not 1 <= fq <= max(1, len(fils)):
                        errors.append(f"{where}.parts[{i}].filament: a filament number 1..{len(fils)}")
                    sub.append((f"{where}.parts[{i}].part", q.get("part")))
        for pwhere, part in sub:
            errors_, warnings_ = _check_part(pwhere, part, base_dir)
            errors += errors_
            warnings += warnings_
        fi = o.get("filament", 1)
        if not isinstance(fi, int) or isinstance(fi, bool) or not 1 <= fi <= max(1, len(fils)):
            errors.append(f"{where}.filament: a filament number 1..{len(fils)}")
        se = o.get("settings")
        if se is not None and not (isinstance(se, dict) and all(isinstance(a, str) and isinstance(b, (str, int, float, bool))
                                                              for a, b in se.items())):
            errors.append(f"{where}.settings: {{OrcaSlicer process key: value}}, e.g. {{'layer_height': 0.12}}")
        orient = o.get("orient", "as_is")
        if not (orient in ("as_is", "flat") or (isinstance(orient, dict) and set(orient) == {"rotate"} and
                isinstance(orient["rotate"], list) and len(orient["rotate"]) == 3 and all(_num(a) for a in orient["rotate"]))):
            errors.append(f"{where}.orient: 'as_is', 'flat' or {{'rotate': [rx, ry, rz]}} (degrees)")
        c = o.get("copies", 1)
        if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= MAX_COPIES:
            errors.append(f"{where}.copies: 1..{MAX_COPIES}")
        else:
            n_parts += c
        at = o.get("at")
        if at is not None:
            if not (isinstance(at, list) and len(at) == 2 and all(_num(a) for a in at)):
                errors.append(f"{where}.at: [x, y] in mm (the centre on the plate)")
            elif o.get("copies", 1) != 1:
                errors.append(f"{where}: 'at' places one copy; use copies 1 (or separate objects)")
    exp = st.get("expect", [])
    if not isinstance(exp, list) or not all(isinstance(e, dict) and isinstance(e.get("quantity"), str) and _num(e.get("value"))
                                            and (e.get("tol") is None or _num(e["tol"])) for e in exp):
        errors.append("expect: [{'quantity': 'estimate.filament_g', 'value': 12.5, 'tol': 0.1}]")
    run = st.get("run", {})
    if not isinstance(run, dict) or set(run) - {"timeout_s"}:
        errors.append("run: {'timeout_s': seconds}")
    elif "timeout_s" in run:
        cap = os.environ.get("P3DT_MAX_TIMEOUT")
        if not _num(run["timeout_s"]) or run["timeout_s"] <= 0:
            errors.append("run.timeout_s: seconds > 0")
        elif cap and run["timeout_s"] > float(cap):
            errors.append(f"run.timeout_s {run['timeout_s']} is above this machine's cap P3DT_MAX_TIMEOUT={cap}")
    lay = st.get("layout", {})
    if not isinstance(lay, dict) or set(lay) - LAYOUT_KEYS:
        errors.append(f"layout: {{overlap: 'footprint'|'first_layer', gap: mm, brims_may_overlap: bool}}")
    else:
        if lay.get("overlap", "footprint") not in ("footprint", "first_layer"):
            errors.append("layout.overlap: 'footprint' or 'first_layer'")
        if "gap" in lay and not (_num(lay["gap"]) and 0 <= lay["gap"] <= 50):
            errors.append("layout.gap: mm between parts, 0..50")
        if "brims_may_overlap" in lay and not isinstance(lay["brims_may_overlap"], bool):
            errors.append("layout.brims_may_overlap: true or false")
    acc = st.get("accept", [])
    if not isinstance(acc, list):
        errors.append("accept: [{check, object, z_range, reason}]")
        acc = []
    for i, a in enumerate(acc):
        if not isinstance(a, dict) or set(a) - ACCEPT_KEYS:
            errors.append(f"accept[{i}]: {{check, object, z_range: [z0, z1], reason}}")
            continue
        if a.get("check") not in ACCEPTABLE:
            errors.append(f"accept[{i}].check: one of {sorted(ACCEPTABLE)} (checks that make the file wrong, "
                          f"such as bounds or temperatures, cannot be accepted)")
        if not (isinstance(a.get("reason"), str) and len(a["reason"].strip()) >= 3):
            errors.append(f"accept[{i}].reason: say why it is accepted (it is listed in the result)")
        if "object" in a and a["object"] not in names:
            errors.append(f"accept[{i}].object: '{a['object']}' is not an object of this study ({names})")
        zr = a.get("z_range")
        if zr is not None and not (isinstance(zr, list) and len(zr) == 2 and all(_num(z) for z in zr) and zr[0] <= zr[1]):
            errors.append(f"accept[{i}].z_range: [z0, z1] in mm")
    intent = st.get("intent") or {}
    if isinstance(intent, dict):
        for i, c in enumerate(intent.get("color_changes") or []):
            if not (isinstance(c, dict) and set(c) <= {"z", "filament"} and _num(c.get("z")) and c["z"] > 0 and
                    isinstance(c.get("filament"), int) and 1 <= c["filament"] <= max(1, len(fils))):
                errors.append(f"intent.color_changes[{i}]: {{z: mm > 0, filament: 1..{len(fils)}}}")
        zs = [c.get("z") for c in intent.get("color_changes") or [] if isinstance(c, dict)]
        if len(set(zs)) != len(zs):
            errors.append("intent.color_changes: one change per height")
        if not isinstance(intent.get("color_changes", []), list):
            errors.append("intent.color_changes: a list of {z, filament}")
        for i, c in enumerate(intent.get("pauses") or []):
            if not (isinstance(c, dict) and set(c) <= {"z", "note"} and _num(c.get("z")) and c["z"] > 0):
                errors.append(f"intent.pauses[{i}]: {{z: mm > 0, note: text}}")
        if not isinstance(intent.get("pauses", []), list):
            errors.append("intent.pauses: a list of {z, note}")
    if "view" in st and not isinstance(st["view"], bool):
        errors.append("view: true or false")
    if "allow_mix_temp" in st and not isinstance(st["allow_mix_temp"], bool):
        errors.append("allow_mix_temp: true or false")

    # profiles (only when the rest is sound and OrcaSlicer's profiles are here)
    if not errors:
        if vendor is None:
            warnings.append("OrcaSlicer's profiles were not found here: printer, filament and setting names "
                            "are not checked (install OrcaSlicer, or set P3DT_ORCA_PROFILES)")
        else:
            try:
                r = P.resolve(vendor, st)
                errors += mix_temperature(st, r)
                warnings += r["warnings"]
                for k, o in enumerate(objs):
                    unknown = sorted(set(o.get("settings") or {}) - P.process_keys(r))
                    if unknown and o.get("_settings_from_3mf"):
                        warnings.append(f"objects[{k}] ({names[k]}): 3MF settings OrcaSlicer does not have, left out: {unknown}")
                    elif unknown:
                        errors.append(f"objects[{k}].settings: {unknown} are not OrcaSlicer process settings")
            except P.ProfileError as e:
                errors.append(str(e))
    passes = 2 if isinstance(st.get("intent"), dict) and st["intent"].get("supports") == "needed" else 1  # at most two slices
    estimate = {"minutes": round((0.5 + 0.2 * n_parts) * passes, 1), "procs": 1, "mem_mb": 1500} if not errors else None
    return {"ok": not errors, "errors": errors, "warnings": warnings, "estimate": estimate}


def mix_temperature(st: dict, r: dict) -> list[str]:
    """OrcaSlicer refuses filaments with different temperatures on one nozzle (-62) unless allowed.
    Returns errors (one-nozzle printers) and appends warnings to r['warnings'] (H2D)."""
    fils = r["filaments"]
    used = sorted({f for o in st.get("objects") or [] if isinstance(o, dict) for f in object_filaments(o)})
    if len(used) < 2:
        return []

    def first(i, key):
        v = fils[i - 1].get(key)
        return float((v[0] if isinstance(v, list) else v) or 0)

    temps = {i: first(i, "nozzle_temperature") for i in used}

    def clash(ids):
        """OrcaSlicer 2.4.2 Print::check_multi_filaments_compatibility: a pair is compatible when each
        one's print temperature lies within the other's nozzle_temperature_range_low..high."""
        bad = []
        for a in ids:
            for b in ids:
                if a < b:
                    lo_a, hi_a = first(a, "nozzle_temperature_range_low"), first(a, "nozzle_temperature_range_high")
                    lo_b, hi_b = first(b, "nozzle_temperature_range_low"), first(b, "nozzle_temperature_range_high")
                    if not (lo_a and hi_a and lo_b and hi_b):
                        continue
                    if not (lo_b <= temps[a] <= hi_b and lo_a <= temps[b] <= hi_a):
                        bad.append((a, b))
        return bad

    if r["model"]["extruders"] == 1:
        bad = clash(used)
        if bad and not st.get("allow_mix_temp"):
            return [f"filaments {bad[0][0]} ({temps[bad[0][0]]:g} C) and {bad[0][1]} ({temps[bad[0][1]]:g} C) are "
                    f"outside each other's temperature range: OrcaSlicer refuses them on one nozzle "
                    f"(FILAMENTS_DIFFERENT_TEMP). Use compatible filaments, or set allow_mix_temp: true (one bed "
                    f"temperature for all)"]
        if bad:
            r["warnings"].append(f"allow_mix_temp: filaments {bad} share one nozzle and one bed outside each other's "
                                 f"temperature range")
        return []
    fmap = r.get("filament_map")
    if not fmap:
        if clash(used):
            r["warnings"].append(f"filaments {clash(used)} have incompatible temperatures: give them different extruders "
                                 f"(filaments[].extruder 'left'/'right'); the automatic map may put them on one nozzle")
        return []
    groups = {}
    for i in used:
        groups.setdefault(fmap[i - 1], []).append(i)
    for ext, ids in groups.items():
        bad = clash(ids)
        if bad and not st.get("allow_mix_temp"):
            return [f"filaments {bad[0][0]} and {bad[0][1]} have incompatible temperatures and share the "
                    f"{'left' if ext == 1 else 'right'} nozzle: put them on different extruders, or set allow_mix_temp: true"]
    return []


def validate(studies, base_dir: str | Path | None = None, vendor: P.Vendor | None = None) -> dict:
    if not isinstance(studies, dict) or not studies:
        return {"_": {"ok": False, "errors": ["give {name: study}"], "warnings": [], "estimate": None}}
    out = {}
    for name, s in studies.items():
        v = validate_study(s, base_dir, vendor)
        if not NAME.match(str(name)):
            v["errors"].insert(0, f"study name '{name}' ({len(str(name))} characters): letters, digits, '_' and '-', "
                                  f"at most 40, starting with a letter or digit")
            v["ok"] = False
        out[name] = v
    return out
