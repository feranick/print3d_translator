#!/usr/bin/env python3
"""compare_bambu.py — spike S4: what does a .gcode.3mf exported by Bambu Studio have that ours lacks
(or has differently)? Compares the archive layout, slice_info.config, model_settings.config,
plate_1.json, project_settings.config (keys) and the G-code header block.
Version: 2026.10.24.8

    python tools/compare_bambu.py BAMBU.gcode.3mf OURS.gcode.3mf [--out report.json]

Read-only; prints a short report and writes the full one as JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from print3d_translator import gcode3mf as A  # noqa: E402


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bambu")
    ap.add_argument("ours")
    ap.add_argument("--out")
    a = ap.parse_args()
    rep = compare(a.bambu, a.ours)
    out = Path(a.out) if a.out else Path(a.bambu).with_suffix(".compare.json")
    out.write_text(json.dumps(rep, indent=1, default=str))
    short = {"archive": {k: v for k, v in rep["archive"].items() if k != "bambu_sizes"},
             "slice_info.plate_metadata": rep["slice_info"]["plate_metadata"],
             "slice_info.header": rep["slice_info"]["header"],
             "filament_attributes": rep["slice_info"]["filament_attributes"],
             "model_settings.plate": rep["model_settings"]["plate"], "plate_json": rep["plate_json"],
             "project_settings": {k: rep["project_settings"][k] for k in ("n_keys",)} |
             {"only_bambu": rep["project_settings"]["only_bambu"][:40], "only_ours": rep["project_settings"]["only_ours"][:40]},
             "gcode": {k: v for k, v in rep["gcode"].items() if k != "header"}}
    print(json.dumps(short, indent=1, default=str))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
