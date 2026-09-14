"""Which code recorded a session: this repo's commit and each submodule's.

Recorded into every manifest, with a dirty flag, because a session recorded from an
uncommitted working tree cannot be traced back to a commit and should say so rather than
quietly naming the last one.
"""
from __future__ import annotations

import subprocess

from . import bootstrap


def git_state(path: str) -> dict:
    """{"commit", "dirty", "error"} for the checkout at `path`. Never raises."""
    try:
        commit = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=5, check=True).stdout.strip()
        status = subprocess.run(
            ["git", "-C", path, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "dirty": None, "error": str(exc).strip() or type(exc).__name__}
    return {"commit": commit, "dirty": bool(status.strip()), "error": None}


def collect() -> dict:
    return {
        "recorder": git_state(bootstrap.REPO_ROOT),
        "submodules": {name: git_state(path) for name, path in bootstrap.SUBMODULES.items()},
    }
