#!/usr/bin/env python3
"""Profiles: merging, validation, and keeping the HMAC key out of anything inlined."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import harness

from episode_recorder import config as config_mod


def write(tmp: str, name: str, text: str) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def raises(fn, exc_type, needle: str = "") -> bool:
    try:
        fn()
    except exc_type as exc:
        return needle in str(exc)
    return False


def main() -> int:
    t = harness.Checks()
    tmp = tempfile.mkdtemp()
    try:
        cfg = config_mod.load()
        t.equal("the standalone default loads", cfg["capture"]["fps"], 30)
        t.check("the standalone default names no plugins", "plugins" not in cfg)
        t.equal("default control port is the bridge's publish_port default",
                config_mod.parse_hostport(cfg["capture"]["control_bind"]), ("0.0.0.0", 9872))

        profile = write(tmp, "profile.toml", '[capture]\nfps = 60\n[action]\n'
                        'dims = [{ name = "lx", source = "axis.lx", kind = "continuous" }]\n')
        merged = config_mod.load(profile)
        t.equal("a profile overrides a key", merged["capture"]["fps"], 60)
        t.equal("and leaves the rest of the table", merged["capture"]["width"], 1920)
        t.equal("a list replaces rather than appends", len(merged["action"]["dims"]), 1)

        t.check("a key in config is refused, pointing at control_key_file",
                raises(lambda: config_mod.load(write(tmp, "k.toml", '[capture]\ncontrol_key = "x"\n')),
                       config_mod.ConfigError, "control_key_file"))
        t.check("a wrongly typed value is refused",
                raises(lambda: config_mod.load(write(tmp, "t.toml", '[capture]\nfps = "30"\n')),
                       config_mod.ConfigError, "fps"))
        t.check("a malformed bind is refused",
                raises(lambda: config_mod.load(write(tmp, "b.toml", '[capture]\ncontrol_bind = "9872"\n')),
                       config_mod.ConfigError, "host:port"))

        t.equal("no key file means no authentication", config_mod.read_key(cfg), None)
        cfg["capture"]["control_key_file"] = write(tmp, "key", "change-me\n")
        t.equal("a key file is read with its trailing newline stripped",
                config_mod.read_key(cfg), b"change-me")
        cfg["capture"]["control_key_file"] = write(tmp, "empty", "\n")
        t.check("an empty key file is refused",
                raises(lambda: config_mod.read_key(cfg), config_mod.ConfigError, "empty"))
    finally:
        shutil.rmtree(tmp)
    return t.exit_code()


if __name__ == "__main__":
    sys.exit(main())
