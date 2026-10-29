"""runner.py — one study end to end: validate, resolve profiles, load / orient / check / place the parts,
write the flattened profiles, STLs and assemble list, run OrcaSlicer, fill in the archive's printer id,
check everything independently, and write <name>.json (atomically).
Version: 2026.10.29.1

Study folder <name>/:
  profiles/          machine.json, process.json, filament_<i>.json (flattened copies; never the installed ones)
  objects/<id>.stl   each placed copy, in bed coordinates
  assemble.json      OrcaSlicer's --load-assemble-list (need_arrange false)
  slice/             OrcaSlicer's output folder: plate.gcode.3mf, plate_1.gcode, orca.log, command.json
  <name>.gcode.3mf   the file to print (printer_model_id filled in), <name>.gcode
  state.json         progress (preparing, slicing, checking, done)
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
        raise ValueError(f"OrcaSlicer's profiles not found next to {exe}: set P3DT_ORCA_PROFILES to its resources/profiles")
    vendor = P.Vendor(prof)
    v = F.validate_study(study, base_dir, vendor)
    if not v["ok"]:
        raise ValueError("; ".join(v["errors"]))
    r = P.resolve(vendor, study)
    F.mix_temperature(study, r)
    warnings = list(dict.fromkeys(r["warnings"]))
    machine, process = r["machine"], r["process"]

    bed = C._poly(machine.get("printable_area"))
    height = _f(machine.get("printable_height"), 250)
    ext_areas = [C._poly(a) for a in C._list(machine.get("extruder_printable_area"))] if machine.get("extruder_printable_area") else []
    nozzle = r["model"]["nozzle"]
    supports = "none" if str(process.get("enable_support", "0")) in ("0", "false") else \
        ("tree" if "tree" in str(process.get("support_type", "")) else "auto")
    angle = _f(process.get("support_threshold_angle"), 30)
    sp = C.support_params(lambda k, d=None: process.get(k, d), nozzle)
    brim_type = str(C._first(process.get("brim_type"), "auto_brim"))
    brim = 0.0 if brim_type == "no_brim" else _f(process.get("brim_width"), 5)
    halo = 3.0 + brim
    fmap = r["filament_map"]

    def extruders_for(fil):
        if not ext_areas:
            return None
        return [fmap[fil - 1]] if fmap else list(range(1, len(ext_areas) + 1))

    def area_for(exts):
        if not exts:
            return bed, "printable area"
        lo = np.max([ext_areas[e - 1].min(axis=0) for e in exts], axis=0)
        hi = np.min([ext_areas[e - 1].max(axis=0) for e in exts], axis=0)
        return np.array([lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]]), f"area extruder(s) {exts} print"

    objects, instances, hard_bad = {}, [], []
    for k, o in enumerate(study["objects"]):
        oname = F.object_names(study)[k]
        part = Gm.load_part(o["part"], base_dir)
        tri, rot, R = Gm.orient(part["tri"], o.get("orient", "as_is"))
        part = {**part, "tri": tri, "area": M.area(tri)}
        fil = int(o.get("filament", 1))
        exts = extruders_for(fil)
        area, area_name = area_for(exts)
        pre = Gm.checks(part, area, height, nozzle, angle, supports, area_name, layer_height=sp["layer_height"],
                        first_layer=_f(process.get("initial_layer_print_height"), sp["layer_height"]),
                        line_width=sp["line_width"])
        for cn, ch in pre.items():
            if ch["hard"] and not ch["passed"]:
                hard_bad.append(f"object {oname}: {cn}: {ch['why']} (got {ch.get('got')})")
            elif not ch["passed"]:
                warnings.append(f"object {oname}: {cn}: {ch['why']}")
        size = np.ptp(tri.reshape(-1, 3), axis=0)
        objects[oname] = {"volume_mm3": round(part["volume"], 3), "volume_from": part["volume_from"],
                          "area_mm2": round(part["area"], 2), "size_mm": [round(float(s), 3) for s in size],
                          "orient": o.get("orient", "as_is"), "rotation_matrix": np.round(R, 6).tolist(),
                          "filament": fil, "copies": int(o.get("copies", 1)), "checks": pre, "centers": []}
        n = int(o.get("copies", 1))
        for c in range(n):
            ident = oname if n == 1 else f"{oname}-{c + 1}"
            instances.append({"id": ident, "object": oname, "tri": tri, "halo": halo, "extruders": exts,
                              "at": o.get("at"), "filament": fil, "volume": part["volume"]})
    if hard_bad:
        raise Gm.GeometryError("; ".join(hard_bad))

    used = sorted({i["filament"] for i in instances})
    tower = None
    if len(used) > 1 and str(process.get("enable_prime_tower", "1")) not in ("0", "false"):
        w = _f(process.get("prime_tower_width"), 35)
        tower = {"w": w, "d": w, "halo": 5.0,
                 "extruders": sorted({e for i in instances for e in (i["extruders"] or [])}) or None}
    wz = machine.get("wrapping_exclude_area")
    exclude = C._poly(wz) if wz and len(C._list(wz) if isinstance(wz, str) else wz) >= 3 else None
    plc = PL.place(instances, bed, ext_areas or None, tower, exclude)
    changes = r["set"]
    if plc["tower"]:
        P._set(process, "wipe_tower_x", [plc["tower"]["x"]], changes, "process")
        P._set(process, "wipe_tower_y", [plc["tower"]["y"]], changes, "process")

    files = P.write(r, folder)
    (folder / "objects").mkdir(exist_ok=True)
    placed, stl_objs = [], []
    for inst in instances:
        p = plc["placed"][inst["id"]]
        stl = M.write_stl(folder / "objects" / f"{inst['id']}.stl", p["tri"], name=inst["id"])
        stl_objs.append({"path": stl, "filament": inst["filament"]})
        objects[inst["object"]]["centers"].append(p["center"])
        placed.append({"name": f"{inst['id']}_1", "object": inst["object"], "filament": inst["filament"],
                       "tri": p["tri"], "volume": inst["volume"]})
    write_json(folder / "assemble.json", orca.assemble_list(stl_objs))
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
            "warnings": warnings, "fmap": fmap, "used": used}


# ---------------------------------------------------------------- the whole study

def run_study(name: str, study: dict, folder: str | Path, base_dir=None) -> dict:
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
                res["log_errors"] = [l[:300] for l in log.read_text(errors="replace").splitlines()
                                     if "error" in l.lower()][-15:]
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
        a0 = A.read(arch)
        ids = {o.get("name"): o.get("identify_id") for o in
               ((a0.get("slice_info") or {}).get("plates") or [{}])[0].get("objects", [])}
        head = G.parse("\n".join((a0["gcode"] or b"").decode("utf-8", "replace").splitlines()[:40])).header
        images = None
        try:
            colors = [C._first(f.get("filament_colour"), "#808080") for f in r["filaments"]]
            images = thumbs.render([{"tri": p["tri"], "color": colors[p["filament"] - 1],
                                     "identify_id": int(ids.get(p["name"]) or 0)} for p in ctx["placed"]],
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
        gc = C.check_gcode(Pg, r, ctx["placed"], tower_bbox=tb, filament_map=fmap)
        checks.update(gc["checks"])
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
        write_json(folder / f"{name}.json", res)
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
