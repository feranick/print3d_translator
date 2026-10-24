"""Tests of mesh.py and orca.py without OrcaSlicer (a fake executable stands in for it), and one real
slice when OrcaSlicer is installed (skipped otherwise).
Version: 2026.10.24.2
"""
import json
import os
import stat
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from print3d_translator import mesh, orca, profiles as P


# ------------------------------------------------------------- mesh

def test_box_volume_area_bbox():
    b = mesh.box(10, 20, 30)
    assert mesh.volume(b) == pytest.approx(6000)
    assert mesh.area(b) == pytest.approx(2 * (200 + 300 + 600))
    assert np.allclose(mesh.bbox(b), [[0, 0, 0], [10, 20, 30]])


def test_stl_roundtrip_binary_and_ascii(tmp_path):
    b = mesh.box(1.5, 2, 3, origin=(5, 6, 7))
    p = mesh.write_stl(tmp_path / "b.stl", b)
    assert np.allclose(mesh.read_stl(p), b)
    assert p.read_bytes() == mesh.write_stl(tmp_path / "c.stl", b).read_bytes()
    lines = ["solid t"] + [f"facet normal 0 0 0\nouter loop\n" + "".join(f"vertex {x} {y} {z}\n" for x, y, z in t)
                           + "endloop\nendfacet" for t in b] + ["endsolid t"]
    (tmp_path / "a.stl").write_text("\n".join(lines))
    assert np.allclose(mesh.read_stl(tmp_path / "a.stl"), b)


def test_rotation_and_place():
    b = mesh.box(10, 20, 30)
    r = mesh.transform(b, mesh.rotation(90, 0, 0))          # Y -> Z
    lo, hi = mesh.bbox(r)
    assert np.allclose(hi - lo, [10, 30, 20])
    assert mesh.volume(r) == pytest.approx(6000)
    p = mesh.place(r, (100, 50))
    lo, hi = mesh.bbox(p)
    assert np.allclose((lo + hi)[:2] / 2, [100, 50]) and lo[2] == pytest.approx(0)


# ------------------------------------------------------------- orca: pure functions

@pytest.mark.parametrize("rc,code,name", [(0, 0, "SUCCESS"), (239, -17, "PROCESS_NOT_COMPATIBLE"),
                                          (204, -52, "OBJECTS_PARTLY_INSIDE"), (156, -100, "SLICING_ERROR")])
def test_decode(rc, code, name):
    d = orca.decode(rc)
    assert d["code"] == code and d["name"] == name


def test_decode_signal():
    assert orca.decode(-11)["name"].startswith("CRASHED")


def test_assemble_list_and_command(tmp_path):
    al = orca.assemble_list([{"path": tmp_path / "a.stl", "filament": 2}])
    o = al["plates"][0]
    assert o["need_arrange"] is False and o["objects"][0]["filaments"] == [2]
    assert o["objects"][0]["pos_x"] == [0.0] and o["objects"][0]["count"] == 1
    argv = orca.command("orca", tmp_path, "m.json", "p.json", ["f1.json", "f2.json"], "a.json",
                        "Textured PEI Plate", [2, 1])
    assert argv[argv.index("--load-settings") + 1] == "m.json;p.json"
    assert argv[argv.index("--load-filaments") + 1] == "f1.json;f2.json"
    assert argv[argv.index("--curr-bed-type") + 1] == "Textured PEI Plate"
    assert argv[argv.index("--export-3mf") + 1] == "plate.gcode.3mf"            # relative to --outputdir
    assert argv[argv.index("--filament-map") + 1] == "2,1"
    assert "--min-save" not in argv
    # no option that would reach a printer or the network
    assert not any(a for a in argv if any(w in a.lower() for w in ("upload", "send", "host", "cloud", "print_host")))


# ------------------------------------------------------------- orca.run with a fake executable

def fake(tmp_path, body: str) -> str:
    p = tmp_path / "fake-orca"
    p.write_text(f"#!{sys.executable}\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_run_success_and_failure(tmp_path):
    ok = fake(tmp_path, "import sys; print('sliced'); sys.exit(0)")
    r = orca.run([ok], tmp_path / "a")
    assert r["ok"] and r["code"] == 0 and "sliced" in r["stdout"]
    assert json.loads((tmp_path / "a" / "command.json").read_text()) == [ok]
    bad = fake(tmp_path, "import sys; sys.stderr.write('no'); sys.exit(-17 % 256)")
    r = orca.run([bad], tmp_path / "b")
    assert not r["ok"] and r["code"] == -17 and "compatible" in r["fix"]


def test_run_timeout_kills_group(tmp_path):
    slow = fake(tmp_path, "import subprocess, sys, time\n"
                          "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                          "time.sleep(60)")
    t = time.monotonic()
    r = orca.run([slow], tmp_path / "c", timeout=1)
    assert r["timed_out"] and not r["ok"] and time.monotonic() - t < 15


def test_run_cancel_flag(tmp_path):
    slow = fake(tmp_path, "import time; time.sleep(60)")
    flag = tmp_path / "cancel"
    flag.write_text("")
    r = orca.run([slow], tmp_path / "d", timeout=60, cancel_file=flag)
    assert r["cancelled"] and not r["ok"]


def test_version_parsing(tmp_path):
    f = fake(tmp_path, "print('OrcaSlicer-2.4.2:'); print('Usage: orca-slicer')")
    orca.version.cache_clear()
    assert orca.version(f) == "2.4.2"


def test_find_orca_env(tmp_path, monkeypatch):
    f = fake(tmp_path, "pass")
    monkeypatch.setenv("P3DT_ORCA", f)
    assert orca.find_orca() == f
    monkeypatch.setenv("P3DT_ORCA", str(tmp_path / "nope"))
    assert orca.find_orca() is None


# ------------------------------------------------------------- one real slice (optional)

EXE = orca.find_orca()


@pytest.mark.skipif(not EXE or not P.find_profiles(EXE), reason="OrcaSlicer not installed")
def test_real_slice_cube(tmp_path):
    v = P.Vendor(P.find_profiles(EXE))
    r = P.resolve(v, {"printer": {"model": "P2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}]})
    files = P.write(r, tmp_path)
    stl = mesh.write_stl(tmp_path / "cube.stl", mesh.place(mesh.box(20, 20, 20), (60, 80)))
    (tmp_path / "assemble.json").write_text(json.dumps(orca.assemble_list([{"path": stl, "filament": 1}])))
    argv = orca.command(EXE, tmp_path, files["machine"], files["process"], files["filaments"],
                        str(tmp_path / "assemble.json"), r["plate"]["bed_type"])
    res = orca.run(argv, tmp_path, timeout=600)
    assert res["ok"], res
    with zipfile.ZipFile(tmp_path / "plate.gcode.3mf") as z:
        assert "Metadata/plate_1.gcode" in z.namelist()
