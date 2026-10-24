# print3d_translator (`p3dt`)

Turns a CAD part into a checked 3D-print file for a Bambu Lab printer, using OrcaSlicer's command line.
An LLM (or a person) writes a short JSON study: the part, the printer, the filaments and the intent.
The translator resolves OrcaSlicer's system profiles into flattened settings and slices the part.
It then checks the G-code independently.

**It never talks to a printer.** No network, no cloud, no submission. It writes files only.

It follows the conventions of the llm-translators family (b3dt, feat, cfdt, lmpt, omct), but is a
standalone repository that runs locally (macOS first).

**Status: in development.** Done so far:
- the profile resolver;
- the OrcaSlicer runner;
- the G-code parser, the `.gcode.3mf` checks and the checker, tested on real P2S and H2D slices.

Next: geometry checks before slicing, the runner that ties it together, API, CLI, MCP server.
See `planning.md`.

## Scope

- **Printers:** Bambu Lab H2D (two extruders), H2S, P2S. Nozzles 0.2, 0.4, 0.6 and 0.8 mm.
- **Filaments:** Bambu PLA Basic/Matte, PETG HF/Basic, and generic PLA/PETG. Any filament profile compatible with the printer resolves.
- **Multi-material:** several filaments per plate. On the H2D, each filament can be assigned to the left or right extruder.
- **Input:** STEP, STL, 3MF, or a build123d-translator part. Units are mm.
- **Output:** the Bambu `.gcode.3mf`, the plain `.gcode`, the slicer's estimates, and the checks.

## Install (development)

```bash
cd ~/Software/print3d_translator
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"            # add ,cad for STEP input and ,mcp for the MCP server
pytest -q
```

OrcaSlicer 2.4.2 is the pinned backend.
- On macOS, install `OrcaSlicer.app` in `/Applications`.
- To use another install, set `P3DT_ORCA_PROFILES` to its `resources/profiles` folder (the one with `BBL.json`).

The tests that need OrcaSlicer's profiles are skipped when it is not found.

## Profiles: how they are resolved

1. **Pick the profiles.**
   - The machine is `Bambu Lab <model> <nozzle> nozzle`.
   - The process comes from the intent's preset, or an explicit name: among the processes whose `compatible_printers` names the machine, the one with the preset's words and the nearest layer height.
   - Each filament is the compatible profile with that base name. Filament names are not uniform across printers (`… @BBL H2S` vs `… @BBL P2S 0.4 nozzle`), so the match goes by `compatible_printers`, never by building names.
2. **Flatten each profile.**
   - The parents are followed through the vendor index (`BBL.json` `sub_path`), root first.
   - A child's key replaces the parent's value whole.
   - `inherits` is dropped and `from` is set to `"system"`.
   - OrcaSlicer 2.4.2's CLI does not follow `inherits` (issue #14718): without flattening, unset keys fall back to defaults silently.
3. **Apply the plate, the intent and the overrides.**
   - Values are typed against OrcaSlicer's own keys (`data/orca_keys.json`, extracted by `tools/orca_keys.py` from the pinned `PrintConfig.cpp`).
   - A key that belongs to another profile is an error that names the right section.
   - Every changed value is reported as `[old, new]`.
4. **Write copies** into the study's folder, as canonical JSON with sorted keys and their sha256. The installed profiles are never modified.

## Running OrcaSlicer

`orca.py` finds the executable in this order:
1. `P3DT_ORCA`;
2. `orca-slicer` or `OrcaSlicer` on PATH;
3. `/Applications/OrcaSlicer.app`;
4. an extracted AppImage.

It reads the version from `--help`.

The geometry goes in through an **assemble list** (`--load-assemble-list`): one STL per object, already oriented and placed in bed coordinates, with `need_arrange: false`. OrcaSlicer's own orient and arrange are heuristics, so they are never used.

The command:

```
OrcaSlicer --load-settings "machine.json;process.json" --load-filaments "filament_1.json;..."
           --load-assemble-list assemble.json --curr-bed-type "Textured PEI Plate" --slice 0
           --outputdir <study folder> --export-3mf plate.gcode.3mf --debug 3 --logfile orca.log
           [--filament-map-mode Manual --filament-map 2,1]        (H2D: 1 = left, 2 = right)
```

- The bed type is always given, because the CLI defaults to Cool Plate, which H2D/H2S/P2S do not support.
- OrcaSlicer runs in its own process group, so a timeout or a `cancel` file in the study folder stops it whole.
- Its exit status is decoded into OrcaSlicer's code (for example -17 `PROCESS_NOT_COMPATIBLE`), with what to change.

`tools/spike.py` runs a few small cases (a cube on the P2S, two cubes with two filaments on the H2D) and summarizes what the CLI produced into `debug/spike/`: archive layout, md5, positions, layers, temperatures, and whether the sliced settings equal ours.

## The checker

The checker reads the G-code itself (`gcode.py`) and does not trust OrcaSlicer's summary.
- **What the parser follows:**
  - every move, including arcs, relative moves and absolute E;
  - tool changes, including Bambu's `M620`/`M620.10` filament-change macros;
  - temperatures;
  - OrcaSlicer's layer, feature and object markers.
- **What it treats as uncertain:**
  - Firmware conditional blocks (`M622 J…`/`M623`). Their temperatures are recorded but not applied, and a move inside one makes the position unknown.
  - Bambu machine routines (`G150`, `G29.x`, `G380`, …). After one, the position is unknown until the next absolute move.
- **Regions:** the machine start G-code, the print, and the machine end G-code. Only the print is held to the part.

Checks on every plate. A failed hard check makes the result `reliable: false`.

| Check | What it verifies | Kind |
|---|---|---|
| `archive_layout` | the `.gcode.3mf` holds `Metadata/plate_1.gcode`, its md5, `slice_info.config`, `model_settings.config`, `project_settings.config` | hard |
| `md5` | `plate_1.gcode.md5` = upper-case hex MD5 of the G-code; the plain `.gcode` is the same file | hard |
| `printer_model_id` | `slice_info.config` names the printer (H2D `O1D`, H2S `O1S`, P2S `N7`); OrcaSlicer 2.4.2 leaves it empty, so it is filled in | hard |
| `settings_roundtrip` | every setting OrcaSlicer knows that we wrote equals what it sliced with (`project_settings.config`). Catches the CLI's silent fallback to defaults. | hard |
| `objects_present` | every placed object is printed, and nothing else | hard |
| `bounds` | every extrusion of the print is inside the printable area of the extruder that prints it (H2D: left 0–325, right 25–350 mm), 0 < z ≤ height | hard |
| `layers` | first layer = `initial_layer_print_height`, then `layer_height`; count and top match the part height | hard |
| `first_layer` | the first extrusion is at the first layer height, and every object starts on the bed | hard |
| `tools` | each object is printed with its own filament; only the study's filaments are used | hard |
| `temperatures` | nozzle and bed targets = the filament's (first layer, then the rest; bed by OrcaSlicer's formula), within the filament's range | hard |
| `unlocated` | no extrusion of the print starts from an unknown position | hard |
| `extrusion_volume` | per object: extruded volume vs walls + top/bottom shells + infill of its exact volume (±25%), never above 1.1 × the volume | hard |
| `footprint` | no extrusion outside its allowed region: the object's silhouette + half a line, + brim, + support margin (5 mm); the prime tower inside its declared box and apart from the objects | hard |
| `travel`, `wrapping_area`, `estimates` | travel stays on the bed; nothing in the wrapping-detection zone; OrcaSlicer's filament lengths and weights equal ours (0.5%, 1%) | soft |

**Tested on real slices.** On OrcaSlicer 2.4.2 slices (P2S cube, H2D two cubes with two filaments) every check passes:
- filament lengths match OrcaSlicer's to 0.01 mm;
- extruded volume is 0.96–0.98 of the model;
- 269 (P2S) and 329 (H2D) settings compared, no difference.

Ten deliberate faults were each caught by the expected check: a part moved by 5 mm, a stray blob, a move off the bed, a dropped layer, a wrong nozzle or bed temperature, swapped filaments, and a study asking for other layers or walls.

**Assumptions** (each with what would catch it):
- An auto brim is at most 10 mm wide (`footprint` would fail on a wider brim).
- Supports stay within 5 mm of the silhouette (`footprint`).
- On the H2D, `M620.10 A1 … P` is the incoming filament's print temperature (`temperatures`).

## Versions

| Version | Changes |
|---|---|
| 2026.10.24.3 | `gcode.py` (streaming G-code reader), `gcode3mf.py` (archive checks, settings round trip, `printer_model_id` patch), `check.py` (the checks above); scipy dependency; spike cases to narrow down the H2D PLA + PETG crash. |
| 2026.10.24.2 | `orca.py` (find, version, command with the assemble list, run in a process group with timeout and cancel, exit codes with fixes); `mesh.py` (STL in/out, box, transforms, volume); `tools/spike.py`; tests with a fake OrcaSlicer and one real slice (skipped without OrcaSlicer). |
| 2026.10.24.1 | Project skeleton; profile resolver (`profiles.py`) with intent and overrides; OrcaSlicer key data (2.4.2); tests on a hand-written profile tree and on the real profiles. |
