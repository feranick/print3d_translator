"""gcode.py — an independent, streaming reader of OrcaSlicer / Bambu Lab G-code. It does not trust the
slicer's summary: it follows every move and returns the extrusion segments, layers, tools, objects,
features and temperatures, which check.py compares with the study.
Version: 2026.10.24.3

What it understands:
- G0/G1 and arcs G2/G3 (I/J centre offsets, Z helix, P full turns), G90/G91, M82/M83, G92, G28.
- T<n> tool changes (n < 255 is filament n+1; T255/T1000/T1001/T65279/T65535 are not filaments).
- M104/M109 (nozzle, with T<n>), M140/M190 (bed), M141/M191 (chamber); Bambu's filament change
  M620.10 A0/A1 ... P<print temperature> T<flush temperature> (the H2D sets the incoming filament's
  nozzle temperature this way, not with M104). An 'A' flag on M104/M109 is kept in 'flags'.
- Firmware conditional blocks M622 J<n> ... M623: their commands may or may not run. Temperatures
  and tool changes inside are recorded as conditional and not applied; moves inside make the
  position unknown afterwards.
- Bambu machine routines (G28.x, G29.x, G39.x, G130, G150.x, G380, G383.x, G389, G392, ...): the
  head moves where the firmware decides, so the position becomes unknown until the next absolute
  X/Y move. Extrusion from an unknown position is counted but not located.
- OrcaSlicer comments: HEADER_BLOCK, CONFIG_BLOCK, '; CHANGE_LAYER', '; Z_HEIGHT:', '; LAYER_HEIGHT:',
  '; FEATURE:', '; printing object <name> id:<id> copy <n>' / '; stop printing object ...', the footer
  '; filament used [mm] = ...'.

Regions: 'start' (machine start G-code, before the first '; CHANGE_LAYER'), 'print', and 'end' (from
the '; FEATURE: Custom' after the last layer, the machine end G-code).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

NOT_FILAMENT_TOOLS = {255, 1000, 1001, 65279, 65535}
_ROUTINE = re.compile(r"^(G28\.\d+|G29(\.\d+)?|G39(\.\d+)?|G130|G150(\.\d+)?|G380|G383(\.\d+)?|G389|G392)$")
_OBJ_START = re.compile(r"^; printing object (\S+) id:(\d+) copy (\d+)")
_OBJ_STOP = re.compile(r"^; stop printing object (\S+) id:(\d+) copy (\d+)")
_TIME = re.compile(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?")
REGIONS = ("start", "print", "end")


def hms_to_s(text: str) -> float | None:
    """'1h 2m 3s' / '14m 58s' / '3d 1h' -> seconds."""
    m = _TIME.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(((d * 24 + h) * 60 + mi) * 60 + s)


def _num_list(text: str) -> list[float]:
    out = []
    for t in re.split(r"[,;]\s*", text.strip()):
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def _params(tokens: list[str]) -> tuple[dict, set]:
    p, flags = {}, set()
    for t in tokens:
        k, v = t[0].upper(), t[1:]
        try:
            p[k] = float(v)
        except ValueError:
            m = re.match(r"^(-?\d*\.?\d+)([A-Za-z]+)$", v)   # 'S0A' -> S=0 and flag A
            if m:
                p[k] = float(m.group(1))
                flags.update(m.group(2).upper())
            else:
                flags.add(t.upper())
    return p, flags


@dataclass
class Parsed:
    """What the G-code contains. segments: float array (n, 6) x0 y0 x1 y1 z e; seg_info: int array
    (n, 5) region tool feature object layer (indexes into regions/features/objects; tool -1 unknown)."""
    header: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    footer: dict = field(default_factory=dict)
    lines: int = 0
    marks: dict = field(default_factory=dict)          # executable start, first layer, end block, exec end (line numbers)
    layers: list = field(default_factory=list)         # [{n, line, z_height, layer_height}]
    features: list = field(default_factory=list)
    objects: list = field(default_factory=list)        # object names, as in the markers
    segments: np.ndarray = None
    seg_info: np.ndarray = None
    e_net: dict = field(default_factory=dict)          # {tool: net E mm over the whole file}
    e_unlocated: dict = field(default_factory=dict)    # {tool: E mm extruded from an unknown position}
    unlocated_by_region: dict = field(default_factory=dict)   # {region: E mm from an unknown position}
    travel_bbox: dict = field(default_factory=dict)    # {region: [xmin, ymin, xmax, ymax, zmax]}
    temps: list = field(default_factory=list)          # [{line, kind, value, tool, wait, conditional, region, layer, flags}]
    tool_changes: list = field(default_factory=list)   # [{line, tool, region, layer, conditional}]
    relative_e: bool | None = None
    routines: dict = field(default_factory=dict)       # {command: count}
    warnings: list = field(default_factory=list)


def parse(text: str, arc_step_deg: float = 10.0) -> Parsed:
    P = Parsed()
    seg: list[tuple] = []
    info: list[tuple] = []
    feat_idx: dict[str, int] = {}
    obj_idx: dict[str, int] = {}

    def fidx(name):
        if name not in feat_idx:
            feat_idx[name] = len(P.features)
            P.features.append(name)
        return feat_idx[name]

    fidx("")                                            # 0: no feature
    lines = text.splitlines()
    P.lines = len(lines)

    # ---- pass 0: blocks, markers and region bounds
    in_header = in_config = False
    change_layers = []
    customs = []
    for i, raw in enumerate(lines):
        if raw.startswith(";"):
            s = raw[1:].strip()
            if s == "HEADER_BLOCK_START":
                in_header = True
                continue
            if s == "HEADER_BLOCK_END":
                in_header = False
                continue
            if s == "CONFIG_BLOCK_START":
                in_config = True
                continue
            if s == "CONFIG_BLOCK_END":
                in_config = False
                continue
            if in_header:
                _header_line(P.header, s)
            elif in_config:
                if " = " in s:
                    k, v = s.split(" = ", 1)
                    P.config.setdefault(k.strip(), v)
                elif s.endswith(" ="):
                    P.config.setdefault(s[:-2].strip(), "")
            elif s == "EXECUTABLE_BLOCK_START":
                P.marks["exec_start"] = i
            elif s == "EXECUTABLE_BLOCK_END":
                P.marks["exec_end"] = i
            elif s == "CHANGE_LAYER":
                change_layers.append(i)
            elif s.startswith("FEATURE: Custom"):
                customs.append(i)
            elif s.startswith("filament used [") and " = " in s:
                k, v = s.split(" = ", 1)
                P.footer[k.strip()] = _num_list(v)
            elif s.startswith("filament cost = "):
                P.footer["filament cost"] = _num_list(s.split(" = ", 1)[1])
    first = change_layers[0] if change_layers else None
    last = change_layers[-1] if change_layers else None
    end_block = next((c for c in customs if last is not None and c > last), None)
    P.marks.update({"first_layer": first, "end_block": end_block})
    if first is None:
        P.warnings.append("no '; CHANGE_LAYER' marker: not OrcaSlicer BBL G-code, or nothing was sliced")

    # ---- pass 1: the machine state
    x = y = z = None
    e_abs_pos = 0.0
    absolute = True
    rel_e = None
    tool = None
    feature = 0
    obj = -1
    layer = -1
    cond_depth = 0
    cond_moved = False
    region = 0
    pending_incoming: list[dict] = []
    tb = {r: [math.inf, math.inf, -math.inf, -math.inf, -math.inf] for r in REGIONS}

    for i, raw in enumerate(lines):
        if first is not None and i == first:
            region = 1
        if end_block is not None and i == end_block:
            region = 2
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            s = line[1:].strip()
            if s == "CHANGE_LAYER":
                layer += 1
                P.layers.append({"n": layer + 1, "line": i, "z_height": None, "layer_height": None})
            elif s.startswith("Z_HEIGHT:") and P.layers and P.layers[-1]["z_height"] is None:
                P.layers[-1]["z_height"] = float(s.split(":", 1)[1])
            elif s.startswith("LAYER_HEIGHT:") and P.layers and P.layers[-1]["layer_height"] is None:
                P.layers[-1]["layer_height"] = float(s.split(":", 1)[1])
            elif s.startswith("FEATURE:"):
                feature = fidx(s.split(":", 1)[1].strip())
            else:
                m = _OBJ_START.match(line)
                if m:
                    name = m.group(1)
                    if name not in obj_idx:
                        obj_idx[name] = len(P.objects)
                        P.objects.append(name)
                    obj = obj_idx[name]
                elif _OBJ_STOP.match(line):
                    obj = -1
            continue
        code = line.split(";", 1)[0].split()
        if not code:
            continue
        cmd = code[0].upper()
        conditional = cond_depth > 0
        if cmd == "M622":
            cond_depth += 1
            cond_moved = False
            continue
        if cmd == "M623":
            cond_depth = max(0, cond_depth - 1)
            if cond_depth == 0 and cond_moved:
                x = y = z = None
            continue
        if cmd in ("G0", "G1", "G2", "G3"):
            p, _ = _params(code[1:])
            if conditional:
                cond_moved = cond_moved or any(k in p for k in "XYZ")
            nx, ny, nz = x, y, z
            if absolute:
                nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
            else:
                nx = None if x is None else x + p.get("X", 0.0)
                ny = None if y is None else y + p.get("Y", 0.0)
                nz = None if z is None else z + p.get("Z", 0.0)
            de = 0.0
            if "E" in p:
                if rel_e is False:
                    de = p["E"] - e_abs_pos
                    e_abs_pos = p["E"]
                else:
                    de = p["E"]
            if de:
                key = tool if tool is not None else -1
                P.e_net[key] = P.e_net.get(key, 0.0) + de
            xy_move = ("X" in p or "Y" in p or cmd in ("G2", "G3"))
            if de > 0 and xy_move:
                if None in (x, y, nx, ny, nz):
                    key = tool if tool is not None else -1
                    P.e_unlocated[key] = P.e_unlocated.get(key, 0.0) + de
                    rk = REGIONS[region]
                    P.unlocated_by_region[rk] = P.unlocated_by_region.get(rk, 0.0) + de
                else:
                    pts = _arc(cmd, x, y, z, nx, ny, nz, p, arc_step_deg) if cmd in ("G2", "G3") else [(nx, ny, nz)]
                    px, py = x, y
                    n = len(pts)
                    for (qx, qy, qz) in pts:
                        seg.append((px, py, qx, qy, qz, de / n))
                        info.append((region, -1 if tool is None else tool, feature, obj, layer))
                        px, py = qx, qy
            elif not (de > 0) and None not in (nx, ny):
                b = tb[REGIONS[region]]
                b[0], b[1], b[2], b[3] = min(b[0], nx), min(b[1], ny), max(b[2], nx), max(b[3], ny)
                if nz is not None:
                    b[4] = max(b[4], nz)
            x, y, z = nx, ny, nz
            continue
        if cmd == "G90":
            absolute = True
        elif cmd == "G91":
            absolute = False
        elif cmd == "M83":
            rel_e = True
            P.relative_e = True if P.relative_e is None else P.relative_e
        elif cmd == "M82":
            rel_e = False
            P.relative_e = False
        elif cmd == "G92":
            p, _ = _params(code[1:])
            if "E" in p:
                e_abs_pos = p["E"]
            x, y, z = p.get("X", x), p.get("Y", y), p.get("Z", z)
        elif cmd == "G28":
            p, flags = _params(code[1:])
            axes = {k for k in p} | {f for f in flags if f in "XYZ"}
            if not axes or "X" in axes:
                x = None
            if not axes or "Y" in axes:
                y = None
            if not axes or "Z" in axes:
                z = None
        elif _ROUTINE.match(cmd):
            P.routines[cmd] = P.routines.get(cmd, 0) + 1
            x = y = z = None
        elif re.fullmatch(r"T\d+", cmd):
            n = int(cmd[1:])
            if n not in NOT_FILAMENT_TOOLS:
                P.tool_changes.append({"line": i, "tool": n, "region": REGIONS[region], "layer": layer + 1,
                                       "conditional": conditional})
                if not conditional:
                    tool = n
                for rec in pending_incoming:
                    rec["tool"] = n
                pending_incoming.clear()
        elif cmd == "M620.10":
            # Bambu filament change (H2D): A0 = outgoing filament, A1 = incoming; P = its print
            # temperature, T = flush temperature. The incoming tool is the next T<n>.
            p, _ = _params(code[1:])
            if "P" in p and "A" in p:
                rec = {"line": i, "kind": "nozzle_change", "value": p["P"], "flush": p.get("T"),
                       "tool": tool if p["A"] == 0 else None, "wait": False, "conditional": conditional,
                       "region": REGIONS[region], "layer": layer + 1, "flags": [f"A{int(p['A'])}"]}
                P.temps.append(rec)
                if p["A"] == 1:
                    pending_incoming.append(rec)
        elif cmd in ("M104", "M109", "M140", "M190", "M141", "M191"):
            p, flags = _params(code[1:])
            if "S" in p:
                kind = {"M104": "nozzle", "M109": "nozzle", "M140": "bed", "M190": "bed",
                        "M141": "chamber", "M191": "chamber"}[cmd]
                t = int(p["T"]) if "T" in p else tool
                P.temps.append({"line": i, "kind": kind, "value": p["S"], "tool": t if kind == "nozzle" else None,
                                "wait": cmd in ("M109", "M190", "M191"), "conditional": conditional,
                                "region": REGIONS[region], "layer": layer + 1, "flags": sorted(flags)})
    P.segments = np.array(seg, dtype=np.float64).reshape(-1, 6)
    P.seg_info = np.array(info, dtype=np.int64).reshape(-1, 5)
    P.travel_bbox = {r: (None if math.isinf(b[0]) else [round(v, 3) for v in b]) for r, b in tb.items()}
    return P


def _header_line(h: dict, s: str) -> None:
    if s.startswith("model printing time:"):
        for part in s.split(";"):
            k, _, v = part.partition(":")
            k = k.strip()
            if k == "model printing time":
                h["model_time_s"] = hms_to_s(v)
            elif k == "total estimated time":
                h["total_time_s"] = hms_to_s(v)
    elif s.startswith("total layer number:"):
        h["layers"] = int(float(s.split(":", 1)[1]))
    elif s.startswith("max_z_height:"):
        h["max_z"] = float(s.split(":", 1)[1])
    elif s.startswith("filament_density:"):
        h["filament_density"] = _num_list(s.split(":", 1)[1])
    elif s.startswith("filament_diameter:"):
        h["filament_diameter"] = _num_list(s.split(":", 1)[1])
    elif s.startswith("filament:"):
        h["filaments"] = [int(v) for v in _num_list(s.split(":", 1)[1])]
    elif s.startswith("model label id:"):
        h["label_ids"] = [int(v) for v in _num_list(s.split(":", 1)[1])]
    elif s.startswith("generated by"):
        h["generated_by"] = s


def _arc(cmd, x, y, z, nx, ny, nz, p, step_deg):
    """Points along a G2 (clockwise) / G3 (counter-clockwise) arc from (x, y, z), I/J relative centre;
    end == start or P >= 1 is a full circle (P turns)."""
    cx, cy = x + p.get("I", 0.0), y + p.get("J", 0.0)
    r = math.hypot(x - cx, y - cy)
    a0 = math.atan2(y - cy, x - cx)
    a1 = math.atan2(ny - cy, nx - cx)
    ccw = cmd == "G3"
    sweep = a1 - a0
    if ccw and sweep <= 0:
        sweep += 2 * math.pi
    if not ccw and sweep >= 0:
        sweep -= 2 * math.pi
    full = abs(nx - x) < 1e-9 and abs(ny - y) < 1e-9
    turns = int(p.get("P", 1 if full else 0))
    if full:
        sweep = (2 * math.pi if ccw else -2 * math.pi) * max(turns, 1)
    elif turns > 1:
        sweep += (2 * math.pi if ccw else -2 * math.pi) * (turns - 1)
    if r == 0:
        return [(nx, ny, nz)]
    n = max(1, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
    pts = []
    for k in range(1, n + 1):
        a = a0 + sweep * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a), z + (nz - z) * k / n))
    pts[-1] = (nx, ny, nz)
    return pts


# ---------------------------------------------------------------- summaries

def filament_area(diameter: float = 1.75) -> float:
    return math.pi * (diameter / 2) ** 2


def extrusion_table(P: Parsed, diameters: list[float] | None = None) -> dict:
    """E (mm of filament) and volume (mm3) per region, tool, feature and object."""
    out = {}
    if not len(P.segments):
        return out
    e = P.segments[:, 5]
    for (r, t, f, o), mask in _groups(P.seg_info[:, :4]):
        d = (diameters[t] if diameters and 0 <= t < len(diameters) else 1.75)
        key = (REGIONS[r], int(t), P.features[f] or "-", P.objects[o] if o >= 0 else "-")
        ee = float(e[mask].sum())
        out[key] = {"e_mm": ee, "volume_mm3": ee * filament_area(d)}
    return out


def _groups(arr: np.ndarray):
    keys, inv = np.unique(arr, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    for k, row in enumerate(keys):
        yield tuple(int(v) for v in row), inv == k


def print_z_levels(P: Parsed) -> np.ndarray:
    """Distinct Z (rounded to 1 um) of the print region's extrusion segments, sorted."""
    m = P.seg_info[:, 0] == 1
    return np.unique(np.round(P.segments[m, 4], 3))


def summary(P: Parsed, diameters: list[float] | None = None) -> dict:
    """A compact JSON-able summary (for results)."""
    zs = print_z_levels(P) if len(P.segments) else np.array([])
    by_tool = {}
    for (r, t, f, o), v in extrusion_table(P, diameters).items():
        d = by_tool.setdefault(str(t), {"e_mm": 0.0, "volume_mm3": 0.0, "by_feature": {}})
        d["e_mm"] += v["e_mm"]
        d["volume_mm3"] += v["volume_mm3"]
        d["by_feature"][f] = round(d["by_feature"].get(f, 0.0) + v["volume_mm3"], 2)
    for d in by_tool.values():
        d["e_mm"] = round(d["e_mm"], 2)
        d["volume_mm3"] = round(d["volume_mm3"], 2)
    return {"lines": P.lines, "layers": len(P.layers), "print_z_levels": int(len(zs)),
            "z_first": float(zs[0]) if len(zs) else None, "z_max": float(zs[-1]) if len(zs) else None,
            "relative_e": P.relative_e, "objects": P.objects, "tools_used": sorted(by_tool),
            "tool_changes": len([t for t in P.tool_changes if t["region"] == "print"]),
            "e_net_mm": {str(k): round(v, 2) for k, v in sorted(P.e_net.items())},
            "e_unlocated_mm": {str(k): round(v, 2) for k, v in sorted(P.e_unlocated.items())},
            "extrusion": by_tool, "travel_bbox": P.travel_bbox, "routines": P.routines,
            "header": P.header, "footer": P.footer}
