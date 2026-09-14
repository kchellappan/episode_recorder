"""The one place that knows where the submodules live.

Nothing else in this repo names a submodule path, and tests/check_boundaries.py fails if
that changes. Without this, the internal layout leaks into every game repo that submodules
this one and can never be reorganised.

The pinned copies go to the front of sys.path, so a `vcap` or `gpb_client` installed
elsewhere cannot silently stand in for the commit this repo was tested against.

A missing submodule does not fail the import of `episode_recorder`: something that needs
only the control-file reader should still work. It fails at the point of use, through
require(), with a message that says what to run.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THIRD_PARTY = os.path.join(REPO_ROOT, "third_party")

SUBMODULES = {
    "hdmi_capture": os.path.join(_THIRD_PARTY, "hdmi_capture"),
    "rpi_gamepad_bridge": os.path.join(_THIRD_PARTY, "rpi_gamepad_bridge"),
}

# Importable package name -> the directory to put on sys.path. Each is the directory that
# *contains* the package, which in both submodules is not the repo root.
PACKAGE_DIRS = {
    "vcap": os.path.join(SUBMODULES["hdmi_capture"], "vcap_py"),
    "gpb_client": os.path.join(SUBMODULES["rpi_gamepad_bridge"], "clients", "python"),
}

MISSING: dict[str, str] = {}


class SubmoduleMissing(ImportError):
    """A submodule this code path needs is not checked out."""


def install() -> None:
    for name, path in PACKAGE_DIRS.items():
        if not os.path.isdir(os.path.join(path, name)):
            MISSING[name] = path
            continue
        MISSING.pop(name, None)
        if path not in sys.path:
            sys.path.insert(0, path)


def require(*names: str) -> None:
    missing = [n for n in names if n in MISSING]
    if missing:
        where = ", ".join(f"{n} (expected at {MISSING[n]})" for n in missing)
        raise SubmoduleMissing(
            f"submodule not checked out: {where}. Clone with --recursive, or run "
            f"`git submodule update --init --recursive`.")
