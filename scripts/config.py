"""Configuration.

Defaults ship as `config.ini` beside this file — a real file in the repository, so
changing what everyone gets is an edit and a pull request rather than a code
change. Personal settings go in ~/.config/machine-monitor/config.ini, which is
read afterwards and overrides the shipped file key by key; anything omitted there
keeps the shipped value, so an override file only ever holds the lines that
actually differ.

The rest of the program consumes a plain nested dict, so the INI shape is an
implementation detail of this module.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any

SHIPPED_PATH = Path(__file__).resolve().parent / "config.ini"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "machine-monitor"
CONFIG_PATH = CONFIG_DIR / "config.ini"

USER_TEMPLATE = """\
# machine-monitor — your personal settings.
#
# Only put lines here that you want DIFFERENT from the shipped defaults in
# <repo>/scripts/config.ini. Everything you leave out keeps its shipped value,
# so this file staying nearly empty is the normal, healthy state — and it means
# pulling an update to the repo actually reaches you instead of being masked by
# a stale full copy.
#
# Uncomment and edit whatever you need. Section names must match the shipped
# file.

# [general]
# refresh_seconds = 2
# top_processes = 20

# [thresholds]
# swap_warn = 10
# stale_server_hours = 8

# [paths]
# project_roots = ~/Documents, ~/work/clients

# [port_labels]
# 8066 = My API

# [port_ranges]
# Port numbering conventions are per-team, so they belong here, not in the repo.
# 3400-3499 = Backend
# 3500-3599 = Frontend
"""

# Types that are not plain strings. Everything else is read verbatim.
_INT_KEYS = {"top_processes", "powermetrics_timeout"}
_FLOAT_SECTIONS = {"thresholds"}
_BOOL_SECTIONS = {"sections"}


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse(parser: configparser.ConfigParser) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "sections": {},
        "thresholds": {},
        "port_labels": {},
        "port_ranges": [],
        "power": {},
    }

    for key, value in parser.items("general") if parser.has_section("general") else []:
        if key in _INT_KEYS:
            cfg[key] = int(float(value))
        else:
            cfg[key] = float(value)

    if parser.has_section("sections"):
        for key, _ in parser.items("sections"):
            cfg["sections"][key] = parser.getboolean("sections", key)

    if parser.has_section("thresholds"):
        for key, value in parser.items("thresholds"):
            cfg["thresholds"][key] = float(value)

    if parser.has_section("power"):
        cfg["power"]["use_powermetrics"] = parser.getboolean(
            "power", "use_powermetrics", fallback=True
        )
        cfg["power"]["powermetrics_timeout"] = float(
            parser.get("power", "powermetrics_timeout", fallback="4")
        )

    if parser.has_section("paths"):
        cfg["project_roots"] = _split_list(parser.get("paths", "project_roots", fallback=""))
        cfg["hide_ports"] = [
            int(p) for p in _split_list(parser.get("paths", "hide_ports", fallback="")) if p.isdigit()
        ]
    cfg.setdefault("project_roots", [])
    cfg.setdefault("hide_ports", [])

    if parser.has_section("port_labels"):
        for port, label in parser.items("port_labels"):
            cfg["port_labels"][port.strip()] = label.strip()

    if parser.has_section("port_ranges"):
        for span, label in parser.items("port_ranges"):
            low, _, high = span.partition("-")
            if low.strip().isdigit() and high.strip().isdigit():
                cfg["port_ranges"].append([int(low), int(high), label.strip()])

    return cfg


def load() -> dict[str, Any]:
    """Shipped defaults, overlaid with the user's overrides.

    A missing or malformed user file degrades to the shipped defaults rather than
    crashing, and reports itself through '_config_error' so the dashboard can say
    so out loud instead of silently ignoring what the user wrote.
    """
    parser = configparser.ConfigParser(interpolation=None)
    error = ""

    try:
        with SHIPPED_PATH.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        # Without the shipped file there is nothing sensible to fall back to.
        return {
            "sections": {}, "thresholds": {}, "port_labels": {}, "port_ranges": [],
            "power": {}, "project_roots": [], "hide_ports": [],
            "refresh_seconds": 3, "cpu_sample_seconds": 0.5, "top_processes": 12,
            "_config_error": f"cannot read {SHIPPED_PATH}: {exc}",
        }

    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error) as exc:
            error = f"{CONFIG_PATH.name}: {exc} — using shipped defaults"
    else:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(USER_TEMPLATE, encoding="utf-8")
        except OSError as exc:
            error = f"could not create {CONFIG_PATH}: {exc}"

    try:
        cfg = _parse(parser)
    except (ValueError, configparser.Error) as exc:
        parser_default = configparser.ConfigParser(interpolation=None)
        with SHIPPED_PATH.open(encoding="utf-8") as handle:
            parser_default.read_file(handle)
        cfg = _parse(parser_default)
        error = f"{CONFIG_PATH.name}: {exc} — using shipped defaults"

    if error:
        cfg["_config_error"] = error
    return cfg


def label_for_port(port: int, cfg: dict[str, Any]) -> str:
    """Exact match first, then explicit numeric ranges.

    Exact-then-range, never a string prefix: the original bash version matched
    port labels with a `56*` glob, so 5601 and 5678 both resolved to the label
    written for port 56.
    """
    exact = cfg.get("port_labels", {}).get(str(port))
    if exact:
        return exact
    for low, high, label in cfg.get("port_ranges", []):
        if low <= port <= high:
            return label
    return ""
