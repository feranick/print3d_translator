"""cli.py — the command line. Every command prints JSON; '-' reads the studies from stdin.
Version: 2026.10.24.7

    p3dt status                         versions; OrcaSlicer and its profiles found or not
    p3dt format                         the study format
    p3dt validate studies.json          static checks (profiles resolved when OrcaSlicer is here)
    p3dt run studies.json [--wait S] [--full]   slice and check (a summary per study; --full: everything)
    p3dt status REQUEST [--wait S]      a request's results and progress
    p3dt inputs studies.json NAME       profiles, placement and the OrcaSlicer command, without slicing
    p3dt bench [NAMES...]               the benchmarks (generated parts, known answers)
    p3dt bench-status BENCH             a bench request
    p3dt cancel REQUEST [NAMES...]      stop studies
    p3dt run-request FOLDER             (internal) the background worker of a request

Relative part paths are relative to the studies file's folder. Nothing is ever sent to a printer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import api


def _load(path: str) -> tuple[dict, str | None]:
    if path == "-":
        return json.load(sys.stdin), None
    p = Path(path).expanduser()
    return json.loads(p.read_text()), str(p.resolve().parent)


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p3dt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("request", nargs="?")
    s.add_argument("--wait", type=float, default=0.0)
    s.add_argument("--full", action="store_true", help="every check in full (default: a summary)")
    sub.add_parser("format")
    s = sub.add_parser("validate")
    s.add_argument("studies")
    s = sub.add_parser("run")
    s.add_argument("studies")
    s.add_argument("--wait", type=float, default=None)
    s.add_argument("--full", action="store_true", help="every check in full (default: a summary)")
    s = sub.add_parser("inputs")
    s.add_argument("studies")
    s.add_argument("name")
    s = sub.add_parser("bench")
    s.add_argument("names", nargs="*")
    s.add_argument("--wait", type=float, default=None)
    s = sub.add_parser("bench-status")
    s.add_argument("bench")
    s.add_argument("--wait", type=float, default=0.0)
    s = sub.add_parser("cancel")
    s.add_argument("request")
    s.add_argument("names", nargs="*")
    s = sub.add_parser("run-request")
    s.add_argument("folder")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        if a.request:
            st = api.status(a.request, a.wait)
            _print(st if a.full else api.summarize_request(st))
        else:
            _print(api.tool_status())
    elif a.cmd == "format":
        _print(api.study_format())
    elif a.cmd == "validate":
        studies, base = _load(a.studies)
        res = api.validate(studies, base)
        _print(res)
        return 0 if all(v["ok"] for v in res.values()) else 1
    elif a.cmd == "run":
        studies, base = _load(a.studies)
        res = api.run(studies, a.wait, base)
        _print(res if a.full or "error" in res else api.summarize_request(res))
        if "error" in res:
            return 1
        return 0 if res.get("done") and all(r.get("reliable") for r in res["studies"].values()) else 2
    elif a.cmd == "inputs":
        studies, base = _load(a.studies)
        if a.name not in studies:
            _print({"error": f"no study '{a.name}'; the file has {sorted(studies)}"})
            return 1
        _print(api.inputs(studies[a.name], a.name, base))
    elif a.cmd == "bench":
        _print(api.bench(a.names or None, a.wait))
    elif a.cmd == "bench-status":
        _print(api.bench_status(a.bench, a.wait))
    elif a.cmd == "cancel":
        _print(api.cancel(a.request, a.names or None))
    elif a.cmd == "run-request":
        api.run_request(a.folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
