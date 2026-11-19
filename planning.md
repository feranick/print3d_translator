# print3d_translator: planning

Session context. A new chat starts with "Read planning.md and start."
Status (2026-11-14): **2026.11.14.3, complete end to end, P2S and H2S only (H2D removed), pinned to
OrcaSlicer 2.5.1, verified on the Mac through MCP** (bench 3/3; cabin parts, colour change + pause and
A350 plate 1 reliable or accepted). A350 plate 1 printed successfully on the real P2S.
194 tests pass, 11 skip (sandbox). Details of the study format, the checker, placement, 3MF input and
OrcaSlicer versions are in the README; this file keeps decisions, setup, lessons and history.

## Decisions

- **Repository:** `print3d_translator` (github.com/feranick/print3d_translator), at `~/Software/print3d_translator`.
  Standalone: **not added to the llm-translators suite**, but follows its conventions
  (docs/create_new_translators.md), so it could join later.
- **It produces and checks files only.** No printer communication at all: no network, no cloud, no submission.
  The user prints `files.gcode_3mf` from the SD card, Bambu Studio or Handy. A printer status probe
  (progress, temperatures, AMS) would be a **separate, read-only tool** outside p3dt (decided 2026-11-14).
- **Runs locally on the Mac.** No remote features unless one gives a clear advantage: no SLURM, executor,
  router or machines; MCP stdio only. `get_file` and a local `cancel` flag file are kept (cheap).
  Linux x86_64/aarch64 stay supported (AppImage layout, tests) but are secondary.
- **Terminology:** "study" (one study = one plate), so API, CLI and MCP names match the other translators.
- **Versioning:** YYYY.MM.DD.x in `__init__.py`, `pyproject.toml`, module docstrings and the README versions table.
- **Licence:** OrcaSlicer's bundled profiles are AGPL-3.0: never copied into the repo; read from the installed
  app or the source checkout. Test fixtures are small hand-written profile trees.
- **Scope:** Bambu Lab P2S (the user's printer) and H2S, one nozzle; the H2D was removed on 2026-11-14
  because it cannot be tested (git tag `h2d-last`, branch `h2d`, `docs/H2D.md`); nozzles 0.2/0.4/0.6/0.8; Bambu PLA Basic/Matte,
  PETG HF/Basic, generic PLA/PETG; multi-material (AMS) included. Out of scope for now: variable layer
  height, ironing, fuzzy skin.

## Names

| Thing | Value |
|---|---|
| Package = repository | `print3d_translator` |
| CLI | `p3dt` |
| MCP server | `p3dt-mcp` (stdio) |
| Settings | `P3DT_ORCA`, `P3DT_ORCA_PROFILES`, `P3DT_OUT`, `P3DT_WORKERS`, `P3DT_TIMEOUT` |
| Output folder | `~/p3dt_runs` |

## Modules

| Module | Job |
|---|---|
| `format.py` | `study_format()`, `validate()`, `expand()` (from_3mf → objects) |
| `orca.py` | Find OrcaSlicer, version (cached per exe + mtime), `PINNED`, run with timeout, assemble list, exit-code explanations, `conflicts()` from the log |
| `profiles.py` | Flatten profiles (inherits + include), intent/overrides, compatible_printers, orca_keys per version, profile version check |
| `geometry.py`, `mesh.py` | STEP/STL/3MF/b3dt loading, orientation, pre-slice checks (fits, closed mesh, bed contact, unsupported regions, thin walls) |
| `threemf.py` | Fast 3MF / Bambu Studio project reader (plates, parts, subtypes, filaments, per-object settings) |
| `placement.py` | Silhouette stamps + FFT free-position search, layout modes, prime tower |
| `supports.py` | Unsupported extrusion on the G-code (per object), bridges, mesh regions |
| `gcode.py` | Streaming G-code parser |
| `gcode3mf.py` | Archive checks, settings round trip, `finalize()` (S4 fixes), pause insertion |
| `check.py` | All post-slice checks → `checks`, `reliable` |
| `thumbs.py`, `viewer.py` | Thumbnails; self-contained WebGL `<name>.view.html` |
| `runner.py` | `run_study()` end to end, `supports: "needed"`, `accept` |
| `api.py`, `cli.py`, `mcp_server.py` | Contract (10 MCP tools, stdio) |
| `tools/` | `orca_keys.py`, `spike.py`, `compare_bambu.py`, `support_corpus.py` |

## Mac setup (current)

- `/Applications/OrcaSlicer.app` = 2.5.1; `/Applications/OrcaSlicer-2.4.2.app` kept for comparison.
- JSON profile tree: `~/Software/orcaslicer-src` (sparse checkout of OrcaSlicer's source, tag v2.5.1).
  2.5 apps ship only the compiled `BBL.opc`. `P3DT_ORCA_PROFILES` points at it, set in `~/.profile` (and, for clients without env fields, ~/.config/p3dt/config.json via `p3dt config`)
  and in the p3dt entry ("env") of `claude_desktop_config.json`. Not in llm-translators' `config/env`.
- Vendor profiles 02.08.00.16; `status` shows `profiles_version.match`.
- The MCP server runs from the project's `.venv` (Python 3.12, build123d 0.13.0,
  build123d-translator 2026.10.28.1). Restart Claude after code updates (the server keeps the code it started with).
- `~/p3dt_runs` and `~/b3d_parts` are shared with the sandbox; the sandbox cannot run OrcaSlicer
  (GitHub release assets blocked) and runs out of memory on the largest plates: verify on the Mac.
- **Updating OrcaSlicer:** README "OrcaSlicer versions" (checkout the new tag in orcaslicer-src,
  `tools/orca_keys.py`, `PINNED`, bench). No config edits needed.

## OrcaSlicer behaviour we design around

- **`--load-settings` ignored `inherits` in 2.4.2** (silent defaults) → we flatten profiles ourselves and verify
  with the settings round trip. **2.5 adds `include`** (G-code templates on machines, variant templates on
  filaments): per level, includes first, then the profile's own keys.
- **Compatibility is by name** → flattened copies keep their names, `from: system`, no `inherits`, and
  `compatible_printers` set. Filament names are not uniform: choose by `compatible_printers`.
- **Bed type defaults to Cool Plate** (unsupported on these printers) → always `--curr-bed-type`; `PLATE_ALIASES`
  map Bambu Studio names to Orca's enum.
- **`filament_colour` missing → SIGSEGV** with 2+ filaments (seen on the H2D; no flush matrix) → every filament gets a colour.
- **Positions:** `--load-assemble-list` with `need_arrange: false`, one STL per object already placed, `--arrange 0 --orient 0`.
  Positions and our `wipe_tower_x/y` are kept. The prime tower must be placed by us (default lands off the P2S bed: -102).
- **Assemble list extras:** `assemble_index` merges parts into `assemble_N`; `print_params` per object;
  `height_ranges` must include `layer_height` (otherwise 2.4.2 segfaults in layer_height_profile_from_ranges).
- **Archive:** `printer_model_id` empty and P2S `first_layer_time` garbage (2.4.2), no thumbnails →
  `finalize()` fills them as the GUI does (checked against GUI exports, S4).
- **Firmware flushes** appear only as `;VG1 E` in VFLUSH blocks; prime lines and purge-chute moves go outside
  the bed → known zones exempted; totals compared with flushes counted.
- **flush_multiplier defaults to 0.3** in OrcaSlicer (CLI and GUI; Bambu Studio 1.0): the colour matrix is right but
  scaled down → p3dt writes the matrix and multiplier 1 itself (flush.py); `flush_adequacy` checks every change.
- **Bed temperature** `by_first_filament` changes at layer 2 with mixed filaments (not a fault).
- **Support-only layers** (normal supports) print at their own heights → layers and unsupported checks handle them.
- **2.5:** `M104 T<n> S.. N0` idle-nozzle pre-heat/cool (`nozzle_standby`); some profile values invalid for Orca itself
  (`normalized_by_orca`); 995 setting keys (2.4.2: 860), `data/orca_keys-<version>.json`.
- **Exit codes** (`libslic3r/Utils.hpp`): -17 process not compatible, -52 outside bed, -62 different temperatures
  (`allow_mix_temp`), -101 G-code path conflicts (log names the objects), -102 path in unprintable area; 139 = segfault.
- `result.json`/`--pipe` are Linux-only; boolean flags take no value; `--export-3mf` wants a relative name.

## History

- **2026-10-24:** research, proposal, repository, local-only decision. Profile resolver; Mac spike S1–S3
  (positions kept, H2D filament map); G-code parser, archive checks, checker; the H2D 2-filament crash explained
  (`filament_colour`); translator complete end to end (geometry, placement, prime tower, API/CLI/MCP).
  First Mac runs; compact MCP summaries; b3dt → p3dt chain; S4 against OrcaSlicer's GUI (`finalize()`);
  3D viewer.
- **2026-10-29:** support detection after the user's cradle failed (a tooth started in mid-air):
  `unsupported_extrusion` on the G-code (hard), `unsupported_regions` on the mesh (soft); corpus of 59 slices.
- **2026-10-30:** support-only layers fixed; `supports: "needed"` (slice without supports, re-slice with tree if needed).
- **2026-11-11:** the A350 report (7 items): multi-part objects, Bambu Studio projects (`from_3mf`), per-object
  settings, colour changes and pauses, -101 explained, closed-mesh check relaxed, placement rewritten (layout
  modes), `accept`, top slivers. Verified on the Mac. OrcaSlicer 2.5.0 support (include, `.opc`, round-trip
  normalization, per-version keys) and switch; b3dt installed in `.venv`. Changelog: `CHANGES_2026-11-11.md`.
- **2026-11-14:** OrcaSlicer 2.5.1 pinned (no relevant CLI/profile change; keys identical to 2.5.0);
  version cache per mtime; one profile checkout `orcaslicer-src`. Bench 3/3, identical to 2.5.0.
  **A350 plate 1 printed successfully** (the first real print reported). Its remaining findings (hatch 4.75 mm
  overhang at z 10, mid-fuselage ceiling at z 208.4, small spots at z 7.4 / 30.8 / 82.2, slivers on the rear
  rim and hatch tip) did not affect the print: findings of this size, accepted with `accept`, are tolerable.
  **2026.11.14.2: H2D support removed** (only a P2S to test on); tag `h2d-last`, branch `h2d`, `docs/H2D.md`.
  `tools/compare_bambu.py` gains the multi-filament comparison and an HTML report (for a P2S GUI export).
  **2026.11.14.3:** cabin C2 (two PLA colours) sliced in OrcaSlicer 2.5.1's GUI vs p3dt: 69 same, 4 close,
  1 difference (`filament_maps` "1" vs the GUI's "1 1"), now padded by `finalize()` as the GUI writes it.
  Fresh slice with the fix (run_20261114_185700_3a3a26): 71 same, 3 close (time 5 s, volumes 0.2 mm³), 0 differ.
  Colour change at exactly a layer top (tile 30×30×4, z 2.0): OrcaSlicer switches by the layer's middle; the
  `tools` check used the layer top and failed it; fixed (and fake_orca). Test print: test_parts/tile30x30x4.stl.
  **bstat** (status/): read-only printer status monitor (web, terminal, window), first version, not yet run
  against the P2S.
- **2026-11-16:** a black + white CAT keychain came out grey: OrcaSlicer's CLI computes Bambu's colour matrix
  (670 / 190 mm³) but keeps its default flush_multiplier 0.3 (201 / 57; the OrcaSlicer GUI does the same,
  Bambu Studio uses 1.0). **2026.11.16.1:** p3dt writes the colour matrix (flush.py, Bambu's model, checked
  against 12 OrcaSlicer matrices: 11 equal, orange -> black 215 vs Orca's 166, ours higher) with multiplier 1;
  intent.flush; from_3mf.flush keep/warn; check flush_adequacy (0.9 x model; hard < 50%); estimate.tool_changes.
  On the Mac (run_20261116_132755_3be2e7): CAT auto reliable, 670 mm³ per black -> white change, flush 4.21 m;
  forced [0,140,140,0] unreliable (flush_adequacy hard); orange reliable. Also fixed: one-part 3MF objects lost
  their filament.

- **2026.11.16.2:** colour-painted 3MF objects (panda clicker): paint decoded (66,968 triangles; states 0/2/3
  as the user counted), `from_3mf.paint` keep/drop, the plate sliced from a trimmed copy of the 3MF. OrcaSlicer
  quirks found on the way: projects from Bambu Studio 02.06 need `--allow-newer-file` (-24 otherwise); without
  project settings the CLI assumes a 200 x 200 bed and shifts everything 28 mm; filaments sharing a preset name
  count as one and the prime tower is turned off unless `--filament-colour` is given; with colours given it
  recomputes the flush matrix (Bambu's measured table: orange -> black 166 vs our formula's 215). Panda on the
  Mac: reliable, 82 changes 2<->3, paint_applied and flush_adequacy pass. Basket (unpainted): unchanged path.

- **2026.11.18.1:** for the Gemini Mac app (Local MCP): handshake measured at 0.2 s (no slow work at import);
  no multiprocessing in p3dt (worker = one subprocess, falls back to a thread; OrcaSlicer must be startable,
  else NOT_PERMITTED with the fix); settings from ~/.config/p3dt/config.json (`p3dt config`); README lists
  the folders and programs a customized sandbox must allow.

## Open

- **After restarting Claude:** check paint 'drop' and a remapping filament_map on the real panda through MCP
  (the running server still validates with 2026.11.16.1, which does not know from_3mf.paint).
- **A real print** of the tree-support cradle; preview and skip-object on the printer still to be checked.
- **ACTION (user, when the printer is free): print the two-colour test tile** on the P2S:
  `~/p3dt_runs/run_20261114_190244_65a951/two_colour_tile/two_colour_tile.gcode.3mf` (30 × 30 × 4 mm, white
  to 2.0 mm then blue; ~18 min, 3.2 g). AMS: filament 1 → white PLA Basic, filament 2 → blue. Check: plate
  preview on the screen, skip-object lists the tile, one change after the layer at 2.0 mm, prime tower at the
  back left, white base / blue top. Then the bigger one if wanted: cabin C2
  (`run_20261114_185700_3a3a26/cab_C2_parts/cab_C2_parts.gcode.3mf`, 2h50m, 28 g).
- **Printer status: `status/bstat`** (separate, read-only, not imported by p3dt; own version 2026.11.14.1;
  status/README.md). Built: probe, web page, terminal dashboard, Tk window; demo and replay; HMS text from
  the slicers' local hms files; camera on port 6000 (P1 protocol). Its only publish is `pushall`
  (`--no-pushall`: nothing sent), checked by a test. Tested in the sandbox: 16 tests, including a fake
  printer (MQTT over TLS, self-signed); the page's script run under node. **ACTION (user):** `pip install -e
  status/`, `bstat web --demo`, `bstat config ...`, `bstat probe` with the P2S as it is now; then decide on
  LAN Only + Developer Mode if refused. Then check the decoding against the recording in ~/p3dt_runs/bstat/.
- Ideas: `orient: "min_support"` (search rotations by unsupported area); suggest an orientation when supports
  are `none` and regions fail; support enforcers/blockers from flagged regions (needs painted 3MF input and a CLI spike).
