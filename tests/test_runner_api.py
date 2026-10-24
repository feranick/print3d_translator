"""End-to-end tests of format.py, runner.py, api.py, cli.py and mcp_server.py with a fake OrcaSlicer
(tests/fake_orca.py) and the hand-written profile tree; and one real run when OrcaSlicer is installed.
Version: 2026.10.24.7
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
    assert res["checks"]["printer_model_id"]["passed"] and res["slice_info"]["printer_model_id_patched"]
    assert res["checks"]["settings_roundtrip"]["passed"] and res["expect"][0]["pass"]
    assert len(res["objects"]["cube"]["centers"]) == 2 and res["estimate"]["time_s"] == 750
    assert "OrcaSlicer: fake_warning" in res["warnings"]
    saved = json.loads((fake / "s" / "s.json").read_text())
    assert saved["reliable"] and json.loads((fake / "s" / "state.json").read_text())["state"] == "done"


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
                     "run_benchmarks", "bench_status", "get_file", "cancel"}
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
