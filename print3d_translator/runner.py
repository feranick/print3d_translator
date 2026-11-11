"""runner.py — one study end to end: validate, resolve profiles, load / orient / check / place the parts,
write the flattened profiles, STLs and assemble list, run OrcaSlicer, fill in the archive's printer id,
check everything independently, and write <name>.json (atomically).
Version: 2026.11.11.2

Study folder <name>/:
  profiles/          machine.json, process.json, filament_<i>.json (flattened copies; never the installed ones)
  objects/<id>.stl   each placed copy, in bed coordinates
  assemble.json      OrcaSlicer's --load-assemble-list (need_arrange false)
  slice/             OrcaSlicer's output folder: plate.gcode.3mf, plate_1.gcode, orca.log, command.json
  <name>.gcode.3mf   the file to print (printer_model_id filled in), <name>.gcode
  state.json         progress (preparing, slicing, checking, done)
  no_supports/       (intent.supports "needed", when supports were needed) the slice without them
  <name>.json        the result
  cancel             (written by cancel) stops OrcaSlicer
"""
from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from pathlib import Path

import numpy as np

from . import __version__
from . import check as C
from . import format as F
from . import gcode as G
from . import gcode3mf as A
from . import geometry as Gm
from . import mesh as M
from . import orca
from . import placement as PL
from . import thumbs
from . import viewer
from . import profiles as P

DEFAULT_TIMEOUT = 600.0


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    return str(o)


def _clean(o):
    """Replace inf/nan (not JSON) by None, recursively."""
    if isinstance(o, float):
        return o if np.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def write_json(path: Path, data) -> None:
    """Atomic: a temporary file, then rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(_clean(json.loads(json.dumps(data, default=_jsonable))), indent=1) + "\n")
    os.replace(tmp, path)


def _state(folder: Path, state: str, **kw) -> None:
    write_json(folder / "state.json", {"state": state, "time": time.time(), **kw})


def timeout_of(study: dict) -> float:
    return float((study.get("run") or {}).get("timeout_s") or os.environ.get("P3DT_TIMEOUT", DEFAULT_TIMEOUT))


def _f(v, default=0.0) -> float:
    return C._f(v, default)


# ---------------------------------------------------------------- preparing (no slicing)

def prepare(name: str, study: dict, folder: Path, base_dir=None, exe: str | None = None) -> dict:
    """Everything up to OrcaSlicer: returns a context with the resolved profiles, placed instances,
    files and the command. Raises ProfileError / GeometryError / PlacementError / ValueError."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    exe = exe or orca.find_orca()
    if not exe:
        raise ValueError("OrcaSlicer not found: install OrcaSlicer 2.4.2, or set P3DT_ORCA to its executable")
    prof = P.find_profiles(exe)
    if not prof:
        opc = P.compiled_profiles_only(exe)
        if opc:
            raise ValueError(f"this OrcaSlicer ({orca.version(exe)}) ships its profiles compiled ({opc}/{P.VENDOR}.opc), "
                             f"which the translator does not read: set P3DT_ORCA_PROFILES to the JSON profiles of the "
                             f"same version (resources/profiles of its source tag; see README 'OrcaSlicer versions')")
        raise ValueError(f"OrcaSlicer's profiles not found next to {exe}: set P3DT_ORCA_PROFILES to its resources/profiles")
    vendor = P.Vendor(prof)
    v = F.validate_study(study, base_dir, vendor)
    if not v["ok"]:
        raise ValueError("; ".join(v["errors"]))
    study, notes = F.expand(study, base_dir)
    if notes or any("_from_3mf" in o for o in study["objects"]):
        write_json(folder / "expanded_study.json", study)
    r = P.resolve(vendor, study)
    F.mix_temperature(study, r)
    warnings = list(dict.fromkeys(r["warnings"] + v["warnings"]))
    machine, process = r["machine"], r["process"]
    n_fil = len(r["filaments"])
    layout = {"overlap": "footprint", "gap": 6.0, "brims_may_overlap": False, **(study.get("layout") or {})}

    bed = C._poly(machine.get("printable_area"))
    height = _f(machine.get("printable_height"), 250)
    ext_areas = [C._poly(a) for a in C._list(machine.get("extruder_printable_area"))] if machine.get("extruder_printable_area") else []
    nozzle = r["model"]["nozzle"]
    first_layer = _f(process.get("initial_layer_print_height"), _f(process.get("layer_height"), 0.2))
    fmap = r["filament_map"]

    def extruders_for(fils):
        if not ext_areas:
            return None
        return sorted({fmap[f - 1] for f in fils}) if fmap else list(range(1, len(ext_areas) + 1))

    def area_for(exts):
        if not exts:
            return bed, "printable area"
        lo = np.max([ext_areas[e - 1].min(axis=0) for e in exts], axis=0)
        hi = np.min([ext_areas[e - 1].max(axis=0) for e in exts], axis=0)
        return np.array([lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]]), f"area extruder(s) {exts} print"

    names = F.object_names(study)
    pkeys = P.process_keys(r)
    objects, instances, hard_bad = {}, [], []
    for k, o in enumerate(study["objects"]):
        oname = names[k]
        parts = _load_parts(o, base_dir, n_fil, oname, warnings)
        counts = [len(q["tri"]) for q in parts]
        tri, rot, R = Gm.orient(np.concatenate([q["tri"] for q in parts]), o.get("orient", "as_is"))
        ptri = np.split(tri, np.cumsum(counts)[:-1])
        settings = {kk: vv for kk, vv in (o.get("settings") or {}).items() if kk in pkeys}
        oproc = {**process, **{kk: str(vv) for kk, vv in settings.items()}}
        sp = C.support_params(lambda kk, d=None: oproc.get(kk, d), nozzle)
        supports = "none" if not sp["supports"] else ("tree" if "tree" in str(C._first(oproc.get("support_type"), "")) else "auto")
        if (study.get("intent") or {}).get("supports") == "needed" and not settings.get("enable_support"):
            supports = "none"
        brim_type = str(C._first(oproc.get("brim_type"), "auto_brim"))
        brim = 0.0 if brim_type == "no_brim" else _f(oproc.get("brim_width"), 5)
        fils = sorted({q["filament"] for q in parts} | {c["filament"] for c in (study.get("intent") or {}).get("color_changes") or []})
        volume = float(sum(q["volume"] for q in parts))
        part = {"tri": tri, "volume": volume, "volume_from": parts[0]["volume_from"] if len(parts) == 1 else
                "+".join(sorted({q["volume_from"] for q in parts})), "area": M.area(tri), "parts_tri": ptri}
        exts = extruders_for(fils)
        area, area_name = area_for(exts)
        pre = Gm.checks(part, area, height, nozzle, sp["angle_deg"], supports, area_name, layer_height=sp["layer_height"],
                        first_layer=_f(oproc.get("initial_layer_print_height"), sp["layer_height"]),
                        line_width=sp["line_width"])
        for cn, ch in pre.items():
            if ch["hard"] and not ch["passed"] and not _accepted(study, cn, oname):
                hard_bad.append(f"object {oname}: {cn}: {ch['why']} (got {ch.get('got')})")
            elif not ch["passed"]:
                warnings.append(f"object {oname}: {cn}: {ch['why']}"
                                + (f" ({'; '.join(ch['problems'][:3])})" if ch.get("problems") else ""))
        size = np.ptp(tri.reshape(-1, 3), axis=0)
        objects[oname] = {"volume_mm3": round(volume, 3), "volume_from": part["volume_from"],
                          "area_mm2": round(part["area"], 2), "size_mm": [round(float(s_), 3) for s_ in size],
                          "orient": o.get("orient", "as_is"), "rotation_matrix": np.round(R, 6).tolist(),
                          "filament": fils[0] if len(fils) == 1 else fils, "copies": int(o.get("copies", 1)),
                          "checks": pre, "centers": []}
        if len(parts) > 1:
            objects[oname]["parts"] = [{"name": q["name"], "filament": q["filament"], "volume_mm3": round(q["volume"], 3)}
                                       for q in parts]
        if settings:
            objects[oname]["settings"] = settings
        if o.get("_from_3mf"):
            objects[oname]["from_3mf"] = o["_from_3mf"]
        halo = layout["gap"] / 2 + (0.0 if layout["brims_may_overlap"] else brim)
        n = int(o.get("copies", 1))
        for c in range(n):
            ident = oname if n == 1 else f"{oname}-{c + 1}"
            instances.append({"id": ident, "object": oname, "tri": tri, "halo": halo, "extruders": exts,
                              "at": o.get("at"), "filaments": fils, "volume": volume, "settings": settings,
                              "parts": [{"name": q["name"], "filament": q["filament"], "tri": t_} for q, t_ in zip(parts, ptri)],
                              "supports": supports})
    if hard_bad:
        raise Gm.GeometryError("; ".join(hard_bad))
    for it in instances:                    # supports spread past the silhouette: more room when placed for them
        if it["at"] is None and it["supports"] != "none":
            it["halo"] += PL.SUPPORT_HALO

    used = sorted({f for i in instances for f in i["filaments"]})
    tower = None
    if len(used) > 1 and str(process.get("enable_prime_tower", "1")) not in ("0", "false"):
        w = _f(process.get("prime_tower_width"), 35)
        tower = {"w": w, "d": w, "halo": 5.0,
                 "extruders": sorted({e for i in instances for e in (i["extruders"] or [])}) or None}
    wz = machine.get("wrapping_exclude_area")
    exclude = C._poly(wz) if wz and len(C._list(wz) if isinstance(wz, str) else wz) >= 3 else None
    plc = PL.place(instances, bed, ext_areas or None, tower, exclude, overlap=layout["overlap"], first_layer=first_layer)
    changes = r["set"]
    if plc["tower"]:
        P._set(process, "wipe_tower_x", [plc["tower"]["x"]], changes, "process")
        P._set(process, "wipe_tower_y", [plc["tower"]["y"]], changes, "process")

    files = P.write(r, folder)
    (folder / "objects").mkdir(exist_ok=True)
    for f_ in (folder / "objects").glob("*.stl"):
        f_.unlink()
    placed, stl_objs, assembled = [], [], {}
    n_assembled = 0
    for inst in instances:
        p = plc["placed"][inst["id"]]
        dx, dy = p["center"]
        ranges = _height_ranges(study, float(M.bbox(p["tri"])[1][2]),
                                _f(inst["settings"].get("layer_height"), _f(process.get("layer_height"), 0.2)))
        objects[inst["object"]]["centers"].append(p["center"])
        if len(inst["parts"]) == 1:
            stl = M.write_stl(folder / "objects" / f"{inst['id']}.stl", p["tri"], name=inst["id"])
            stl_objs.append({"path": stl, "filament": inst["parts"][0]["filament"], "print_params": inst["settings"],
                             "height_ranges": ranges})
            marker = f"{inst['id']}_1"
            part_tris = [(inst["parts"][0]["name"], inst["parts"][0]["filament"], p["tri"])]
        else:
            n_assembled += 1
            part_tris = []
            for j, q in enumerate(inst["parts"]):
                t_ = M.transform(q["tri"], None, (dx, dy, 0.0))
                stl = M.write_stl(folder / "objects" / f"{inst['id']}__p{j + 1}.stl", t_, name=f"{inst['id']}__p{j + 1}")
                stl_objs.append({"path": stl, "filament": q["filament"], "assemble_index": n_assembled})
                part_tris.append((q["name"], q["filament"], t_))
            assembled[n_assembled] = {"print_params": {kk: str(vv) for kk, vv in inst["settings"].items()},
                                      "height_ranges": ranges}
            marker = f"assemble_{n_assembled}"
        placed.append({"name": marker, "id": inst["id"], "object": inst["object"], "filament": inst["filaments"][0],
                       "filaments": inst["filaments"], "tri": p["tri"], "volume": inst["volume"],
                       "settings": inst["settings"], "parts": part_tris})
    write_json(folder / "assemble.json", orca.assemble_list(stl_objs, assembled=assembled))
    slice_dir = folder / "slice"
    if slice_dir.exists():
        shutil.rmtree(slice_dir)
    slice_dir.mkdir()
    argv = orca.command(exe, slice_dir, files["machine"], files["process"], files["filaments"],
                        str(folder / "assemble.json"), r["plate"]["bed_type"], fmap)
    if study.get("allow_mix_temp"):
        argv.append("--allow-mix-temp")
    return {"name": name, "folder": folder, "exe": exe, "vendor": vendor, "resolved": r, "objects": objects,
            "placed": placed, "tower": plc["tower"], "files": files, "argv": argv, "slice_dir": slice_dir,
            "warnings": warnings, "fmap": fmap, "used": used, "study": study, "instances": instances, "layout": {**layout, "strategy": plc.get("strategy")}}


def _load_parts(o: dict, base_dir, n_fil: int, oname: str, warnings: list) -> list[dict]:
    """An object's solids: [{name, tri, filament, volume, volume_from}]. A 3MF part brings its own parts
    and filaments: kept when they differ (mapped by filament_map), else the object's 'filament' applies."""
    specs = o["parts"] if "parts" in o else [{"name": None, "part": o["part"], "filament": o.get("filament")}]
    out = []
    for q in specs:
        lp = Gm.load_part(q["part"], base_dir)
        for note in lp.get("ignored") or []:
            if not any(note in w for w in warnings):       # once per file, not once per object
                warnings.append(f"object {oname}: 3MF: not read: {note}")
        sub = lp.get("parts")
        if sub and len(sub) > 1:
            fm = {int(a): int(b) for a, b in (q["part"].get("filament_map") or {}).items()}
            own = {(fm.get(x["filament"], x["filament"]) if x["filament"] else None) for x in sub}
            if len(own - {None}) <= 1:              # one filament: the object's (or the 3MF's), every part
                one = q.get("filament") or o.get("filament") or (next(iter(own - {None}), None)) or 1
                fm = {x["filament"]: int(one) for x in sub if x["filament"]}
                sub = [{**x, "filament": x["filament"] or int(one)} for x in sub]
            if True:
                if q.get("filament") is not None and "parts" not in o and o.get("filament") is not None:
                    warnings.append(f"object {oname}: the 3MF assigns filaments {sorted(own - {None})} to its parts; "
                                    f"kept ('filament' {o['filament']} applies to parts without one; map them with "
                                    f"part.filament_map)")
                for x in sub:
                    f = fm.get(x["filament"], x["filament"]) if x["filament"] else int(q.get("filament") or 1)
                    if not 1 <= f <= n_fil:
                        raise ValueError(f"object {oname}: 3MF part {x['name']} uses filament {f}; the study has {n_fil}: "
                                         f"add filaments, or map it with part.filament_map {{'{x['filament']}': 1}}")
                    out.append({"name": x["name"], "tri": x["tri"], "filament": f, "volume": abs(M.volume(x["tri"])),
                                "volume_from": "mesh"})
                continue
            if q.get("filament") is None and not o.get("filament") and own - {None}:
                f = own.pop() if len(own) == 1 else 1
                q = {**q, "filament": f if f and 1 <= f <= n_fil else 1}
        name = q.get("name") or (sub[0]["name"] if sub else None) or oname
        out.append({"name": name, "tri": lp["tri"], "filament": int(q.get("filament") or o.get("filament") or 1),
                    "volume": lp["volume"], "volume_from": lp["volume_from"]})
    return out


def _conflict_why(ctx: dict, pairs: list) -> str | None:
    """-101 explained: which objects, how far apart they are, what to do."""
    if not pairs:
        return None
    by_marker = {p["name"]: p for p in ctx["placed"]}
    a, b = (by_marker.get(n) for n in pairs[0])
    if not a or not b:
        return None
    (alo, ahi), (blo, bhi) = M.bbox(a["tri"]), M.bbox(b["tri"])
    reach = float(np.max(np.maximum(blo[:2] - ahi[:2], alo[:2] - bhi[:2]).clip(0))) + 2.0
    fp = C.Footprint(a["tri"], res=0.25, pad=min(reach, 60.0))
    gap = float(fp.distance(b["tri"][:, :, :2].reshape(-1, 2)).min())
    gap_txt = f"{gap:.1f} mm apart" if np.isfinite(gap) else "more than 60 mm apart"
    sup = {i.get("supports") for i in ctx.get("instances", [])} - {"none", None}
    hint = ("with supports on, leave room for them to spread (10 mm or more for tall parts), use supports 'auto' "
            "(normal supports stay closer to the part), or put the parts on separate plates" if sup else
            "their brims or outer walls reach each other: move them apart (with layout.brims_may_overlap the "
            "translator lets brims run into each other; OrcaSlicer may not)")
    return (f"OrcaSlicer stopped (GCODE_PATH_CONFLICTS, -101): the toolpaths of {a['id']} and {b['id']} cross on some "
            f"layer. This is OrcaSlicer's conflict check, not a multi-extruder problem: the two parts, or their "
            f"supports or brims, reach each other. They are {gap_txt} (outline to outline); {hint}.")


def _height_ranges(study: dict, top: float = 10000.0, layer_height: float = 0.2) -> list[dict]:
    """intent.color_changes -> OrcaSlicer height ranges with the filament ('extruder') from each z up to the
    object's top. Each range carries its layer_height: OrcaSlicer reads it from every range (a range
    without one crashes the 2.4.2 CLI)."""
    cc = sorted((study.get("intent") or {}).get("color_changes") or [], key=lambda c: c["z"])
    out = []
    for i, c in enumerate(cc):
        if c["z"] >= top:
            continue
        hi = min(cc[i + 1]["z"], top) if i + 1 < len(cc) else top
        out.append({"min_z": float(c["z"]), "max_z": float(round(hi + 1e-3, 4)),
                    "range_params": {"extruder": str(int(c["filament"])), "layer_height": f"{layer_height:g}"}})
    return out


def apply_accept(study: dict, checks: dict, marker_to_object: dict) -> tuple[list[dict], list[str]]:
    """study.accept: known findings accepted on purpose. unsupported_extrusion: the layers of the named
    object within z_range are left out, and the check passes when what remains is within its limit;
    other checks: the whole check passes. Returns (accepted [{check, object, z_range, reason, covered}],
    warnings for entries that matched nothing). Checks keep 'accepted' and their original got."""
    out, unused = [], []
    for i, a in enumerate(study.get("accept") or []):
        ch = checks.get(a["check"])
        if ch is None:
            unused.append(f"accept[{i}]: no check {a['check']} in this result")
            continue
        if a["check"] == "unsupported_extrusion":
            zr = a.get("z_range") or [-1e9, 1e9]
            covered = [f for f in ch.get("_findings", []) if (a.get("object") in (None, marker_to_object.get(f["object"], f["object"])))
                       and f["z"] is not None and zr[0] - 1e-6 <= f["z"] <= zr[1] + 1e-6]
            if not covered:
                unused.append(f"accept[{i}] (unsupported_extrusion{', ' + a['object'] if a.get('object') else ''}"
                              f"{', z ' + str(zr) if a.get('z_range') else ''}) matched nothing")
                continue
            for f in covered:
                f["accepted"] = True
            out.append({**a, "covered": {"layers": len(covered), "severe_mm": round(sum(f["severe_mm"] for f in covered), 1),
                                         "z": [min(f["z"] for f in covered), max(f["z"] for f in covered)]}})
        else:
            if ch.get("passed"):
                unused.append(f"accept[{i}] ({a['check']}): the check passed; nothing to accept")
                continue
            out.append({**a, "covered": "the whole check"})
            ch["passed"] = True
        ch.setdefault("accepted", []).append({k: a[k] for k in ("object", "z_range", "reason") if k in a})
    ue = checks.get("unsupported_extrusion")
    if ue is not None:
        f_ = ue.pop("_findings", [])
        if ue.get("accepted"):
            left = sum(f["severe_mm"] for f in f_ if not f.get("accepted"))
            ue.setdefault("got", {})["severe_mm_not_accepted"] = round(left, 1)
            ue["passed"] = left <= C.SEVERE_MAX
    return out, unused


def _accepted(study: dict, check: str, obj: str | None = None) -> list[dict]:
    """The accept entries that cover this check (and object)."""
    return [a for a in study.get("accept") or [] if a.get("check") == check and (obj is None or a.get("object") in (None, obj))]


# ---------------------------------------------------------------- the whole study

def run_study(name: str, study: dict, folder: str | Path, base_dir=None) -> dict:
    """One study. intent.supports "needed": sliced without supports first; if unsupported_extrusion fails,
    that result moves to <folder>/no_supports/ and the study is sliced again with tree supports.
    The result's supports_decision says which was kept and why."""
    if (study.get("intent") or {}).get("supports") != "needed":
        return _run_once(name, study, folder, base_dir)
    folder = Path(folder)
    plain = {**study, "intent": {**study["intent"], "supports": "none"}}
    tmp = folder / f"{name}.without_supports.json"         # not <name>.json: status would take it as final
    first = _run_once(name, plain, folder, base_dir, result=tmp)
    tmp.unlink(missing_ok=True)
    ch = (first.get("checks") or {}).get("unsupported_extrusion")
    if not first.get("ok") or not ch or ch.get("passed"):
        first["supports_decision"] = {"mode": "needed", "used": "none",
                                      "why": "nothing has to be held up" if ch and ch.get("passed") else
                                      "not decided: the plate did not slice or was not checked"}
        write_json(folder / f"{name}.json", first)
        return first
    if (folder / "cancel").exists():
        write_json(folder / f"{name}.json", first)
        return first
    _state(folder, "slicing again with supports", why="; ".join(ch.get("problems", [])[:2]))
    keep = folder / "no_supports"
    if keep.exists():
        shutil.rmtree(keep)
    keep.mkdir()
    for f in (f"{name}.gcode.3mf", f"{name}.gcode", f"{name}.view.html"):
        if (folder / f).exists():
            shutil.move(str(folder / f), str(keep / f))
    moved = {k: str(keep / Path(v).name) for k, v in (first.get("files") or {}).items()
             if isinstance(v, str) and (keep / Path(v).name).exists()}
    first["files"] = {**(first.get("files") or {}), **moved}
    first["note"] = "the slice without supports; profiles/, objects/ and slice/ of the folder above are the second slice's"
    write_json(keep / f"{name}.json", first)
    tmp2 = folder / f"{name}.with_supports.json"
    tried = []
    for kind in P.NEEDED_FALLBACKS:                     # tree; normal supports when tree supports reach a neighbour
        second = _run_once(name, {**study, "intent": {**study["intent"], "supports": kind}}, folder, base_dir,
                           result=tmp2)
        tmp2.unlink(missing_ok=True)
        tried.append({"supports": kind, "ok": second.get("ok"), "reliable": second.get("reliable"),
                      **({"why": second.get("why")} if not second.get("ok") else {})})
        if second.get("ok") or (second.get("slicer") or {}).get("code") != -101 or (folder / "cancel").exists():
            break
    e1, e2 = first.get("estimate") or {}, second.get("estimate") or {}
    second["supports_decision"] = {
        "mode": "needed", "used": tried[-1]["supports"] if second.get("ok") else None,
        "why": "without supports: " + "; ".join(ch.get("problems", [])[:3]),
        "tried": tried,
        "without_supports": {"result": str(keep / f"{name}.json"), "severe_mm": (ch.get("got") or {}).get("severe_mm")},
        **({"support_cost": {"filament_g": round((e2.get("filament_g") or 0) - (e1.get("filament_g") or 0), 2),
                             "time_s": round((e2.get("time_s") or 0) - (e1.get("time_s") or 0), 0)}}
           if first.get("ok") and second.get("ok") else {}),
        "seconds_without": (first.get("seconds") or {}).get("total")}
    write_json(folder / f"{name}.json", second)
    return second


def _run_once(name: str, study: dict, folder: str | Path, base_dir=None, result: Path | None = None) -> dict:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    seconds = {}
    res = {"study": name, "translator": __version__, "ok": False, "reliable": False, "unreliable_because": [],
           "warnings": [], "checks": {}, "files": {}}
    try:
        _state(folder, "preparing")
        ctx = prepare(name, study, folder, base_dir)
        seconds["prepare"] = round(time.monotonic() - t0, 2)
        r = ctx["resolved"]
        res["warnings"] = ctx["warnings"]
        res["printer"] = {**r["model"], "plate": r["plate"]["name"], "bed_type": r["plate"]["bed_type"],
                          "build_volume": [*(C._poly(r["machine"].get("printable_area")).max(axis=0).round(1).tolist()),
                                           _f(r["machine"].get("printable_height"))]}
        res["profiles"] = {**r["names"], "orca_version": orca.version(ctx["exe"]), "vendor_version": r["vendor_version"],
                           "set": r["set"], "sha256": ctx["files"]["sha256"], "filament_map": ctx["fmap"]}
        res["objects"] = ctx["objects"]
        res["placement"] = {"tower": ctx["tower"]}
        res["files"]["profiles"] = str(folder / "profiles")

        _state(folder, "slicing", started=time.time(), timeout_s=timeout_of(study))
        t1 = time.monotonic()
        run = orca.run(ctx["argv"], ctx["slice_dir"], timeout=timeout_of(study), cancel_file=folder / "cancel")
        seconds["slice"] = round(time.monotonic() - t1, 2)
        res["slicer"] = {k: run[k] for k in ("returncode", "code", "name", "fix", "seconds", "timed_out", "cancelled")}
        res["files"]["log"] = str(ctx["slice_dir"] / "orca.log")
        res["files"]["command"] = str(ctx["slice_dir"] / "command.json")
        if not run["ok"]:
            why = ("cancelled" if run["cancelled"] else f"timed out after {timeout_of(study):g} s (run.timeout_s)"
                   if run["timed_out"] else f"OrcaSlicer stopped: {run['name']} ({run['code']})"
                   + (f": {run['fix']}" if run["fix"] else ""))
            res["why"] = why
            res["errors"] = [why]
            log = ctx["slice_dir"] / "orca.log"
            if log.exists():
                lt = log.read_text(errors="replace")
                res["log_errors"] = [l[:300] for l in lt.splitlines() if "error" in l.lower()][-15:]
                if run["code"] == -101:
                    res["why"] = res["errors"][0] = _conflict_why(ctx, orca.conflicts(lt)) or why
            return res

        _state(folder, "checking")
        t2 = time.monotonic()
        arch = ctx["slice_dir"] / "plate.gcode.3mf"
        plain = ctx["slice_dir"] / "plate_1.gcode"
        if not arch.exists():
            res["why"] = "OrcaSlicer reported success but wrote no plate.gcode.3mf"
            res["errors"] = [res["why"]]
            return res
        model_id = r["model"]["model_id"]
        pauses = (ctx["study"].get("intent") or {}).get("pauses") or []
        pause_gcode = str(C._first(r["machine"].get("machine_pause_gcode"), "M400 U1") or "M400 U1")
        if pauses:                                  # into the G-code the checks then read: the file printed
            a_ = A.read(arch)
            text, done = A.insert_pauses((a_["gcode"] or b"").decode("utf-8", "replace"), pauses, pause_gcode)
            A.replace_gcode(arch, text)
            if plain.exists():
                plain.write_text(text, encoding="utf-8")
            res["pauses"] = done
        a0 = A.read(arch)
        ids = {o.get("name"): o.get("identify_id") for o in
               ((a0.get("slice_info") or {}).get("plates") or [{}])[0].get("objects", [])}
        head = G.parse("\n".join((a0["gcode"] or b"").decode("utf-8", "replace").splitlines()[:40])).header
        images = None
        try:
            colors = [C._first(f.get("filament_colour"), "#808080") for f in r["filaments"]]
            images = thumbs.render([{"tri": t_, "color": colors[f_ - 1], "identify_id": int(ids.get(p["name"]) or 0)}
                                    for p in ctx["placed"] for (_, f_, t_) in p["parts"]],
                                   C._poly(r["machine"].get("printable_area")))
        except Exception as e:                           # pictures are cosmetic: never fail a print on them
            res["warnings"].append(f"thumbnails not rendered: {type(e).__name__}: {e}")
        fin = A.finalize(arch, model_id, r["model"]["extruders"], head.get("first_layer_time_s"), images)
        patched = fin["changed"]
        a = A.read(arch)
        checks = A.check_archive(a, plain.read_bytes() if plain.exists() else None, model_id, r["model"]["nozzle"],
                                 len(r["filaments"]), ctx["fmap"])
        rt = A.roundtrip(r["machine"], r["process"], r["filaments"], a.get("project") or {}, set(P.orca_keys()["keys"]))
        checks["settings_roundtrip"] = {"passed": rt["passed"], "hard": True,
                                        "why": "every setting we wrote is the one OrcaSlicer sliced with",
                                        "got": {"compared": rt["compared"], "differ": dict(list(rt["differ"].items())[:20]),
                                                "skipped_unknown": rt["skipped_unknown"]}}
        if a["gcode"] is None:
            res["why"] = "the archive has no Metadata/plate_1.gcode"
            res["errors"] = [res["why"]]
            res["checks"] = checks
            return res
        Pg = G.parse(a["gcode"].decode("utf-8", errors="replace"))
        tb = next((o["bbox"] for o in ((a.get("plate_json") or {}).get("bbox_objects") or []) if o.get("name") == "wipe_tower"), None)
        fmap = ctx["fmap"] or [int(_f(x)) for x in ((a.get("project") or {}).get("filament_map") or [])] or None
        gc = C.check_gcode(Pg, r, ctx["placed"], tower_bbox=tb, filament_map=fmap,
                           color_changes=(ctx["study"].get("intent") or {}).get("color_changes"))
        checks.update(gc["checks"])
        if pauses:
            checks["pauses"] = C.check_pauses(a["gcode"].decode("utf-8", errors="replace"), pauses, pause_gcode)
        acc, unused = apply_accept(ctx["study"], checks, {p["name"]: p["object"] for p in ctx["placed"]})
        if acc:
            res["accepted"] = acc
        res["warnings"] += unused
        seconds["check"] = round(time.monotonic() - t2, 2)
        si = ((a.get("slice_info") or {}).get("plates") or [{}])[0]
        res["warnings"] += [f"OrcaSlicer: {w.get('msg')}" for w in si.get("warnings", [])]
        res["warnings"] += [f"check {k}: {v.get('why')}" + (f" ({'; '.join(v['problems'][:3])})" if v.get("problems") else "")
                            for k, v in checks.items() if not v.get("hard", True) and not v.get("passed")]
        res["estimate"] = gc["estimate"]
        res["gcode"] = gc["gcode"]
        res["slice_info"] = {"prediction_s": _f(si.get("metadata", {}).get("prediction"), 0) or None,
                             "weight_g": _f(si.get("metadata", {}).get("weight"), 0) or None,
                             "filaments": si.get("filaments"), "finalized": patched}
        out3mf = folder / f"{name}.gcode.3mf"
        shutil.copyfile(arch, out3mf)
        res["files"]["gcode_3mf"] = str(out3mf)
        if plain.exists():
            shutil.copyfile(plain, folder / f"{name}.gcode")
            res["files"]["gcode"] = str(folder / f"{name}.gcode")
        res["checks"] = checks
        res["expect"] = expectations(study, res)
        reliable, because = C.verdict(checks)
        because += [f"expect {e['quantity']}: got {e['got']}, expected {e['value']} +- {e['tol'] * 100:g}%"
                    for e in res["expect"] if not e["pass"]]
        res["ok"] = True
        res["reliable"] = reliable and all(e["pass"] for e in res["expect"])
        res["unreliable_because"] = because
        if study.get("view"):
            try:
                res["files"]["view"] = viewer.for_study(folder, res)
            except Exception as e:                       # the view is a convenience: never fail a print on it
                res["warnings"].append(f"view not written: {type(e).__name__}: {e}")
    except (P.ProfileError, Gm.GeometryError, PL.PlacementError, ValueError) as e:
        res["why"] = str(e)
        res["errors"] = [str(e)]
    except Exception as e:                                      # report, never crash the pool
        res["why"] = f"internal error: {type(e).__name__}: {e}"
        res["errors"] = [res["why"]]
        (folder / "traceback.txt").write_text(traceback.format_exc())
        res["files"]["traceback"] = str(folder / "traceback.txt")
    finally:
        seconds["total"] = round(time.monotonic() - t0, 2)
        res["seconds"] = seconds
        res["execution"] = {"host": os.uname().nodename if hasattr(os, "uname") else None,
                            "orca": orca.find_orca(), "timeout_s": timeout_of(study)}
        write_json(result or folder / f"{name}.json", res)
        _state(folder, "done", ok=res["ok"], reliable=res["reliable"])
    return res


def lookup(res: dict, path: str):
    cur = res
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def expectations(study: dict, res: dict) -> list[dict]:
    out = []
    for e in study.get("expect") or []:
        got = lookup(res, e["quantity"])
        tol = float(e.get("tol", 0.05))
        ok = isinstance(got, (int, float)) and not isinstance(got, bool) and \
            abs(got - e["value"]) <= tol * max(abs(e["value"]), 1e-12)
        out.append({"quantity": e["quantity"], "value": e["value"], "got": got, "tol": tol, "pass": bool(ok)})
    return out
