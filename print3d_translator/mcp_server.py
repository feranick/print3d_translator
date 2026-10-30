"""mcp_server.py — MCP server (stdio): lets LLM-based tools turn parts into checked Bambu Lab print files
with OrcaSlicer. It never talks to a printer.
Version: 2026.10.30.2

Tools (all JSON):
  study_format      the study format: printer, filaments, objects, intent, overrides. Read it first.
  validate_studies  static checks of {name: study} (fast; fix errors before running)
  run_studies       slices and checks {name: study}; returns within wait_s (<= 50 s); slow ones stay pending
  study_status      collects the pending studies of a run_studies request
  get_inputs        the flattened profiles, placement and OrcaSlicer command of one study, without slicing
  run_benchmarks    benchmark studies with known answers; bench_status collects them
  get_file          a written file as base64 (e.g. the .gcode.3mf)
  make_view         a self-contained 3D view (.view.html) of a finished study
  cancel            stops a request's studies

Run:  p3dt-mcp          (stdio; local only)
"""
from __future__ import annotations

from . import api

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """print3d_translator turns parts into checked 3D-print files for Bambu Lab printers (H2D, H2S,
P2S) with OrcaSlicer: one study = one plate (printer, nozzle, plate type, filaments, objects, intent). Parts
are STEP, STL or 3MF files, build123d-translator goals, or b3dt results ({'b3dt': {step, volume}}). The
translator flattens OrcaSlicer's profiles, orients and places the parts (and the prime tower), slices, and
checks the result independently. It writes files only: it never sends anything to a printer.
Workflow: call study_format once and follow its conventions; validate_studies and fix every error;
run_studies (most plates finish in seconds; slow ones come back 'pending': collect with study_status).
Before using a result check 'reliable' (false: do not print it; 'unreliable_because' says why), 'warnings'
(unsupported regions, long bridges, thin walls, small bed contact, shared nozzle, hot bed) and 'checks'.
unsupported_extrusion fails when part of a layer has nothing below it (a feature starting in mid-air, a
cantilever): enable supports ("tree") or orient the part differently, and say where it was (z, xy).
The file to print is files.gcode_3mf: the user prints it from the printer's SD card, Bambu Studio or Handy.
Hints: orient 'flat' puts the largest flat face down; 'at' [x, y] fixes a part's centre; H2D filaments take
extruder 'left'/'right'; filaments at different temperatures on one nozzle need allow_mix_temp: true;
give each filament a color (flush volumes depend on it); intent.supports "needed" slices without supports and
again with tree supports only if something would have nothing below it (supports_decision says which and why); "view": true (or make_view) writes a 3D view to open in a browser.
Polling costs tokens: when a run needs more than about 5 minutes, tell the user when it should finish and stop.
Units: mm, degrees C, %, mm/s, g, s."""

server = _Server("print3d-translator", instructions=INSTRUCTIONS)


@server.tool()
def study_format() -> dict:
    """The study format (printer, filaments, objects, part forms, orientation, intent, overrides, expect),
    conventions, an example, and status: OrcaSlicer and its profiles found here, build123d, versions."""
    return api.study_format()


@server.tool()
def validate_studies(studies: dict) -> dict:
    """Static checks of {name: study}: keys, files, filament and process profiles (resolved against
    OrcaSlicer's), settings, temperature compatibility. Per study: ok, errors (each says the fix),
    warnings, estimate {minutes, procs, mem_mb}. Use absolute file paths."""
    return api.validate(studies)


@server.tool()
def run_studies(studies: dict, wait_s: float = 45) -> dict:
    """Slices and checks each study. Per study: reliable, unreliable_because, warnings, files (gcode_3mf to
    print, gcode, profiles, log), estimate (time, filament g and m), objects (exact volumes, orientation,
    placement, checks before slicing), checks (archive, md5, printer id, settings round trip, bounds,
    layers, temperatures, tools, extrusion volume, footprint, unsupported extrusion), expect. Returns
    after at most wait_s
    seconds (<= 50); unfinished studies are in 'pending' with progress: collect them with study_status.
    Each study is summarized (failed checks in full, passed ones by name); 'result' is the full JSON's path."""
    return api.summarize_request(api.run(studies, wait=min(max(float(wait_s), 0.0), 50.0)))


@server.tool()
def study_status(request: str, wait_s: float = 45) -> dict:
    """The studies of a run_studies request (its 'request' path): finished ones with their results, the
    others in 'pending' with progress. Waits up to wait_s seconds (at most 50)."""
    return api.summarize_request(api.status(request, min(max(float(wait_s), 0.0), 50.0)))


@server.tool()
def get_inputs(study: dict, name: str = "study") -> dict:
    """Without slicing: the profiles chosen, every setting changed ([old, new]), the objects' orientation,
    placement and checks, the prime tower, and the OrcaSlicer command that run_studies would use."""
    return api.inputs(study, name)


@server.tool()
def run_benchmarks(names: list[str] | None = None, wait_s: float = 45) -> dict:
    """Benchmark studies with known answers (P2S cube, H2S copies with brim, H2D two filaments) as one
    request; returns within wait_s (<= 50): passed, failed, pending. Collect with bench_status."""
    return api.bench(names, wait=min(max(float(wait_s), 0.0), 50.0))


@server.tool()
def bench_status(bench: str, wait_s: float = 30) -> dict:
    """A run_benchmarks request (its 'bench' path): passed, failed and pending cases."""
    return api.bench_status(bench, min(max(float(wait_s), 0.0), 50.0))


@server.tool()
def get_file(path: str, max_mb: float = 20) -> dict:
    """A file written by run_studies (a path from its result, e.g. files.gcode_3mf) as base64."""
    return api.read_file(path, max_mb)


@server.tool()
def make_view(study: str) -> dict:
    """Writes a self-contained 3D view (<name>.view.html) of a finished study: the parts on the bed and the
    toolpaths, layer slider, colours by feature or filament. 'study' is the study folder or its result path
    (a study's 'result'). Returns the file's path: tell the user to open it in a browser."""
    return api.view(study)


@server.tool()
def cancel(request: str, names: list[str] | None = None) -> dict:
    """Stops a request's running studies (or the named ones)."""
    return api.cancel(request, names)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
