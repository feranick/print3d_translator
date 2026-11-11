"""End-to-end tests of format.py, runner.py, api.py, cli.py and mcp_server.py with a fake OrcaSlicer
(tests/fake_orca.py) and the hand-written profile tree; and one real run when OrcaSlicer is installed.
Version: 2026.11.11.1
"""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from print3d_translator import api, format as F, mesh as M, orca, profiles as P, runner as R

HERE = Path(__file__).parent
FAKE = HERE / "fake_orca.py"
FIX = HERE / "fixtures" / "profiles"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("P3DT_ORCA", str(FAKE))
    monkeypatch.setenv("P3DT_ORCA_PROFILES", str(FIX))
    monkeypatch.setenv("P3DT_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("FAKE_ORCA_MODE", "ok")
    monkeypatch.setenv("PYTHONPATH", str(HERE.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    orca.version.cache_clear()
    M.write_stl(tmp_path / "cube.stl", M.box(20, 20, 10))
    return tmp_path


def p2s(tmp, **kw):
    s = {"printer": {"model": "P2S"}, "filaments": [{"profile": "Bambu PLA Basic"}],
         "objects": [{"name": "cube", "part": {"stl": str(tmp / "cube.stl")}}]}
    s.update(kw)
    return s


def h2d(tmp, **kw):
    s = {"printer": {"model": "H2D"},
         "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right", "color": "#FF0000"},
                       {"type": "PETG", "extruder": "left", "color": "#00FF00"}],
         "objects": [{"name": "a", "part": {"stl": str(tmp / "cube.stl")}},
                     {"name": "b", "part": {"stl": str(tmp / "cube.stl")}, "filament": 2}]}
    s.update(kw)
    return s


# ------------------------------------------------------------- format

def test_study_format_is_compact_and_has_status(fake):
    f = api.study_format()
    assert len(json.dumps(f)) < 12000
    assert f["status"]["orca"]["version"] == "2.4.2" and f["example"]


def test_validate_reports_fixes(fake):
    bad = {"printer": {"model": "P2S"}, "filaments": [{"profile": "Nope"}],
           "objects": [{"part": {"stl": "/nope.stl"}, "copies": 0, "orient": "sideways"}], "extra": 1}
    v = api.validate({"good": p2s(fake), "bad": bad, "bad name!": p2s(fake)})
    assert v["good"]["ok"] and v["good"]["estimate"]["minutes"] > 0
    e = " | ".join(v["bad"]["errors"])
    assert "unknown keys ['extra']" in e and "not found" in e and "copies" in e and "orient" in e
    assert not v["bad name!"]["ok"]
    v = api.validate({"x": p2s(fake, filaments=[{"profile": "Bambu PLA Basci"}])})
    assert "choose one of" in v["x"]["errors"][0]


def test_validate_mixed_temperatures(fake):
    two = {"filaments": [{"profile": "Bambu PLA Basic"}, {"type": "PETG"}],
           "objects": [{"name": "a", "part": {"stl": str(fake / "cube.stl")}},
                       {"name": "b", "part": {"stl": str(fake / "cube.stl")}, "filament": 2}]}
    assert not api.validate({"x": p2s(fake, **two)})["x"]["ok"]
    assert api.validate({"x": p2s(fake, allow_mix_temp=True, **two)})["x"]["ok"]
    assert api.validate({"x": h2d(fake)})["x"]["ok"]
    same = h2d(fake)
    same["filaments"][1]["extruder"] = "right"
    assert "share the right nozzle" in api.validate({"x": same})["x"]["errors"][0]


def test_validate_without_profiles(fake, monkeypatch):
    monkeypatch.setenv("P3DT_ORCA_PROFILES", str(fake / "none"))
    monkeypatch.setenv("P3DT_ORCA", str(fake / "none"))
    v = api.validate({"x": p2s(fake)})["x"]
    assert v["ok"] and "not found" in v["warnings"][0]


# ------------------------------------------------------------- runner

def test_run_study_reliable_with_files(fake):
    res = R.run_study("s", p2s(fake, objects=[{"name": "cube", "part": {"stl": str(fake / "cube.stl")}, "copies": 2}],
                                expect=[{"quantity": "objects.cube.volume_mm3", "value": 4000}]), fake / "s")
    assert res["ok"] and res["reliable"], res.get("unreliable_because")
    assert Path(res["files"]["gcode_3mf"]).name == "s.gcode.3mf" and Path(res["files"]["gcode"]).exists()
    assert res["checks"]["printer_model_id"]["passed"] and "slice_info.config" in res["slice_info"]["finalized"] and "thumbnails" in res["slice_info"]["finalized"]
    assert res["checks"]["settings_roundtrip"]["passed"] and res["expect"][0]["pass"]
    assert len(res["objects"]["cube"]["centers"]) == 2 and res["estimate"]["time_s"] == 750
    assert "OrcaSlicer: fake_warning" in res["warnings"]
    saved = json.loads((fake / "s" / "s.json").read_text())
    assert saved["reliable"] and json.loads((fake / "s" / "state.json").read_text())["state"] == "done"


def test_supports_needed(fake, monkeypatch):
    cube = [{"name": "cube", "part": {"stl": str(fake / "cube.stl")}}]
    # the part prints without supports: one slice, no supports
    res = R.run_study("n", p2s(fake, objects=cube, intent={"supports": "needed"}), fake / "n")
    assert res["reliable"] and res["supports_decision"]["used"] == "none", (res.get("why"), res.get("unreliable_because"))
    assert not (fake / "n" / "no_supports").exists()
    # the part overhangs: sliced again with tree supports, the first slice kept in no_supports/
    monkeypatch.setenv("FAKE_ORCA_MODE", "overhang")
    plain = R.run_study("o", p2s(fake, objects=cube, intent={"supports": "none"}), fake / "o")
    assert not plain["reliable"] and not plain["checks"]["unsupported_extrusion"]["passed"]
    res = R.run_study("h", p2s(fake, objects=cube, intent={"supports": "needed"}), fake / "h")
    d = res["supports_decision"]
    assert res["reliable"], res.get("unreliable_because")
    assert d["used"] == "tree" and "without supports: z" in d["why"] and d["without_supports"]["severe_mm"] > 5
    first = json.loads(Path(d["without_supports"]["result"]).read_text())
    assert not first["reliable"] and Path(first["files"]["gcode_3mf"]).parent.name == "no_supports"
    assert json.loads((fake / "h" / "h.json").read_text())["supports_decision"]["used"] == "tree"
    assert res["profiles"]["set"]["process.support_type"][1] == "tree(auto)"


def test_run_study_h2d_tower_and_map(fake):
    res = R.run_study("h", h2d(fake), fake / "h")
    assert res["reliable"], res["unreliable_because"]
    assert res["placement"]["tower"] and res["profiles"]["filament_map"] == [2, 1]
    assert res["profiles"]["set"]["process.wipe_tower_x"][1] == [str(res["placement"]["tower"]["x"]).rstrip("0").rstrip(".")]
    assert any("bed_vs_filaments" in w for w in res["warnings"])


@pytest.mark.parametrize("mode,why", [("shift", "footprint"), ("hot", "temperatures")])
def test_run_study_catches_bad_slices(fake, monkeypatch, mode, why):
    monkeypatch.setenv("FAKE_ORCA_MODE", mode)
    res = R.run_study("x", p2s(fake), fake / mode)
    assert res["ok"] and not res["reliable"] and any(b.startswith(why) for b in res["unreliable_because"])


@pytest.mark.parametrize("mode,why", [("fail17", "PROCESS_NOT_COMPATIBLE"), ("crash", "CRASHED")])
def test_run_study_slicer_errors(fake, monkeypatch, mode, why):
    monkeypatch.setenv("FAKE_ORCA_MODE", mode)
    res = R.run_study("x", p2s(fake), fake / mode)
    assert not res["ok"] and why in res["why"]


def test_run_study_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "sleep")
    t = time.monotonic()
    res = R.run_study("x", p2s(fake, run={"timeout_s": 1}), fake / "t")
    assert not res["ok"] and "timed out" in res["why"] and time.monotonic() - t < 20


def test_run_study_geometry_and_placement_errors(fake):
    M.write_stl(fake / "huge.stl", M.box(300, 20, 10))
    res = R.run_study("x", p2s(fake, objects=[{"part": {"stl": str(fake / "huge.stl")}}]), fake / "g")
    assert not res["ok"] and "fits" in res["why"]
    res = R.run_study("y", p2s(fake, objects=[{"part": {"stl": str(fake / "cube.stl")}, "at": [2, 2]}]), fake / "p")
    assert not res["ok"] and "outside the area" in res["why"]


def test_expectations_and_lookup():
    res = {"estimate": {"filament_g": 10.0}, "gcode": {"layers": 50}, "a": [{"b": 3}]}
    assert R.lookup(res, "a.0.b") == 3 and R.lookup(res, "x.y") is None
    out = R.expectations({"expect": [{"quantity": "estimate.filament_g", "value": 10.4},
                                     {"quantity": "gcode.layers", "value": 49, "tol": 0},
                                     {"quantity": "nope", "value": 1}]}, res)
    assert [e["pass"] for e in out] == [True, False, False]


# ------------------------------------------------------------- api / cli / mcp

def test_api_run_status_inputs_and_read_file(fake):
    out = api.run({"one": p2s(fake), "two": h2d(fake)}, wait=60)
    assert out["done"] and set(out["studies"]) == {"one", "two"}
    assert all(r["reliable"] for r in out["studies"].values())
    again = api.status(out["request"])
    assert again["done"] and again["studies"]["one"]["reliable"]
    f = api.read_file(out["studies"]["one"]["files"]["gcode_3mf"])
    assert f["bytes"] > 0 and f["base64"]
    assert "error" in api.read_file("/etc/hosts")
    inp = api.inputs(p2s(fake, intent={"walls": 3}), "i")
    assert inp["ok"] and inp["profiles"]["set"]["process.wall_loops"] == ["2", "3"] and inp["command"][0] == str(FAKE)
    bad = api.run({"x": p2s(fake, filaments=[{"profile": "Nope"}])})
    assert "error" in bad and not bad["validation"]["x"]["ok"]


def test_api_cancel(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "sleep")
    d = api.start({"slow": p2s(fake)})
    for _ in range(100):
        st = api.status(str(d))
        if st["progress"].get("slow", {}).get("state") == "slicing":
            break
        time.sleep(0.2)
    assert api.cancel(str(d))["cancelled"] == ["slow"]
    st = api.status(str(d), wait=30)
    assert st["done"] and "cancelled" in st["studies"]["slow"]["why"]


def test_cli(fake):
    (fake / "studies.json").write_text(json.dumps({"c": p2s(fake, objects=[{"part": {"stl": "cube.stl"}}])}))
    env = {**os.environ}
    run = subprocess.run([sys.executable, "-m", "print3d_translator.cli", "run", str(fake / "studies.json")],
                         capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    out = json.loads(run.stdout)
    assert out["studies"]["c"]["reliable"]
    st = subprocess.run([sys.executable, "-m", "print3d_translator.cli", "status"], capture_output=True, text=True,
                        env=env, timeout=120)
    assert json.loads(st.stdout)["orca"]["version"] == "2.4.2"
    val = subprocess.run([sys.executable, "-m", "print3d_translator.cli", "validate", "-"],
                         input=json.dumps({"x": {"printer": {}}}), capture_output=True, text=True, env=env, timeout=120)
    assert val.returncode == 1 and not json.loads(val.stdout)["x"]["ok"]


def test_mcp_tools(fake):
    mcp = pytest.importorskip("mcp")
    import asyncio
    from print3d_translator import mcp_server as S
    names = {t.name for t in asyncio.run(S.server.list_tools())}
    assert names == {"study_format", "validate_studies", "run_studies", "study_status", "get_inputs",
                     "run_benchmarks", "bench_status", "get_file", "make_view", "cancel"}
    assert len(S.INSTRUCTIONS.splitlines()) <= 30


# ------------------------------------------------------------- real OrcaSlicer (optional)

REAL = orca.find_orca() if not os.environ.get("P3DT_ORCA") else None


@pytest.mark.skipif(not REAL or orca.version(REAL) is None, reason="OrcaSlicer not installed")
def test_real_bench_cube(tmp_path, monkeypatch):
    monkeypatch.setenv("P3DT_OUT", str(tmp_path))
    out = api.bench(["p2s_cube"], wait=300)
    assert out["passed"] == ["p2s_cube"], out


def test_explicit_profiles_setting_has_no_fallback(fake, monkeypatch):
    monkeypatch.setenv("P3DT_ORCA_PROFILES", str(fake / "none"))
    assert P.find_profiles() is None


def test_summaries_are_small(fake):
    out = api.run({"one": p2s(fake), "two": h2d(fake)}, wait=60)
    s = api.summarize_request(out)
    assert len(json.dumps(s)) < 0.5 * len(json.dumps(out))
    one = s["studies"]["one"]
    assert one["reliable"] and one["result"].endswith("one/one.json") and "settings_roundtrip" in one["checks_passed"]
    assert not one.get("checks_failed")


def test_view_written_by_study_and_by_api(fake):
    res = R.run_study("v", p2s(fake, view=True), fake / "v")
    assert res["reliable"] and Path(res["files"]["view"]).name == "v.view.html"
    html = Path(res["files"]["view"]).read_text()
    assert "v.view.html" not in html and '"title": "v"' in html
    Path(res["files"]["view"]).unlink()
    out = api.view(str(fake / "v" / "v.json"))
    assert Path(out["view"]).exists() and out["bytes"] > 1000
    assert "error" in api.view(str(fake / "nope"))
    assert not api.validate({"x": p2s(fake, view="yes")})["x"]["ok"]


# ------------------------------------------------------------- objects of several parts, 3MF projects, layout

def bambu_3mf(path, objects, plates=None):
    """A Bambu-Studio-like project: objects [{id, name, at: (x, y), extruder, settings, parts: [{id, box, z0,
    extruder, subtype}]}], plates [[object ids]] (6 plates at most; bed 256)."""
    import zipfile
    import numpy as np
    sub, res, items, ms = [], [], [], ['<?xml version="1.0" encoding="UTF-8"?>\n<config>']
    for o in objects:
        comps = []
        ms.append(f'<object id="{o["id"]}"><metadata key="name" value="{o["name"]}"/>'
                  f'<metadata key="extruder" value="{o.get("extruder", 1)}"/>'
                  + "".join(f'<metadata key="{k}" value="{v}"/>' for k, v in (o.get("settings") or {}).items()))
        for p in o["parts"]:
            t = M.box(*p["box"]) + np.array([0, 0, p.get("z0", 0.0)])
            v = t.reshape(-1, 3)
            sub.append(f'<object id="{p["id"]}" type="model"><mesh><vertices>'
                       + "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v)
                       + "</vertices><triangles>"
                       + "".join(f'<triangle v1="{3 * i}" v2="{3 * i + 1}" v3="{3 * i + 2}"/>' for i in range(len(t)))
                       + "</triangles></mesh></object>")
            comps.append(f'<component p:path="/3D/Objects/o.model" objectid="{p["id"]}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            ms.append(f'<part id="{p["id"]}" subtype="{p.get("subtype", "normal_part")}"><metadata key="name" value="{o["name"]}_{p["id"]}"/>'
                      + (f'<metadata key="extruder" value="{p["extruder"]}"/>' if p.get("extruder") else "") + "</part>")
        ms.append("</object>")
        res.append(f'<object id="{o["id"]}" type="model"><components>{"".join(comps)}</components></object>')
        x, y = o.get("at", (128, 128))
        items.append(f'<item objectid="{o["id"]}" transform="1 0 0 0 1 0 0 0 1 {x} {y} 0"/>')
    for i, ids in enumerate(plates or []):
        ms.append(f'<plate><metadata key="plater_id" value="{i + 1}"/><metadata key="plater_name" value="P{i + 1}"/>'
                  + "".join(f'<model_instance><metadata key="object_id" value="{oid}"/><metadata key="instance_id" value="0"/></model_instance>'
                            for oid in ids) + "</plate>")
    ms.append("</config>")
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources>{"".join(res)}</resources>'
                                       f'<build>{"".join(items)}</build></model>')
        z.writestr("3D/Objects/o.model", f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources>{"".join(sub)}</resources></model>')
        z.writestr("Metadata/model_settings.config", "\n".join(ms))
        z.writestr("Metadata/project_settings.config", json.dumps({"printable_area": ["0x0", "256x0", "256x256", "0x256"]}))
    return path


def two_colours(tmp, **kw):
    return p2s(tmp, filaments=[{"profile": "Bambu PLA Basic", "color": "#FFFFFF"},
                               {"profile": "Bambu PLA Basic", "color": "#1E88E5"}], **kw)


def test_object_of_two_parts_with_their_filaments(fake):
    M.write_stl(fake / "floor.stl", M.box(30, 20, 2))
    M.write_stl(fake / "seats.stl", M.box(30, 20, 6) + [0, 0, 2])      # fake_orca prints bounding boxes
    st = two_colours(fake, objects=[{"name": "cabin", "parts": [
        {"name": "floor", "part": {"stl": str(fake / "floor.stl")}, "filament": 1},
        {"name": "seats", "part": {"stl": str(fake / "seats.stl")}, "filament": 2}]},
        {"name": "cube", "part": {"stl": str(fake / "cube.stl")}}])
    res = R.run_study("c", st, fake / "c")
    assert res["reliable"], res.get("unreliable_because")
    asm = json.loads((fake / "c" / "assemble.json").read_text())["plates"][0]["objects"]
    assert [o.get("assemble_index") for o in asm] == [[1], [1], None] and [o["filaments"] for o in asm] == [[1], [2], [1]]
    assert res["checks"]["tools"]["got"]["by_object"] == {"assemble_1": [1, 2], "cube_1": [1]}
    assert res["objects"]["cabin"]["filament"] == [1, 2] and [p["name"] for p in res["objects"]["cabin"]["parts"]] == ["floor", "seats"]
    assert res["placement"]["tower"] and res["estimate"]["by_filament"][1]["m"] > 0     # two filaments: a prime tower
    # the seats stay on the floor (one rigid object): parts placed together
    floor, seats = (M.read_stl(fake / "c" / "objects" / f"cabin__p{i}.stl") for i in (1, 2))
    assert M.bbox(seats)[0][2] == pytest.approx(2.0) and M.bbox(floor)[0][2] == pytest.approx(0.0)


def test_3mf_keeps_its_part_filaments(fake):
    p = bambu_3mf(fake / "cab.3mf", [{"id": 3, "name": "C2", "parts": [
        {"id": 1, "box": (30, 20, 2), "extruder": 1}, {"id": 2, "box": (30, 20, 6), "z0": 2, "extruder": 2},
        {"id": 4, "box": (5, 5, 5), "subtype": "modifier_part"}]}])
    res = R.run_study("t", two_colours(fake, objects=[{"name": "C2", "part": {"3mf": str(p)}}]), fake / "t")
    assert res["reliable"], res.get("unreliable_because")
    assert res["objects"]["C2"]["filament"] == [1, 2]
    assert res["objects"]["C2"]["volume_mm3"] == pytest.approx(30 * 20 * 8)      # the modifier is not a solid
    assert any("modifier_part" in w for w in res["warnings"])
    # a one-filament study: the 3MF's filament 2 has no filament here
    bad = R.run_study("u", p2s(fake, objects=[{"name": "C2", "part": {"3mf": str(p)}}]), fake / "u")
    assert not bad["ok"] and "filament_map" in bad["why"]
    ok = R.run_study("v", p2s(fake, objects=[{"name": "C2", "part": {"3mf": str(p), "filament_map": {"2": 1}}}]), fake / "v")
    assert ok["reliable"] and ok["objects"]["C2"]["filament"] == 1, ok.get("unreliable_because")


def test_from_3mf_plate_keeps_layout_and_settings(fake):
    p = bambu_3mf(fake / "proj.3mf", [
        {"id": 2, "name": "wing.stl", "at": (60, 60), "settings": {"wall_loops": "4", "skeleton_infill_density": "5%"},
         "parts": [{"id": 1, "box": (40, 30, 5)}]},
        {"id": 4, "name": "tail.stl", "at": (190, 60), "parts": [{"id": 3, "box": (30, 30, 5)}]},
        {"id": 6, "name": "nose.stl", "at": (307.2 + 100, 100), "parts": [{"id": 5, "box": (20, 20, 5)}]}],
        plates=[[2, 4], [6]])
    v = F.validate({"s": p2s(fake, objects=[{"from_3mf": {"path": str(p), "plate": 1}}])}, vendor=P.Vendor(FIX))["s"]
    assert v["ok"], v["errors"]
    assert any("skeleton_infill_density" in w for w in v["warnings"])
    res = R.run_study("p", p2s(fake, objects=[{"from_3mf": {"path": str(p), "plate": 1}}]), fake / "p")
    assert res["reliable"], res.get("unreliable_because")
    assert sorted(res["objects"]) == ["tail_4", "wing_2"]
    assert res["objects"]["wing_2"]["centers"] == [[80.0, 75.0]] and res["objects"]["wing_2"]["settings"] == {"wall_loops": "4"}
    asm = json.loads((fake / "p" / "assemble.json").read_text())["plates"][0]["objects"]
    assert {"wall_loops": "4"} in [o.get("print_params") for o in asm]
    # plate 2's object, at its plate-local position
    res2 = R.run_study("q", p2s(fake, objects=[{"from_3mf": {"path": str(p), "plate": 2}}]), fake / "q")
    assert res2["reliable"] and res2["objects"]["nose_6"]["centers"] == [[110.0, 110.0]]


def test_layout_overlap_names_the_other_object_and_options(fake):
    M.write_stl(fake / "slab.stl", M.box(40, 20, 3))
    near = [{"name": "a", "part": {"stl": str(fake / "slab.stl")}, "at": [100, 100]},
            {"name": "b", "part": {"stl": str(fake / "slab.stl")}, "at": [100, 122]}]       # 2 mm apart
    bad = R.run_study("n", p2s(fake, objects=near, intent={"brim": "none"}), fake / "n")
    assert not bad["ok"] and "overlaps object a" in bad["why"]
    ok = R.run_study("m", p2s(fake, objects=near, intent={"brim": "none"}, layout={"gap": 1}), fake / "m")
    assert ok["reliable"], ok.get("why") or ok.get("unreliable_because")
    brims = R.run_study("b", p2s(fake, objects=near, intent={"brim": 5}, layout={"gap": 1}), fake / "b")
    assert not brims["ok"] and "overlaps" in brims["why"]
    ok2 = R.run_study("o", p2s(fake, objects=near, intent={"brim": 5}, layout={"gap": 1, "brims_may_overlap": True}), fake / "o")
    assert ok2["ok"], ok2.get("why")


def test_placement_packs_rows_when_the_centre_fragments(fake):
    import numpy as np
    from print3d_translator import placement as PL
    bed = np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float)
    items = [{"id": f"f{i}", "tri": M.place(M.box(64, 69, 50), (0, 0)), "halo": 13.0, "extruders": None, "at": None}
             for i in range(3)] + \
            [{"id": f"c{i}", "tri": M.place(M.box(49, 36, 10), (0, 0)), "halo": 13.0, "extruders": None, "at": None}
             for i in range(2)]
    r = PL.place(items, bed)
    assert len(r["placed"]) == 5 and r["strategy"] in ("centre", "bottom-left", "shelves")


def test_first_layer_overlap_allows_interleaving_but_not_touching(fake):
    import numpy as np
    from print3d_translator import placement as PL
    bed = np.array([[0, 0], [256, 0], [256, 256], [0, 256]], float)
    # an L standing on a small foot whose arm reaches over the other part's base
    foot = M.box(10, 10, 20)
    arm = M.box(40, 10, 5) + np.array([0, 0, 20])
    L_ = np.concatenate([foot, arm])
    low = M.box(15, 10, 8)
    a = {"id": "L", "tri": M.place(L_, (0, 0)), "halo": 1.0, "extruders": None, "at": [100, 100]}
    b = {"id": "low", "tri": M.place(low, (0, 0)), "halo": 1.0, "extruders": None, "at": [110, 100]}
    with pytest.raises(PL.PlacementError):
        PL.place([a, b], bed)                                   # silhouettes overlap
    r = PL.place([a, b], bed, overlap="first_layer")             # interleaved, 12 mm apart in z
    assert r["collisions_checked"]
    tall = {**b, "tri": M.place(M.box(15, 10, 25), (0, 0))}      # reaches the arm
    with pytest.raises(PL.PlacementError, match="come within"):
        PL.place([a, tall], bed, overlap="first_layer")


def test_color_change_and_pause(fake):
    st = two_colours(fake, intent={"color_changes": [{"z": 5, "filament": 2}], "pauses": [{"z": 3, "note": "magnets"}]})
    res = R.run_study("cc", st, fake / "cc")
    assert res["reliable"], res.get("unreliable_because")
    asm = json.loads((fake / "cc" / "assemble.json").read_text())["plates"][0]["objects"]
    assert asm[0]["height_ranges"] == [{"min_z": 5.0, "max_z": 10000.0, "range_params": {"extruder": "2"}}]
    assert res["checks"]["tools"]["got"]["by_object"] == {"cube_1": [1, 2]}
    assert res["checks"]["pauses"]["passed"] and res["pauses"][0]["layer_z"] == pytest.approx(3.2)
    text = Path(res["files"]["gcode"]).read_text()
    assert "; PAUSE_PRINTING" in text and "M400 U1" in text.split("; PAUSE_PRINTING")[1][:200]
    import hashlib, zipfile
    with zipfile.ZipFile(res["files"]["gcode_3mf"]) as z:
        g = z.read("Metadata/plate_1.gcode")
        assert z.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(g).hexdigest().upper()
    assert g.decode() == text                                     # the file checked is the file printed
    # a change to a filament the object does not get is caught
    from print3d_translator import check as C, gcode as G
    prof = fake / "cc" / "profiles"
    resolved = {"machine": json.loads((prof / "machine.json").read_text()), "process": json.loads((prof / "process.json").read_text()),
                "filaments": [json.loads((prof / f"filament_{i}.json").read_text()) for i in (1, 2)],
                "plate": {"name": "textured_pei", "bed_type": "Textured PEI Plate", "temp_key": "textured_plate_temp"}}
    r = C.check_gcode(G.parse(text), resolved,
                      [{"name": "cube_1", "filament": 1, "tri": M.place(M.box(20, 20, 10), (128, 128))}],
                      color_changes=[{"z": 5, "filament": 1}])
    assert not r["checks"]["tools"]["passed"]


def test_accept_a_known_unsupported_bridge(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "overhang")
    cube = [{"name": "cube", "part": {"stl": str(fake / "cube.stl")}}]
    plain = R.run_study("o", p2s(fake, objects=cube, intent={"supports": "none"}), fake / "o")
    assert not plain["reliable"]
    zs = [w["z_mm"] for w in plain["checks"]["unsupported_extrusion"]["got"]["worst"]]
    acc = [{"check": "unsupported_extrusion", "object": "cube", "z_range": [min(zs) - 0.1, max(zs) + 0.1],
            "reason": "internal floor bridges by design; supports would be trapped"}]
    res = R.run_study("a", p2s(fake, objects=cube, intent={"supports": "none"}, accept=acc), fake / "a")
    assert res["reliable"], res.get("unreliable_because")
    assert res["accepted"][0]["covered"]["severe_mm"] > 5 and res["checks"]["unsupported_extrusion"]["accepted"]
    assert "_findings" not in res["checks"]["unsupported_extrusion"]
    # another height range does not cover it
    off = [{**acc[0], "z_range": [0.0, 1.0]}]
    res2 = R.run_study("b", p2s(fake, objects=cube, intent={"supports": "none"}, accept=off), fake / "b")
    assert not res2["reliable"] and any("matched nothing" in w for w in res2["warnings"])
    # checks that make the file wrong cannot be accepted
    v = F.validate({"s": p2s(fake, accept=[{"check": "bounds", "reason": "because"}])})["s"]
    assert not v["ok"] and "cannot be accepted" in v["errors"][0]


def test_path_conflict_is_explained(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "conflict")
    M.write_stl(fake / "slab.stl", M.box(40, 20, 3))
    objs = [{"name": "a", "part": {"stl": str(fake / "slab.stl")}, "at": [100, 100]},
            {"name": "b", "part": {"stl": str(fake / "slab.stl")}, "at": [100, 130]}]
    res = R.run_study("x", p2s(fake, objects=objs, intent={"brim": "none"}), fake / "x")
    assert not res["ok"] and "toolpaths of a and b" in res["why"] and "not a multi-extruder" in res["why"]
    assert "10.0 mm apart" in res["why"]


def test_needed_falls_back_to_normal_supports_on_conflict(fake, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_MODE", "overhang,tree_conflict")
    cube = [{"name": "cube", "part": {"stl": str(fake / "cube.stl")}}]
    res = R.run_study("n", p2s(fake, objects=cube, intent={"supports": "needed"}), fake / "n")
    d = res["supports_decision"]
    assert res["reliable"], res.get("unreliable_because") or res.get("why")
    assert [t["supports"] for t in d["tried"]] == ["tree", "auto"] and d["used"] == "auto"
    assert "support_cost" in d and not d["tried"][0]["ok"]
