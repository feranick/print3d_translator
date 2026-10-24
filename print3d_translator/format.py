"""format.py — the study format (study_format) and static validation (validate): no slicing.
Version: 2026.10.24.6

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
              "description"}
OBJECT_KEYS = {"name", "part", "filament", "orient", "copies", "at"}
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
            "objects": "[{name, part, filament (1), orient ('as_is'), copies (1), at: [x, y] mm}]",
            "part": "{step: path} | {stl: path} | {3mf: path} | {goal: build123d-translator goal} | "
                    "{b3dt: {step: path, volume: mm3}} (a build123d-translator result)",
            "intent": {"preset": f"{'|'.join(P.PRESETS)} (picks the process profile; default standard)",
                       "process": "an exact process profile name instead of preset",
                       "layer_height": "mm", "first_layer_height": "mm", "walls": "wall loops",
                       "top_layers": "n", "bottom_layers": "n", "infill": "%", "infill_pattern":
                       "gyroid|grid|cubic|rectilinear|honeycomb|lightning|crosshatch|...",
                       "supports": "none|auto|tree", "support_angle": "degrees from horizontal (overhangs flatter "
                       "than this get support)", "brim": "none|auto|<width mm>", "seam": "aligned|back|nearest|random"},
            "overrides": "{process: {key: value}, machine: {...}, filament: {'all' | '1' | '2': {...}}}: raw "
                         "OrcaSlicer keys (validated against OrcaSlicer's own keys)",
            "allow_mix_temp": "true: allow filaments with different temperatures on one nozzle (one bed temperature)",
            "expect": "[{quantity: dotted result path (estimate.filament_g, estimate.time_s, gcode.layers, "
                      "objects.<name>.volume_mm3), value, tol (relative, default 0.05)}]",
            "run": "{timeout_s}: OrcaSlicer's time limit for this study (default P3DT_TIMEOUT, 600)"},
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


def validate_study(st, base_dir: str | Path | None = None, vendor: P.Vendor | None = None) -> dict:
    """{ok, errors, warnings, estimate}. Each error says what to change. With `vendor` (OrcaSlicer's
    profiles) the profiles are resolved too."""
    errors, warnings = [], []
    if not isinstance(st, dict):
        return {"ok": False, "errors": ["a study is a JSON object"], "warnings": [], "estimate": None}
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
        bad = set(o) - OBJECT_KEYS
        if bad:
            errors.append(f"{where}: unknown keys {sorted(bad)}; known {sorted(OBJECT_KEYS)}")
        if not NAME.match(names[k]):
            errors.append(f"{where}.name '{names[k]}': letters, digits, '_' and '-' (up to 40), starting with a letter or digit")
        part = o.get("part")
        forms = [f for f in PART_FORMS if isinstance(part, dict) and f in part]
        if len(forms) != 1:
            errors.append(f"{where}.part: exactly one of {{{', '.join(PART_FORMS)}}}")
        else:
            form = forms[0]
            if form in ("step", "stl", "3mf"):
                p = Path(str(part[form])).expanduser()
                if not p.is_absolute() and base_dir:
                    p = Path(base_dir) / p
                if not p.is_file():
                    errors.append(f"{where}.part.{form}: file {p} not found")
                elif not p.name.lower().endswith({"step": (".step", ".stp"), "stl": (".stl",), "3mf": (".3mf",)}[form]):
                    warnings.append(f"{where}.part.{form}: {p.name} does not have a .{form} extension")
            if form in ("step", "b3dt", "goal"):
                try:
                    import build123d  # noqa: F401
                except ImportError:
                    errors.append(f"{where}.part.{form}: needs build123d here (pip install \"print3d-translator[cad]\"), "
                                  f"or give an STL/3MF")
            if form == "b3dt" and not (isinstance(part["b3dt"], dict) and part["b3dt"].get("step")):
                errors.append(f"{where}.part.b3dt: the build123d-translator result's {{'step': path, 'volume': mm3}}")
            if form == "goal":
                try:
                    from build123d_translator.goalformat import validate_goal
                    v = validate_goal(part["goal"])
                    errors += [f"{where}.part.goal: {e}" for e in v.get("errors", [])]
                except ImportError:
                    errors.append(f"{where}.part.goal: build123d-translator is not installed here; give its result "
                                  f"({{'b3dt': {{'step', 'volume'}}}}) instead")
        fi = o.get("filament", 1)
        if not isinstance(fi, int) or isinstance(fi, bool) or not 1 <= fi <= max(1, len(fils)):
            errors.append(f"{where}.filament: a filament number 1..{len(fils)}")
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
    if "allow_mix_temp" in st and not isinstance(st["allow_mix_temp"], bool):
        errors.append("allow_mix_temp: true or false")

    # profiles (only when the rest is sound and OrcaSlicer's profiles are here)
    if not errors:
        if vendor is None:
            warnings.append("OrcaSlicer's profiles were not found here: printer, filament and setting names "
                            "are not checked (install OrcaSlicer 2.4.2 or set P3DT_ORCA_PROFILES)")
        else:
            try:
                r = P.resolve(vendor, st)
                errors += mix_temperature(st, r)
                warnings += r["warnings"]
            except P.ProfileError as e:
                errors.append(str(e))
    estimate = {"minutes": round(0.5 + 0.2 * n_parts, 1), "procs": 1, "mem_mb": 1500} if not errors else None
    return {"ok": not errors, "errors": errors, "warnings": warnings, "estimate": estimate}


def mix_temperature(st: dict, r: dict) -> list[str]:
    """OrcaSlicer refuses filaments with different temperatures on one nozzle (-62) unless allowed.
    Returns errors (one-nozzle printers) and appends warnings to r['warnings'] (H2D)."""
    fils = r["filaments"]
    used = sorted({int(o.get("filament", 1)) for o in st.get("objects") or []})
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
            v["errors"].insert(0, f"study name '{name}': letters, digits, '_' and '-' (up to 40)")
            v["ok"] = False
        out[name] = v
    return out
