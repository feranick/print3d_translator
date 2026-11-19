"""settings.py — p3dt's settings from a config file, for MCP clients that cannot set environment variables
(the Gemini Mac app's Local MCP servers have no field for them).
Version: 2026.11.18.1

File: $P3DT_CONFIG, else ~/.config/p3dt/config.json, a JSON object such as
    {"P3DT_ORCA_PROFILES": "/Users/me/Software/orcaslicer-src/resources/profiles"}
Keys: the environment variables below (or their short names: "orca", "orca_profiles", "out", "workers",
"timeout", "max_timeout", "in_process"). A variable set in the environment wins over the file.
'p3dt config KEY=VALUE ...' writes the file.

  P3DT_ORCA           OrcaSlicer's executable (default: /Applications/OrcaSlicer.app/..., then PATH)
  P3DT_ORCA_PROFILES  OrcaSlicer's JSON profiles (needed for OrcaSlicer 2.5+, which ships them compiled)
  P3DT_OUT            where runs are written (~/p3dt_runs)
  P3DT_WORKERS        studies sliced at once (2)
  P3DT_TIMEOUT        OrcaSlicer's time limit per study, s (600)
  P3DT_MAX_TIMEOUT    the largest run.timeout_s a study may ask for
  P3DT_IN_PROCESS     1: run the studies in the server's own process instead of a background worker process
                      (automatic where starting processes is not permitted)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

KEYS = ("P3DT_ORCA", "P3DT_ORCA_PROFILES", "P3DT_OUT", "P3DT_WORKERS", "P3DT_TIMEOUT", "P3DT_MAX_TIMEOUT",
        "P3DT_IN_PROCESS")
SHORT = {k[5:].lower(): k for k in KEYS}
LOADED: dict = {"file": None, "applied": [], "error": None}


def path() -> Path:
    return Path(os.environ.get("P3DT_CONFIG", "~/.config/p3dt/config.json")).expanduser()


def read() -> dict:
    p = path()
    if not p.is_file():
        return {}
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{p}: a JSON object of settings, e.g. {{\"P3DT_ORCA_PROFILES\": \"/path\"}}")
    out = {}
    for k, v in data.items():
        key = SHORT.get(str(k).lower(), str(k).upper())
        if key not in KEYS:
            raise ValueError(f"{p}: unknown setting {k!r}; known: {', '.join(KEYS)}")
        out[key] = str(v)
    return out


def load() -> dict:
    """Puts the file's settings into os.environ where the environment does not set them (cheap: one small
    file). Never raises: a broken file is reported in LOADED['error'] (shown by status)."""
    try:
        data = read()
    except (OSError, ValueError) as e:
        LOADED.update(file=str(path()), applied=[], error=str(e))
        return LOADED
    applied = []
    for k, v in data.items():
        if k not in os.environ:
            os.environ[k] = os.path.expanduser(v) if k in ("P3DT_ORCA", "P3DT_ORCA_PROFILES", "P3DT_OUT") else v
            applied.append(k)
    LOADED.update(file=str(path()) if data else None, applied=applied, error=None)
    return LOADED


def write(pairs: dict) -> Path:
    """Adds / replaces settings in the file (an empty value removes one)."""
    p = path()
    data = read() if p.is_file() else {}
    for k, v in pairs.items():
        key = SHORT.get(str(k).lower(), str(k).upper())
        if key not in KEYS:
            raise ValueError(f"unknown setting {k!r}; known: {', '.join(KEYS)}")
        if v in ("", None):
            data.pop(key, None)
        else:
            data[key] = str(v)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1) + "\n")
    return p
