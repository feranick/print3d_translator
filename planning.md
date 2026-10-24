# print3d_translator: planning

Session context. A new chat starts with "Read planning.md and start."
Status: **complete end to end, 2026.10.24.8, verified on the Mac; S4 pending Bambu Studio files** (2026-10-24); verifying on the Mac (see "State log").

## Decisions (2026-10-24)

- **Repository:** `print3d_translator` (github.com/feranick/print3d_translator). It replaces the first
  folder, `3d-print-translator`. Standalone: not added to llm-translators.
- **Terminology:** "study", so the API, CLI and MCP tool names match the other translators.
- **Runs locally on the Mac.** No remote features unless one gives a clear advantage:
  - no SLURM, executor, router or machines;
  - no MCP HTTP mode (stdio only);
  - `get_file` is kept, because it is cheap and lets a client read the outputs.
- **Linux:** x86_64 and aarch64 stay supported (the backend finder knows the AppImage layout and the tests run there), but they are secondary.

## What this is

A translator in the llm-translators style (b3dt, feat, cfdt, lmpt, omct), kept as a **standalone
repository**: it is not added to the llm-translators suite. It follows the suite's conventions
(docs/create_new_translators.md) so it could join later.

It takes a CAD part and a short JSON study (printer, filaments, intent) and returns a checked Bambu Lab
`.gcode.3mf`, the plain `.gcode`, and the slicer's estimates. It uses the OrcaSlicer CLI. It **never
talks to a printer**: no network, no cloud, no submission.

- Printers: Bambu Lab **H2D** (two extruders), **H2S**, **P2S**.
- Nozzles: 0.2, 0.4, 0.6 and 0.8 mm.
- Filaments: Bambu PLA Basic/Matte and PETG HF/Basic, plus generic PLA and PETG.
- Multi-material (several filaments per plate, AMS) is in scope from v1.
- Hosts: macOS (primary, local). Linux x86_64 and aarch64 headless are secondary.

## Names

| Thing | Value |
|---|---|
| Repository | `print3d_translator` |
| Package | `print3d_translator` |
| Key = CLI | `p3dt` |
| MCP server | `p3dt-mcp` (stdio) |
| Settings prefix | `P3DT_` (`P3DT_ORCA`, `P3DT_ORCA_PROFILES`, `P3DT_OUT`, `P3DT_WORKERS`, `P3DT_TIMEOUT`) |
| Output folder | `~/p3dt_runs` |

## Research summary (OrcaSlicer CLI, Bambu .gcode.3mf), 2026-10-24

OrcaSlicer stable is **v2.4.2** (2026-07-07); v2.5.0-alpha came out 2026-10-16. Pin 2.4.2 and record
`--help` and the profile hashes in every result.

### Reliable

- **CLI actions.** `--slice N` (0 slices all plates), `--export-3mf`, `--outputdir`, `--load-settings "machine.json;process.json"`, `--load-filaments "f1.json;f2.json"`, `--export-settings`, `--curr-bed-type`, `--debug`/`--logfile`, `--datadir`. Any config key can be set as `--key=value`.
- **Exit codes** are in `libslic3r/Utils.hpp`. Examples: -5 config, -17 process not compatible, -52 object outside the bed, -61 filament/bed mismatch, -66 filament cannot be mapped, -100 slicing error. The process status is the code mod 256, and 139 means a segfault.
- **Profiles.** They live in `resources/profiles/BBL.json` (the index, with `sub_path`) and `BBL/{machine,process,filament}/*.json`. Inheritance is single-parent through `inherits`.
  - Names follow a fixed pattern: `Bambu Lab H2D 0.4 nozzle`, `0.20mm Standard @BBL H2D`, `Bambu PLA Basic @BBL H2D`, plus nozzle suffixes for 0.2, 0.6 and 0.8.
  - Model ids: H2D `O1D`, H2S `O1S`, P2S `N7`.
- **H2D arrays.**
  - `nozzle_diameter` has 2 entries; extruder-variant arrays have 4.
  - `extruder_printable_area`: left 0..325, right 25..350 (both × 0..320). Bed 350×320, height 325.
  - H2S is 340×320×340; P2S is 256³.
- **Linux aarch64 builds exist** (AppImage and Flatpak). `--appimage-extract` avoids FUSE. The slicing CLI runs with no display.
- **G-code markers** (BBL flavour): `; CHANGE_LAYER`, `; LAYER_HEIGHT:`, `; FEATURE:`, `; WIPE_TOWER_START/END`, `; FLUSH_START/END`. Header: `; HEADER_BLOCK_START` … `; total layer number`, `; total filament weight [g]`.
- **Extrusion and heat.** Start G-code sets `M83` (relative E). Tool changes sit in `M620 S{n}A … T{n} … M621 S{n}A` blocks.

### Fragile (design around it)

1. **`--load-settings` ignores `inherits` in 2.4.2.** It silently uses defaults: one report got 17h32m instead of 3h17m (issue #14718; fixed after 2.4.2 by PR #15438). → We flatten profiles ourselves, and verify by re-reading `--export-settings`.
2. **Compatibility is checked by name.** `compatible_printers` must contain the machine name, otherwise you get -17. → We set it in the flattened copies.
3. **The bed type defaults to Cool Plate,** which H2D, H2S and P2S do not support. → Always pass `--curr-bed-type`.
4. **`result.json` and `--pipe` are Linux-only.** → We rely on the exit code plus our own checks everywhere.
5. **Boolean flags take no value** (`--min-save 1` makes `1` an input file). `--export-3mf` wants a relative name, together with `--outputdir`.
6. **Multi-material mapping.** `--load-filament-ids` reportedly works only with STL inputs. For 3MF inputs it comes from per-object `extruder` metadata. → Spike S3.
7. **`--orient` and `--arrange` are heuristics.** → We orient and place parts ourselves, and run with `--arrange 0 --orient 0`. Spike S2: does the CLI keep our positions?
8. **Thumbnails are broken in the 2.4.2 CLI** (wrong GL version request), so `plate_1.png` is missing. → We write our own small placeholder PNG, or a top-view raster from our geometry.
9. **The `.gcode.3mf` format is not officially specified.**
   - Seen in practice: `Metadata/plate_N.gcode`, `plate_N.gcode.md5` (uppercase hex MD5 of the G-code bytes), `slice_info.config` (XML: `printer_model_id`, `nozzle_diameters`, `prediction`, `weight`, per-filament `used_m`/`used_g`), `model_settings.config`, `project_settings.config`, `plate_N.json`.
   - CLI output lacks some keys that Bambu Studio writes (bambox's findings, P1S only). → Spike S4: diff against a real H2D/H2S/P2S file sliced in Bambu Studio.
10. **Flush is done in firmware on these printers.**
    - Purging is not in the G-code (`;VG1`, `SYNC T…`).
    - Prime lines and purge-chute moves (`G150*`, `G130`) go outside the bed.
    → The checker exempts the start and end blocks within known zones and compares filament against the part only.
11. **Licence.** The bundled profiles are AGPL-3.0. → We never copy them into the repo; we read them from the installed OrcaSlicer. Test fixtures are small hand-written profile trees.

### Projects looked at (learn, don't copy)

- **jeff-roche/orca-mcp.** Temporary profile copies with merged overrides; falls back to `xvfb-run`.
- **ShreddyKrueger75/claude-orcaslicer-mcp.** Flattens inheritance itself; pins `compatible_printers` and `curr_bed_type`.
- **estampo/bambox.** The best notes on the `.gcode.3mf` format and what Bambu Connect validates.
- **DMontgomery40/bambu-printer-mcp.** Warns that CLI slicing breaks when profiles drift.

## Proposed study format

One study = one plate. JSON, `{name: study}`. Units: mm, °C, %, mm/s.

```json
{
  "bracket": {
    "printer":   {"model": "H2D", "nozzle": 0.4, "plate": "textured_pei"},
    "filaments": [
      {"profile": "Bambu PLA Basic", "color": "#F0F0F0", "extruder": "right"},
      {"type": "PETG", "brand": "Bambu", "variant": "HF", "extruder": "left"}
    ],
    "objects": [
      {"part": {"step": "bracket.step"}, "filament": 1, "orient": "flat", "copies": 2},
      {"part": {"b3dt": {"step": "/…/clip.step", "volume": 1834.2}}, "filament": 2,
       "orient": {"rotate": [90, 0, 0]}}
    ],
    "intent": {
      "preset": "standard",
      "layer_height": 0.2, "walls": 3, "top_layers": 5, "bottom_layers": 4,
      "infill": 25, "infill_pattern": "gyroid",
      "supports": "tree", "support_angle": 40,
      "brim": "auto"
    },
    "overrides": {"process": {"seam_position": "back"}, "filament": {"1": {"nozzle_temperature": [215]}}},
    "expect": [{"quantity": "estimate.filament_g", "value": 22, "tol": 0.2}],
    "run": {"timeout_s": 600}
  }
}
```

- **`printer`**
  - `model`: H2D, H2S or P2S.
  - `nozzle`: 0.2, 0.4, 0.6 or 0.8. For H2D, one size for both extruders in v1; mixed sizes need 2.5.
  - `plate`: `textured_pei` (default), `smooth_pei`, `engineering` or `high_temp`. Cool plates are refused for these printers.
- **`filaments`** (1–N; their order is the filament number 1..N)
  - Give `profile` (an exact system name without the `@BBL …` suffix), or `type` + `brand` + `variant`. Validation lists the valid choices.
  - `extruder` (H2D only): `left`, `right` or `auto`.
- **`objects`**
  - `part` is one of:
    - `{"step": path}`
    - `{"stl": path}`
    - `{"3mf": path}`
    - `{"goal": <b3dt goal>}`, built through build123d-translator when it is installed
    - `{"b3dt": {"step", "volume"}}`: a b3dt result
  - `filament` is a number into `filaments`.
  - `orient`: `"as_is"`, `"flat"` (the largest planar or near-planar face down, computed by us), or `{"rotate": [rx, ry, rz]}` in degrees, applied X then Y then Z.
  - `copies`, and an optional `at: [x, y]` (otherwise we place parts deterministically on a grid).
- **`intent`**
  - `preset` picks the base process profile: `fine`, `standard`, `strength` or `draft`, mapped per printer and nozzle to the profiles that exist.
  - The other keys map to profile keys:
    - `layer_height` → `layer_height`
    - `walls` → `wall_loops`
    - `infill` → `sparse_infill_density`
    - `supports` → `enable_support` + `support_type`
    - `support_angle` → `support_threshold_angle`
    - `brim` → `brim_type` + `brim_width`
    - and so on.
  - Unknown keys are errors that name the right key.
- **`overrides`.** Raw profile keys, only for keys that exist in the flattened profile. Validation reports unknown keys and type mismatches. They are written into temporary copies in the run folder; saved profiles are never touched.
- **`expect` and `run`** work as in the other translators.

## Proposed result (`<name>.json`)

```json
{
  "ok": true, "reliable": true, "unreliable_because": [], "warnings": [],
  "printer": {"model": "H2D", "model_id": "O1D", "nozzle": 0.4, "plate": "textured_pei",
              "build_volume": [350, 320, 325]},
  "profiles": {"machine": "Bambu Lab H2D 0.4 nozzle", "process": "0.20mm Standard @BBL H2D",
               "filaments": ["Bambu PLA Basic @BBL H2D", "Bambu PETG HF @BBL H2D"],
               "orca_version": "2.4.2", "sha256": {"machine": "…", "process": "…", "filaments": ["…"]},
               "set": {"wall_loops": [2, 3], "sparse_infill_density": ["15%", "25%"]}},
  "geometry": {"bracket#1": {"volume_mm3": 12034.5, "volume_from": "step", "area_mm2": 6120.0,
                "rotation_deg": [0, 0, 0], "bbox": [[…], […]], "fits": true,
                "bed_contact_mm2": 820.0, "overhang_mm2": 145.0, "overhang_frac": 0.024,
                "thin_walls": {"min_mm": 0.9, "area_mm2": 0.0}}},
  "estimate": {"time_s": 5420, "time": "1h30m20s", "filament_g": 21.7, "filament_m": 7.18,
               "by_filament": [{"n": 1, "g": 15.1, "m": 5.0}, {"n": 2, "g": 6.6, "m": 2.18}]},
  "gcode": {"layers": 140, "z_max": 28.0, "first_layer_z": 0.2,
            "extruded_mm3": {"part": 9810.0, "support": 640.0, "brim": 120.0, "tower": 380.0},
            "tool_changes": 22, "temps": {"nozzle": {"1": [220, 220], "2": [250, 255]}, "bed": [55, 55]}},
  "checks": {"md5": {"passed": true}, "bounds": {"passed": true, "got": "…", "expected": "…"}, "…": {}},
  "expect": [],
  "files": {"gcode_3mf": "…/bracket.gcode.3mf", "gcode": "…/bracket.gcode",
            "profiles": "…/profiles/", "log": "…/orca.log"},
  "seconds": {"prepare": 0.8, "slice": 14.2, "check": 1.1},
  "execution": {"host": "mac", "orca": "/Applications/OrcaSlicer.app/…", "timeout_s": 600}
}
```

## The checker (independent of OrcaSlicer's own summary)

`H` = hard: if it fails, the result is `reliable: false`. `W` = warning.

**Before slicing (our geometry: STEP via OCP when available, otherwise the triangle mesh):**

| Check | How | Kind |
|---|---|---|
| fits | Rotated bbox inside the printable volume; on H2D, inside the assigned extruder's printable area | H (no slicing) |
| mesh | Watertight, consistent normals, positive volume (STL/3MF) | H |
| bed_contact | Area of faces within 0.05 mm of z_min with normal ≈ −Z; warn when it is under 5% of the footprint or under a size limit, and suggest a brim | W |
| overhang | Area of downward faces steeper than `support_angle`, not touching the bed; warn when supports are `none` | W |
| thin_walls | Inward ray distance at sampled surface points, compared with the line width (about 1.125 × nozzle); report the minimum and the area below it | W |

**After slicing (our own streaming G-code parser):**

| Check | How | Kind |
|---|---|---|
| profile_roundtrip | `--export-settings` from the same run equals our flattened and overridden settings for every key we set and every key the checks use (this catches the inherits/defaults bug) | H |
| archive | Zip opens; `Metadata/plate_1.gcode`, `.md5`, `slice_info.config`, `model_settings.config`, `project_settings.config` present; md5 (uppercase hex) matches; plain `.gcode` equals the archived one; `printer_model_id` and `nozzle_diameters` match the printer | H |
| bounds | Every extruding move inside the printable area (per extruder on H2D) and 0 < z ≤ height. Travel moves inside the machine limits. Start and end blocks are allowed only in known zones (prime line, purge chute), per model | H |
| layers | Z rises monotonically; the number of distinct print Zs = expected ± 1; every layer step equals `layer_height` (first layer = `initial_layer_print_height`) within 0.005 mm; z_max ≈ the part height | H |
| first_layer | The first extrusion is at z = initial layer height, and some extrusion exists at that z | H |
| temps | `M104`/`M109` per tool equals the filament's `nozzle_temperature(_initial_layer)` and lies inside its range; `M140`/`M190` equals the plate temperature for that filament and plate type | H |
| filament_map | The tools used are exactly the ones the objects ask for; each object's extrusions come from its own tool | H |
| extrusion_volume | E per `; FEATURE:` × filament cross-section, split into part, support, brim and tower. Part extrusion within ±20% of a model: shell (surface area × wall thickness, plus top/bottom skins) + infill% × the remaining volume, capped at the exact volume. Never more than 1.1 × volume | H |
| footprint | Rasterized XY silhouette of each placed part (0.2 mm grid), grown by the line width + brim width (layer 1), the support margin (when supports are on) and the wipe tower rectangle. Any extrusion outside fails | H |
| estimates | Header time equals `slice_info` prediction; g = m × area × density for each filament; our E total within 2% of the header length (flush excluded) | W |
| expect | The study's `expect` entries | H |

## Architecture (modules)

| Module | Job |
|---|---|
| `format.py` | `study_format()`, `validate()` (with the available profile names per printer and nozzle) |
| `orca.py` | Find OrcaSlicer (`P3DT_ORCA`, PATH, `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`, an extracted AppImage), its version, its profiles folder; run it in its own process group with a timeout |
| `profiles.py` | The resolver: index from `BBL.json`, resolve by name through `sub_path`, merge root first, drop `inherits`, set `compatible_printers`, apply intent and overrides, write to the run folder, sha256 |
| `geometry.py` | STEP (OCP, optional) / STL / 3MF reading, volume, orientation (`flat`), placement, one STL per object in bed coordinates plus the assemble list for the CLI, pre-slice checks |
| `gcode.py` | Streaming G-code parser: G0/G1/G2/G3, G90/G91, M82/M83, T/M620/M621, temperatures, markers, per-layer and per-feature totals |
| `gcode3mf.py` | Read and validate the archive; patch missing metadata if spike S4 says so |
| `check.py` | The checks above → `checks`, `reliable` |
| `runner.py` | `run_study()` end to end |
| `api.py`, `cli.py`, `mcp_server.py` | The contract below |
| `bench.py` | Benchmarks (cube, overhang test, two-colour part) with known layer counts and volumes |

**API/CLI/MCP.**
- API: `study_format`, `validate`, `run`, `start`, `status`, `read_file`, `tool_status`, `inputs` (the flattened profiles and the CLI command, without running), `bench`, `bench_status`.
- MCP tools: `study_format`, `validate_studies`, `run_studies`, `study_status`, `get_inputs`, `get_file`, `run_benchmarks`, `bench_status`.
- Local only: a slice takes seconds to a few minutes. Studies run in a local process pool, as in b3dt. No SLURM, `jobs`, `cancel` via queue, router or HTTP. A local `cancel` flag file is kept, because it is cheap.

**Dependencies.**
- Required: `numpy`.
- `cad` extra: `build123d`, for STEP (exact volume, tessellation).
- `mcp` and `test` extras.

## Spikes before the main build

- **S1. The CLI on each host.** The Mac first (primary). On Linux aarch64, download and extract the v2.4.2 aarch64 AppImage, find the apt libraries it needs, then `--help` and slice a 20 mm cube with flattened profiles.
- **S2. Positions.** Use `--load-assemble-list` with `need_arrange: false` (see the state log), and check the positions in the G-code.
- **S3. Multi-material.** Per-object `filaments` in the assemble list. On H2D, `filament_map` (1 = left, 2 = right) with `filament_map_mode` "Manual": check which nozzle prints what.
- **S4. Archive keys.** Diff the CLI's `.gcode.3mf` against one sliced in Bambu Studio for the same printer. Needs a reference file from the user per model.

## State log

- 2026-10-24: Read the llm-translators template and the feat/b3dt references. Research done (above). Proposal written.
- 2026-10-24: Proposal approved. Moved to the new repository print3d_translator. Local-only (Mac) decided.
- 2026-10-24: **Sandbox (Linux aarch64, Ubuntu 22.04).**
  - Its proxy blocks GitHub release assets, so the AppImage cannot be downloaded or run there; S1 moves to the Mac.
  - Sparse-cloned OrcaSlicer v2.4.2 source instead (profiles BBL machine/process + 83 filament files, PrintConfig.cpp, OrcaSlicer.cpp, Config.cpp).
- 2026-10-24: **Findings from the v2.4.2 source.**
  - **`--load-assemble-list FILE` settles S2/S3 on paper.** JSON `{"plates": [{"plate_name", "need_arrange": false, "plate_params"?, "objects": [{"path" (.stl/.obj), "count", "filaments": [n], "pos_x"/"pos_y"/"pos_z": [mm], "print_params"?: {key: value}}]}]}`.
    - Objects are translated by `pos` from their own coordinates.
    - Plates with `need_arrange: false` are locked (not arranged).
    - `filaments[0]` becomes the object's `extruder`.
    → We write one STL per object, already oriented and placed in bed coordinates, with pos 0 and need_arrange false. Verify on the Mac; the footprint check catches a moved part anyway.
  - **`printer_model_id` is probably empty in Orca's output.** In 2.4.2 the CLI looks it up in `resources/profiles/BBL/machine_full/<printer_model>.json`, which OrcaSlicer does not ship (it comes from Bambu Studio). → The packager fills it in from the machine_model profile (`model_id`). Verify in S4.
  - **The CLI reads each settings file alone.** `load_from_json` reads one file, and `"from": "system"` sets the system name to the profile's own name. The compatibility check compares that name with `compatible_printers`. → Flattened copies keep their names, `from: system`, and no `inherits`.
  - **Arrays for scalar keys.** BBL multi-extruder profiles store some scalar keys (`outer_wall_speed`, ...) as arrays, one per extruder variant. The loader joins them with ',' and deserializes the result. → Overrides keep the profile's shape (a list stays a list).
  - **Plate names differ.** `not_support_bed_type` uses Bambu Studio names ("Smooth Cool Plate"), while `curr_bed_type` uses Orca's enum ("Cool Plate"). → `PLATE_ALIASES`.
  - **Filament names are not uniform** (`Bambu PETG HF @BBL H2S` vs `@BBL P2S 0.4 nozzle` vs `@BBL H2D 0.4 nozzle`). Some profiles serve several nozzles (PETG Basic for the H2D 0.8 is the "0.6 nozzle" profile). → Choose by `compatible_printers`.
  - **No Strength process for H2S/P2S.** "strength" falls back to Standard with `wall_loops` 4 and `sparse_infill_density` 25%, with a warning.
  - **Arcs.** `enable_arc_fitting` is 1 in the BBL processes, so the G-code has G2/G3 and the parser must handle arcs.
  - **`wrapping_exclude_area`** is set on the H2D (145..256 × 310..326) and P2S (153..216 × 235..256). It is a bed zone; the checker should flag extrusion in it.
- 2026-10-24: **Step 1 done.** `profiles.py`, `data/orca_keys.json` (860 keys, 2.4.2), `tools/orca_keys.py`, 50 offline tests plus 12 against the real profiles (H2D/H2S/P2S × 4 nozzles × 4 filaments), all passing.
  - Next: `orca.py` (find the binary, version, run with the assemble list).
  - The Mac spike S1 (OrcaSlicer installed, `--help`, slice a cube) is needed first.
- 2026-10-24: **S1 on the Mac passed.**
  - OrcaSlicer 2.4.2 is at `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`, with profiles at `Contents/Resources/profiles` (BBL 02.01.00.19).
  - `--help` lists `--load-assemble-list`.
  - All 62 tests pass on the Mac, including the real-profile tests.
- 2026-10-24: The repository moved to `~/Software/print3d_translator`. Its `.venv` must be recreated, because its scripts point at the old path.
- 2026-10-24: **Step 2 (2026.10.24.2).**
  - Written: `orca.py`, `mesh.py`, `tools/spike.py`, `tests/test_mesh_orca.py`. 64 offline tests pass in the sandbox.
  - From the source: with `--outputdir`, the CLI also writes `plate_1.gcode` there. `--export-settings` saves the config when the option is processed. The archive's `project_settings.config` is the settings actually sliced, so it is what the settings check reads.
  - Next: run `tools/spike.py` on the Mac and read `debug/spike/*/summary.json`, then write gcode.py and gcode3mf.py from what it shows.
- 2026-10-24: **Spike on the Mac (OrcaSlicer 2.4.2).**
  - p2s_cube, p2s_cube_minsave and h2d_two_pla slice in about 0.3 s. h2d_pla_petg crashes (SIGSEGV).
  - **Positions are kept** (assemble list, need_arrange false): the extrusion box is exactly the placed cube. S2 done.
  - **H2D filament map:** `--filament-map-mode Manual --filament-map 2,1` reaches project_settings and slice_info (`filament_maps "2 1"`). model_settings still says "Auto For Flush" (cosmetic). S3 done, apart from which nozzle is physically "1".
  - **Archive:** layout as in gcode3mf.py; the md5 is upper-case and matches; the plain gcode is identical; there are no thumbnails.
    - `printer_model_id` is empty, so it is patched.
    - slice_info also holds OrcaSlicer warnings (e.g. `bed_temperature_too_high_than_filament` for PLA Basic + PLA Matte).
    - `--min-save` only drops `3D/Objects/*.model`.
  - **G-code:**
    - The footer `; filament used [mm]` is the positive E of XY moves per filament, including the start G-code's prime line; it matches ours exactly.
    - Temperatures are often inside M622/M623 blocks. On the H2D the second nozzle's temperature is in `M620.10 A1 ... P<temp>`.
    - Object markers `; printing object <stl stem>_<copy> id:...`.
  - **Settings round trip:** after normalization (unknown keys skipped, variant lists collapsed, points 0X0 = 0x0), 0 differences out of 269 (P2S) and 329 (H2D). A changed value is caught. This confirms the flattening.
- 2026-10-24: **Step 3 (2026.10.24.3).**
  - Written: `gcode.py`, `gcode3mf.py`, `check.py`, `tests/test_gcode_check.py` (synthetic G-code and archives).
  - 103 tests pass in the sandbox. All checks pass on the real P2S and H2D slices, and 10 injected faults are each caught.
  - Fixed: the volume model counted a bottom shell when `bottom_shell_layers` = 0.
  - **Open: the PLA + PETG crash on the H2D.** The log ends inside `update_values_to_printer_extruders_for_multiple_filaments`; the same "variant index out of range" warnings appear in the run that succeeded. `CRASH_CASES` in tools/spike.py narrow it down: PETG + PETG, auto map, `--allow-mix-temp`, Generic PETG, PETG alone, PLA + PETG on the P2S. Run `python tools/spike.py crash` on the Mac.
  - Next: geometry checks before slicing (`geometry.py`: STEP/STL/3MF, orientation, overhang, bed contact, thin walls, fit), then `runner.py`, `format.py`, `api.py`, `cli.py`, `mcp_server.py`.
- 2026-10-24: **The crash is explained (2026.10.24.4).**
  - The crash cases on the Mac:
    - every H2D plate with 2 filaments and no colours crashed (PETG+PETG, PLA+PETG, auto map, `--allow-mix-temp`, Generic PETG);
    - PETG alone sliced;
    - the P2S with PLA + PETG refused cleanly with -62 FILAMENTS_DIFFERENT_TEMP.
  - The macOS crash report: a null read in `calc_filament_change_info_by_toolorder` ← `reorder_extruders_for_minimum_flush_volume` ← `Print::_make_wipe_tower`.
  - The log diff shows the cause: "no filament colors found in projects". OrcaSlicer.cpp computes `flush_volumes_matrix` only when the project has `filament_colour`, and no BBL filament profile sets it. h2d_two_pla worked only because the spike gave colours.
  - Fix: `profiles.resolve` sets `filament_colour` on every filament (the study's `color`, or a distinct default with a warning for 2+ filaments).
  - **Open: rerun `python tools/spike.py crash`.** Expected:
    - the H2D cases now slice, or stop on a clean code such as -62 for PLA + PETG;
    - p2s_pla_petg_mixtemp tells whether `--allow-mix-temp` lets PLA + PETG through.
    Then decide the study key for mixing temperatures (e.g. `allow_mix_temp`) and validation's message.
- 2026-10-24: **Crash cases rerun on the Mac (2026.10.24.4): every H2D case slices.**
  - Cases: PETG+PETG, PLA+PETG (manual, auto, `--allow-mix-temp`, Generic PETG), PETG alone.
  - Our checks rate all of them reliable. The settings round trip matches 268–331 keys with 0 differences. Temperatures: PLA 220, PETG HF 245, Generic PETG 255 on the second nozzle (from `M620.10 A1 P`), bed 70 (`by_highest_temp`).
  - Every H2D slice_info has the warning `bed_temperature_too_high_than_filament` (level 3), even PETG alone. Results should list slice_info warnings.
  - **H2D auto map:** PLA + PETG both went to extruder 1 (left). slice_info and project_settings write `filament_map` with a single value "1" for 2 filaments; the CLI pads missing entries with the first value, and check.py now does the same. The new soft check `shared_nozzle` flags it.
  - **P2S PLA + PETG:**
    - without the flag: -62 FILAMENTS_DIFFERENT_TEMP, a clean refusal;
    - with `--allow-mix-temp`: -102 GCODE_PATH_IN_UNPRINTABLE_AREA. The prime tower went to OrcaSlicer's built-in default (165, 250), about 24 mm deep, which is off the 256 mm P2S bed. With a locked plate (need_arrange false) it is not moved.
  - → **Placement must place the prime tower.** Set process `wipe_tower_x`/`wipe_tower_y` (Floats, one per plate). OrcaSlicer keeps them when present (`has_wipe_tower_position`). Choose a free spot on the bed, clear of the parts and brims, sized prime_tower_width × about prime_tower_width + margin. Observed footprints: H2D 31 × 27 mm (width 60), P2S 24 × 24 mm (width 35).
  - → Study key `allow_mix_temp: true` (passes `--allow-mix-temp`). Validation warns about the shared bed temperature, and `bed_vs_filaments` reports it.
  - **2026.10.24.5:** soft checks `shared_nozzle` and `bed_vs_filaments`; short filament maps padded. 106 tests pass in the sandbox.
- 2026-10-24: **Step 4 (2026.10.24.6): the translator is complete end to end.**
  - Written: `geometry.py`, `placement.py`, `format.py`, `runner.py`, `api.py`, `cli.py`, `mcp_server.py`, the benchmarks, `examples/studies.json`, `tests/fake_orca.py`, `tests/test_geometry_placement.py`, `tests/test_runner_api.py`.
  - **Geometry:**
    - STEP and b3dt via build123d `tessellate(0.01, 0.1)`. Exact volume from OpenCascade; a b3dt result's volume is cross-checked against its STEP.
    - 3MF: core spec plus production components.
    - `flat` = the largest coplanar same-facing area down; ties go to the lower height.
    - Checks: fits (hard), closed mesh (hard for STL/3MF), bed contact, overhang (support_threshold_angle), thin walls (ray casting, fixed seed).
    - On b3dt's example parts it takes 0.2–1.7 s per part, and exact vs mesh volume agree within 0.04%.
  - **Placement:** a 1 mm grid with integral images. Each part gets a halo of 3 mm + brim width and stays in its extruder's area and out of the wrapping zone. `at` is kept, the largest part goes to the centre, and the prime tower (prime_tower_width square + 5 mm) goes in a back corner reachable by every extruder used. The process gets `wipe_tower_x/y`.
  - **Validation:** OrcaSlicer's exact filament compatibility rule from `Print::check_multi_filaments_compatibility`: each filament's print temperature must lie in the other's `nozzle_temperature_range_low..high`. On one nozzle (or one H2D nozzle) that is an error unless `allow_mix_temp`.
  - **API:** requests run in a background process (`cli run-request`, P3DT_WORKERS threads). status reads the files; cancel writes a flag file; inputs runs prepare without slicing; read_file is limited to P3DT_OUT.
  - **MCP:** 9 tools, stdio only, 17 lines of instructions.
  - **Tests:** 143 pass and 14 skip in the sandbox (skips: real OrcaSlicer, real profiles in the default path). The fake OrcaSlicer covers ok, shift, hot, fail17, crash and sleep (timeout and cancel).
  - **To verify on the Mac:**
    1. `pytest -q` with OrcaSlicer: the real benchmark test.
    2. `p3dt bench`.
    3. `p3dt run examples/studies.json`.
    Watch:
    - Does OrcaSlicer keep our `wipe_tower_x/y`? If it changes them, settings_roundtrip will show `process.wipe_tower_x`; exempt it and check the tower box instead.
    - The P2S PLA+PETG `allow_mix_temp` example: the tower must now be on the bed.
    - The auto-brim width on the H2S slabs.
  - Then: S4 (compare our .gcode.3mf with one from Bambu Studio per printer), real STEP parts from b3dt, and an overhang part with tree supports (support margin).

## Open ideas

- `orient: "min_support"`: search rotations by overhang area.
- Variable layer height, ironing, fuzzy skin: out of scope for v1.
- A top-view PNG thumbnail drawn from our own geometry.
- 2026-10-24: **First full runs on the Mac (2026.10.24.6).**
  - `pytest`: 156 passed, 1 failed (test_validate_without_profiles: find_profiles fell back to /Applications despite an explicit P3DT_ORCA_PROFILES).
  - `p3dt bench`: 3/3 passed.
  - **`p3dt run examples`: exit 2.** P2S PLA+PETG with allow_mix_temp was unreliable:
    - **temperatures:** the bed went 55 → 70 C at layer 2, which is OrcaSlicer's `by_first_filament` (the filament active at the layer change), not a fault;
    - **estimates:** OrcaSlicer's totals include the firmware flushes, which appear only as `;VG1 E` in `VFLUSH` blocks (41.6 mm per change on the P2S).

    With flushes counted, the totals match to 0.01 mm (11676.44). The split between the two filaments differs by 208 mm, so totals are compared.
  - **The prime tower position we set is kept** (wipe_tower_x/y round trip, printed inside the planned box): an assumption confirmed.
  - **The macOS "Python crashed" report** was the fake OrcaSlicer's crash mode (SIGSEGV), now SIGKILL.
  - **MCP run_studies returned 82 kB** for 4 studies, too large for a model. Now a summary per study (~3 kB), with the full JSON on disk.
  - The p3dt MCP server is connected to this Claude session: studies can be run on the Mac directly; `~/p3dt_runs` is shared read-only with the sandbox.
- 2026-10-24: **2026.10.24.7.** Fixes above. All 7 real slices re-checked: reliable; P2S PLA+PETG: `bed_vs_filaments` warns (PLA on a 70 C bed). 147 tests pass in the sandbox.
  - Next: real STEP parts from b3dt; an overhang part with tree supports; S4 (compare with Bambu Studio's .gcode.3mf).
- 2026-10-24: **b3dt → p3dt chain (2026.10.24.8).**
  - The b3dt MCP server runs on the Mac and writes `~/b3d_parts`, now shared with the sandbox.
  - Built `s4_bracket` (b3dt's example) and `overhang_shelf` (a 20 × 20 × 30 post with a 25 × 20 × 5 shelf at the top). Ran them through p3dt with `{b3dt: {step, volume}}`; all 5 studies are reliable:
    - bracket P2S/H2S/H2D: 135 layers, ~21.0 g, volume ratio 0.993;
    - shelf without supports: `overhang` warns with 500 mm² (exact);
    - shelf with tree supports: 1438 mm³ of support, z 0.2–24.8, max 0.00 mm outside the silhouette (the 5 mm support margin was not needed).
  - **Found:** OrcaSlicer 2.4.2 writes `slice_info first_layer_time = 3.07e32` on the P2S (uninitialised); the H2S/H2D write 0. `patch_model_id` now resets it, and the soft check `slice_info_values` reports it.
  - `tools/compare_bambu.py` written for S4. The STEP files are in `examples/`, so the user can slice them in Bambu Studio.
  - Note: the p3dt MCP server in the Claude app runs the code it started with; restart it after updates. Its output was still the old full form.
