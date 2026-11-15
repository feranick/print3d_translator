"""hms.py — error codes in plain words, from the HMS description files the slicers keep on this Mac.
Version: 2026.11.14.1

Bambu Studio and OrcaSlicer download the printer's error descriptions (hms_<lang>_*.json) into their own
folders; they are read from there, never fetched from the network. Without them an error shows its code
and a link to search Bambu's wiki.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SEARCH = ["~/Library/Application Support/OrcaSlicer/hms", "~/Library/Application Support/BambuStudio/hms",
          "~/Library/Application Support/OrcaSlicer", "~/Library/Application Support/BambuStudio",
          "~/Software/orcaslicer-src/resources/hms", "~/.config/OrcaSlicer/hms", "~/.config/BambuStudio/hms"]
WIKI = "https://wiki.bambulab.com/en/search?q={code}"


def _walk(obj, out: dict):
    if isinstance(obj, dict):
        if "ecode" in obj and "intro" in obj:
            out.setdefault(str(obj["ecode"]).upper().replace("_", ""), str(obj["intro"]).strip())
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


@lru_cache(maxsize=1)
def table(lang: str = "en") -> dict:
    """{ecode (hex, no underscores): text} from every hms_<lang>*.json found (first one wins per code)."""
    out = {}
    for d in SEARCH:
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        for f in sorted(p.glob(f"hms*{lang}*.json")) + sorted(p.glob("hms*/*.json")):
            try:
                _walk(json.loads(f.read_text(errors="replace")), out)
            except (ValueError, OSError):
                continue
    return out


def text(code: str) -> str | None:
    return table().get(code.upper().replace("_", ""))


def link(code: str) -> str:
    return WIKI.format(code=code)
