"""orca.py — the OrcaSlicer command line: find it, tell its version, build the command, run it in its
own process group with a timeout and a cancel flag, and decode its exit status.
Version: 2026.10.24.9

The translator never asks OrcaSlicer to do anything but slice files: no printer, network or cloud
options are ever passed. Geometry comes in through an assemble list (--load-assemble-list): one STL
per object, already oriented and placed in bed coordinates, need_arrange false, so OrcaSlicer neither
orients nor arranges (both are heuristics).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from functools import lru_cache
from pathlib import Path

PINNED = "2.4.2"

# OrcaSlicer 2.4.2 src/libslic3r/Utils.hpp; the process exit status is the code mod 256
EXIT_CODES = {
    0: "SUCCESS", -1: "ENVIRONMENT_ERROR", -2: "INVALID_PARAMS", -3: "FILE_NOTFOUND",
    -4: "FILELIST_INVALID_ORDER", -5: "CONFIG_FILE_ERROR", -6: "DATA_FILE_ERROR", -7: "INVALID_PRINTER_TECH",
    -8: "UNSUPPORTED_OPERATION", -9: "COPY_OBJECTS_ERROR", -10: "SCALE_TO_FIT_ERROR", -11: "EXPORT_STL_ERROR",
    -12: "EXPORT_OBJ_ERROR", -13: "EXPORT_3MF_ERROR", -14: "OUT_OF_MEMORY",
    -15: "3MF_NOT_SUPPORT_MACHINE_CHANGE", -16: "3MF_NEW_MACHINE_NOT_SUPPORTED", -17: "PROCESS_NOT_COMPATIBLE",
    -18: "INVALID_VALUES_IN_3MF", -19: "POSTPROCESS_NOT_SUPPORTED", -20: "PRINTABLE_SIZE_REDUCED",
    -21: "OBJECT_ARRANGE_FAILED", -22: "OBJECT_ORIENT_FAILED", -23: "MODIFIED_PARAMS_TO_PRINTER",
    -24: "FILE_VERSION_NOT_SUPPORTED", -50: "NO_SUITABLE_OBJECTS", -51: "VALIDATE_ERROR",
    -52: "OBJECTS_PARTLY_INSIDE", -53: "EXPORT_CACHE_DIRECTORY_CREATE_FAILED", -54: "EXPORT_CACHE_WRITE_FAILED",
    -55: "IMPORT_CACHE_NOT_FOUND", -56: "IMPORT_CACHE_DATA_CAN_NOT_USE", -57: "IMPORT_CACHE_LOAD_FAILED",
    -58: "SLICING_TIME_EXCEEDS_LIMIT", -59: "TRIANGLE_COUNT_EXCEEDS_LIMIT", -60: "NO_SUITABLE_OBJECTS_AFTER_SKIP",
    -61: "FILAMENT_NOT_MATCH_BED_TYPE", -62: "FILAMENTS_DIFFERENT_TEMP", -63: "OBJECT_COLLISION_IN_SEQ_PRINT",
    -64: "OBJECT_COLLISION_IN_LAYER_PRINT", -65: "SPIRAL_MODE_INVALID_PARAMS", -66: "FILAMENT_CAN_NOT_MAP",
    -67: "ONLY_ONE_TPU_SUPPORTED", -68: "FILAMENTS_NOT_SUPPORTED_BY_EXTRUDER", -100: "SLICING_ERROR",
    -101: "GCODE_PATH_CONFLICTS", -102: "GCODE_PATH_IN_UNPRINTABLE_AREA",
}
# what to change, for the codes a study can cause
EXIT_FIXES = {
    -5: "a settings file was rejected: see orca.log (profile 'from'/'type', or a value OrcaSlicer cannot parse)",
    -17: "the process profile is not compatible with the machine: choose intent.process for this printer",
    -50: "no object is on the plate: check the parts' placement",
    -51: "OrcaSlicer rejected the settings: see orca.log for the key",
    -52: "an object is partly outside the printable area: rotate or scale it, or use a larger printer",
    -58: "slicing took too long: simplify the part or raise run.timeout_s",
    -59: "too many triangles: simplify the mesh",
    -61: "a filament does not suit the plate: choose another printer.plate",
    -62: "the filaments' temperatures are too far apart to print together",
    -64: "objects collide: give them more room",
    -66: "a filament cannot be mapped to an extruder: set filaments[].extruder",
    -68: "a filament is not supported by its extruder: assign it to the other extruder",
    -101: "G-code paths conflict (multi-extruder): move the objects apart",
    -102: "G-code in an area one extruder cannot reach: move the objects, or assign the filament to the other extruder",
}

_MAC_APPS = ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
             "~/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"]
_LINUX = ["~/opt/orca/squashfs-root/AppRun", "~/OrcaSlicer/squashfs-root/AppRun", "/opt/OrcaSlicer/AppRun",
          "/usr/bin/orca-slicer", "/usr/local/bin/orca-slicer"]


def find_orca() -> str | None:
    """P3DT_ORCA, then orca-slicer / OrcaSlicer on PATH, then the usual install folders."""
    env = os.environ.get("P3DT_ORCA")
    if env:
        p = Path(env).expanduser()
        return str(p) if p.exists() else None
    for name in ("orca-slicer", "OrcaSlicer"):
        w = shutil.which(name)
        if w:
            return w
    for c in _MAC_APPS + _LINUX:
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


@lru_cache(maxsize=8)
def version(exe: str) -> str | None:
    """'2.4.2' from the first line of --help ('OrcaSlicer-2.4.2:'), or None."""
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"OrcaSlicer[- ]v?(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*)", out.stdout + out.stderr)
    return m.group(1) if m else None


def status() -> dict:
    """{exe, version, pinned, matches, profiles} or {error} when OrcaSlicer is not found."""
    from . import profiles
    exe = find_orca()
    if not exe:
        return {"error": "OrcaSlicer not found: install OrcaSlicer 2.4.2, or set P3DT_ORCA to its executable",
                "pinned": PINNED}
    v = version(exe)
    prof = profiles.find_profiles(exe)
    return {"exe": exe, "version": v, "pinned": PINNED, "matches": v == PINNED,
            "profiles": str(prof) if prof else None}


def decode(returncode: int) -> dict:
    """Exit status -> {code, name, fix}. OrcaSlicer exits with a negative code (mod 256); a negative
    Python returncode is a signal (a crash)."""
    if returncode < 0:
        sig = -returncode
        try:
            sname = signal.Signals(sig).name
        except ValueError:
            sname = str(sig)
        return {"code": None, "name": f"CRASHED ({sname})", "fix": "OrcaSlicer crashed: see orca.log"}
    code = returncode - 256 if returncode > 127 else returncode
    return {"code": code, "name": EXIT_CODES.get(code, f"UNKNOWN_{code}"), "fix": EXIT_FIXES.get(code)}


def assemble_list(objects: list[dict], plate_name: str = "") -> dict:
    """The --load-assemble-list JSON for one plate. objects: [{path (STL in bed coordinates),
    filament (1-based)}]. Positions are 0 (the STL is already placed) and the plate is not arranged."""
    return {"plates": [{"plate_name": plate_name, "need_arrange": False,
                        "objects": [{"path": str(o["path"]), "count": 1, "filaments": [int(o["filament"])],
                                     "pos_x": [0.0], "pos_y": [0.0], "pos_z": [0.0]} for o in objects]}]}


def command(exe: str, folder: Path, machine: str, process: str, filaments: list[str], assemble: str,
            bed_type: str, filament_map: list[int] | None = None, out_name: str = "plate.gcode.3mf",
            min_save: bool = False, debug: int = 3) -> list[str]:
    """The argument list. Boolean flags take no value (a value after them would be read as an input
    file); --export-3mf takes a name relative to --outputdir."""
    argv = [exe,
            "--load-settings", f"{machine};{process}",
            "--load-filaments", ";".join(filaments),
            "--load-assemble-list", assemble,
            "--curr-bed-type", bed_type,
            "--slice", "0",
            "--outputdir", str(folder),
            "--export-3mf", out_name,
            "--debug", str(debug),
            "--logfile", str(Path(folder) / "orca.log")]
    if filament_map:
        argv += ["--filament-map-mode", "Manual", "--filament-map", ",".join(str(i) for i in filament_map)]
    if min_save:
        argv.append("--min-save")
    return argv


def run(argv: list[str], cwd: str | Path, timeout: float = 600, cancel_file: str | Path | None = None) -> dict:
    """Runs OrcaSlicer in its own process group; kills the whole group on timeout or when cancel_file
    appears. Returns {returncode, code, name, fix, ok, seconds, timed_out, cancelled, stdout, stderr}
    (stdout/stderr: the last 4000 characters; also written to orca.stdout / orca.stderr)."""
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "command.json").write_text(json.dumps(argv, indent=1) + "\n")
    t0 = time.monotonic()
    timed_out = cancelled = False
    with open(cwd / "orca.stdout", "w") as so, open(cwd / "orca.stderr", "w") as se:
        p = subprocess.Popen(argv, cwd=cwd, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        while True:
            try:
                p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - t0 > timeout:
                timed_out = True
            elif cancel_file and Path(cancel_file).exists():
                cancelled = True
            if timed_out or cancelled:
                _kill_group(p)
                break
    rc = p.returncode
    out = {"returncode": rc, **decode(rc if rc is not None else -9), "seconds": round(time.monotonic() - t0, 2),
           "timed_out": timed_out, "cancelled": cancelled,
           "stdout": (cwd / "orca.stdout").read_text(errors="replace")[-4000:],
           "stderr": (cwd / "orca.stderr").read_text(errors="replace")[-4000:]}
    out["ok"] = rc == 0 and not timed_out and not cancelled
    rj = cwd / "result.json"                       # written by Linux builds only
    if rj.is_file():
        try:
            out["result_json"] = json.loads(rj.read_text())
        except ValueError:
            pass
    return out


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
