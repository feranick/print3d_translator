"""Tests of supports.py: unsupported regions of a mesh (before slicing), unsupported extrusion in G-code
(after slicing), the check_gcode check and the viewer's red toolpaths.
Version: 2026.10.29.1
"""
import numpy as np
import pytest

from print3d_translator import check as C, gcode as G, geometry as Gm, mesh as M, supports as S, viewer

LH = 0.2


# ------------------------------------------------------------- synthetic G-code

def loop(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def rects(*rs, w=0.42):
    """Two perimeter loops per rectangle (x0, y0, x1, y1), as OrcaSlicer prints a wall."""
    out = []
    for (x0, y0, x1, y1) in rs:
        for inset in (w / 2, 1.5 * w):
            out.append(("Outer wall", loop(x0 + inset, y0 + inset, x1 - inset, y1 - inset)))
    return out


def zigzag(x0, y0, x1, y1, spacing=2.5):
    pts, x, up = [], x0, True
    while x <= x1 + 1e-9:
        pts += [(x, y0), (x, y1)] if up else [(x, y1), (x, y0)]
        x += spacing
        up = not up
    return [("Support", pts)]


def gcode(layers, name="part_1"):
    """layers: per layer, [(feature, polyline)]. Each polyline is one continuous extrusion."""
    out = ["M83", "G90", "G1 X0 Y0 Z0.2 F6000"]
    for n, paths in enumerate(layers):
        z = round(LH * (n + 1), 4)
        out += ["; CHANGE_LAYER", f"; Z_HEIGHT: {z}", f"; LAYER_HEIGHT: {LH}",
                f"; printing object {name} id:0 copy 0", f"G1 Z{z}"]
        for feat, pts in paths:
            out += [f"; FEATURE: {feat}", f"G1 X{pts[0][0]:.3f} Y{pts[0][1]:.3f}"]
            for (x, y) in pts[1:]:
                out.append(f"G1 X{x:.3f} Y{y:.3f} E0.05")
        out.append(f"; stop printing object {name} id:0 copy 0")
    return G.parse("\n".join(out) + "\n")


def run(layers):
    return S.unsupported(layers, 0.42, LH, 30)


COLUMN = (40, 40, 50, 50)


def test_column_is_supported():
    u = run(gcode([rects(COLUMN)] * 10))
    assert u["unsupported_mm"] == 0 and u["severe_mm"] == 0 and not u["flags"].any()


def test_slow_taper_is_within_reach():
    # grows 0.2 mm per layer on one side: steeper than 30 deg from horizontal (reach 0.556 mm)
    layers = [rects((40, 40, 50 + 0.2 * k, 50)) for k in range(15)]
    assert run(gcode(layers))["severe_mm"] == 0


def test_cantilever_is_severe_and_flagged():
    layers = [rects(COLUMN)] * 10 + [rects((40, 40, 50, 70))] * 3        # arm 20 mm past the column
    P = gcode(layers)
    u = run(P)
    assert u["severe_mm"] > 30
    w = u["worst"][0]
    assert w["layer"] == 11 and w["z_mm"] == pytest.approx(2.2) and not w["floating"] and w["beyond_at_least"]
    assert w["objects"] == ["part_1"] and 40 <= w["xy"][0] <= 50 and w["xy"][1] > 50
    flagged = P.segments[u["flags"]]
    assert len(flagged) and (np.maximum(flagged[:, 1], flagged[:, 3]) > 52).all()
    # layer 12 rests on layer 11: only the first layer of the arm is unsupported
    assert set(u["by_layer"]) == {10}


def test_island_starts_in_mid_air():
    layers = [rects(COLUMN)] * 10 + [rects(COLUMN, (70, 40, 75, 45))] * 3
    u = run(gcode(layers))
    w = u["worst"][0]
    assert w["floating"] and w["severe_mm"] > 10 and w["xy"][0] > 70


def test_short_bridge_is_accepted_long_bridge_reported():
    pillars = rects((40, 40, 50, 50), (60, 40, 70, 50))                 # 10 mm gap
    u = run(gcode([pillars] * 10 + [rects((40, 40, 70, 50))] * 2))
    assert u["severe_mm"] == 0 and u["long_bridges"]["layers"] == 0
    far = rects((40, 40, 50, 50), (70, 40, 80, 50))                     # 20 mm gap
    u = run(gcode([far] * 10 + [rects((40, 40, 80, 50))] * 2))
    assert u["severe_mm"] == 0 and u["long_bridges"]["longest_mm"] == pytest.approx(19.5, abs=1.0)


def test_bridge_split_into_features_is_one_path():
    # OrcaSlicer switches to "Overhang wall" over the gap without a travel: still one path, a bridge
    pillars = rects((40, 40, 50, 50), (60, 40, 70, 50))
    top = [("Outer wall", [(40.2, 45), (50, 45)]), ("Overhang wall", [(50, 45), (60, 45)]),
           ("Outer wall", [(60, 45), (69.8, 45)])]
    u = run(gcode([pillars] * 10 + [top]))
    assert u["severe_mm"] == 0


def test_support_below_carries_the_overhang():
    base = [rects(COLUMN) + zigzag(40.5, 50.5, 49.5, 69.5)] * 10
    u = run(gcode(base + [rects((40, 40, 50, 70))] * 3))
    assert u["severe_mm"] == 0


# ------------------------------------------------------------- check_gcode

def resolved(supports="0"):
    return {"machine": {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "256",
                        "nozzle_diameter": ["0.4"]},
            "process": {"layer_height": str(LH), "initial_layer_print_height": str(LH), "outer_wall_line_width": "0.42",
                        "line_width": "0.42", "support_threshold_angle": "30", "enable_support": supports},
            "filaments": [{"filament_diameter": ["1.75"]}], "plate": {}}


def test_check_unsupported_extrusion_is_hard():
    P = gcode([rects(COLUMN)] * 10 + [rects((40, 40, 50, 70))] * 3)
    ch, lb = C._support(P, lambda k, d=None: resolved()["process"].get(k, d),
                        lambda k, d=None: resolved()["machine"].get(k, d))
    assert not ch["passed"] and ch["hard"] and "supports" in ch["why"] and ch["problems"][0].startswith("z 2.2 mm")
    assert lb["passed"] and not lb["hard"]
    ok, why = C.verdict({"unsupported_extrusion": ch})
    assert not ok and why[0].startswith("unsupported_extrusion")
    ch2, _ = C._support(P, lambda k, d=None: resolved("1")["process"].get(k, d),
                        lambda k, d=None: resolved("1")["machine"].get(k, d))
    assert "supports do not reach it" in ch2["why"]


def test_support_params_percent_width():
    sp = C.support_params(lambda k, d=None: {"line_width": "110%", "enable_support": "1"}.get(k, d), 0.4)
    assert sp["line_width"] == pytest.approx(0.44) and sp["supports"] and sp["angle_deg"] == 30


# ------------------------------------------------------------- geometry (before slicing)

def gamma():
    """A column 10 x 10 x 20 with an arm 10 x 20 x 5 sticking out at the top (one closed mesh)."""
    pts = [(0, 0), (10, 0), (10, 20), (30, 20), (30, 25), (0, 25)]     # side profile in x-z, extruded along y
    tri, n = [], len(pts)
    for i in range(n):
        (x0, z0), (x1, z1) = pts[i], pts[(i + 1) % n]
        a, b, c, d = (x0, 0, z0), (x1, 0, z1), (x1, 10, z1), (x0, 10, z0)
        tri += [(a, c, b), (a, d, c)]
    for (i, j, k) in [(0, 1, 2), (0, 2, 5), (2, 3, 4), (2, 4, 5)]:
        tri.append(((pts[i][0], 0, pts[i][1]), (pts[j][0], 0, pts[j][1]), (pts[k][0], 0, pts[k][1])))
        tri.append(((pts[i][0], 10, pts[i][1]), (pts[k][0], 10, pts[k][1]), (pts[j][0], 10, pts[j][1])))
    t = np.array(tri, float)
    return t if M.volume(t) > 0 else t[:, ::-1]


def test_regions_gamma_is_a_cantilever():
    reg = S.regions(gamma(), LH, LH, 0.42, 30)
    g = reg["regions"][0]
    assert g["kind"] == "cantilever" and g["beyond_mm"] == pytest.approx(20, abs=0.5)
    assert g["z_mm"][0] == pytest.approx(20.0, abs=0.2)
    assert not S.verdict(reg, "none")[0] and S.verdict(reg, "tree")[0]


def test_regions_island_and_bridge():
    tooth = M.box(5, 5, 5) + np.array([30, 0, 20])                     # a separate shell, 20 mm up
    reg = S.regions(np.concatenate([M.box(10, 10, 30), tooth]), LH, LH, 0.42, 30)
    assert reg["regions"][0]["kind"] == "island" and reg["regions"][0]["z_mm"][0] == pytest.approx(20, abs=0.2)
    pillars = np.concatenate([M.box(5, 10, 10), M.box(5, 10, 10) + np.array([15, 0, 0]),
                              M.box(20, 10, 3) + np.array([0, 0, 10])])
    reg = S.regions(pillars, LH, LH, 0.42, 30)
    assert [g["kind"] for g in reg["regions"]] == ["bridge"] and S.verdict(reg, "none")[0]


def test_regions_coarsen_for_big_parts():
    reg = S.regions(M.box(200, 200, 50), LH, LH, 0.42, 30, max_voxels=1e6)
    assert reg["grid_mm"] > S.RES and reg["regions"] == []


def test_geometry_check_is_soft():
    c = Gm.checks({"tri": gamma(), "volume": M.volume(gamma()), "volume_from": "mesh"},
                  np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float), 256, 0.4, 30, "none")
    u = c["unsupported_regions"]
    assert not u["passed"] and not u["hard"] and "cantilever" in u["problems"][0]


# ------------------------------------------------------------- viewer

def test_viewer_draws_flagged_red():
    P = gcode([rects(COLUMN)] * 10 + [rects((40, 40, 50, 70))] * 3)
    u = run(P)
    _, recs, _ = viewer._toolpaths(P, u["flags"])
    red = viewer.FEATURE_INDEX["Unsupported"]
    assert (recs[:, 3] == red).any() and set(recs[recs[:, 3] == red, 2]) == {10}
    html = viewer.build([], np.array([[0, 0], [256, 256]], float), 256, P, summary={"unsupported": "x"}, flags=u["flags"])
    assert '"Unsupported"' in html
