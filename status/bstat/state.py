"""state.py — the printer's reports, merged and turned into a plain status.
Version: 2026.11.14.1

The printer publishes JSON on device/<serial>/report. A message carries {"print": {...}} with only the
fields that changed (P1/P2 series), or the full state after a 'pushall' request. merge() keeps the latest
value of every field; normalize() turns the merged report into the status the front-ends show.

Field names follow the community documentation of the protocol (OpenBambuAPI, ha-bambulab). Newer
firmware (H2 series, possibly the P2S) also reports temperatures in a 'device' block with packed values
(target << 16 | current): both forms are read. Unknown or missing fields give None, never an error.
"""
from __future__ import annotations

import copy
import time

STATES = {"IDLE": "idle", "PREPARE": "preparing", "RUNNING": "printing", "PAUSE": "paused",
          "FINISH": "finished", "FAILED": "failed", "SLICING": "slicing", "INIT": "starting"}
SPEEDS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}
NO_TRAY, EXTERNAL = 255, 254


def merge(base: dict, update: dict) -> dict:
    """Deep merge (in place): dicts merge key by key; lists of AMS units / trays merge by 'id';
    anything else is replaced."""
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list) and v and all(isinstance(x, dict) and "id" in x for x in v) \
                and all(isinstance(x, dict) and "id" in x for x in base[k]):
            by_id = {str(x["id"]): x for x in base[k]}
            for x in v:
                if str(x["id"]) in by_id:
                    merge(by_id[str(x["id"])], x)
                else:
                    base[k].append(copy.deepcopy(x))
        else:
            base[k] = copy.deepcopy(v)
    return base


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _packed(v):
    """(current, target) from a packed temperature: target << 16 | current."""
    n = _int(v)
    if n is None:
        return None, None
    return float(n & 0xFFFF), float(n >> 16)


def _fan(v):
    """Fan speeds come as '0'..'15' (sixteenths of full speed) on most firmware; as % on some."""
    n = _num(v)
    if n is None:
        return None
    return round(n / 15 * 100) if n <= 15 else round(min(n, 100))


def _color(c):
    if not c or not isinstance(c, str):
        return None
    c = c.strip().lstrip("#")
    return "#" + c[:6].upper() if len(c) >= 6 else None


def hms_code(attr, code) -> str:
    """The HMS code as Bambu writes it: XXXX_XXXX_XXXX_XXXX."""
    a, c = int(attr) & 0xFFFFFFFF, int(code) & 0xFFFFFFFF
    return f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"


def _tray(t: dict, unit_id, active_index) -> dict:
    slot = _int(t.get("id"))
    remain = _int(t.get("remain"))
    return {"slot": slot, "label": f"{chr(ord('A') + int(unit_id))}{slot + 1}" if unit_id is not None and slot is not None else None,
            "type": t.get("tray_type") or None, "brand": t.get("tray_sub_brands") or None, "color": _color(t.get("tray_color")),
            "remain": remain if remain is not None and remain >= 0 else None,
            "temp_range": [_num(t.get("nozzle_temp_min")), _num(t.get("nozzle_temp_max"))] if t.get("nozzle_temp_min") else None,
            "active": active_index is not None and unit_id is not None and slot is not None
            and active_index == int(unit_id) * 4 + slot, "empty": not t.get("tray_type")}


def _ams(p: dict) -> dict:
    ams = p.get("ams") or {}
    now = _int(ams.get("tray_now"))
    active = None if now in (None, NO_TRAY) else ("external" if now == EXTERNAL else now)
    units = []
    for u in ams.get("ams") or []:
        uid = _int(u.get("id"))
        hum_raw = _num(u.get("humidity_raw"))
        units.append({"id": uid, "name": f"AMS {chr(ord('A') + uid)}" if uid is not None else "AMS",
                      "humidity_pct": hum_raw, "humidity_level": _int(u.get("humidity")), "temp": _num(u.get("temp")),
                      "trays": [_tray(t, uid, active if isinstance(active, int) else None)
                                for t in sorted(u.get("tray") or [], key=lambda t: _int(t.get("id")) or 0)]})
    vt = p.get("vt_tray") or {}
    external = None
    if vt:
        external = {**_tray({**vt, "id": 0}, None, None), "label": "External", "active": active == "external"}
    active_label = None
    if active == "external":
        active_label = "External"
    elif isinstance(active, int):
        active_label = f"{chr(ord('A') + active // 4)}{active % 4 + 1}"
    return {"units": units, "external": external, "active": active_label}


def _temps(p: dict, device: dict) -> dict:
    nozzle = [_num(p.get("nozzle_temper")), _num(p.get("nozzle_target_temper"))]
    bed = [_num(p.get("bed_temper")), _num(p.get("bed_target_temper"))]
    chamber = _num(p.get("chamber_temper"))
    ext = ((device.get("extruder") or {}).get("info") or [])
    if nozzle[0] is None and ext:
        nozzle = list(_packed(ext[0].get("temp")))
    b = (device.get("bed") or {}).get("info") or {}
    if bed[0] is None and isinstance(b, dict) and "temp" in b:
        bed = list(_packed(b.get("temp")))
    c = (device.get("ctc") or {}).get("info") or {}
    if chamber is None and isinstance(c, dict) and "temp" in c:
        chamber = _packed(c.get("temp"))[0]
    return {"nozzle": nozzle, "bed": bed, "chamber": chamber}


def normalize(raw: dict, received: float | None = None, hms_text=None) -> dict:
    """The merged report -> {state, file, percent, layer, remaining_min, eta, speed, temps, fans, ams, errors,
    nozzle, wifi, lights}. hms_text(code) -> text or None (hms.py)."""
    p = raw.get("print") or {}
    device = p.get("device") or raw.get("device") or {}
    gs = p.get("gcode_state")
    remaining = _int(p.get("mc_remaining_time"))
    received = received or time.time()
    errors = []
    for h in p.get("hms") or []:
        try:
            code = hms_code(h.get("attr", 0), h.get("code", 0))
        except (TypeError, ValueError):
            continue
        errors.append({"code": code, "kind": "hms", "text": hms_text(code) if hms_text else None})
    pe = _int(p.get("print_error"))
    if pe:
        code = f"{pe >> 16:04X}_{pe & 0xFFFF:04X}"
        errors.append({"code": code, "kind": "print_error", "text": hms_text(code) if hms_text else None})
    lights = {l.get("node"): l.get("mode") for l in p.get("lights_report") or [] if isinstance(l, dict)}
    spd = _int(p.get("spd_lvl"))
    return {
        "state": STATES.get(gs, gs.lower() if isinstance(gs, str) else None),
        "file": p.get("subtask_name") or p.get("gcode_file") or None,
        "percent": _int(p.get("mc_percent")),
        "layer": [_int(p.get("layer_num")), _int(p.get("total_layer_num"))],
        "remaining_min": remaining,
        "eta": received + remaining * 60 if remaining is not None and gs in ("RUNNING", "PAUSE", "PREPARE") else None,
        "stage": _int(p.get("mc_print_stage")),
        "speed": SPEEDS.get(spd, spd),
        "temps": _temps(p, device),
        "fans": {"part": _fan(p.get("cooling_fan_speed")), "aux": _fan(p.get("big_fan1_speed")),
                 "chamber": _fan(p.get("big_fan2_speed")), "heatbreak": _fan(p.get("heatbreak_fan_speed"))},
        "ams": _ams(p),
        "errors": errors,
        "nozzle": {"diameter": _num(p.get("nozzle_diameter")), "type": p.get("nozzle_type")},
        "wifi": p.get("wifi_signal"),
        "lights": lights,
    }
