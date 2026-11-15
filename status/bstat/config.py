"""config.py — where the printer is: host (IP or name), serial number and LAN access code.
Version: 2026.11.14.1

Read from, in order: the environment (BSTAT_HOST, BSTAT_SERIAL, BSTAT_CODE), then ~/.config/bstat/printer.json
(written by 'bstat config', mode 600). The access code is a secret: it is never printed, logged or
written to a recording.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PATH = Path(os.environ.get("BSTAT_CONFIG", "~/.config/bstat/printer.json")).expanduser()
KEYS = ("host", "serial", "access_code")


class ConfigError(ValueError):
    pass


def load() -> dict:
    cfg = {}
    if PATH.exists():
        try:
            cfg = json.loads(PATH.read_text())
        except ValueError as e:
            raise ConfigError(f"{PATH}: not valid JSON ({e})")
    env = {"host": os.environ.get("BSTAT_HOST"), "serial": os.environ.get("BSTAT_SERIAL"),
           "access_code": os.environ.get("BSTAT_CODE")}
    cfg.update({k: v for k, v in env.items() if v})
    return cfg


def require(cfg: dict) -> dict:
    missing = [k for k in KEYS if not cfg.get(k)]
    if missing:
        raise ConfigError(f"missing {missing}: run 'bstat config --host <ip> --serial <serial> --code <access code>' "
                          f"(on the printer: Settings > Network shows the IP and the access code; the serial is "
                          f"under Settings > Device), or set BSTAT_HOST / BSTAT_SERIAL / BSTAT_CODE")
    return cfg


def save(host: str | None, serial: str | None, code: str | None) -> Path:
    cfg = {}
    if PATH.exists():
        try:
            cfg = json.loads(PATH.read_text())
        except ValueError:
            cfg = {}
    for k, v in (("host", host), ("serial", serial), ("access_code", code)):
        if v:
            cfg[k] = v.strip()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(PATH)
    return PATH


def redacted(cfg: dict) -> dict:
    """For printing: the access code hidden, the serial shortened."""
    s = cfg.get("serial") or ""
    return {"host": cfg.get("host"), "serial": (s[:4] + "…" + s[-3:]) if len(s) > 8 else s,
            "access_code": "set" if cfg.get("access_code") else None}
