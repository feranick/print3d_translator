# print3d_translator (`p3dt`)

Turns a CAD part into a checked 3D-print file for a Bambu Lab printer, using OrcaSlicer's command line.
An LLM (or a person) writes a short JSON study: the part, the printer, the filaments and the intent.

The translator then works through these steps:
1. resolves OrcaSlicer's system profiles into flattened settings;
2. orients the parts, checks them and places them (and the prime tower);
3. slices;
4. checks the G-code and the `.gcode.3mf` independently.

**It never talks to a printer.** No network, no cloud, no submission. It writes files, and you print them from the printer's SD card, Bambu Studio or Bambu Handy.

It follows the conventions of the llm-translators family (b3dt, feat, cfdt, lmpt, omct). It is a standalone repository that runs locally (macOS first). The session context is in `planning.md`.

## Scope

- **Printers:** Bambu Lab H2D (two extruders), H2S, P2S. Nozzles 0.2, 0.4, 0.6 and 0.8 mm. Other Bambu Lab models resolve, with a warning that they are untested.
- **Filaments:** any OrcaSlicer filament profile compatible with the printer. Tested with Bambu PLA Basic/Matte, PETG HF/Basic and Generic PLA/PETG.
- **Multi-material:** several filaments per plate. On the H2D, each filament can go to the left or right extruder.
- **Parts:**
  - STEP, STL or 3MF files;
  - a build123d-translator goal;
  - a b3dt result (`{step, volume}`).

  Units are mm. STEP and goals need the `cad` extra.
- **Output:**
  - the Bambu `.gcode.3mf` (with `printer_model_id` filled in) and the plain `.gcode`;
  - the flattened profiles;
  - estimates: time, filament g and m;
  - the checks.

## Install

```bash
cd ~/Software/print3d_translator
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test,cad,mcp]"          # cad: STEP and b3dt parts (build123d); mcp: the MCP server
pytest -q
```

- **Backend:** OrcaSlicer 2.4.2 (pinned). On macOS, install `OrcaSlicer.app` in `/Applications`.
- **Other installs:** set `P3DT_ORCA` (the executable) and, if needed, `P3DT_ORCA_PROFILES` (its `resources/profiles`, the folder with `BBL.json`).
- **Linux:** the extracted AppImage works (`squashfs-root/AppRun`) and needs no display. Its release build is for Ubuntu 24.04 or later.
- **Tests:** they run without OrcaSlicer. A fake OrcaSlicer (`tests/fake_orca.py`) and a hand-written profile tree stand in. The tests that need the real one are skipped when it is not found.
- **b3dt goals:** to use build123d-translator goals, install it in the same venv (`pip install -e ~/Software/llm-translators/build123d-translator`).

## Use

```bash
p3dt status                          # versions; OrcaSlicer and its profiles found or not
p3dt format                          # the study format
p3dt validate examples/studies.json  # static checks, with profiles resolved (no slicing)
p3dt run examples/studies.json       # slice and check; prints the results
p3dt inputs examples/studies.json NAME   # profiles, placement, OrcaSlicer command, without slicing
p3dt bench                           # benchmarks on generated parts
p3dt view STUDY_FOLDER               # write <name>.view.html: parts and toolpaths in 3D
p3dt status REQUEST | cancel REQUEST
```

Results go to `~/p3dt_runs/run_<date>_<time>_<id>/<study>/`. Relative part paths are relative to the studies file.

Settings, read from the environment:

| Setting | Meaning | Default |
|---|---|---|
| `P3DT_ORCA` | the OrcaSlicer executable | found automatically |
| `P3DT_ORCA_PROFILES` | OrcaSlicer's `resources/profiles` | next to the executable |
| `P3DT_OUT` | output folder | `~/p3dt_runs` |
| `P3DT_WORKERS` | studies at once | 2 |
| `P3DT_TIMEOUT` | seconds per slice | 600 |
| `P3DT_MAX_TIMEOUT` | cap for a study's `run.timeout_s` | none |

## A study

```json
{"bracket_petg": {
  "printer":   {"model": "P2S", "nozzle": 0.4, "plate": "textured_pei"},
  "filaments": [{"profile": "Bambu PETG HF", "color": "#202020"}],
  "objects":   [{"name": "bracket", "part": {"step": "bracket.step"}, "orient": "flat", "copies": 2}],
  "intent":    {"preset": "strength", "walls": 4, "infill": 30, "infill_pattern": "gyroid",
                "supports": "none", "brim": "auto"},
  "expect":    [{"quantity": "estimate.filament_g", "value": 40, "tol": 0.3}]}}
```

| Key | Meaning |
|---|---|
| `printer` | `model` (H2D, H2S, P2S), `nozzle` (mm), `plate`: `textured_pei` (default), `smooth_pei`, `engineering`, `high_temp`, `supertack`. Cool plates are refused where the printer does not support them. |
| `filaments` | `{profile}` or `{type, brand, variant}`, `color` (`#RRGGBB`), `extruder` (`left`/`right`/`auto`, H2D). Filament n is position n. |
| `objects` | `{name, part, filament, orient, copies, at}`. `part`: `{step}`, `{stl}`, `{3mf}`, `{goal}` or `{b3dt: {step, volume}}`. `orient`: `as_is`, `flat` (largest flat area down), or `{rotate: [rx, ry, rz]}` in degrees (X, then Y, then Z). `at`: a fixed centre `[x, y]`. |
| `intent` | `preset` (`fine`, `standard`, `strength`, `draft`) or `process` (an exact name); `layer_height`, `first_layer_height`, `walls`, `top_layers`, `bottom_layers`, `infill`, `infill_pattern`, `supports` (`none`/`auto`/`tree`), `support_angle`, `brim` (`none`/`auto`/mm), `seam`. |
| `overrides` | raw OrcaSlicer keys: `{process: {...}, machine: {...}, filament: {"all"/"1"/...: {...}}}`. They are checked against OrcaSlicer's keys and types. |
| `allow_mix_temp` | `true`: allow filaments outside each other's temperature range on one nozzle (one bed temperature for all) |
| `expect` | `[{quantity: dotted result path, value, tol (relative, default 0.05)}]` |
| `run` | `{timeout_s}` |
| `view` | `true`: also write `<name>.view.html`, a 3D view of the plate (see below) |

**Validation.** It refuses what OrcaSlicer would refuse, with the fix:
- an unknown printer, nozzle, filament or process;
- a cool plate on these printers;
- incompatible filaments on one nozzle, using OrcaSlicer's own rule: each filament's print temperature must lie in the other's range.

### Results (`<study>/<study>.json`)

| Key | Content |
|---|---|
| `ok`, `reliable`, `unreliable_because`, `warnings` | whether it sliced, whether every hard check passed (if not, do not print it), and the soft findings (OrcaSlicer's own warnings, unsupported regions, long bridges, thin walls, bed contact, shared nozzle, hot bed) |
| `files` | `gcode_3mf` (the file to print), `gcode`, `profiles` (the flattened copies), `log`, `command` |
| `estimate` | `time_s`, `time`, `filament_g`, `filament_m`, `by_filament`: OrcaSlicer's numbers, checked against ours |
| `objects.<name>` | `volume_mm3` (exact for STEP/b3dt), `volume_from`, `area_mm2`, `size_mm`, orientation, `centers`, `checks` (before slicing) |
| `profiles` | `machine`, `process`, `filaments`, `set` (every value changed: `[old, new]`), `sha256`, `orca_version`, `filament_map` |
| `placement` | the prime tower's position |
| `checks` | below, each `{passed, hard, why, got, expected}` |
| `gcode` | our own reading: layers, Z range, extrusion by filament and feature, travel range, header and footer |
| `slicer` | OrcaSlicer's exit code and name, seconds |
| `expect`, `seconds`, `execution` | as in the other translators |

## The checker

**Checks on the parts, before slicing** (`geometry.py`):

| Check | What it verifies | Kind |
|---|---|---|
| `fits` | the oriented part fits the area its extruder prints and the printable height | hard |
| `closed_mesh` | a closed, consistently oriented mesh. Hard for STL/3MF; a warning for STEP tessellations. | hard (mesh) |
| `bed_contact` | at least 20 mm² and 5% of the footprint flat on the bed, and height / √contact ≤ 10; otherwise use a brim or another orientation | soft |
| `unsupported_regions` | with supports `none`: nothing starts in mid-air (island) and nothing reaches more than 1 mm past the layer below on one side only (cantilever). Ceilings held on two sides are bridges and pass. The part is voxelized at the real layer heights (0.25 mm grid, coarser for big parts). A warning only: `unsupported_extrusion` on the slice decides. | soft |
| `thin_walls` | wall thickness from rays cast inward from area-weighted surface points (fixed seed); at most 1% thinner than one line (1.125 × nozzle) | soft |

**Checks on the slice.** The checker reads the G-code itself (`gcode.py`) and does not trust OrcaSlicer's summary.
- **What the parser follows:**
  - every move, including arcs, relative moves and absolute E;
  - tool changes, including Bambu's `M620`/`M620.10` filament-change macros;
  - temperatures;
  - OrcaSlicer's layer, feature and object markers.
- **What it treats as uncertain:**
  - Firmware conditional blocks (`M622 J…`/`M623`): recorded, not applied.
  - Bambu machine routines (`G150`, `G29.x`, `G380`, …): after one, the position is unknown until the next absolute move.
- **Regions:** the machine start G-code, the print, and the machine end G-code. Only the print is held to the part.

| Check | What it verifies | Kind |
|---|---|---|
| `archive_layout` | the `.gcode.3mf` holds `Metadata/plate_1.gcode`, its md5, `slice_info.config`, `model_settings.config`, `project_settings.config` | hard |
| `md5` | `plate_1.gcode.md5` = upper-case hex MD5 of the G-code; the plain `.gcode` is the same file | hard |
| `printer_model_id` | `slice_info.config` names the printer (H2D `O1D`, H2S `O1S`, P2S `N7`); OrcaSlicer 2.4.2 leaves it empty, so it is filled in | hard |
| `slice_info_values` | `first_layer_time` is sane. On the P2S, OrcaSlicer 2.4.2 writes an uninitialised 3.07e32; it is set from the G-code header's first-layer estimate. | soft |
| `thumbnails` | the plate pictures are present, as OrcaSlicer's GUI writes them (rendered by `thumbs.py`) | soft |
| `nozzle_diameters`, `filament_map` | `slice_info` has the study's nozzle and filament → extruder map | hard |
| `settings_roundtrip` | every setting OrcaSlicer knows that we wrote equals what it sliced with (`project_settings.config`). Catches the CLI's silent fallback to defaults. | hard |
| `objects_present` | every placed object is printed, and nothing else | hard |
| `bounds` | every extrusion of the print is inside the printable area of the extruder that prints it (H2D: left 0–325, right 25–350 mm), 0 < z ≤ height | hard |
| `layers` | first layer = `initial_layer_print_height`, then `layer_height`; count and top match the part height | hard |
| `first_layer` | the first extrusion is at the first layer height, and every object starts on the bed | hard |
| `tools` | each object is printed with its own filament; only the study's filaments are used | hard |
| `temperatures` | nozzle and bed targets = the filament's (first layer, then the rest), within the filament's range. The bed follows OrcaSlicer's formula: `by_first_filament` = the filament active at each layer change, `by_highest_temp` = the hottest used filament. | hard |
| `unlocated` | no extrusion of the print starts from an unknown position | hard |
| `extrusion_volume` | per object: extruded volume vs walls + top/bottom shells + infill of its exact volume (±25%), never above 1.1 × the volume | hard |
| `footprint` | no extrusion outside its allowed region: the object's silhouette + half a line, + brim, + support margin (5 mm); the prime tower inside its declared box and apart from the objects | hard |
| `unsupported_extrusion` | every extrusion of the parts rests on the layer below, or on support up to 3 layers below. "Rests" means within reach = half a line + `layer_height` / tan(`support_angle`), 0.556 mm at 0.42 / 0.2 / 30°. What a layer offers the next is its area (perimeters filled) and its support lines with their spacing filled. An unsupported stretch held at both ends and about straight is a bridge and passes. Fails when more than 5 mm of extrusion lies more than 1 mm beyond reach; says where (z, xy, object), whether it starts in mid-air, and the fix. | hard |
| `long_bridges` | bridges up to 15 mm (longer ones sag) | soft |
| `travel`, `wrapping_area`, `estimates` | travel stays on the bed; nothing in the wrapping-detection zone; OrcaSlicer's filament totals equal ours (0.5% length, 2% weight). Ours include the firmware flushes on filament changes, which the G-code carries only as `;VG1 E…` comments in `VFLUSH` blocks. | soft |
| `shared_nozzle` | (H2D) filaments whose print temperatures differ by more than 15 °C are not on the same nozzle. OrcaSlicer's auto map can put PLA and PETG on one nozzle. | soft |
| `bed_vs_filaments` | the hottest bed of the print is not more than 10 °C above a used filament's own bed temperature. PLA + PETG on one plate gets a 70 °C bed on some or all layers. | soft |
| `expect` | the study's expected values | hard |

**Tested on real slices.** On OrcaSlicer 2.4.2 every hard check passes for:
- the spike cases: P2S cube; H2D PLA+PLA, PLA+PETG, PETG+PETG;
- the benchmarks;
- `examples/studies.json`: P2S cube, H2S PETG copies, H2D two colours, P2S PLA+PETG with `allow_mix_temp` and a prime tower.

On those slices:
- filament totals match OrcaSlicer's to 0.01 mm, including 6.26 m of firmware flush on the P2S;
- extruded volume is 0.96–0.98 of the model;
- 268–335 settings compared per slice, no difference;
- the prime tower is printed inside the box we placed it in.

Ten deliberate faults were each caught by the expected check: a part moved by 5 mm, a stray blob, a move off the bed, a dropped layer, a wrong nozzle or bed temperature, swapped filaments, and a study asking for other layers or walls.

**b3dt → p3dt chain** (2026-10-24, on the Mac): parts built by build123d-translator's MCP server, given to p3dt as `{b3dt: {step, volume}}`. All reliable:
- the bracket (49,069.8 mm³) on the P2S, H2S and H2D: 135 layers, 21.0 g, extruded volume 0.993 of the model;
- the shelf (a 25 × 20 mm overhang at z = 25):
  - without supports, the 500 mm² underside was reported (as `overhang` then; now `unsupported_regions` and `unsupported_extrusion`);
  - with tree supports, 1438 mm³ of support from z 0.2 to 24.8, all within the part's silhouette.

**Support detection** (2026-10-29) was checked against 59 slices from the Mac:
- **The cradle that failed to print** (`examples/cradle.stl`, upright, no supports): a tooth starts in mid-air at z 42.6, 6 mm in front of the back plate. Its underside is 49° from horizontal, so a face-angle test misses it.
  - Without supports: 165 mm unsupported, unreliable.
  - With tree supports: 0 mm, reliable.
  - Laid flat: a 7.5 mm cantilever, unreliable.
- **Every bikeholder plate with that cradle** fails the same way. Some also have jaws that start in mid-air at z 17.2.
- **Plates that pass:**
  - the grid tiles, the mesh mat and coupons, and both pixel stands (whose 9.6 mm slot roofs are bridges);
  - a battery tray, with a soft warning for its 21 mm bridges.
- **Speed:** about 0.3–3 s per plate (1.1 M segments for the largest).

**Benchmarks** (`p3dt bench`, on generated boxes with exact volumes):
- `p2s_cube`: 20 mm PLA cube, 100 layers, 8000 mm³;
- `h2s_fine_copies`: three PETG slabs, fine preset, 3 mm brim;
- `h2d_two_filaments`: PLA left + PETG right, prime tower.

A benchmark passes when the result is reliable and its expected values hold.

### Assumptions about OrcaSlicer (each with what would catch it)

- **The flattened profile equals the GUI's.** Child keys replace parent keys whole, root first (`settings_roundtrip`).
- **A locked plate keeps our positions** (assemble list, `need_arrange: false`). Seen on the P2S and H2D (`footprint`, `bounds`).
- **The CLI keeps a prime tower position given in `wipe_tower_x/y`** (`footprint` checks the tower against its declared box, `bounds` against the bed).
- **On the H2D, `M620.10 A1 … P` is the incoming filament's print temperature** (`temperatures`).
- **An auto brim is at most 10 mm wide, and supports stay within 5 mm of the silhouette** (`footprint`).
- **Every filament needs a colour.** Without one, the 2.4.2 CLI crashes on 2+ filaments, so the translator always sets one.

## The 3D view

`"view": true` in a study, `p3dt view STUDY_FOLDER`, or the MCP tool `make_view` writes `<name>.view.html` next to the result. It shows:
- the placed parts on the bed;
- the toolpaths that our own G-code reader found, coloured by feature (outer and inner walls, infill, top and bottom surfaces, bridges, supports, brim, prime tower) or by filament;
- extrusion with nothing below it (`unsupported_extrusion`) drawn red in both colour modes;
- a layer slider, with an option to show only the current layer.

To move around: drag to rotate, shift-drag to pan, scroll to zoom.

It is one HTML file with no libraries and nothing loaded from the internet: plain WebGL2, with the meshes and toolpaths embedded as zlib-compressed binary that the browser inflates. It runs in Safari 16.4+, Chrome or Firefox and is about 200–300 kB for a small plate.

## Comparing with a GUI export (S4)

`tools/compare_bambu.py BAMBU.gcode.3mf OURS.gcode.3mf` lists what Bambu Studio's export has that ours lacks, or has differently: archive members, `slice_info` metadata and filament attributes, `model_settings`, `plate_1.json`, project-settings keys, the G-code header and thumbnails.

Put GUI exports (OrcaSlicer or Bambu Studio, "Export plate sliced file") in `debug/bambu/` (not committed). They contain Bambu's start G-code.

**Result** (2026-10-24, the bracket sliced by OrcaSlicer 2.4.2's GUI vs our CLI run, P2S and H2D):
- **The G-code is equivalent:** same placement, same 135 layers, the same extruded volume per feature to 1 mm³, and settings identical apart from list padding. Ours has more, shorter moves, because our STL is tessellated more finely.
- **The archives differed only in packaging.** `gcode3mf.finalize()` now writes what the GUI writes:
  - `printer_model_id`;
  - `first_layer_time` (from the G-code header);
  - `nozzle_volume_type` with one value per extruder (H2D `"0 0"`);
  - the `model_settings` plate entries (`filament_maps`, `thumbnail_file`, `thumbnail_no_light_file`, `top_file`, `pick_file`, `pattern_bbox_file`);
  - the five plate pictures.
- **The pictures** (`thumbs.py`, numpy z-buffer):
  - `top_1.png` matches the GUI's layout pixel for pixel;
  - `plate_1.png` is an isometric close-up, as in the GUI;
  - `pick_1.png` encodes each object's `identify_id` for the printer's skip-object.
- **What still differs:**
  - our archive keeps the meshes (`3D/Objects/*.model`), which is harmless;
  - the GUI's `slice_info first_layer_time` (457 s) counts more than the header's first layer (313 s), so ours uses the header's.

## How it works

| Module | Job |
|---|---|
| `profiles.py` | Find OrcaSlicer's profiles; pick machine, process (preset → nearest layer height), filaments (by `compatible_printers`); flatten (OrcaSlicer 2.4.2's CLI ignores `inherits`, issue #14718); apply the plate, intent, overrides (typed against `data/orca_keys.json`) and colours; write canonical copies with sha256. |
| `geometry.py` | Load STEP / b3dt (build123d, exact volume; tessellated at 0.01 mm), STL, 3MF (with components); orient; check before slicing. |
| `placement.py` | Place the parts and the prime tower on a 1 mm grid. Each part stays inside the area its extruder prints and outside the wrapping zone, with a halo for brim and gap. Fixed `at` positions are kept, the largest part goes nearest the centre, and the tower goes in a back corner. |
| `orca.py` | Find OrcaSlicer and its version. Build the command (assemble list, bed type, filament map, `--allow-mix-temp`). Run it in its own process group with a timeout and a cancel file. Decode the exit codes. |
| `gcode.py` | Read the G-code: segments with region, tool, feature, object and layer; temperatures; tool changes; header and footer. |
| `gcode3mf.py` | Read and check the archive; compare the settings round trip; `finalize()` writes what OrcaSlicer's GUI export has and the CLI lacks. |
| `viewer.py` | The self-contained 3D view (`<name>.view.html`). |
| `supports.py` | Will it print without supports? `regions()` before slicing (voxels: islands, cantilevers, bridges), `unsupported()` after (G-code: extrusion with nothing below, flags for the view). |
| `thumbs.py` | The plate pictures (isometric preview, top view, skip-object pick image), rendered from the placed meshes. |
| `check.py` | The checks on the slice. |
| `format.py` | `study_format` and validation, including OrcaSlicer's filament-compatibility rule. |
| `runner.py` | One study end to end, into its folder; the result is written atomically. |
| `api.py`, `cli.py`, `mcp_server.py` | Requests run in a background process; status reads files. |

## For LLM tools: the MCP server

`p3dt-mcp` (stdio, local). Tools:
- `study_format`, `validate_studies`;
- `run_studies` (returns within `wait_s` ≤ 50 s) and `study_status`;
- `get_inputs` (profiles, placement and command without slicing);
- `run_benchmarks`, `bench_status`;
- `get_file`, `make_view`, `cancel`.

The instructions tell the model to:
- check `reliable` before using a result;
- give every filament a colour;
- put filaments with different temperatures on separate H2D nozzles;
- stop polling runs that take more than about 5 minutes.

`run_studies` and `study_status` return a summary per study (about 3 kB): the verdict, warnings, files, estimate, profiles, objects, the failed checks in full and the passed ones by name. The full result stays on disk (`result`). `p3dt run` prints the same summary; `--full` prints everything.

Claude desktop config:

```json
{"mcpServers": {"p3dt": {"command": "/Users/feranick/Software/print3d_translator/.venv/bin/p3dt-mcp"}}}
```

## Files

```
print3d_translator/   profiles.py geometry.py placement.py orca.py gcode.py gcode3mf.py check.py
                      format.py runner.py api.py cli.py mcp_server.py mesh.py data/orca_keys.json
tools/                orca_keys.py (key data from OrcaSlicer's PrintConfig.cpp), spike.py (real-CLI experiments)
tests/                fixtures/profiles (hand-written), fake_orca.py, test_*.py
examples/studies.json
planning.md           session context and state log
```

OrcaSlicer's profiles are AGPL-3.0, so they are read from the installed OrcaSlicer and never copied into this repository.

## Versions

| Version | Changes |
|---|---|
| 2026.10.29.2 | Support messages: "more than N mm" when the distance passes the computed window; pre-slice warnings name what they found; chamfer slivers left out of `unsupported_regions`. Checked on the Mac through MCP (cradle: none and flat unreliable, tree reliable). |
| 2026.10.29.1 | Support detection (`supports.py`): hard check `unsupported_extrusion` on the slice, soft `long_bridges`; `unsupported_regions` replaces `overhang` before slicing; unsupported toolpaths drawn red in the 3D view; `examples/cradle.stl`. |
| 2026.10.24.10 | 3D view (`viewer.py`): study key `view`, `p3dt view`, MCP tool `make_view`. |
| 2026.10.24.9 | S4 against OrcaSlicer's GUI exports. The G-code is equivalent. `gcode3mf.finalize()` and `thumbs.py` make the archive match the GUI's: plate pictures with the skip-object pick image, `model_settings` entries, per-extruder `nozzle_volume_type`, `first_layer_time` from the header. Soft check `thumbnails`; the plate name matches the GUI (""). |
| 2026.10.24.8 | b3dt → p3dt chain tested on the Mac (bracket on P2S/H2S/H2D; a shelf with tree supports and without); `examples/s4_bracket.step`, `overhang_shelf.step`, `goals.json`; `slice_info` `first_layer_time` garbage reset; `tools/compare_bambu.py` (S4). |
| 2026.10.24.7 | From the first full runs on the Mac:
- the bed check follows `by_first_filament` per layer;
- `estimates` compares totals including firmware flushes (`;VG1` in `VFLUSH`);
- `bed_vs_filaments` uses the hottest bed of the print;
- MCP and CLI return compact summaries (the full JSON stays on disk);
- an explicit `P3DT_ORCA_PROFILES` has no fallback;
- the fake OrcaSlicer's crash mode uses SIGKILL, so macOS writes no crash report. |
| 2026.10.24.6 | `geometry.py` (STEP/b3dt/STL/3MF loading, orientation, checks before slicing), `placement.py` (parts and prime tower: fixes the off-bed tower on the P2S), `format.py` (study format and validation with OrcaSlicer's filament-compatibility rule), `runner.py`, `api.py`, `cli.py`, `mcp_server.py`, benchmarks, `allow_mix_temp`; fake OrcaSlicer and end-to-end tests. |
| 2026.10.24.5 | Soft checks `shared_nozzle` and `bed_vs_filaments`; a filament map shorter than the filament list is padded with its first value, as OrcaSlicer does. |
| 2026.10.24.4 | Fix: every filament gets a `filament_colour` (the study's, or a distinct default with a warning). Without one, OrcaSlicer 2.4.2's CLI skips the flush-volume matrix and crashes (SIGSEGV) on any plate with 2+ filaments. Spike case P2S PLA + PETG with `--allow-mix-temp`. |
| 2026.10.24.3 | `gcode.py` (streaming G-code reader), `gcode3mf.py` (archive checks, settings round trip, `printer_model_id` patch), `check.py`; scipy dependency; spike cases to narrow down the H2D PLA + PETG crash. |
| 2026.10.24.2 | `orca.py` (find, version, command with the assemble list, run in a process group with timeout and cancel, exit codes with fixes); `mesh.py`; `tools/spike.py`; tests with a fake OrcaSlicer and one real slice. |
| 2026.10.24.1 | Project skeleton; profile resolver (`profiles.py`) with intent and overrides; OrcaSlicer key data (2.4.2); tests on a hand-written profile tree and on the real profiles. |
