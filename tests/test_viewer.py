"""Tests of viewer.py: the self-contained 3D view.
Version: 2026.10.24.10
"""
import base64
import json
import re
import zlib

import numpy as np
import pytest

from print3d_translator import gcode as G, mesh as M, viewer

BED = np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float)


def payload(html):
    meta = json.loads(re.search(r'<script id="meta" type="application/json">(.*?)</script>', html, re.S).group(1))
    data = re.search(r'DATA="([A-Za-z0-9+/=]*)"', html).group(1)
    return meta, zlib.decompress(base64.b64decode(data))


@pytest.mark.parametrize("name,group", [("Outer wall", "Outer wall"), ("Inner wall", "Inner wall"),
                                        ("Sparse infill", "Sparse infill"), ("Internal solid infill", "Solid infill"),
                                        ("Top surface", "Top surface"), ("Support interface", "Support interface"),
                                        ("Support", "Support"), ("Prime tower", "Prime tower"), ("Brim", "Brim / skirt"),
                                        ("Internal Bridge", "Bridge"), ("Overhang wall", "Overhang wall"), ("Custom", "Other")])
def test_feature_groups(name, group):
    assert viewer.FEATURES[viewer.feature_group(name)][0] == group


def test_build_embeds_meshes_and_toolpaths():
    text = ("M83\nG1 X10 Y10 Z0.2\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X20 Y10 E1\nG1 X20 Y20 E1\n"
            "; FEATURE: Sparse infill\nG1 X10 Y20 E1\n; CHANGE_LAYER\n; Z_HEIGHT: 0.4\nG1 X10 Y10 Z0.4\n"
            "; FEATURE: Outer wall\nG1 X20 Y10 E1\n")
    P = G.parse(text)
    box = M.place(M.box(10, 10, 0.4), (15, 15))
    html = viewer.build([{"name": "a", "tri": box, "color": "#ff0000"}], BED, 256, P, ["#ff0000"], "t",
                        {"printer": "P2S", "reliable": True})
    meta, raw = payload(html)
    assert len(raw) == meta["raw_bytes"] and meta["layer_z"] == [0.2, 0.4]
    o = meta["objects"][0]
    verts = np.frombuffer(raw[o["v"][0]:o["v"][0] + 4 * o["v"][1]], dtype=np.float32).reshape(-1, 3)
    idx = np.frombuffer(raw[o["i"][0]:o["i"][0] + 4 * o["i"][1]], dtype=np.uint32)
    assert len(verts) == 8 and len(idx) == 36                    # a box: 8 shared vertices, 12 triangles
    tp = meta["toolpaths"]
    pts = np.frombuffer(raw[tp["pts"][0]:tp["pts"][0] + 2 * tp["pts"][1]], dtype=np.uint16).reshape(-1, 2) * 0.01 - 20
    recs = np.frombuffer(raw[tp["recs"][0]:tp["recs"][0] + 4 * tp["recs"][1]], dtype=np.uint32).reshape(-1, 5)
    assert recs.tolist() == [[0, 3, 0, 0, 0], [3, 2, 0, 3, 0], [5, 2, 1, 0, 0]]   # walls, infill, walls on layer 2
    assert np.allclose(pts[:3], [[10, 10], [20, 10], [20, 20]])
    assert "<canvas" in html and "cdn" not in html.lower()       # self-contained


def test_build_without_gcode():
    meta, raw = payload(viewer.build([{"name": "a", "tri": M.box(5, 5, 5)}], BED, 256))
    assert meta["toolpaths"]["pts"][1] == 0 and len(raw) == meta["raw_bytes"]
