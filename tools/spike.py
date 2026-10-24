#!/usr/bin/env python3
"""spike.py — runs OrcaSlicer on small cases to learn what the CLI really does (spikes S2, S3, S4):
positions from the assemble list, filament mapping on the H2D, the .gcode.3mf layout and md5, and
whether the settings sliced (project_settings.config) equal our flattened profiles.
Version: 2026.10.24.3

    python tools/spike.py [case ...]        cases: p2s_cube, p2s_cube_minsave, h2d_two_pla, h2d_pla_petg,
                                            and CRASH_CASES (narrowing down the PLA + PETG crash)
    python tools/spike.py crash             only the CRASH_CASES

Writes debug/spike/<case>/ (gitignored) and prints a short summary. Nothing is sent anywhere.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from print3d_translator import mesh, orca, profiles as P

ROOT = Path(__file__).resolve().parents[1] / "debug" / "spike"

CASES = {
    # one 20 mm cube, deliberately off-centre at (60, 80): does it stay there?
    "p2s_cube": {"printer": {"model": "P2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}],
                 "objects": [{"size": [20, 20, 20], "at": [60, 80], "filament": 1}]},
    "p2s_cube_minsave": {"printer": {"model": "P2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}],
                         "objects": [{"size": [20, 20, 20], "at": [60, 80], "filament": 1}], "min_save": True},
    # two cubes, two PLAs, filament 1 on the right extruder, filament 2 on the left
    "h2d_two_pla": {"printer": {"model": "H2D", "nozzle": 0.4},
                    "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right", "color": "#FF0000"},
                                  {"profile": "Bambu PLA Matte", "extruder": "left", "color": "#0000FF"}],
                    "objects": [{"size": [20, 20, 10], "at": [250, 160], "filament": 1},
                                {"size": [20, 20, 10], "at": [100, 160], "filament": 2}]},
    # PLA + PETG on one plate (different temperatures): does the CLI refuse (-62) or slice?
    "h2d_pla_petg": {"printer": {"model": "H2D", "nozzle": 0.4},
                     "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right"},
                                   {"profile": "Bambu PETG HF", "extruder": "left"}],
                     "objects": [{"size": [20, 20, 10], "at": [250, 160], "filament": 1},
                                 {"size": [20, 20, 10], "at": [100, 160], "filament": 2}]},
}
# narrowing down the h2d_pla_petg crash (SIGSEGV in OrcaSlicer 2.4.2): which ingredient triggers it?
_TWO = [{"size": [20, 20, 10], "at": [250, 160], "filament": 1}, {"size": [20, 20, 10], "at": [100, 160], "filament": 2}]
CRASH_CASES = {
    "h2d_petg_petg": {"printer": {"model": "H2D"}, "objects": _TWO,          # two PETGs: is it PETG itself?
                      "filaments": [{"profile": "Bambu PETG HF", "extruder": "right"},
                                    {"profile": "Bambu PETG Basic", "extruder": "left"}]},
    "h2d_pla_petg_auto": {"printer": {"model": "H2D"}, "objects": _TWO,      # no manual filament map
                          "filaments": [{"profile": "Bambu PLA Basic"}, {"profile": "Bambu PETG HF"}]},
    "h2d_pla_petg_mixtemp": {"printer": {"model": "H2D"}, "objects": _TWO, "extra": ["--allow-mix-temp"],
                             "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right"},
                                           {"profile": "Bambu PETG HF", "extruder": "left"}]},
    "h2d_pla_petg_generic": {"printer": {"model": "H2D"}, "objects": _TWO,   # generic PETG profile instead
                             "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right"},
                                           {"profile": "Generic PETG", "extruder": "left"}]},
    "h2d_petg_only": {"printer": {"model": "H2D"}, "objects": _TWO[:1],     # one PETG object alone
                      "filaments": [{"profile": "Bambu PETG HF", "extruder": "right"}]},
    "p2s_pla_petg": {"printer": {"model": "P2S"},                           # the same mix on a one-nozzle printer
                     "objects": [{"size": [20, 20, 10], "at": [80, 128], "filament": 1},
                                 {"size": [20, 20, 10], "at": [176, 128], "filament": 2}],
                     "filaments": [{"profile": "Bambu PLA Basic"}, {"profile": "Bambu PETG HF"}]},
}
CASES.update(CRASH_CASES)

META = {"name", "from", "inherits", "setting_id", "instantiation", "type", "compatible_printers",
        "compatible_prints", "filament_id", "description", "version", "renamed_from", "url"}


def run_case(name: str, case: dict, exe: str, vendor: P.Vendor) -> dict:
    d = ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*"):
        if f.is_file():
            f.unlink()
    r = P.resolve(vendor, case)
    files = P.write(r, d)
    objs = []
    for i, o in enumerate(case["objects"], start=1):
        tri = mesh.place(mesh.box(*o["size"]), o["at"])
        p = mesh.write_stl(d / f"object_{i}.stl", tri)
        objs.append({"path": p, "filament": o["filament"]})
    (d / "assemble.json").write_text(json.dumps(orca.assemble_list(objs), indent=1))
    argv = orca.command(exe, d, files["machine"], files["process"], files["filaments"], str(d / "assemble.json"),
                        r["plate"]["bed_type"], r["filament_map"], min_save=case.get("min_save", False))
    argv += case.get("extra", [])
    res = orca.run(argv, d, timeout=600)
    s = {"case": name, "ok": res["ok"], "exit": {k: res[k] for k in ("returncode", "code", "name", "fix", "seconds")},
         "profiles": r["names"], "filament_map": r["filament_map"],
         "outdir": sorted(f.name for f in d.iterdir())}
    if not res["ok"]:
        s["stderr_tail"] = res["stderr"][-1500:]
        s["log_errors"] = [l for l in (d / "orca.log").read_text(errors="replace").splitlines()
                           if "error" in l.lower()][-20:] if (d / "orca.log").exists() else []
        return s
    z = d / "plate.gcode.3mf"
    with zipfile.ZipFile(z) as zf:
        s["zip"] = {i.filename: i.file_size for i in zf.infolist()}
        g = zf.read("Metadata/plate_1.gcode")
        md5 = zf.read("Metadata/plate_1.gcode.md5").decode().strip() if "Metadata/plate_1.gcode.md5" in zf.namelist() else None
        s["md5"] = {"file": md5, "computed_upper": hashlib.md5(g).hexdigest().upper(), "match": md5 == hashlib.md5(g).hexdigest().upper()}
        plain = d / "plate_1.gcode"
        s["plain_gcode_identical"] = plain.exists() and plain.read_bytes() == g
        s["slice_info"] = zf.read("Metadata/slice_info.config").decode(errors="replace")[:3000]
        for extra in ("Metadata/plate_1.json", "Metadata/model_settings.config"):
            if extra in zf.namelist():
                s[extra.split("/")[-1]] = zf.read(extra).decode(errors="replace")[:2500]
        proj = json.loads(zf.read("Metadata/project_settings.config")) if "Metadata/project_settings.config" in zf.namelist() else {}
    s["roundtrip"] = roundtrip(r, proj)
    s["gcode"] = gcode_summary(g.decode(errors="replace"), case)
    return s


def roundtrip(r: dict, proj: dict) -> dict:
    """Keys of our flattened machine/process (and filament 1) whose value differs in project_settings."""
    out = {"keys_in_project": len(proj)}
    for kind in ("machine", "process"):
        diff = {k: [v, proj.get(k, "<missing>")] for k, v in r[kind].items()
                if k not in META and proj.get(k, "<missing>") != v}
        out[kind] = {"checked": len([k for k in r[kind] if k not in META]), "differ": len(diff),
                     "examples": dict(list(diff.items())[:25])}
    f1 = r["filaments"][0]
    diff = {}
    for k, v in f1.items():
        if k in META:
            continue
        pv = proj.get(k, "<missing>")
        if pv != v and not (isinstance(pv, list) and isinstance(v, list) and pv[:len(v)] == v):
            diff[k] = [v, pv]
    out["filament_1"] = {"checked": len([k for k in f1 if k not in META]), "differ": len(diff),
                         "examples": dict(list(diff.items())[:25])}
    for k in ("filament_map", "filament_map_mode", "curr_bed_type", "printer_model", "print_settings_id",
              "printer_settings_id", "filament_settings_id", "inherits_group", "different_settings_to_system"):
        out.setdefault("selected", {})[k] = proj.get(k)
    return out


def gcode_summary(text: str, case: dict) -> dict:
    lines = text.splitlines()
    head = []
    for l in lines:
        head.append(l)
        if l.startswith("; HEADER_BLOCK_END"):
            break
    layer_idx = [i for i, l in enumerate(lines) if l.startswith("; CHANGE_LAYER")]
    first = layer_idx[0] if layer_idx else 0
    x = y = z = 0.0
    rel = False
    tool = None
    ext = {}
    zs = set()
    feats = {}
    feat = None
    temps = []
    tools = []
    arcs = 0
    num = re.compile(r"([XYZEIJF])(-?\d*\.?\d+)")
    for i, l in enumerate(lines):
        c = l.split(";", 1)[0].strip()
        if l.startswith("; FEATURE:"):
            feat = l.split(":", 1)[1].strip()
        if not c:
            continue
        w = c.split()[0]
        if w in ("M83",):
            rel = True
        if w in ("M104", "M109", "M140", "M190") and len(temps) < 30:
            temps.append(f"{i}: {c}")
        if re.fullmatch(r"T\d+", w) or w in ("M620", "M621"):
            if len(tools) < 40:
                tools.append(f"{i}: {c}")
            if re.fullmatch(r"T\d+", w) and int(w[1:]) < 255:
                tool = int(w[1:])
        if w in ("G0", "G1", "G2", "G3"):
            p = dict((k, float(v)) for k, v in num.findall(c))
            arcs += w in ("G2", "G3")
            x, y, z = p.get("X", x), p.get("Y", y), p.get("Z", z)
            if p.get("E", 0) > 0 and ("X" in p or "Y" in p) and i > first:
                e = ext.setdefault(str(tool), [1e9, 1e9, -1e9, -1e9, 0.0])
                e[0], e[1], e[2], e[3] = min(e[0], x), min(e[1], y), max(e[2], x), max(e[3], y)
                e[4] += p["E"]
                zs.add(round(z, 3))
                feats[feat] = feats.get(feat, 0) + 1
    return {"header": head[:40], "lines": len(lines), "change_layers": len(layer_idx),
            "distinct_print_z": len(zs), "z_min": min(zs) if zs else None, "z_max": max(zs) if zs else None,
            "relative_e": rel, "arcs": arcs,
            "extrusion_bbox_by_tool": {t: [round(v, 2) for v in b] for t, b in ext.items()},
            "expected_objects": [{"at": o["at"], "size": o["size"], "filament": o["filament"]} for o in case["objects"]],
            "features": feats, "temps": temps, "tool_lines": tools,
            "start_gcode_first_moves": [f"{i}: {l}" for i, l in enumerate(lines[:first]) if l.startswith(("G0", "G1"))][:15]}


def main() -> None:
    exe = orca.find_orca()
    if not exe:
        sys.exit("OrcaSlicer not found (set P3DT_ORCA)")
    prof = P.find_profiles(exe)
    print(f"OrcaSlicer {orca.version(exe)} at {exe}; profiles {prof}")
    vendor = P.Vendor(prof)
    names = sys.argv[1:] or list(CASES)
    if names == ["crash"]:
        names = list(CRASH_CASES)
    allsum = {}
    for n in names:
        s = run_case(n, CASES[n], exe, vendor)
        allsum[n] = s
        (ROOT / n / "summary.json").write_text(json.dumps(s, indent=1))
        line = f"{n}: {'OK' if s['ok'] else 'FAILED'} {s['exit']['name']} in {s['exit']['seconds']} s"
        if s["ok"]:
            g = s["gcode"]
            line += (f"; md5 {s['md5']['match']}; layers {g['change_layers']}; z {g['z_min']}..{g['z_max']}; "
                     f"bbox by tool {g['extrusion_bbox_by_tool']}; roundtrip differ: machine "
                     f"{s['roundtrip']['machine']['differ']}, process {s['roundtrip']['process']['differ']}, "
                     f"filament {s['roundtrip']['filament_1']['differ']}")
        print(line)
    (ROOT / "all.json").write_text(json.dumps(allsum, indent=1))
    print(f"details: {ROOT}/<case>/summary.json")


if __name__ == "__main__":
    main()
