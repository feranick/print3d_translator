"""profiles.py — OrcaSlicer system profiles: find them, index them, flatten them, choose the machine,
process and filament profiles for a study, apply the intent and overrides, and write the flattened
copies the OrcaSlicer CLI loads.
Version: 2026.10.24.4

Why we flatten: OrcaSlicer 2.4.2's --load-settings / --load-filaments read one JSON file and do not
follow "inherits" (OrcaSlicer issue #14718): every key a leaf profile does not set silently falls back
to a built-in default. We resolve the chain ourselves, root first, through the vendor index
(BBL.json "sub_path"; file names do not always match profile names), and write self-contained files
with "from": "system" and no "inherits". The saved profiles are never touched: everything is written
into the study's folder.

Merge rule (as OrcaSlicer's PresetBundle): a key set in a child replaces the parent's value whole
(lists are not merged element by element).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path

VENDOR = "BBL"
META_KEYS = ("inherits",)        # dropped from flattened copies
_KINDS = {"machine_model_list": "machine_model", "machine_list": "machine",
          "process_list": "process", "filament_list": "filament"}

# printers this translator is built and tested for; other Bambu Lab models resolve with a warning
SUPPORTED_MODELS = ("H2D", "H2S", "P2S")

# study plate name -> (OrcaSlicer curr_bed_type, filament bed temperature key)
PLATES = {
    "textured_pei": ("Textured PEI Plate", "textured_plate_temp"),
    "smooth_pei": ("High Temp Plate", "hot_plate_temp"),
    "high_temp": ("High Temp Plate", "hot_plate_temp"),
    "engineering": ("Engineering Plate", "eng_plate_temp"),
    "cool": ("Cool Plate", "cool_plate_temp"),
    "supertack": ("Supertack Plate", "supertack_plate_temp"),
    "textured_cool": ("Textured Cool Plate", "textured_cool_plate_temp"),
}
# other names of the same plates (machine_model 'not_support_bed_type' uses Bambu Studio's names)
PLATE_ALIASES = {"Cool Plate": {"Smooth Cool Plate"}, "High Temp Plate": {"Smooth PEI Plate"},
                 "Supertack Plate": {"SuperTack Plate", "Cool Plate (SuperTack)"}}

# intent preset -> words in the process profile name (in order of preference) and the target
# layer height as a fraction of the nozzle diameter
PRESETS = {
    "fine": {"words": ["Fine", "High Quality", "Balanced Quality", "Extra Fine"], "lh": 0.3},
    "standard": {"words": ["Standard"], "lh": 0.5},
    "strength": {"words": ["Balanced Strength", "Strength"], "lh": 0.5,
                 "fallback": "standard", "fallback_set": {"wall_loops": 4, "sparse_infill_density": 25}},
    "draft": {"words": ["Draft", "Standard"], "lh": 0.6},
}

SUPPORTS = {"none": None, "auto": "normal(auto)", "tree": "tree(auto)"}
# filament colours when the study gives none (distinct, so flush volumes are not underestimated)
DEFAULT_COLORS = ["#FFFFFF", "#000000", "#FF0000", "#0000FF", "#00A000", "#FFD700", "#808080", "#FF8000"]
EXTRUDERS = {"left": 1, "right": 2}      # H2D: filament_map values (extruder_printable_area[0] is the left one)


class ProfileError(ValueError):
    """A profile could not be found or resolved; the message says what to change."""


# ---------------------------------------------------------------- finding the profiles

def profile_dir_candidates(orca_exe: str | None = None) -> list[Path]:
    """Folders that may hold OrcaSlicer's resources/profiles (with BBL.json), in search order."""
    out = []
    env = os.environ.get("P3DT_ORCA_PROFILES")
    if env:
        out.append(Path(env).expanduser())
    if orca_exe:
        exe = Path(orca_exe).expanduser().resolve()
        for up in exe.parents:
            if up.name == "Contents":                          # macOS: OrcaSlicer.app/Contents
                out.append(up / "Resources" / "profiles")
                break
        out += [exe.parent / "resources" / "profiles",          # extracted AppImage (squashfs-root)
                exe.parent.parent / "resources" / "profiles",   # squashfs-root/bin/orca-slicer
                exe.parent.parent / "share" / "OrcaSlicer" / "resources" / "profiles"]
    out += [Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles"),
            Path.home() / "Applications/OrcaSlicer.app/Contents/Resources/profiles",
            Path("/var/lib/flatpak/app/io.github.softfever.OrcaSlicer/current/active/files/share/OrcaSlicer/resources/profiles"),
            Path("/usr/share/OrcaSlicer/resources/profiles"),
            Path("/opt/OrcaSlicer/resources/profiles")]
    return out


def find_profiles(orca_exe: str | None = None) -> Path | None:
    """The first candidate folder with <VENDOR>.json and a <VENDOR>/ folder, or None."""
    for d in profile_dir_candidates(orca_exe):
        if (d / f"{VENDOR}.json").is_file() and (d / VENDOR).is_dir():
            return d
    return None


# ---------------------------------------------------------------- OrcaSlicer's keys

@lru_cache(maxsize=1)
def orca_keys() -> dict:
    """{key: {type, enum?, min?, max?, nullable?}} for the pinned OrcaSlicer (data/orca_keys.json,
    made by tools/orca_keys.py from its PrintConfig.cpp)."""
    p = Path(__file__).with_name("data") / "orca_keys.json"
    return json.loads(p.read_text())


def _fmt_num(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def to_orca_value(key: str, value, current=None):
    """A study value -> OrcaSlicer's JSON form (strings, or lists of strings for vector keys).
    Raises ProfileError with the fix when the key is unknown or the value does not fit its type."""
    spec = orca_keys()["keys"].get(key)
    if spec is None:
        raise ProfileError(f"'{key}' is not an OrcaSlicer {orca_keys()['orca_version']} setting; "
                           f"check the spelling (profile keys use underscores, e.g. 'wall_loops')")
    typ = spec["type"]
    # vector keys (Bools, Ints, Floats, Strings, ...), and scalar keys that a profile stores as a list
    # (BBL multi-extruder profiles, e.g. outer_wall_speed per extruder variant): keep the profile's shape
    if typ.endswith("s") or isinstance(current, list):
        if not typ.endswith("s"):
            typ += "s"
        vals = value if isinstance(value, list) else None
        if vals is None:
            n = len(current) if isinstance(current, list) and current else 1
            vals = [value] * n
        return [_scalar(key, spec, typ[:-1], v) for v in vals]
    if isinstance(value, list):
        raise ProfileError(f"'{key}' takes one value, not a list")
    return _scalar(key, spec, typ, value)


def _scalar(key: str, spec: dict, typ: str, v) -> str:
    if v == "nil" and spec.get("nullable"):
        return "nil"
    try:
        if typ == "Bool":
            if isinstance(v, str):
                if v.lower() in ("1", "true", "yes", "on"):
                    return "1"
                if v.lower() in ("0", "false", "no", "off"):
                    return "0"
                raise ValueError
            return "1" if bool(v) else "0"
        if typ == "Int":
            if isinstance(v, bool) or float(v) != int(float(v)):
                raise ValueError
            out, num = str(int(float(v))), float(v)
        elif typ == "Float":
            num = float(v)
            out = _fmt_num(num)
        elif typ == "Percent":
            num = float(str(v).rstrip("%"))
            out = _fmt_num(num) + "%"
        elif typ == "FloatOrPercent":
            s = str(v)
            num = float(s.rstrip("%"))
            out = _fmt_num(num) + ("%" if s.endswith("%") else "")
        elif typ == "Enum":
            if str(v) not in spec["enum"]:
                raise ProfileError(f"'{key}': '{v}' is not one of {spec['enum']}")
            return str(v)
        else:                                              # String, Point
            return str(v)
    except ProfileError:
        raise
    except (TypeError, ValueError):
        raise ProfileError(f"'{key}' needs a {typ.lower()} value, not {v!r}") from None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and num < lo or hi is not None and num > hi:
        raise ProfileError(f"'{key}' = {v} is outside [{lo}, {hi}]")
    return out


# ---------------------------------------------------------------- the vendor tree

class Vendor:
    """One vendor's system profiles (BBL): the index, raw leaf files and flattened profiles."""

    def __init__(self, root: str | Path, vendor: str = VENDOR):
        self.root = Path(root)
        self.vendor = vendor
        idx_file = self.root / f"{vendor}.json"
        if not idx_file.is_file():
            raise ProfileError(f"no {vendor}.json in {self.root}: set P3DT_ORCA_PROFILES to OrcaSlicer's "
                               f"resources/profiles folder")
        idx = json.loads(idx_file.read_text(encoding="utf-8"))
        self.version = idx.get("version")
        self.paths: dict[str, Path] = {}
        self.kinds: dict[str, str] = {}
        for lst, kind in _KINDS.items():
            for e in idx.get(lst, []):
                self.paths[e["name"]] = self.root / vendor / e["sub_path"]
                self.kinds[e["name"]] = kind
        self._raw: dict[str, dict] = {}
        self._flat: dict[str, dict] = {}

    # -- raw and flattened
    def raw(self, name: str) -> dict:
        if name not in self._raw:
            p = self.paths.get(name)
            if p is None:
                raise ProfileError(f"profile '{name}' is not in {self.vendor}.json")
            if not p.is_file():
                raise ProfileError(f"profile '{name}': file {p} is missing")
            self._raw[name] = json.loads(p.read_text(encoding="utf-8"))
        return self._raw[name]

    def chain(self, name: str) -> list[str]:
        """The inheritance chain, leaf first, root last."""
        out = []
        n = name
        while n:
            if n in out:
                raise ProfileError(f"profile '{name}': inheritance loop at '{n}'")
            out.append(n)
            n = self.raw(n).get("inherits") or ""
        return out

    def flatten(self, name: str) -> dict:
        """All keys of a profile with its parents' resolved, root first; 'inherits' dropped,
        'from' = 'system'. Returns a copy."""
        if name not in self._flat:
            merged: dict = {}
            for n in reversed(self.chain(name)):
                merged.update(self.raw(n))
            for k in META_KEYS:
                merged.pop(k, None)
            merged["name"] = name
            merged["from"] = "system"
            self._flat[name] = merged
        return json.loads(json.dumps(self._flat[name]))

    def _leaves(self, kind: str) -> list[str]:
        """Instantiable profiles of a kind; index entries whose file is missing or unreadable are
        skipped and listed in self.missing (a damaged install still resolves what it has)."""
        out = []
        self.missing = getattr(self, "missing", [])
        for n, k in self.kinds.items():
            if k != kind:
                continue
            try:
                raw = self.raw(n)
            except (ProfileError, ValueError):
                if n not in self.missing:
                    self.missing.append(n)
                continue
            if str(raw.get("instantiation", "true")).lower() == "true":
                out.append(n)
        return sorted(out)

    def compatible(self, kind: str, machine: str) -> list[str]:
        """Instantiable process or filament profiles whose compatible_printers names the machine."""
        return [n for n in self._leaves(kind)
                if machine in (self.raw(n).get("compatible_printers") or [])]

    # -- printers
    def models(self) -> dict[str, dict]:
        """{short name: machine_model profile}, e.g. {'H2D': {...'model_id': 'O1D'...}}."""
        out = {}
        for n, k in self.kinds.items():
            if k == "machine_model":
                short = n.replace("Bambu Lab ", "", 1).replace(" ", "")
                out[short] = self.raw(n)
        return out

    def machine(self, model: str, nozzle: float) -> tuple[str, dict]:
        """The machine profile name for a model ('H2D') and nozzle (0.4), and its machine_model."""
        models = self.models()
        key = str(model).replace("Bambu Lab ", "").replace(" ", "").upper()
        mm = next((v for k, v in models.items() if k.upper() == key), None)
        if mm is None:
            raise ProfileError(f"printer.model '{model}' is not a {self.vendor} printer here; choose one of "
                               f"{sorted(models)}")
        noz = _fmt_num(nozzle)
        offered = [s.strip() for s in str(mm.get("nozzle_diameter", "")).split(";") if s.strip()]
        name = f"{mm['name']} {noz} nozzle"
        if name not in self.paths or (offered and noz not in offered):
            raise ProfileError(f"printer.nozzle {noz} is not offered for the {mm['name']}; choose one of {offered}")
        return name, mm


# ---------------------------------------------------------------- choosing profiles

_LH = re.compile(r"^(\d+(?:\.\d+)?)mm\b")


def _process_lh(name: str) -> float | None:
    m = _LH.match(name)
    return float(m.group(1)) if m else None


def choose_process(v: Vendor, machine: str, nozzle: float, intent: dict) -> tuple[str, list[str], dict]:
    """(process name, warnings, extra intent settings) for the intent's preset or explicit process."""
    procs = v.compatible("process", machine)
    if not procs:
        raise ProfileError(f"no process profile is compatible with '{machine}'")
    if intent.get("process"):
        p = intent["process"]
        if p not in procs:
            raise ProfileError(f"intent.process '{p}' is not compatible with '{machine}'; choose one of {procs}")
        return p, [], {}
    preset = intent.get("preset", "standard")
    if preset not in PRESETS:
        raise ProfileError(f"intent.preset '{preset}': choose one of {sorted(PRESETS)} (or name intent.process)")
    rule = PRESETS[preset]
    target = float(intent.get("layer_height") or rule["lh"] * float(nozzle))
    for rank, word in enumerate(rule["words"]):
        cands = [p for p in procs if re.search(rf"\b{re.escape(word)}\b", p) and _process_lh(p) is not None]
        if word == "Strength":
            cands = [p for p in cands if "Balanced Strength" not in p]
        if cands:
            best = min(cands, key=lambda p: (abs(_process_lh(p) - target), p))
            return best, [], {}
    if "fallback" in rule:
        name, w, _ = choose_process(v, machine, nozzle, {**intent, "preset": rule["fallback"]})
        return name, w + [f"no '{preset}' process profile for {machine}: used '{name}' with "
                          f"{rule['fallback_set']}"], dict(rule["fallback_set"])
    raise ProfileError(f"no '{preset}' process profile for '{machine}'; choose intent.process from {procs}")


def _base(name: str) -> str:
    return name.split(" @")[0]


def filament_choices(v: Vendor, machine: str) -> list[str]:
    """Base names (without '@BBL ...') of the filaments compatible with the machine."""
    return sorted({_base(n) for n in v.compatible("filament", machine)})


def choose_filament(v: Vendor, machine: str, spec: dict) -> str:
    """The filament profile for {profile} or {type, brand, variant}, compatible with the machine."""
    comp = v.compatible("filament", machine)
    if spec.get("profile"):
        want = str(spec["profile"])
    else:
        if not spec.get("type"):
            raise ProfileError("give 'profile' (e.g. 'Bambu PLA Basic') or 'type' (+ 'brand', 'variant')")
        brand = str(spec.get("brand") or "Generic")
        brand = "Bambu" if brand.lower() in ("bambu", "bambu lab", "bambulab", "bbl") else brand
        want = " ".join(x for x in (brand, str(spec["type"]), str(spec.get("variant") or "")) if x)
    hits = [n for n in comp if n == want] or [n for n in comp if _base(n).lower() == want.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        choices = filament_choices(v, machine)
        near = [c for c in choices if want.split()[-1].lower() in c.lower()] or choices
        raise ProfileError(f"no filament '{want}' for '{machine}'; choose one of {near[:40]}")
    raise ProfileError(f"filament '{want}' matches several profiles for '{machine}': {hits}; name one in 'profile'")


# ---------------------------------------------------------------- intent and overrides

def _set(cfg: dict, key: str, value, changes: dict, where: str) -> None:
    new = to_orca_value(key, value, cfg.get(key))
    old = cfg.get(key)
    if old != new:
        changes[f"{where}.{key}"] = [old, new]
    cfg[key] = new


def apply_intent(process: dict, machine: dict, intent: dict, nozzle: float, changes: dict) -> list[str]:
    """Maps the intent keys onto the process profile (in place). Returns warnings."""
    warnings = []
    lh = intent.get("layer_height")
    if lh is not None:
        lo = min(float(x) for x in _as_list(machine.get("min_layer_height", ["0.04"])))
        hi = max(float(x) for x in _as_list(machine.get("max_layer_height", [str(0.75 * nozzle)])))
        if not lo <= float(lh) <= hi:
            raise ProfileError(f"intent.layer_height {lh} is outside this nozzle's range [{lo}, {hi}] mm")
        _set(process, "layer_height", lh, changes, "process")
    simple = {"first_layer_height": "initial_layer_print_height", "walls": "wall_loops",
              "top_layers": "top_shell_layers", "bottom_layers": "bottom_shell_layers",
              "infill": "sparse_infill_density", "infill_pattern": "sparse_infill_pattern",
              "support_angle": "support_threshold_angle", "seam": "seam_position"}
    for k, key in simple.items():
        if intent.get(k) is not None:
            _set(process, key, intent[k], changes, "process")
    sup = intent.get("supports")
    if sup is not None:
        if sup not in SUPPORTS:
            raise ProfileError(f"intent.supports '{sup}': choose one of {list(SUPPORTS)}")
        _set(process, "enable_support", sup != "none", changes, "process")
        if SUPPORTS[sup]:
            _set(process, "support_type", SUPPORTS[sup], changes, "process")
    brim = intent.get("brim")
    if brim is not None:
        if brim in ("none", 0, False):
            _set(process, "brim_type", "no_brim", changes, "process")
        elif brim == "auto":
            _set(process, "brim_type", "auto_brim", changes, "process")
        elif isinstance(brim, (int, float)) and not isinstance(brim, bool) and brim > 0:
            _set(process, "brim_type", "outer_only", changes, "process")
            _set(process, "brim_width", brim, changes, "process")
        else:
            raise ProfileError(f"intent.brim {brim!r}: use 'none', 'auto' or a width in mm")
    return warnings


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


INTENT_KEYS = {"preset", "process", "layer_height", "first_layer_height", "walls", "top_layers",
               "bottom_layers", "infill", "infill_pattern", "supports", "support_angle", "brim", "seam"}


def apply_overrides(cfg: dict, overrides: dict, where: str, others: dict[str, dict], changes: dict) -> None:
    """Raw OrcaSlicer keys into one flattened profile (in place). A key that only another section's
    profile has is an error that names the right section."""
    for key, value in (overrides or {}).items():
        if key in ("name", "type", "from", "inherits", "compatible_printers", "setting_id", "instantiation"):
            raise ProfileError(f"overrides.{where}.{key}: this key identifies the profile and cannot be overridden")
        if key not in cfg:
            owner = [w for w, c in others.items() if key in c]
            if owner:
                raise ProfileError(f"overrides.{where}.{key}: '{key}' is a {owner[0]} setting; "
                                   f"move it to overrides.{owner[0]}")
        _set(cfg, key, value, changes, where)


# ---------------------------------------------------------------- the whole resolution

def resolve(v: Vendor, study: dict) -> dict:
    """Machine, process and filament profiles for a study, flattened, with the plate, intent and
    overrides applied. Returns {machine, process, filaments, model, plate, set, warnings} (configs are
    dicts ready to write). Raises ProfileError."""
    printer = study.get("printer") or {}
    nozzle = float(printer.get("nozzle", 0.4))
    mname, mm = v.machine(printer.get("model", ""), nozzle)
    short = mm["name"].replace("Bambu Lab ", "").replace(" ", "")
    warnings = []
    if short not in SUPPORTED_MODELS:
        warnings.append(f"{mm['name']} is not one of the tested printers {list(SUPPORTED_MODELS)}")
    machine = v.flatten(mname)
    intent = study.get("intent") or {}
    unknown = set(intent) - INTENT_KEYS
    if unknown:
        raise ProfileError(f"intent: unknown keys {sorted(unknown)}; known: {sorted(INTENT_KEYS)} "
                           f"(raw profile keys go in overrides.process)")
    pname, w, extra = choose_process(v, mname, nozzle, intent)
    warnings += w
    process = v.flatten(pname)
    fils_spec = study.get("filaments") or []
    if not fils_spec:
        raise ProfileError("filaments: give at least one, e.g. [{'profile': 'Bambu PLA Basic'}]")
    fnames = [choose_filament(v, mname, f) for f in fils_spec]
    filaments = [v.flatten(n) for n in fnames]

    plate = printer.get("plate", "textured_pei")
    if plate not in PLATES:
        raise ProfileError(f"printer.plate '{plate}': choose one of {sorted(PLATES)}")
    bed_type, temp_key = PLATES[plate]
    refused = [s.strip() for s in str(mm.get("not_support_bed_type", "")).split(";") if s.strip()]
    if ({bed_type} | PLATE_ALIASES.get(bed_type, set())) & set(refused):
        raise ProfileError(f"printer.plate '{plate}' ({bed_type}) is not supported by the {mm['name']}")

    changes: dict = {}
    for k, val in extra.items():
        _set(process, k, val, changes, "process")
    warnings += apply_intent(process, machine, intent, nozzle, changes)

    ov = study.get("overrides") or {}
    bad = set(ov) - {"machine", "process", "filament"}
    if bad:
        raise ProfileError(f"overrides: unknown sections {sorted(bad)}; use 'machine', 'process', 'filament'")
    apply_overrides(machine, ov.get("machine"), "machine", {"process": process, "filament": filaments[0]}, changes)
    apply_overrides(process, ov.get("process"), "process", {"machine": machine, "filament": filaments[0]}, changes)
    fov = ov.get("filament") or {}
    for i, cfg in enumerate(filaments, start=1):
        merged = {**(fov.get("all") or {}), **(fov.get(str(i)) or {})}
        apply_overrides(cfg, merged, f"filament.{i}", {"machine": machine, "process": process}, changes)
    badf = set(fov) - {"all"} - {str(i) for i in range(1, len(filaments) + 1)}
    if badf:
        raise ProfileError(f"overrides.filament: keys {sorted(badf)}; use 'all' or a filament number "
                           f"1..{len(filaments)}")

    for i, (cfg, fs) in enumerate(zip(filaments, fils_spec), start=1):
        # Every filament gets a colour. No BBL filament profile sets filament_colour, and without one
        # OrcaSlicer 2.4.2's CLI skips the flush-volume matrix; with 2+ filaments it then crashes
        # (SIGSEGV in calc_filament_change_info_by_toolorder). Flush volumes depend on the colours.
        color = fs.get("color") or DEFAULT_COLORS[(i - 1) % len(DEFAULT_COLORS)]
        if not fs.get("color") and len(fils_spec) > 1:
            warnings.append(f"filament {i} has no color: used {color} (flush volumes between filaments depend "
                            f"on their colors; give filaments[{i - 1}].color)")
        _set(cfg, "filament_colour", color, changes, f"filament.{i}")
        if not cfg.get(temp_key) or cfg.get(temp_key) in (["0"], "0"):
            warnings.append(f"filament {i} ({cfg['name']}) has no {temp_key}: is the {bed_type} right for it?")

    # H2D: filament -> extruder (filament_map: 1 left, 2 right), manual when any is given
    n_ext = len(_as_list(machine.get("nozzle_diameter", ["0.4"])))
    fmap = None
    wants = [fs.get("extruder", "auto") for fs in fils_spec]
    if n_ext > 1 and any(w != "auto" for w in wants):
        for i, w in enumerate(wants, start=1):
            if w not in ("auto", *EXTRUDERS):
                raise ProfileError(f"filaments[{i - 1}].extruder '{w}': choose 'left', 'right' or 'auto'")
        fmap = [EXTRUDERS.get(w, 2) for w in wants]
    elif n_ext == 1 and any(w != "auto" for w in wants):
        warnings.append(f"the {mm['name']} has one extruder: filaments[].extruder is ignored")

    return {"machine": machine, "process": process, "filaments": filaments,
            "names": {"machine": mname, "process": pname, "filaments": fnames},
            "model": {"name": mm["name"], "short": short, "model_id": mm.get("model_id"),
                      "nozzle": nozzle, "extruders": n_ext},
            "plate": {"name": plate, "bed_type": bed_type, "temp_key": temp_key},
            "filament_map": fmap, "set": changes, "warnings": warnings,
            "vendor_version": v.version}


def canonical(cfg: dict) -> str:
    """The text written for a profile: sorted keys, so the same settings give the same bytes."""
    return json.dumps(cfg, indent=4, sort_keys=True, ensure_ascii=False) + "\n"


def sha256(cfg: dict) -> str:
    return hashlib.sha256(canonical(cfg).encode("utf-8")).hexdigest()


def write(resolved: dict, folder: str | Path) -> dict:
    """Writes machine.json, process.json, filament_<i>.json into <folder>/profiles; returns
    {machine, process, filaments: [...], sha256: {...}} (paths as strings)."""
    d = Path(folder) / "profiles"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    out = {"filaments": [], "sha256": {"filaments": []}}
    for kind in ("machine", "process"):
        p = d / f"{kind}.json"
        p.write_text(canonical(resolved[kind]), encoding="utf-8")
        out[kind] = str(p)
        out["sha256"][kind] = sha256(resolved[kind])
    for i, cfg in enumerate(resolved["filaments"], start=1):
        p = d / f"filament_{i}.json"
        p.write_text(canonical(cfg), encoding="utf-8")
        out["filaments"].append(str(p))
        out["sha256"]["filaments"].append(sha256(cfg))
    return out
