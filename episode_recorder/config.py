"""Loading a capture profile.

A profile is TOML, merged over configs/default.toml. The merged result is what gets
inlined into each session manifest, so every default that applied is written down with
the data, not left in a file that may have changed since.

The HMAC key is deliberately *not* a config value. The config is inlined into every
manifest, and a key there would be copied into every session directory and every dataset
built from one. The config names a file; the key stays in it.
"""
from __future__ import annotations

import copy
import os
import tomllib

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs",
                            "default.toml")

_NUMBER = (int, float)

CAPTURE_SCHEMA = {
    "fps": _NUMBER,
    "width": int,
    "height": int,
    "device_match": str,
    "control_bind": str,
    "control_key_file": str,
    "bridge_host": str,
    "bridge_caps_port": int,
    "max_session_seconds": _NUMBER,
    "socket_rcvbuf_bytes": int,
    "flush_interval_ms": int,
    "control_quiet_warn_ms": int,
    "video_patience_s": _NUMBER,
    "video_queue_depth": int,
    "budget_bytes_per_frame": int,
    "disk_headroom": _NUMBER,
}

PREFLIGHT_SCHEMA = {
    "video_seconds": _NUMBER,
    "control_seconds": _NUMBER,
    "min_control_datagrams": int,
}

# Names that suggest someone put a secret where it would be inlined into manifests.
FORBIDDEN_KEYS = {"control_key", "key", "publish_key", "hmac_key"}


class ConfigError(ValueError):
    pass


def load(path: str | None = None) -> dict:
    with open(DEFAULT_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    if path:
        with open(path, "rb") as fh:
            _merge(cfg, tomllib.load(fh))
    validate(cfg)
    return cfg


def _merge(base: dict, override: dict) -> None:
    # Tables merge key by key; everything else, lists included, replaces outright. A
    # profile that lists three action dims means three, not three appended to the default.
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _check_section(cfg: dict, section: str, schema: dict) -> None:
    table = cfg.get(section)
    if not isinstance(table, dict):
        raise ConfigError(f"[{section}] is missing")
    for key, kind in schema.items():
        if key not in table:
            raise ConfigError(f"[{section}] {key} is missing")
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ConfigError(f"[{section}] {key} = {value!r} has the wrong type")
    for key in table:
        if key in FORBIDDEN_KEYS:
            raise ConfigError(
                f"[{section}] {key}: keys do not belong in config -- it is inlined into "
                f"every session manifest. Put the key in a file and set "
                f"capture.control_key_file.")


def validate(cfg: dict) -> None:
    _check_section(cfg, "capture", CAPTURE_SCHEMA)
    _check_section(cfg, "preflight", PREFLIGHT_SCHEMA)
    parse_hostport(cfg["capture"]["control_bind"])
    dims = cfg.get("action", {}).get("dims", [])
    if not isinstance(dims, list):
        raise ConfigError("[action] dims must be a list")
    for dim in dims:
        if not isinstance(dim, dict) or not isinstance(dim.get("source"), str):
            raise ConfigError(f"[action] dim {dim!r} has no source")


def parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"expected host:port, got {value!r}")
    return host, int(port)


def read_key(cfg: dict) -> bytes | None:
    """The HMAC key from capture.control_key_file, or None when authentication is off.

    Surrounding whitespace is stripped, so a file written with `echo` -- which appends a
    newline the bridge's ini value does not have -- still matches.
    """
    path = cfg["capture"]["control_key_file"]
    if not path:
        return None
    with open(os.path.expanduser(path), "rb") as fh:
        key = fh.read().strip()
    if not key:
        raise ConfigError(f"control_key_file {path} is empty")
    return key
