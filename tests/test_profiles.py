"""Tests of the profile resolver on a hand-written profile tree (tests/fixtures/profiles), and on the
installed OrcaSlicer's profiles when they are found (skipped otherwise).
Version: 2026.10.24.1
"""
import json
from pathlib import Path

import pytest

from print3d_translator import profiles as P

FIX = Path(__file__).parent / "fixtures" / "profiles"


@pytest.fixture()
def v():
    return P.Vendor(FIX)


def study(**kw):
    s = {"printer": {"model": "P2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}]}
    s.update(kw)
    return s


# ------------------------------------------------------------- flattening

def test_chain_follows_index_not_file_names(v):
    # fdm_process_single_common lives in fdm_process_single_common_v2.json
    assert v.chain("0.20mm Standard @BBL P2S") == ["0.20mm Standard @BBL P2S", "fdm_process_single_common",
                                                    "fdm_process_common"]


def test_flatten_merges_root_first_and_drops_inherits(v):
    f = v.flatten("Bambu PLA Basic @BBL H2D")
    assert "inherits" not in f
    assert f["from"] == "system" and f["name"] == "Bambu PLA Basic @BBL H2D"
    assert f["nozzle_temperature"] == ["220", "220"]          # leaf
    assert f["filament_density"] == ["1.26"]                  # @base over common
    assert f["filament_type"] == ["PLA"]                      # fdm_filament_pla
    assert f["filament_diameter"] == ["1.75"]                 # root
    assert f["filament_id"] == "GFA00"


def test_flatten_replaces_lists_whole(v):
    m = v.flatten("Bambu Lab H2D 0.4 nozzle")
    assert m["retraction_length"] == ["0.8", "0.8", "0.8", "0.8"]
    assert m["nozzle_diameter"] == ["0.4", "0.4"]
    assert m["printable_height"] == "325"


def test_flatten_returns_copies(v):
    a = v.flatten("0.20mm Standard @BBL P2S")
    a["wall_loops"] = "9"
    assert v.flatten("0.20mm Standard @BBL P2S")["wall_loops"] == "2"


def test_missing_parent_and_loop(tmp_path):
    (tmp_path / "BBL" / "process").mkdir(parents=True)
    idx = {"process_list": [{"name": "a", "sub_path": "process/a.json"}, {"name": "b", "sub_path": "process/b.json"}]}
    (tmp_path / "BBL.json").write_text(json.dumps(idx))
    (tmp_path / "BBL/process/a.json").write_text(json.dumps({"name": "a", "inherits": "b"}))
    (tmp_path / "BBL/process/b.json").write_text(json.dumps({"name": "b", "inherits": "a"}))
    with pytest.raises(P.ProfileError, match="loop"):
        P.Vendor(tmp_path).flatten("a")
    (tmp_path / "BBL/process/b.json").write_text(json.dumps({"name": "b", "inherits": "zz"}))
    with pytest.raises(P.ProfileError, match="'zz' is not in"):
        P.Vendor(tmp_path).flatten("a")


def test_no_index(tmp_path):
    with pytest.raises(P.ProfileError, match="P3DT_ORCA_PROFILES"):
        P.Vendor(tmp_path)


# ------------------------------------------------------------- choosing profiles

def test_machine_names_and_errors(v):
    assert v.machine("H2D", 0.4)[0] == "Bambu Lab H2D 0.4 nozzle"
    assert v.machine("h2d", "0.20")[0] == "Bambu Lab H2D 0.2 nozzle"
    assert v.machine("Bambu Lab P2S", 0.4)[1]["model_id"] == "N7"
    with pytest.raises(P.ProfileError, match="choose one of"):
        v.machine("H2X", 0.4)
    with pytest.raises(P.ProfileError, match="not offered"):
        v.machine("P2S", 0.6)


@pytest.mark.parametrize("preset,model,nozzle,want", [
    ("standard", "P2S", 0.4, "0.20mm Standard @BBL P2S"),
    ("fine", "P2S", 0.4, "0.12mm High Quality @BBL P2S"),
    ("draft", "P2S", 0.4, "0.24mm Standard @BBL P2S"),
    ("fine", "H2D", 0.4, "0.12mm Fine @BBL H2D"),
    ("strength", "H2D", 0.4, "0.20mm Balanced Strength @BBL H2D"),
    ("standard", "H2D", 0.2, "0.10mm Standard @BBL H2D 0.2 nozzle"),
])
def test_presets(v, preset, model, nozzle, want):
    m, _ = v.machine(model, nozzle)
    assert P.choose_process(v, m, nozzle, {"preset": preset})[0] == want


def test_strength_falls_back_with_settings(v):
    r = P.resolve(v, study(intent={"preset": "strength"}))
    assert r["names"]["process"] == "0.20mm Standard @BBL P2S"
    assert r["process"]["wall_loops"] == "4" and r["process"]["sparse_infill_density"] == "25%"
    assert any("strength" in w for w in r["warnings"])


def test_layer_height_picks_nearest_base(v):
    r = P.resolve(v, study(intent={"layer_height": 0.25}))
    assert r["names"]["process"] == "0.24mm Standard @BBL P2S"
    assert r["process"]["layer_height"] == "0.25"


def test_explicit_process(v):
    r = P.resolve(v, study(intent={"process": "0.24mm Standard @BBL P2S"}))
    assert r["names"]["process"] == "0.24mm Standard @BBL P2S"
    with pytest.raises(P.ProfileError, match="not compatible"):
        P.resolve(v, study(intent={"process": "0.20mm Standard @BBL H2D"}))


def test_filament_by_profile_type_and_errors(v):
    m = "Bambu Lab H2D 0.4 nozzle"
    assert P.choose_filament(v, m, {"profile": "Bambu PLA Basic"}) == "Bambu PLA Basic @BBL H2D"
    assert P.choose_filament(v, m, {"type": "PLA", "brand": "Bambu Lab", "variant": "Basic"}) == "Bambu PLA Basic @BBL H2D"
    assert P.choose_filament(v, m, {"type": "PETG"}) == "Generic PETG @BBL H2D"
    # the same filament profile serves two printers through compatible_printers
    assert P.choose_filament(v, "Bambu Lab P2S 0.4 nozzle", {"type": "PETG"}) == "Generic PETG @BBL H2D"
    with pytest.raises(P.ProfileError, match="choose one of"):
        P.choose_filament(v, m, {"profile": "Bambu PLA Basci"})
    with pytest.raises(P.ProfileError, match="'type'"):
        P.choose_filament(v, m, {})


def test_non_instantiable_leaves_are_ignored(v):
    assert "Bambu PLA Basic @BBL P2S old" not in v.compatible("filament", "Bambu Lab P2S 0.4 nozzle")


# ------------------------------------------------------------- intent, overrides, plate

def test_intent_mapping(v):
    r = P.resolve(v, study(intent={"walls": 3, "top_layers": 5, "bottom_layers": 4, "infill": 30,
                                   "infill_pattern": "gyroid", "supports": "tree", "support_angle": 40,
                                   "brim": 4, "seam": "back", "first_layer_height": 0.25}))
    p = r["process"]
    assert (p["wall_loops"], p["top_shell_layers"], p["bottom_shell_layers"]) == ("3", "5", "4")
    assert (p["sparse_infill_density"], p["sparse_infill_pattern"]) == ("30%", "gyroid")
    assert (p["enable_support"], p["support_type"], p["support_threshold_angle"]) == ("1", "tree(auto)", "40")
    assert (p["brim_type"], p["brim_width"]) == ("outer_only", "4")
    assert p["initial_layer_print_height"] == "0.25" and p["seam_position"] == "back"
    assert r["set"]["process.wall_loops"] == ["2", "3"]


@pytest.mark.parametrize("intent,match", [
    ({"wall": 3}, "unknown keys"),
    ({"infill": 120}, "outside"),
    ({"infill_pattern": "spirals"}, "is not one of"),
    ({"supports": "yes"}, "choose one of"),
    ({"brim": "wide"}, "'none', 'auto'"),
    ({"layer_height": 0.5}, "range"),
    ({"walls": 2.5}, "int"),
    ({"preset": "ultra"}, "choose one of"),
])
def test_intent_errors(v, intent, match):
    with pytest.raises(P.ProfileError, match=match):
        P.resolve(v, study(intent=intent))


def test_overrides_go_to_copies_and_are_typed(v):
    r = P.resolve(v, study(overrides={"process": {"outer_wall_speed": 150, "enable_support": True},
                                      "filament": {"1": {"nozzle_temperature": 215}}}))
    assert r["process"]["outer_wall_speed"] == ["150"]
    assert r["process"]["enable_support"] == "1"
    assert r["filaments"][0]["nozzle_temperature"] == ["215"]
    # the cached system profile is unchanged
    assert v.flatten("Bambu PLA Basic @BBL P2S")["nozzle_temperature"] == ["220"]


def test_override_list_replicates_to_current_length(v):
    s = {"printer": {"model": "H2D", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}],
         "overrides": {"filament": {"all": {"nozzle_temperature": 225}}}}
    assert P.resolve(v, s)["filaments"][0]["nozzle_temperature"] == ["225", "225"]


@pytest.mark.parametrize("ov,match", [
    ({"process": {"nozzle_temperature": 200}}, "filament setting"),
    ({"process": {"wal_loops": 3}}, "not an OrcaSlicer"),
    ({"process": {"inherits": "x"}}, "identifies the profile"),
    ({"printer": {}}, "unknown sections"),
    ({"filament": {"2": {"nozzle_temperature": 200}}}, "filament number"),
])
def test_override_errors(v, ov, match):
    with pytest.raises(P.ProfileError, match=match):
        P.resolve(v, study(overrides=ov))


def test_plates(v):
    assert P.resolve(v, study())["plate"]["bed_type"] == "Textured PEI Plate"
    assert P.resolve(v, study(printer={"model": "P2S", "plate": "engineering"}))["plate"]["temp_key"] == "eng_plate_temp"
    with pytest.raises(P.ProfileError, match="not supported"):
        P.resolve(v, study(printer={"model": "P2S", "plate": "cool"}))
    with pytest.raises(P.ProfileError, match="choose one of"):
        P.resolve(v, study(printer={"model": "P2S", "plate": "glass"}))


def test_h2d_extruder_map(v):
    s = {"printer": {"model": "H2D"}, "filaments": [{"profile": "Bambu PLA Basic", "extruder": "right"},
                                                    {"type": "PETG", "extruder": "left"}]}
    r = P.resolve(v, s)
    assert r["filament_map"] == [2, 1] and r["model"]["extruders"] == 2
    s["filaments"][0]["extruder"] = "middle"
    with pytest.raises(P.ProfileError, match="'left', 'right'"):
        P.resolve(v, s)
    assert P.resolve(v, {**s, "filaments": [{"profile": "Bambu PLA Basic"}]})["filament_map"] is None


def test_single_extruder_ignores_extruder(v):
    r = P.resolve(v, study(filaments=[{"profile": "Bambu PLA Basic", "extruder": "left"}]))
    assert r["filament_map"] is None and any("one extruder" in w for w in r["warnings"])


def test_model_without_processes(v):
    m, mm = v.machine("X9", 0.4)
    assert mm["model_id"] == "X9"
    with pytest.raises(P.ProfileError, match="no process profile"):
        P.resolve(v, {"printer": {"model": "X9"}, "filaments": [{"type": "PETG"}]})


def test_color(v):
    r = P.resolve(v, study(filaments=[{"profile": "Bambu PLA Basic", "color": "#FF0000"}]))
    assert r["filaments"][0]["filament_colour"] == ["#FF0000"]


# ------------------------------------------------------------- writing

def test_write_is_reproducible(v, tmp_path):
    r = P.resolve(v, study(intent={"walls": 3}))
    a = P.write(r, tmp_path / "a")
    b = P.write(P.resolve(P.Vendor(FIX), study(intent={"walls": 3})), tmp_path / "b")
    assert a["sha256"] == b["sha256"]
    assert Path(a["machine"]).read_bytes() == Path(b["machine"]).read_bytes()
    loaded = json.loads(Path(a["process"]).read_text())
    assert loaded["wall_loops"] == "3" and "inherits" not in loaded and loaded["from"] == "system"
    assert all(isinstance(x, (str, list)) for x in loaded.values())


# ------------------------------------------------------------- values

@pytest.mark.parametrize("key,value,want", [
    ("wall_loops", 3, "3"), ("wall_loops", "3", "3"), ("layer_height", 0.2, "0.2"),
    ("layer_height", 0.1 + 0.2, "0.3"), ("sparse_infill_density", 20, "20%"),
    ("sparse_infill_density", "20%", "20%"), ("enable_support", "true", "1"), ("enable_support", False, "0"),
    ("brim_type", "no_brim", "no_brim"),
])
def test_to_orca_value(key, value, want):
    assert P.to_orca_value(key, value) == want


def test_keys_data_is_pinned():
    k = P.orca_keys()
    assert k["orca_version"] and "wall_loops" in k["keys"] and len(k["keys"]) > 500


# ------------------------------------------------------------- the installed OrcaSlicer (optional)

REAL = P.find_profiles()


@pytest.mark.skipif(REAL is None or REAL == FIX, reason="OrcaSlicer profiles not found (set P3DT_ORCA_PROFILES)")
@pytest.mark.parametrize("model", ["H2D", "H2S", "P2S"])
@pytest.mark.parametrize("nozzle", [0.2, 0.4, 0.6, 0.8])
def test_real_profiles_resolve(model, nozzle):
    v = P.Vendor(REAL)
    for fil in ("Bambu PLA Basic", "Bambu PLA Matte", "Bambu PETG HF", "Bambu PETG Basic"):
        r = P.resolve(v, {"printer": {"model": model, "nozzle": nozzle}, "filaments": [{"profile": fil}]})
        assert r["machine"]["nozzle_diameter"][0] == P._fmt_num(nozzle)
        assert r["model"]["model_id"]
        assert r["names"]["machine"] in r["process"]["compatible_printers"]
        assert r["names"]["machine"] in r["filaments"][0]["compatible_printers"]
        assert "inherits" not in r["process"] and "layer_height" in r["process"]
