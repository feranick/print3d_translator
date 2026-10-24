# print3d_translator (`p3dt`)

Turns a CAD part into a checked 3D-print file for a Bambu Lab printer, using OrcaSlicer's command line.
An LLM (or a person) writes a short JSON study: the part, the printer, the filaments and the intent.
The translator resolves OrcaSlicer's system profiles into flattened settings and slices the part.
It then checks the G-code independently.

**It never talks to a printer.** No network, no cloud, no submission. It writes files only.

It follows the conventions of the llm-translators family (b3dt, feat, cfdt, lmpt, omct), but is a
standalone repository that runs locally (macOS first).

**Status: in development.** Done so far: the profile resolver and the OrcaSlicer runner. Next: the
G-code parser and checker, the `.gcode.3mf` checks, geometry checks, API, MCP server. See `planning.md`.

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

## Versions

| Version | Changes |
|---|---|
| 2026.10.24.2 | `orca.py` (find, version, command with the assemble list, run in a process group with timeout and cancel, exit codes with fixes); `mesh.py` (STL in/out, box, transforms, volume); `tools/spike.py`; tests with a fake OrcaSlicer and one real slice (skipped without OrcaSlicer). |
| 2026.10.24.1 | Project skeleton; profile resolver (`profiles.py`) with intent and overrides; OrcaSlicer key data (2.4.2); tests on a hand-written profile tree and on the real profiles. |
