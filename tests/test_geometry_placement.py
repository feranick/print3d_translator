"""Tests of geometry.py (loading, orientation, checks before slicing) and placement.py.
Version: 2026.10.24.6
"""
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from print3d_translator import geometry as Gm, mesh as M, placement as PL

BED = np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float)


def l_shape():
    """An L: 30 x 10 x 5 base with a 10 x 10 x 25 post (closed, union as one mesh of two boxes is not
    closed, so build the L from its outline: an extruded polygon)."""
    pts = [(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)]
    h = 5.0
    tri = []
    n = len(pts)
    for i in range(n):                                       # sides
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
        a, b, c, d = (x0, y0, 0), (x1, y1, 0), (x1, y1, h), (x0, y0, h)
        tri += [(a, b, c), (a, c, d)]
    # caps (fan from a convex-safe triangulation of the L)
    caps = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 5)]
    for (i, j, k) in caps:
        tri.append(((*pts[i], h), (*pts[j], h), (*pts[k], h)))
        tri.append(((*pts[i], 0), (*pts[k], 0), (*pts[j], 0)))
    return np.array(tri, float)


def write_3mf(path, tri, transform="1 0 0 0 1 0 0 0 1 10 20 0", component=False):
    v = tri.reshape(-1, 3)
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
    tris = "".join(f'<triangle v1="{3 * i}" v2="{3 * i + 1}" v3="{3 * i + 2}"/>' for i in range(len(tri)))
    obj = f'<object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
    if component:
        obj += '<object id="2" type="model"><components><component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 5"/></components></object>'
    item = f'<item objectid="{2 if component else 1}" transform="{transform}"/>'
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources>{obj}</resources><build>{item}</build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
    return path


# ------------------------------------------------------------- loading

def test_load_stl_and_3mf(tmp_path):
    b = M.box(10, 20, 30)
    p = M.write_stl(tmp_path / "b.stl", b)
    part = Gm.load_part({"stl": "b.stl"}, tmp_path)
    assert part["volume"] == pytest.approx(6000) and part["volume_from"] == "mesh"
    t = Gm.read_3mf(write_3mf(tmp_path / "b.3mf", b))
    assert np.allclose(M.bbox(t), [[10, 20, 0], [20, 40, 30]])
    t = Gm.read_3mf(write_3mf(tmp_path / "c.3mf", b, component=True))
    assert np.allclose(M.bbox(t), [[10, 20, 5], [20, 40, 35]])
    assert Gm.load_part({"3mf": str(tmp_path / "b.3mf")})["volume"] == pytest.approx(6000)


@pytest.mark.parametrize("part,match", [
    ({"stl": "/nope.stl"}, "not found"), ({"bad": 1}, "give one of"),
    ({"b3dt": {"volume": 1}}, "step"),
])
def test_load_errors(part, match):
    with pytest.raises(Gm.GeometryError, match=match):
        Gm.load_part(part)


def test_step_needs_build123d_or_works(tmp_path):
    try:
        import build123d
    except ImportError:
        (tmp_path / "x.step").write_text("x")
        with pytest.raises(Gm.GeometryError, match="build123d"):
            Gm.load_part({"step": str(tmp_path / "x.step")})
        return
    from build123d import Box, export_step
    export_step(Box(10, 20, 30), str(tmp_path / "b.step"))
    part = Gm.load_part({"step": str(tmp_path / "b.step")})
    assert part["volume"] == pytest.approx(6000) and part["volume_from"] == "step"
    assert abs(M.volume(part["tri"])) == pytest.approx(6000, rel=1e-6)
    part = Gm.load_part({"b3dt": {"step": str(tmp_path / "b.step"), "volume": 6000}})
    assert part["volume_from"] == "b3dt"
    with pytest.raises(Gm.GeometryError, match="does not match"):
        Gm.load_part({"b3dt": {"step": str(tmp_path / "b.step"), "volume": 5000}})


# ------------------------------------------------------------- mesh quality and orientation

def test_closedness():
    assert Gm.closedness(M.box(1, 2, 3))["closed"]
    assert Gm.closedness(l_shape())["closed"]
    open_box = M.box(1, 1, 1)[:-1]
    c = Gm.closedness(open_box)
    assert not c["closed"] and c["boundary_edges"] > 0
    assert Gm.closedness(M.box(1, 1, 1)[:, ::-1])["inverted"]


def test_orient_flat_puts_the_largest_face_down():
    tall = M.transform(M.box(5, 40, 20), M.rotation(0, 90, 0))     # 20 x 40 x 5 stood on its side
    tri, rot, R = Gm.orient(tall, "flat")
    lo, hi = M.bbox(tri)
    assert np.allclose(hi - lo, [5, 40, 20]) or np.allclose(hi - lo, [20, 40, 5])
    assert hi[2] - lo[2] == pytest.approx(5)                      # the 40 x 20 face is down
    assert lo[2] == pytest.approx(0) and np.allclose((lo + hi)[:2] / 2, 0)
    assert abs(M.volume(tri)) == pytest.approx(4000)


def test_orient_rotate_and_errors():
    tri, rot, _ = Gm.orient(M.box(10, 20, 30), {"rotate": [90, 0, 0]})
    lo, hi = M.bbox(tri)
    assert rot == [90.0, 0.0, 0.0] and np.allclose(hi - lo, [10, 30, 20])
    with pytest.raises(Gm.GeometryError, match="as_is"):
        Gm.orient(M.box(1, 1, 1), "upside")


# ------------------------------------------------------------- checks before slicing

def checks_of(tri, supports="none", bed=BED, height=256):
    t, _, _ = Gm.orient(tri, "as_is")
    return Gm.checks({"tri": t, "volume": abs(M.volume(t)), "volume_from": "mesh"}, bed, height, 0.4, 30, supports)


def test_checks_good_box():
    c = checks_of(M.box(20, 20, 10))
    assert all(v["passed"] for v in c.values()), c


def test_checks_fit_and_height():
    c = checks_of(M.box(300, 20, 10))
    assert not c["fits"]["passed"] and c["fits"]["hard"]
    assert not checks_of(M.box(20, 20, 300))["fits"]["passed"]


def test_checks_overhang_and_supports():
    upside = M.transform(l_shape(), M.rotation(-90, 0, 0))         # a Γ: the top arm overhangs (20 x 5 mm)
    t, _, _ = Gm.orient(upside, "as_is")
    c = checks_of(t)
    assert c["overhang"]["got"]["area_mm2"] == pytest.approx(100, abs=1) and not c["overhang"]["passed"]
    assert checks_of(l_shape())["overhang"]["got"]["area_mm2"] == 0
    assert checks_of(t, supports="tree")["overhang"]["passed"]


def test_checks_small_contact_and_thin_walls():
    pin = M.box(2, 2, 40)
    c = checks_of(pin)
    assert not c["bed_contact"]["passed"]
    thin = M.box(30, 0.3, 10)
    c = checks_of(thin)
    assert not c["thin_walls"]["passed"] and c["thin_walls"]["got"]["min_mm"] == pytest.approx(0.3, abs=0.01)


def test_open_mesh_is_hard_for_mesh_input():
    c = checks_of(M.box(10, 10, 10)[:-1])
    assert not c["closed_mesh"]["passed"] and c["closed_mesh"]["hard"]


# ------------------------------------------------------------- placement

def item(i, sx, sy, ext=None, at=None, halo=3.0):
    return {"id": i, "tri": M.place(M.box(sx, sy, 5), (0, 0)), "halo": halo, "extruders": ext, "at": at}


def rects(out):
    r = {}
    for k, v in out["placed"].items():
        lo, hi = M.bbox(v["tri"])
        r[k] = (lo[0], lo[1], hi[0], hi[1])
    return r


def overlap(a, b, gap):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def test_place_no_overlap_on_bed_and_deterministic():
    items = [item(f"p{k}", 30 + 5 * k, 20) for k in range(6)]
    out = PL.place(items, BED)
    r = rects(out)
    for a in r:
        assert 0 <= r[a][0] and r[a][2] <= 256 and 0 <= r[a][1] and r[a][3] <= 256
        for b in r:
            if a < b:
                assert not overlap(r[a], r[b], 6 - 1e-6), (a, b)
    assert PL.place(items, BED)["placed"]["p0"]["center"] == out["placed"]["p0"]["center"]
    assert np.allclose(out["placed"]["p5"]["center"], [128, 128], atol=0.5)   # the largest at the centre (1 mm grid)


def test_place_fixed_tower_and_zone():
    zone = np.array([[153, 235], [216, 256]], float)
    out = PL.place([item("a", 20, 20, at=[60, 60]), item("b", 40, 40)], BED,
                   tower={"w": 35, "d": 35, "halo": 5, "extruders": None}, exclude=zone)
    assert out["placed"]["a"]["center"] == [60.0, 60.0]
    t = out["tower"]["rect"]
    assert t[2] <= 256 and t[3] <= 256 and t[1] > 128                # on the bed, at the back
    assert not overlap(t, (153, 235, 216, 256), 0)
    for r in rects(out).values():
        assert not overlap(r, t, 0)


def test_place_per_extruder_areas():
    h2d = np.array([[0, 0], [350, 0], [350, 320], [0, 320]], float)
    areas = [np.array([[0, 0], [325, 320]], float), np.array([[25, 0], [350, 320]], float)]
    with pytest.raises(PL.PlacementError, match="outside the area"):
        PL.place([item("r", 20, 20, ext=[2], at=[20, 100])], h2d, areas)
    out = PL.place([item("l", 20, 20, ext=[1], at=[20, 100])], h2d, areas)
    assert out["placed"]["l"]["center"] == [20.0, 100.0]


@pytest.mark.parametrize("items,match", [
    ([item("big", 300, 10)], "does not fit"),
    ([item("x", 20, 20, at=[5, 5])], "outside the area"),
    ([item("x", 20, 20, at=[100, 100]), item("y", 20, 20, at=[110, 100])], "overlaps"),
    ([item(f"p{k}", 80, 80) for k in range(12)], "no room"),
])
def test_place_errors(items, match):
    with pytest.raises(PL.PlacementError, match=match):
        PL.place(items, BED)
