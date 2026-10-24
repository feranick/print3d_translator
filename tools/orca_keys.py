#!/usr/bin/env python3
"""orca_keys.py — extracts OrcaSlicer's configuration keys (name, type, enum values, min, max)
from its source file src/libslic3r/PrintConfig.cpp into print3d_translator/data/orca_keys.json.
Version: 2026.10.24.1

Only the interface is kept (key names, value types, accepted enum strings, limits); no code or
text from OrcaSlicer. Run it once per pinned OrcaSlicer version:

    python3 tools/orca_keys.py /path/to/OrcaSlicer/src/libslic3r/PrintConfig.cpp 2.4.2
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ADD = re.compile(r'def\s*=\s*this->add(_nullable)?\("([A-Za-z0-9_]+)",\s*co([A-Za-z]+)\)')
KEYS_MAP = re.compile(r'static\s+(?:const\s+)?t_config_enum_values\s+s_keys_map_([A-Za-z0-9_]+)\s*=?\s*\{(.*?)\};', re.S)
MAP_ITEM = re.compile(r'\{\s*"([^"]+)"\s*,')
ENUM_REF = re.compile(r'ConfigOptionEnum<([A-Za-z0-9_]+)>::get_enum_values')
ENUM_VALUE = re.compile(r'def->enum_values\.(?:push_back|emplace_back)\("([^"]*)"\)')
MINMAX = re.compile(r'def->(min|max)\s*=\s*(-?[0-9.]+)\s*;')


def extract(text: str) -> dict:
    maps = {name: MAP_ITEM.findall(body) for name, body in KEYS_MAP.findall(text)}
    keys = {}
    adds = list(ADD.finditer(text))
    for i, m in enumerate(adds):
        block = text[m.end(): adds[i + 1].start() if i + 1 < len(adds) else len(text)]
        nullable, key, typ = bool(m.group(1)), m.group(2), m.group(3)
        rec = {"type": typ}
        if nullable:
            rec["nullable"] = True
        if typ in ("Enum", "Enums"):
            ref = ENUM_REF.search(block)
            values = maps.get(ref.group(1)) if ref else None
            rec["enum"] = values or ENUM_VALUE.findall(block)
        for which, val in MINMAX.findall(block):
            rec[which] = float(val)
        keys.setdefault(key, rec)
    return keys


def main() -> None:
    src, version = Path(sys.argv[1]), sys.argv[2]
    keys = extract(src.read_text(encoding="utf-8", errors="replace"))
    out = Path(__file__).resolve().parents[1] / "print3d_translator" / "data" / "orca_keys.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"orca_version": version, "source": "src/libslic3r/PrintConfig.cpp",
                               "keys": dict(sorted(keys.items()))}, indent=0, sort_keys=False) + "\n")
    print(f"{len(keys)} keys -> {out}")


if __name__ == "__main__":
    main()
