"""api.py — what the CLI and the MCP server call. JSON in, JSON out.
Version: 2026.10.24.10

    tool_status()                 versions; OrcaSlicer and its profiles found or not; build123d
    study_format()                the study format (with tool_status), for the caller to read
    validate(studies)             static checks (profiles resolved when OrcaSlicer is here); no slicing
    run(studies, wait)            start + status: results of the studies that finish within wait seconds
    start(studies)                a request folder P3DT_OUT/run_<date>_<time>_<id>/; runs in the background
    status(request, wait)         a request's results and the progress of the others
    cancel(request, names)        stops studies (a 'cancel' file that the running OrcaSlicer watches)
    inputs(study, name)           the flattened profiles, placement and OrcaSlicer command, without slicing
    bench(names, wait)            benchmark studies with known answers; bench_status(bench, wait)
    view(study)                   a self-contained 3D view (<name>.view.html) of a finished study
    read_file(path)               a written file as base64 (only under P3DT_OUT)

Local only: studies run in a background process (P3DT_WORKERS at once, default 2) that writes each
result as <name>/<name>.json; status reads files, so any process can collect a request. Nothing
is ever sent to a printer.
Environment: P3DT_ORCA, P3DT_ORCA_PROFILES, P3DT_OUT (~/p3dt_runs), P3DT_WORKERS (2),
P3DT_TIMEOUT (600 s per study), P3DT_MAX_TIMEOUT (cap for a study's run.timeout_s).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from . import format as F
from . import mesh as M
from . import orca
from . import profiles as P


def out_root() -> Path:
    return Path(os.environ.get("P3DT_OUT", "~/p3dt_runs")).expanduser()


def workers() -> int:
    return max(1, int(os.environ.get("P3DT_WORKERS", "2")))


def _vendor() -> P.Vendor | None:
    prof = P.find_profiles(orca.find_orca())
    try:
        return P.Vendor(prof) if prof else None
    except P.ProfileError:
        return None


def tool_status() -> dict:
    st = {"translator": __version__, "orca": orca.status(), "workers": workers(),
          "timeout_s": float(os.environ.get("P3DT_TIMEOUT", 600)), "out": str(out_root()),
          "executor": {"kind": "local", "caps": {"timeout_s": os.environ.get("P3DT_MAX_TIMEOUT")}},
          "printer": "none: files only, never sent"}
    try:
        import build123d
        st["build123d"] = getattr(build123d, "__version__", "installed")
    except ImportError:
        st["build123d"] = None
    try:
        import build123d_translator
        st["b3dt"] = build123d_translator.__version__
    except ImportError:
        st["b3dt"] = None
    return st


def study_format() -> dict:
    return F.study_format(tool_status())


def validate(studies: dict, base_dir: str | None = None) -> dict:
    return F.validate(studies, base_dir, _vendor())


# ---------------------------------------------------------------- requests

def _request_dir(prefix: str = "run") -> Path:
    d = out_root() / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True)
    return d


def start(studies: dict, base_dir: str | None = None, prefix: str = "run") -> Path:
    """Writes the request folder and starts the background worker; returns the folder."""
    if not isinstance(studies, dict) or not studies:
        raise ValueError("give {name: study}")
    d = _request_dir(prefix)
    (d / "studies.json").write_text(json.dumps(studies, indent=1))
    req = {"created": time.time(), "translator": __version__, "names": list(studies),
           "base_dir": str(Path(base_dir).resolve()) if base_dir else None}
    log = open(d / "worker.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "print3d_translator.cli", "run-request", str(d)],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ})
    req["pid"] = p.pid
    (d / "request.json").write_text(json.dumps(req, indent=1))
    return d


def run_request(folder: str | Path) -> None:
    """The background worker: runs every study of a request (P3DT_WORKERS at once)."""
    from concurrent.futures import ThreadPoolExecutor
    from . import runner
    d = Path(folder)
    studies = json.loads((d / "studies.json").read_text())
    req = json.loads((d / "request.json").read_text()) if (d / "request.json").exists() else {}
    base = req.get("base_dir")

    def one(name):
        sd = d / name
        if (sd / f"{name}.json").exists():
            return
        runner.run_study(name, studies[name], sd, base)

    with ThreadPoolExecutor(max_workers=workers()) as ex:
        list(ex.map(one, list(studies)))


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _collect(d: Path) -> dict:
    req = json.loads((d / "request.json").read_text()) if (d / "request.json").exists() else {}
    names = req.get("names") or json.loads((d / "studies.json").read_text()).keys()
    out = {"request": str(d), "translator": __version__, "studies": {}, "pending": [], "progress": {}}
    worker_alive = _alive(req.get("pid"))
    for n in names:
        rf = d / n / f"{n}.json"
        if rf.exists():
            try:
                out["studies"][n] = json.loads(rf.read_text())
                continue
            except ValueError:
                pass
        sf = d / n / "state.json"
        st = json.loads(sf.read_text()) if sf.exists() else {"state": "queued"}
        if not worker_alive:
            out["studies"][n] = {"ok": False, "reliable": False, "why": "the worker stopped before this study "
                                 f"finished (see {d / 'worker.log'})", "errors": ["worker stopped"]}
            continue
        st["elapsed_s"] = round(time.time() - st.get("time", time.time()), 1)
        out["pending"].append(n)
        out["progress"][n] = st
    out["done"] = not out["pending"]
    out["next"] = ("done: check each study's 'reliable', 'checks' and 'warnings'" if out["done"] else
                   f"{len(out['pending'])} still slicing (usually seconds to a few minutes): call study_status "
                   f"with this request")
    return out


def summarize(res: dict, path: str | None = None) -> dict:
    """A study's result in a few kB, for LLM tools and the terminal: the verdict, warnings, files,
    estimate, profiles, objects, the failed checks in full and the passed ones by name. The full result
    stays in <study>/<study>.json ('result')."""
    checks = res.get("checks") or {}
    failed = {k: {kk: v.get(kk) for kk in ("hard", "why", "got", "expected", "problems") if v.get(kk) is not None}
              for k, v in checks.items() if not v.get("passed")}
    for v in failed.values():
        if len(json.dumps(v.get("got"), default=str)) > 1500:
            v["got"] = "(large: see the full result)"
    objs = {}
    for n, o in (res.get("objects") or {}).items():
        objs[n] = {k: o.get(k) for k in ("volume_mm3", "volume_from", "size_mm", "orient", "filament", "copies", "centers")}
        bad = {k: c.get("got") for k, c in (o.get("checks") or {}).items() if not c.get("passed")}
        if bad:
            objs[n]["failed_checks"] = bad
    prof = res.get("profiles") or {}
    out = {"ok": res.get("ok"), "reliable": res.get("reliable"), "unreliable_because": res.get("unreliable_because"),
           "why": res.get("why"), "errors": res.get("errors"), "warnings": res.get("warnings"),
           "result": path, "files": res.get("files"), "estimate": res.get("estimate"),
           "printer": res.get("printer"),
           "profiles": {k: prof.get(k) for k in ("machine", "process", "filaments", "filament_map", "set", "orca_version")},
           "objects": objs, "placement": res.get("placement"), "checks_failed": failed,
           "checks_passed": sorted(k for k, v in checks.items() if v.get("passed")),
           "expect": res.get("expect"), "slicer": res.get("slicer"), "seconds": res.get("seconds")}
    return {k: v for k, v in out.items() if v not in (None, [], {})} | {"ok": res.get("ok"), "reliable": res.get("reliable")}


def summarize_request(st: dict) -> dict:
    """status/run output with each finished study summarized."""
    if "studies" not in st:
        return st
    d = Path(st.get("request", "."))
    return {**st, "studies": {n: summarize(r, str(d / n / f"{n}.json")) for n, r in st["studies"].items()}}


def status(request: str, wait: float = 0.0) -> dict:
    d = Path(request).expanduser()
    if not (d / "studies.json").exists():
        return {"error": f"no request at {d}"}
    t0 = time.monotonic()
    while True:
        out = _collect(d)
        if out["done"] or time.monotonic() - t0 >= wait:
            return out
        time.sleep(0.5)


def run(studies: dict, wait: float | None = None, base_dir: str | None = None) -> dict:
    """Validates, starts and waits up to `wait` seconds (default: until done)."""
    v = validate(studies, base_dir)
    bad = {n: r for n, r in v.items() if not r["ok"]}
    if bad:
        return {"error": "fix the studies first (nothing was run)", "validation": v}
    d = start(studies, base_dir)
    return status(str(d), 1e9 if wait is None else wait)


def cancel(request: str, names: list[str] | None = None) -> dict:
    d = Path(request).expanduser()
    out = _collect(d) if (d / "studies.json").exists() else {"error": f"no request at {d}"}
    if "error" in out:
        return out
    for n in names or out["pending"]:
        if n in out["pending"]:
            (d / n).mkdir(exist_ok=True)
            (d / n / "cancel").write_text("cancel\n")
    return {"request": str(d), "cancelled": [n for n in (names or out["pending"]) if n in out["pending"]]}


def inputs(study: dict, name: str = "study", base_dir: str | None = None) -> dict:
    """Profiles, placement and the OrcaSlicer command of one study, written but not run."""
    from . import runner
    d = out_root() / "inputs" / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    try:
        ctx = runner.prepare(name, study, d, base_dir)
    except (P.ProfileError, ValueError) as e:
        return {"ok": False, "errors": [str(e)]}
    r = ctx["resolved"]
    return {"ok": True, "folder": str(d), "profiles": {**r["names"], "set": r["set"], "files": ctx["files"]},
            "objects": ctx["objects"], "tower": ctx["tower"], "filament_map": ctx["fmap"],
            "command": ctx["argv"], "assemble_list": str(d / "assemble.json"), "warnings": ctx["warnings"]}


def view(study: str) -> dict:
    """Writes <study>/<name>.view.html for a finished study (its folder, or its <name>.json): the parts on the
    bed and the toolpaths, self-contained. Returns {view: path, bytes} or {error}."""
    from . import viewer
    p = Path(study).expanduser()
    if p.suffix == ".json":
        p = p.parent
    if not (p / f"{p.name}.json").exists():
        return {"error": f"no study result at {p} (give a study folder from a run's 'result' path)"}
    try:
        out = viewer.for_study(p)
    except ValueError as e:
        return {"error": str(e)}
    return {"view": out, "bytes": Path(out).stat().st_size,
            "open": "open it in a browser (Safari, Chrome, Firefox); no internet needed"}


def read_file(path: str, max_mb: float = 20) -> dict:
    p = Path(path).expanduser().resolve()
    root = out_root().resolve()
    if root not in p.parents:
        return {"error": f"only files under {root} can be read"}
    if not p.is_file():
        return {"error": f"no file {p}"}
    if p.stat().st_size > max_mb * 1e6:
        return {"error": f"{p.stat().st_size / 1e6:.1f} MB is more than max_mb"}
    return {"name": p.name, "bytes": p.stat().st_size, "base64": base64.b64encode(p.read_bytes()).decode()}


# ---------------------------------------------------------------- benchmarks

def bench_studies() -> dict:
    """Studies with known answers, on generated boxes (exact volumes)."""
    parts = out_root() / "bench_parts"
    parts.mkdir(parents=True, exist_ok=True)
    cube = M.write_stl(parts / "cube20.stl", M.box(20, 20, 20))
    slab = M.write_stl(parts / "slab40x30x6.stl", M.box(40, 30, 6))
    small = M.write_stl(parts / "cube15x15x10.stl", M.box(15, 15, 10))
    return {
        "p2s_cube": {"printer": {"model": "P2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PLA Basic"}],
                     "objects": [{"name": "cube", "part": {"stl": str(cube)}}],
                     "expect": [{"quantity": "gcode.layers", "value": 100, "tol": 0},
                                {"quantity": "objects.cube.volume_mm3", "value": 8000, "tol": 1e-6}]},
        "h2s_fine_copies": {"printer": {"model": "H2S", "nozzle": 0.4}, "filaments": [{"profile": "Bambu PETG HF"}],
                            "objects": [{"name": "slab", "part": {"stl": str(slab)}, "copies": 3}],
                            "intent": {"preset": "fine", "brim": 3}},
        "h2d_two_filaments": {"printer": {"model": "H2D", "nozzle": 0.4},
                              "filaments": [{"profile": "Bambu PLA Basic", "color": "#FF0000", "extruder": "left"},
                                            {"profile": "Bambu PETG HF", "color": "#0000FF", "extruder": "right"}],
                              "objects": [{"name": "a", "part": {"stl": str(small)}, "filament": 1},
                                          {"name": "b", "part": {"stl": str(small)}, "filament": 2}],
                              "expect": [{"quantity": "gcode.layers", "value": 50, "tol": 0}]},
    }


def _bench_summary(st: dict) -> dict:
    cases, passed, failed = {}, [], []
    for n, r in st.get("studies", {}).items():
        ok = bool(r.get("ok") and r.get("reliable"))
        cases[n] = {"passed": ok, "why": r.get("why") or r.get("unreliable_because"),
                    "expect": r.get("expect"), "estimate": r.get("estimate")}
        (passed if ok else failed).append(n)
    return {"bench": st.get("request"), "passed": passed, "failed": failed, "pending": st.get("pending", []),
            "cases": cases, "next": st.get("next")}


def bench(names: list[str] | None = None, wait: float | None = None) -> dict:
    allb = bench_studies()
    pick = {n: allb[n] for n in (names or allb) if n in allb}
    if not pick:
        return {"error": f"no such benchmarks; choose from {sorted(allb)}"}
    d = start(pick, prefix="bench")
    return _bench_summary(status(str(d), 1e9 if wait is None else wait))


def bench_status(bench_dir: str, wait: float = 0.0) -> dict:
    return _bench_summary(status(bench_dir, wait))
