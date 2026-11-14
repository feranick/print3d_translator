#!/usr/bin/env python3
"""compare_bambu.py — a .gcode.3mf exported by OrcaSlicer's or Bambu Studio's GUI ("Export plate sliced
file") against p3dt's for the same plate. Two parts:

  packaging (spike S4)  archive members, slice_info.config, model_settings.config, plate_1.json,
                        project_settings.config keys, the G-code header block
  multi-filament        (P2S + AMS) each filament's type, colour, grams and metres; filament_maps and
                        layer_filament_lists (slice_info); filament_sequence.json; the tool changes and the
                        filaments printed on each layer (read from the G-code); firmware flushes per
                        filament; the prime tower (size, volume per filament); nozzle and bed temperatures;
                        the multi-filament settings (flush matrix, prime tower, colours). Each row gets a
                        verdict: same, close (within tolerance), differs, or info (expected to differ:
                        positions, names).
Version: 2026.11.14.2

    python tools/compare_bambu.py GUI.gcode.3mf OURS.gcode.3mf [--out report.json] [--html report.html]

Read-only. Prints a short report and writes the full one as JSON and as a self-contained HTML page
(default: next to the GUI file, <name>.compare.json / .compare.html). Nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from print3d_translator import __version__  # noqa: E402
from print3d_translator import gcode as G  # noqa: E402
from print3d_translator import gcode3mf as A  # noqa: E402

# multi-filament settings compared one by one (project_settings.config)
MF_SETTINGS = ["filament_settings_id", "filament_colour", "filament_type", "nozzle_temperature",
               "nozzle_temperature_initial_layer", "flush_volumes_matrix", "flush_volumes_vector", "flush_multiplier",
               "enable_prime_tower", "prime_tower_width", "prime_tower_brim_width", "prime_volume",
               "prime_tower_infill_gap", "wipe_tower_rotation_angle", "wipe_tower_bridging", "wipe_tower_cone_angle",
               "purge_in_prime_tower", "flush_into_infill", "flush_into_objects", "flush_into_support",
               "single_extruder_multi_material", "filament_map", "filament_map_mode", "curr_bed_type",
               "print_sequence", "print_settings_id", "printer_settings_id", "layer_height",
               "initial_layer_print_height", "bed_temperature_formula"]
POSITIONAL = {"wipe_tower_x", "wipe_tower_y"}
TOL = {"grams": 0.03, "metres": 0.03, "flush": 0.05, "time": 0.05, "tower_mm": 1.0, "volume": 0.05}


def header_block(gcode: bytes) -> list[str]:
    out = []
    for line in gcode.decode("utf-8", "replace").splitlines():
        out.append(line)
        if line.startswith("; HEADER_BLOCK_END") or len(out) > 60:
            break
    return out


def side_by_side(a: dict, b: dict) -> dict:
    keys = sorted(set(a) | set(b))
    return {k: {"bambu": a.get(k), "ours": b.get(k)} for k in keys if a.get(k) != b.get(k)}


def compare(bambu: str, ours: str) -> dict:
    A_, B_ = A.read(bambu), A.read(ours)
    rep = {"files": {"bambu": bambu, "ours": ours}}
    na, nb = set(A_["names"]), set(B_["names"])
    rep["archive"] = {"only_bambu": sorted(na - nb), "only_ours": sorted(nb - na)}
    with zipfile.ZipFile(bambu) as z:
        rep["archive"]["bambu_sizes"] = {i.filename: i.file_size for i in z.infolist()}
    sa = (A_.get("slice_info") or {}).get("plates", [{}])[0]
    sb = (B_.get("slice_info") or {}).get("plates", [{}])[0]
    rep["slice_info"] = {
        "header": side_by_side((A_.get("slice_info") or {}).get("header", {}), (B_.get("slice_info") or {}).get("header", {})),
        "plate_metadata": side_by_side(sa.get("metadata", {}), sb.get("metadata", {})),
        "filament_attributes": {"bambu": sorted({k for f in sa.get("filaments", []) for k in f}),
                                "ours": sorted({k for f in sb.get("filaments", []) for k in f})},
        "filaments": {"bambu": sa.get("filaments"), "ours": sb.get("filaments")},
        "objects": {"bambu": sa.get("objects"), "ours": sb.get("objects")},
        "warnings": {"bambu": sa.get("warnings"), "ours": sb.get("warnings")},
        "other_elements": {"bambu": [k for k in sa if k not in ("metadata", "objects", "filaments", "warnings", "nozzles")],
                           "ours": [k for k in sb if k not in ("metadata", "objects", "filaments", "warnings", "nozzles")]}}
    with zipfile.ZipFile(bambu) as z:
        raw_si = z.read("Metadata/slice_info.config").decode("utf-8", "replace") if "Metadata/slice_info.config" in z.namelist() else ""
    rep["slice_info"]["bambu_raw"] = raw_si[:6000]
    ma = (A_.get("model_settings") or {})
    mb = (B_.get("model_settings") or {})
    rep["model_settings"] = {"plate": side_by_side((ma.get("plates") or [{}])[0], (mb.get("plates") or [{}])[0])}
    pa, pb = A_.get("plate_json") or {}, B_.get("plate_json") or {}
    rep["plate_json"] = {"only_bambu": sorted(set(pa) - set(pb)), "only_ours": sorted(set(pb) - set(pa)),
                         "differ": {k: {"bambu": pa[k], "ours": pb[k]} for k in set(pa) & set(pb)
                                    if pa[k] != pb[k] and k != "bbox_objects"}}
    ja, jb = A_.get("project") or {}, B_.get("project") or {}
    picks = ["printer_model", "printer_settings_id", "print_settings_id", "filament_settings_id", "curr_bed_type",
             "layer_height", "nozzle_diameter", "filament_map", "filament_map_mode", "printer_extruder_variant",
             "nozzle_volume_type", "extruder_type", "default_print_profile", "inherits_group", "version",
             "different_settings_to_system", "wipe_tower_x", "wipe_tower_y", "enable_prime_tower"]
    rep["project_settings"] = {"n_keys": {"bambu": len(ja), "ours": len(jb)},
                               "only_bambu": sorted(set(ja) - set(jb)), "only_ours": sorted(set(jb) - set(ja)),
                               "selected": {k: {"bambu": ja.get(k), "ours": jb.get(k)} for k in picks}}
    ga, gb = A_.get("gcode") or b"", B_.get("gcode") or b""
    rep["gcode"] = {"header": {"bambu": header_block(ga), "ours": header_block(gb)},
                    "md5_ok": {"bambu": A_["md5_file"] == A.md5_upper(ga) if ga else None,
                               "ours": B_["md5_file"] == A.md5_upper(gb) if gb else None},
                    "bambu_has_thumbnail_block": b"; THUMBNAIL_BLOCK_START" in ga[:200000],
                    "ours_has_thumbnail_block": b"; THUMBNAIL_BLOCK_START" in gb[:200000],
                    "lines": {"bambu": ga.count(b"\n"), "ours": gb.count(b"\n")},
                    "layers": {"bambu": ga.count(b"; CHANGE_LAYER"), "ours": gb.count(b"; CHANGE_LAYER")}}
    m = re.search(rb"; total estimated time: ([^\n;]+)", ga)
    rep["gcode"]["bambu_time"] = m.group(1).decode() if m else None
    return rep


# ---------------------------------------------------------------- multi-filament: one archive

def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _float(v, default=None):
    try:
        return float(_first(v))
    except (TypeError, ValueError):
        return default


def _ranges(by_layer: dict) -> list[dict]:
    """{layer: set of filaments} -> [{filaments, layers: [first, last]}] for runs of equal sets."""
    out = []
    for layer in sorted(by_layer):
        f = sorted(by_layer[layer])
        if out and out[-1]["filaments"] == f and out[-1]["layers"][1] == layer - 1:
            out[-1]["layers"][1] = layer
        else:
            out.append({"filaments": f, "layers": [layer, layer]})
    return out


def filament_facts(path: str) -> dict:
    """What one .gcode.3mf says about its filaments: slice_info, plate_1.json, filament_sequence.json,
    project settings, and our own reading of the G-code. Filament numbers are 1-based throughout."""
    a = A.read(path)
    out = {"path": str(path)}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        raw_si = z.read("Metadata/slice_info.config") if "Metadata/slice_info.config" in names else b""
        seq = z.read("Metadata/filament_sequence.json") if "Metadata/filament_sequence.json" in names else None
    si = (a.get("slice_info") or {}).get("plates") or [{}]
    md = si[0].get("metadata", {})
    out["slice_info"] = {
        "client": (a.get("slice_info") or {}).get("header", {}),
        "metadata": {k: md.get(k) for k in ("printer_model_id", "nozzle_diameters", "prediction", "weight",
                                            "filament_maps", "limit_filament_maps", "enable_filament_dynamic_map",
                                            "has_filament_switcher", "support_used", "label_object_enabled")},
        "filaments": [{k: f.get(k) for k in ("id", "type", "color", "tray_info_idx", "used_m", "used_g",
                                             "used_for_object", "used_for_support")} for f in si[0].get("filaments", [])],
        "objects": [o.get("name") for o in si[0].get("objects", [])],
        "warnings": [w.get("msg") for w in si[0].get("warnings", [])]}
    lfl = []
    if raw_si:
        for e in ET.fromstring(raw_si).iter("layer_filament_list"):
            fl = [int(x) + 1 for x in (e.get("filament_list") or "").split()]
            lr = [int(x) for x in (e.get("layer_ranges") or "").split()]
            lfl.append({"filaments": fl, "layers": lr})
    out["slice_info"]["layer_filament_lists"] = lfl
    try:
        out["filament_sequence"] = json.loads(seq) if seq else None
    except ValueError:
        out["filament_sequence"] = None
    pj = a.get("plate_json") or {}
    tower = next((o for o in pj.get("bbox_objects") or [] if o.get("name") == "wipe_tower"), None)
    out["plate_json"] = {"filament_colors": pj.get("filament_colors"), "filament_ids": pj.get("filament_ids"),
                         "first_extruder": pj.get("first_extruder"),
                         "objects": [o.get("name") for o in pj.get("bbox_objects") or [] if o.get("name") != "wipe_tower"]}
    if tower:
        b = tower["bbox"]
        out["tower"] = {"bbox": [round(v, 2) for v in b], "size": [round(b[2] - b[0], 2), round(b[3] - b[1], 2)]}
    else:
        out["tower"] = None
    proj = a.get("project") or {}
    out["settings"] = {k: proj.get(k) for k in MF_SETTINGS + sorted(POSITIONAL)}
    out["gcode"] = _gcode_facts(a.get("gcode") or b"", proj)
    return out


def _gcode_facts(gcode: bytes, proj: dict) -> dict:
    if not gcode:
        return {}
    P = G.parse(gcode.decode("utf-8", errors="replace"))
    diam = [_float(d, 1.75) for d in (proj.get("filament_diameter") or ["1.75"])]
    dens = [_float(d, 1.24) for d in (proj.get("filament_density") or ["1.24"])]
    info, seg = P.seg_info, P.segments
    printing = info[:, 0] == 1 if len(info) else np.zeros(0, bool)
    tower_f = [i for i, f in enumerate(P.features) if f and "prime tower" in f.lower()]
    is_tower = np.isin(info[:, 2], tower_f) if len(info) else np.zeros(0, bool)
    base = int(info[printing, 4].min()) if printing.any() else 0
    by_layer, tower_layers = {}, {}
    if printing.any():
        for layer, tool in np.unique(info[printing & ~is_tower][:, [4, 1]], axis=0):
            by_layer.setdefault(int(layer) - base, set()).add(int(tool) + 1)
        for layer, tool in np.unique(info[printing & is_tower][:, [4, 1]], axis=0):
            tower_layers.setdefault(int(layer) - base, set()).add(int(tool) + 1)
    vol = {}
    for (r, t, f, o), v in G.extrusion_table(P, diam).items():
        if r != "print":
            continue
        d = vol.setdefault(t + 1, {"part": 0.0, "tower": 0.0, "support": 0.0})
        key = "tower" if "prime tower" in f.lower() else "support" if "support" in f.lower() else "part"
        d[key] += v["volume_mm3"]
    for d in vol.values():
        for k in d:
            d[k] = round(d[k], 1)
    changes = [{"layer": t["layer"] - 1 - base, "filament": t["tool"] + 1} for t in P.tool_changes
               if t["region"] == "print" and not t["conditional"] and t["tool"] is not None and t["tool"] >= 0]
    temps = {}
    for t in P.temps:
        if t["conditional"] or t["region"] == "end" or (t["region"] == "start" and t["kind"] != "bed"):
            continue
        if t["kind"] in ("nozzle", "nozzle_change") and t["tool"] is not None:
            temps.setdefault(f"nozzle F{t['tool'] + 1}", set()).add(t["value"])
        elif t["kind"] == "bed":
            temps.setdefault("bed", set()).add(t["value"])
    flush = {}
    for t, mm in sorted(P.e_flush.items()):
        i = int(t)
        g = mm * G.filament_area(diam[i] if i < len(diam) else 1.75) * (dens[i] if i < len(dens) else 1.24) / 1000
        flush[i + 1] = {"mm": round(mm, 1), "g": round(g, 2)}
    zs = G.print_z_levels(P) if len(seg) else np.array([])
    return {"layers": len(P.layers), "z_max": float(zs[-1]) if len(zs) else None,
            "time_s": P.header.get("total_time_s"), "objects": P.objects,
            "tool_changes": changes, "filaments_by_layer": _ranges(by_layer),
            "tower_layers": _ranges(tower_layers), "volume_mm3": vol, "flush": flush,
            "temperatures": {k: sorted(v) for k, v in sorted(temps.items())},
            "footer_g": P.footer.get("filament used [g]"), "footer_m": P.footer.get("filament used [mm]"),
            "chart": {"n_layers": int(max(by_layer) + 1) if by_layer else 0,
                      "by_layer": {str(k): sorted(v) for k, v in by_layer.items()}}}


# ---------------------------------------------------------------- multi-filament: two archives

def _rel(a, b):
    a, b = _float(a), _float(b)
    if a is None or b is None:
        return None
    return abs(a - b) / max(abs(a), abs(b), 1e-9)


def _row(section, key, gui, ours, verdict=None, tol=None, note=""):
    if verdict is None:
        if gui == ours:
            verdict = "same"
        elif isinstance(gui, list) and isinstance(ours, list) and gui and ours and len(gui) != len(ours) \
                and len(set(map(str, gui))) == 1 and set(map(str, gui)) == set(map(str, ours)):
            verdict, note = "close", note or "same value, list padded to another length (extruder variants)"
        elif tol is not None and _rel(gui, ours) is not None and _rel(gui, ours) <= tol:
            verdict = "close"
        else:
            verdict = "differs"
    return {"section": section, "key": key, "gui": gui, "ours": ours, "verdict": verdict, "note": note}


def compare_filaments(gui: str, ours: str) -> dict:
    """Rows [{section, key, gui, ours, verdict, note}] and the facts of both files."""
    g, o = filament_facts(gui), filament_facts(ours)
    rows = []
    gs, os_ = g["slice_info"], o["slice_info"]
    # filaments
    gf = {f["id"]: f for f in gs["filaments"]}
    of = {f["id"]: f for f in os_["filaments"]}
    rows.append(_row("filaments", "filaments used", sorted(gf), sorted(of)))
    for fid in sorted(set(gf) | set(of), key=lambda x: int(x or 0)):
        a, b = gf.get(fid, {}), of.get(fid, {})
        for k in ("type", "color", "tray_info_idx", "used_for_object", "used_for_support"):
            same_colour = k == "color" and (a.get(k) or "").upper() == (b.get(k) or "").upper()
            rows.append(_row("filaments", f"F{fid} {k}", a.get(k), b.get(k), "same" if same_colour else None))
        rows.append(_row("filaments", f"F{fid} grams", a.get("used_g"), b.get("used_g"), tol=TOL["grams"]))
        rows.append(_row("filaments", f"F{fid} metres", a.get("used_m"), b.get("used_m"), tol=TOL["metres"]))
    rows.append(_row("filaments", "total grams", gs["metadata"].get("weight"), os_["metadata"].get("weight"), tol=TOL["grams"]))
    rows.append(_row("filaments", "time (s)", gs["metadata"].get("prediction"), os_["metadata"].get("prediction"),
                     tol=TOL["time"]))
    # maps and lists
    for k in ("filament_maps", "limit_filament_maps", "enable_filament_dynamic_map", "has_filament_switcher"):
        rows.append(_row("filament map", f"slice_info {k}", gs["metadata"].get(k), os_["metadata"].get(k)))
    rows.append(_row("filament map", "slice_info layer_filament_lists", gs["layer_filament_lists"], os_["layer_filament_lists"]))
    rows.append(_row("filament map", "filament_sequence.json", g["filament_sequence"], o["filament_sequence"]))
    rows.append(_row("filament map", "plate_1.json filament_colors",
                     [c.upper() for c in g["plate_json"]["filament_colors"] or []],
                     [c.upper() for c in o["plate_json"]["filament_colors"] or []]))
    rows.append(_row("filament map", "plate_1.json filament_ids", g["plate_json"]["filament_ids"], o["plate_json"]["filament_ids"]))
    rows.append(_row("filament map", "plate_1.json first_extruder", g["plate_json"]["first_extruder"], o["plate_json"]["first_extruder"]))
    # G-code
    gg, og = g["gcode"], o["gcode"]
    rows.append(_row("G-code", "layers", gg.get("layers"), og.get("layers")))
    rows.append(_row("G-code", "top z (mm)", gg.get("z_max"), og.get("z_max")))
    rows.append(_row("G-code", "tool changes (count)", len(gg.get("tool_changes", [])), len(og.get("tool_changes", []))))
    rows.append(_row("G-code", "tool changes (layer, filament)",
                     [[c["layer"], c["filament"]] for c in gg.get("tool_changes", [])],
                     [[c["layer"], c["filament"]] for c in og.get("tool_changes", [])]))
    rows.append(_row("G-code", "filaments per layer (objects)", gg.get("filaments_by_layer"), og.get("filaments_by_layer")))
    rows.append(_row("G-code", "filaments per layer (prime tower)", gg.get("tower_layers"), og.get("tower_layers")))
    for label, f in (("GUI", g), ("p3dt", o)):
        lists, read = f["slice_info"]["layer_filament_lists"], f["gcode"].get("filaments_by_layer")
        if lists:
            ok = lists == read
            rows.append(_row("G-code", f"{label}: slice_info layer_filament_lists = the G-code's", "yes" if ok else lists,
                             "yes" if ok else read, "same" if ok else "differs",
                             note="the archive's own list against what its G-code prints (objects)"))
    for f in sorted(set(gg.get("flush", {})) | set(og.get("flush", {}))):
        rows.append(_row("G-code", f"F{f} firmware flush (mm)", (gg.get("flush", {}).get(f) or {}).get("mm"),
                         (og.get("flush", {}).get(f) or {}).get("mm"), tol=TOL["flush"]))
    for f in sorted(set(gg.get("volume_mm3", {})) | set(og.get("volume_mm3", {}))):
        a, b = gg.get("volume_mm3", {}).get(f, {}), og.get("volume_mm3", {}).get(f, {})
        for k in ("part", "tower", "support"):
            if a.get(k) or b.get(k):
                rows.append(_row("G-code", f"F{f} {k} volume (mm3)", a.get(k), b.get(k), tol=TOL["volume"]))
    for k in sorted(set(gg.get("temperatures", {})) | set(og.get("temperatures", {}))):
        rows.append(_row("G-code", f"temperatures {k} (C)", gg.get("temperatures", {}).get(k), og.get("temperatures", {}).get(k)))
    # prime tower
    gt, ot = g["tower"], o["tower"]
    if gt or ot:
        sz = [(gt or {}).get("size"), (ot or {}).get("size")]
        v = "same" if sz[0] == sz[1] else ("close" if all(sz) and max(abs(x - y) for x, y in zip(*sz)) <= TOL["tower_mm"]
                                             else "differs")
        rows.append(_row("prime tower", "size w x d (mm)", sz[0], sz[1], v))
        rows.append(_row("prime tower", "bbox (mm)", (gt or {}).get("bbox"), (ot or {}).get("bbox"), "info",
                         note="placement differs by design (the GUI arranges; p3dt places)"))
    # settings
    for k in MF_SETTINGS:
        rows.append(_row("settings", k, g["settings"].get(k), o["settings"].get(k)))
    for k in sorted(POSITIONAL):
        rows.append(_row("settings", k, g["settings"].get(k), o["settings"].get(k), "info", note="a position"))
    # names
    rows.append(_row("names", "objects (G-code)", gg.get("objects"), og.get("objects"), "info",
                     note="p3dt names merged parts assemble_N"))
    rows.append(_row("names", "slicer", gs["client"], os_["client"], "info"))
    rows.append(_row("names", "slice_info warnings", gs["warnings"], os_["warnings"]))
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ("same", "close", "differs", "info")}
    return {"files": {"gui": gui, "ours": ours}, "counts": counts, "rows": rows, "gui": g, "ours": o}


# ---------------------------------------------------------------- the HTML page

def _fmt(v) -> str:
    if v is None:
        return "<span class='none'>—</span>"
    if isinstance(v, (list, dict)):
        t = json.dumps(v, separators=(", ", ": "))
        if len(t) > 220:
            return f"<details><summary>{html.escape(t[:90])}…</summary><code>{html.escape(t)}</code></details>"
        return f"<code>{html.escape(t)}</code>"
    return html.escape(str(v))


def _chart(mf: dict) -> str:
    """Filaments printed on each layer (objects), one band per file, with tool changes marked."""
    files = [("GUI", mf["gui"]), ("p3dt", mf["ours"])]
    n = max((f["gcode"].get("chart", {}).get("n_layers") or 0) for _, f in files) or 1
    W, row_h, gap, left = 960, 34, 22, 56
    H = len(files) * (row_h + gap) + 24
    cw = (W - left - 8) / n
    parts = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Filaments per layer' class='chart'>"]
    for i, (label, f) in enumerate(files):
        y = 8 + i * (row_h + gap)
        cols = [c.upper() for c in (f["plate_json"].get("filament_colors") or [])]
        parts.append(f"<text x='0' y='{y + row_h / 2 + 4}' class='lbl'>{label}</text>")
        parts.append(f"<rect x='{left}' y='{y}' width='{W - left - 8}' height='{row_h}' class='track'/>")
        for layer, fils in (f["gcode"].get("chart", {}).get("by_layer") or {}).items():
            x = left + int(layer) * cw
            h = row_h / max(1, len(fils))
            for k, fil in enumerate(fils):
                col = cols[fil - 1] if fil - 1 < len(cols) else "#999999"
                parts.append(f"<rect x='{x:.2f}' y='{y + k * h:.2f}' width='{max(cw, 0.6):.2f}' height='{h:.2f}' "
                             f"fill='{html.escape(col)}' class='cell'><title>layer {layer}: F{fil}</title></rect>")
        for c in f["gcode"].get("tool_changes", []):
            x = left + c["layer"] * cw
            parts.append(f"<line x1='{x:.2f}' x2='{x:.2f}' y1='{y - 4}' y2='{y + row_h + 4}' class='tc'>"
                         f"<title>tool change to F{c['filament']} at layer {c['layer']}</title></line>")
    yl = H - 4
    for t in range(0, n + 1, max(1, n // 8)):
        parts.append(f"<text x='{left + t * cw:.1f}' y='{yl}' class='tick'>{t}</text>")
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--same:#2e7d32;--close:#8a6d00;
--differs:#c62828;--info:#4f6d8f;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;
--card:#1f1f1d;--line:#34332f;--same:#7bc47f;--close:#e0c050;--differs:#ef7a7a;--info:#8fb0d6;--chip:#2a2a27}}
:root[data-theme=dark]{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1d;--line:#34332f;--same:#7bc47f;
--close:#e0c050;--differs:#ef7a7a;--info:#8fb0d6;--chip:#2a2a27}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--muted);margin:0 0 16px;
word-break:break-all}.cards{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.card{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:110px}.card b{display:block;font-size:22px}
.v-same b,.v-same{color:var(--same)}.v-close b,.v-close{color:var(--close)}.v-differs b,.v-differs{color:var(--differs)}
.v-info b,.v-info{color:var(--info)}.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);
border-radius:8px}table{border-collapse:collapse;width:100%;min-width:640px}th,td{text-align:left;padding:6px 10px;
border-top:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted);border-top:none;
position:sticky;top:0;background:var(--card)}td.k{white-space:nowrap}code{font:12px/1.4 ui-monospace,Menlo,monospace;
word-break:break-all}.badge{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--chip);
font-size:12px;font-weight:600}.note{color:var(--muted);font-size:12px}.none{color:var(--muted)}
.chart{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px}
.chart .lbl,.chart .tick{fill:var(--muted);font-size:12px}.chart .track{fill:var(--chip)}.chart .cell{stroke:var(--line);
stroke-width:.3}.chart .tc{stroke:var(--differs);stroke-width:1.5}.filters{margin:8px 0;display:flex;gap:12px;
flex-wrap:wrap;color:var(--muted)}details summary{cursor:pointer}pre{white-space:pre-wrap;word-break:break-all}
"""


def html_report(mf: dict, s4: dict | None) -> str:
    c = mf["counts"]
    sections = []
    for sec in dict.fromkeys(r["section"] for r in mf["rows"]):
        trs = "".join(
            f"<tr data-v='{r['verdict']}'><td class='k'>{html.escape(r['key'])}</td><td>{_fmt(r['gui'])}</td>"
            f"<td>{_fmt(r['ours'])}</td><td><span class='badge v-{r['verdict']}'>{r['verdict']}</span>"
            + (f"<div class='note'>{html.escape(r['note'])}</div>" if r["note"] else "") + "</td></tr>"
            for r in mf["rows"] if r["section"] == sec)
        sections.append(f"<h2>{html.escape(sec.capitalize())}</h2><div class='wrap'><table><thead><tr><th>What</th>"
                        f"<th>GUI export</th><th>p3dt</th><th>Verdict</th></tr></thead><tbody>{trs}</tbody></table></div>")
    s4_html = ""
    if s4:
        s4_html = ("<h2>Packaging (S4)</h2><details><summary>Archive members, slice_info, model_settings, plate_1.json, "
                   "project-settings keys</summary><pre>" + html.escape(json.dumps(
                       {k: v for k, v in s4.items() if k not in ("files",)}, indent=1, default=str)[:60000]) + "</pre></details>")
    cards = "".join(f"<div class='card v-{k}'><b>{c[k]}</b>{k}</div>" for k in ("same", "close", "differs", "info"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Multi-filament comparison</title>
<style>{CSS}</style></head><body><main>
<h1>Multi-filament comparison</h1>
<p class="sub">GUI: {html.escape(mf['files']['gui'])}<br>p3dt: {html.escape(mf['files']['ours'])}<br>
compare_bambu.py, print3d_translator {html.escape(__version__)}</p>
<div class="cards">{cards}</div>
<div class="filters"><label><input type="checkbox" id="hide" > show only rows that differ or are close</label></div>
<h2>Filaments per layer</h2>{_chart(mf)}
<p class="note">Each column is a layer (objects only, not the prime tower); colours are the plate's filament colours;
red lines are tool changes.</p>
{''.join(sections)}{s4_html}
</main><script>
document.getElementById('hide').addEventListener('change',e=>{{document.querySelectorAll('tr[data-v]').forEach(r=>{{
r.style.display=e.target.checked&&(r.dataset.v==='same'||r.dataset.v==='info')?'none':''}})}})
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Compare a GUI-exported .gcode.3mf with p3dt's for the same plate.")
    ap.add_argument("gui", help="the .gcode.3mf exported by OrcaSlicer's or Bambu Studio's GUI")
    ap.add_argument("ours", help="p3dt's .gcode.3mf for the same plate")
    ap.add_argument("--out", help="JSON report (default: <gui>.compare.json)")
    ap.add_argument("--html", help="HTML report (default: <gui>.compare.html)")
    a = ap.parse_args()
    s4 = compare(a.gui, a.ours)
    mf = compare_filaments(a.gui, a.ours)
    base = Path(a.gui)
    stem = base.name.removesuffix(".gcode.3mf").removesuffix(".3mf")
    out = Path(a.out) if a.out else base.with_name(stem + ".compare.json")
    page = Path(a.html) if a.html else base.with_name(stem + ".compare.html")
    out.write_text(json.dumps({"packaging": s4, "multi_filament": {k: v for k, v in mf.items() if k not in ("gui", "ours")},
                               "facts": {"gui": mf["gui"], "ours": mf["ours"]}}, indent=1, default=str))
    page.write_text(html_report(mf, s4))
    print(json.dumps(mf["counts"]))
    for r in mf["rows"]:
        if r["verdict"] in ("differs", "close"):
            g, o = json.dumps(r["gui"], default=str), json.dumps(r["ours"], default=str)
            print(f"{r['verdict']:8s} {r['section']} / {r['key']}: GUI {g[:80]}  p3dt {o[:80]}")
    print(f"\nreport: {page}\njson:   {out}")


if __name__ == "__main__":
    main()
