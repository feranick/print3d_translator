# H2D support (removed in 2026.11.14.2)

The Bambu Lab H2D (two extruders) was supported from 2026.10.24 to 2026.11.14.1. It was removed because
it cannot be tested here (only a P2S is available). It was never printed on a real H2D: only sliced
(OrcaSlicer 2.4.2 and 2.5.x) and compared with OrcaSlicer's GUI export (S4).

The last version with it is kept in git:

```bash
git show h2d-last                 # tag on the last commit with H2D support (2026.11.14.1)
git checkout h2d                  # branch at the same commit
git diff h2d-last main -- print3d_translator tests tools   # everything the removal changed
```

To bring it back: revert the removal commit (or re-apply `git diff main h2d-last`) onto the current code, then
re-run the H2D tests (`tests/test_profiles.py::test_h2d_extruder_map`, `test_runner_api.py::test_run_study_h2d_tower_and_map`,
`test_gcode_check.py::test_per_extruder_area`, `test_geometry_placement.py::test_place_per_extruder_areas`)
and the H2D benchmark (`h2d_two_filaments`).

## What was removed

| Module | H2D code |
|---|---|
| `profiles.py` | `SUPPORTED_MODELS` included `H2D`; `EXTRUDERS = {"left": 1, "right": 2}`; `resolve()` turned `filaments[].extruder` into `filament_map` (manual when any filament named one) and returned `model.extruders`. Now a machine with 2+ nozzles raises `ProfileError`. |
| `format.py` | `filaments[].extruder` (`left`/`right`/`auto`); `mix_temperature()` grouped filaments per extruder and warned when the automatic map might put incompatible ones on one nozzle. |
| `orca.py` | `command(filament_map)` → `--filament-map-mode Manual --filament-map 2,1`; fixes for exit codes -66 (filament cannot be mapped) and -68 (filament not supported by its extruder); -102 suggested the other extruder. |
| `runner.py` | Per-extruder printable areas (`extruder_printable_area`), `extruders_for()` / `area_for()` (the intersection of the areas of the extruders an object uses), passed to the pre-slice `fits` check and to placement; the tower placed where every used extruder reaches; `filament_map` in the result and fed to the checks (falling back to the archive's `project_settings.filament_map`). |
| `placement.py` | `place(items, bed, areas, ...)`: each item's `extruders`, `area_of()` / `allowed()`; the prime tower in a back corner reachable by every extruder used. |
| `check.py` | `bounds` per extruder (each tool's extrusions inside its extruder's area; a `filament_map` shorter than the filaments padded with its first value, as the CLI does); soft check `shared_nozzle` (two used filaments on one nozzle whose print temperatures differ by more than 15 °C). |
| `gcode3mf.py` | `check_archive(filament_map)`: `slice_info filament_maps` equals the study's map; `finalize(extruders)` expanded `nozzle_volume_type` to one value per extruder (`"0 0"`, as the GUI writes). |
| `api.py`, `mcp_server.py`, `format.py` docs | Benchmark `h2d_two_filaments` (PLA left + PETG right); hints "H2D filaments take extruder 'left'/'right'". |
| `tools/spike.py` | Cases `h2d_two_pla`, `h2d_pla_petg` and the `CRASH_CASES` that narrowed down the 2.4.2 crash. |
| `examples/studies.json` | `two_color_h2d` (now `two_color_p2s`). |

Kept, because the P2S uses them too: the `M620.10 A0/A1 ... P<temp>` parsing (filament-change temperatures),
`nozzle_standby` (`M104 T0 S.. N0`, written on the P2S by OrcaSlicer 2.5), per-variant list handling in the
flattener and the settings round trip, and a colour on every filament.

## What was learned

**Machine (vendor profiles 02.01 / 02.08):**
- Model id `O1D`. Bed 350 × 320 mm, height 325 mm.
- `extruder_printable_area`: left 0–325, right 25–350 (× 0–320). `extruder_printable_area[0]` is the left one.
- `nozzle_diameter` has 2 entries; extruder-variant arrays have 4 (scalar keys such as `outer_wall_speed` are
  arrays, one per variant; overrides must keep the profile's shape).
- `wrapping_exclude_area` 145–256 × 310–326.
- Names: `Bambu Lab H2D 0.4 nozzle`, `0.20mm Standard @BBL H2D`, filaments `... @BBL H2D` or
  `... @BBL H2D 0.4 nozzle`; some profiles serve several nozzles (PETG Basic for the 0.8 nozzle is the
  "0.6 nozzle" profile): choose by `compatible_printers`. Processes include `Balanced Strength` (H2S/P2S have none).

**Filament map:**
- `filament_map` values: 1 = left, 2 = right. `--filament-map-mode Manual --filament-map 2,1` reaches
  `project_settings` and `slice_info` (`filament_maps "2 1"`); `model_settings` still says "Auto For Flush"
  (cosmetic). Which physical nozzle is "1" was never checked on a printer.
- With the automatic map, OrcaSlicer 2.4.2 put PLA + PETG both on the left nozzle and wrote a single
  `filament_map` value for 2 filaments (the CLI pads missing entries with the first value).
- OrcaSlicer's filament compatibility rule (`Print::check_multi_filaments_compatibility`) applies per nozzle:
  each filament's print temperature must lie in the other's `nozzle_temperature_range_low..high`.

**Crashes and quirks:**
- OrcaSlicer 2.4.2 segfaulted on every H2D plate with 2 filaments and no `filament_colour`
  (`calc_filament_change_info_by_toolorder` ← `reorder_extruders_for_minimum_flush_volume` ←
  `Print::_make_wipe_tower`): no BBL filament profile sets a colour, so the flush matrix was never computed.
  The fix (a colour on every filament) is kept for all printers.
- The second nozzle's temperature is set by `M620.10 A1 ... P<temp>` on a filament change, not by `M104`.
- OrcaSlicer 2.5 pre-heats / pre-cools the idle nozzle with `M104 T<nozzle> S.. N0` (`nozzle_standby`).
- Every H2D `slice_info` carried the warning `bed_temperature_too_high_than_filament` (level 3), even with PETG alone.
- Bed temperature with mixed filaments: `by_highest_temp` (70 °C with PETG).
- The 2.4.2 CLI wrote one `nozzle_volume_type` value; the GUI writes `"0 0"` (2.5.1's CLI writes one per nozzle).
- Prime tower footprint: about 31 × 27 mm (width 60) with the 02.01 profiles; smaller with 02.08.
