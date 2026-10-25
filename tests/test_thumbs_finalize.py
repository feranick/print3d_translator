"""Tests of thumbs.py (plate pictures) and gcode3mf.finalize (the archive as OrcaSlicer's GUI writes it).
Version: 2026.10.24.9
"""
import json
import struct
import zipfile
import zlib

import numpy as np
import pytest

from print3d_translator import gcode3mf as A, mesh as M, thumbs

BED = np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float)


def decode_png(b):
    """Minimal decoder for our own PNGs (filter 0): (h, w, 4) uint8."""
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", b[16:24])
    i, idat = 8, b""
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]
        if b[i + 4:i + 8] == b"IDAT":
            idat += b[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (4 * w + 1) + 1:(y + 1) * (4 * w + 1)] for y in range(h)]
    return np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(h, w, 4)


def test_render_sizes_and_pick_ids():
    a = M.place(M.box(40, 20, 10), (80, 128))
    b = M.place(M.box(20, 20, 30), (180, 128))
    imgs = thumbs.render([{"tri": a, "color": "#FF0000", "identify_id": 92},
                          {"tri": b, "color": "#0000FF", "identify_id": 300}], BED)
    assert set(imgs) == {"plate", "plate_small", "no_light", "top", "pick"}
    assert decode_png(imgs["plate"]).shape == (512, 512, 4) and decode_png(imgs["plate_small"]).shape == (128, 128, 4)
    pick = decode_png(imgs["pick"])
    ids = {int(p[0]) | int(p[1]) << 8 | int(p[2]) << 16 for p in pick[pick[..., 3] == 255]}
    assert ids == {92, 300}
    top = decode_png(imgs["top"])
    # top view, bed-fitted: part a covers x 60..100 mm -> about 120..200 px, and nothing outside the parts is opaque
    assert top[256, 160, 3] == 255 and top[256, 20, 3] == 0 and top[20, 256, 3] == 0
    plate = decode_png(imgs["plate"])
    assert (plate[..., 3] == 255).mean() > 2 * (top[..., 3] == 255).mean()   # zoomed to the parts


def test_render_shades_by_light():
    imgs = thumbs.render([{"tri": M.place(M.box(30, 30, 30), (128, 128)), "color": "#808080"}], BED)
    plate = decode_png(imgs["plate"])
    shades = {tuple(p[:3]) for p in plate[plate[..., 3] == 255]}
    assert len(shades) >= 3                                       # top and two sides differ
    flat = decode_png(imgs["no_light"])
    assert {tuple(p[:3]) for p in flat[flat[..., 3] == 255]} == {(128, 128, 128)}


def cli_archive(path, extruders=1):
    si = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <plate>\n    <metadata key="index" value="1"/>\n'
          '    <metadata key="printer_model_id" value=""/>\n'
          f'    <metadata key="nozzle_volume_type" value="0"/>\n'
          '    <metadata key="first_layer_time" value="307348170571033591697770805198848.000000"/>\n'
          '    <metadata key="filament_maps" value="2"/>\n'
          '    <object identify_id="92" name="part_1" skipped="false" />\n  </plate>\n</config>\n')
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
          '    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>\n    <model_instance>\n'
          '      <metadata key="object_id" value="2"/>\n    </model_instance>\n  </plate>\n</config>\n')
    g = b"; HEADER_BLOCK_START\n; estimated first layer printing time (normal mode) = 5m 13s\n; HEADER_BLOCK_END\nG1 X1\n"
    with zipfile.ZipFile(path, "w") as z:
        for n, d in [("[Content_Types].xml", "<T/>"), ("_rels/.rels", "<R/>"), ("3D/3dmodel.model", "<m/>"),
                     ("Metadata/plate_1.gcode", g), ("Metadata/plate_1.gcode.md5", A.md5_upper(g)),
                     ("Metadata/slice_info.config", si), ("Metadata/model_settings.config", ms),
                     ("Metadata/project_settings.config", "{}"), ("Metadata/plate_1.json", json.dumps({"first_layer_time": 0.0}))]:
            z.writestr(n, d)
    return g


def test_finalize_matches_the_gui(tmp_path):
    p = tmp_path / "x.gcode.3mf"
    g = cli_archive(p)
    imgs = thumbs.render([{"tri": M.place(M.box(10, 10, 10), (100, 100)), "color": "#00FF00", "identify_id": 92}], BED)
    out = A.finalize(p, "O1D", extruders=2, first_layer_s=313.0, images=imgs)
    assert set(out["changed"]) == {"slice_info.config", "model_settings.config", "plate_1.json", "thumbnails"}
    a = A.read(p)
    md = a["slice_info"]["plates"][0]["metadata"]
    assert md["printer_model_id"] == "O1D" and md["first_layer_time"] == "313.000000"
    assert md["nozzle_volume_type"] == "0 0"
    ms = a["model_settings"]["plates"][0]
    assert ms["thumbnail_file"] == "Metadata/plate_1.png" and ms["pick_file"] == "Metadata/pick_1.png"
    assert ms["top_file"] == "Metadata/top_1.png" and ms["thumbnail_no_light_file"] == "Metadata/plate_no_light_1.png"
    assert ms["filament_maps"] == "2" and ms["pattern_bbox_file"] == "Metadata/plate_1.json"
    assert a["plate_json"]["first_layer_time"] == 313.0
    assert a["gcode"] == g and a["md5_file"] == A.md5_upper(g)
    c = A.check_archive(a, g, "O1D", 0.4, 1)
    assert c["thumbnails"]["passed"] and c["slice_info_values"]["passed"] and c["printer_model_id"]["passed"]
    assert A.finalize(p, "O1D", 2, 313.0, imgs)["changed"] == []          # idempotent
    with zipfile.ZipFile(p) as z:
        assert len(z.namelist()) == len(set(z.namelist()))


def test_finalize_without_header_time_resets_garbage(tmp_path):
    p = tmp_path / "y.gcode.3mf"
    cli_archive(p)
    A.finalize(p, "N7")
    md = A.read(p)["slice_info"]["plates"][0]["metadata"]
    assert md["first_layer_time"] == "0.000000" and md["nozzle_volume_type"] == "0"
    assert not A.check_archive(A.read(p), None, "N7", 0.4, 1)["thumbnails"]["passed"]
