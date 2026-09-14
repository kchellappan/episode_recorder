#!/usr/bin/env python3
"""Fail if code that must run on a bare machine imports outside the standard library.

Capture, storage and read-back need nothing installed: a rig should record a session with
no venv, no pip and no build step. That property erodes one convenient import at a time,
so it is checked rather than asserted, following hdmi_capture's check of the same name.

The declared boundary is build/, which may use numpy, pillow and lerobot (docs/design.md).
Nothing outside it may, and nothing in capture/ may import build/ (check_boundaries.py).
If this fails, the question is not how to install the package. It is whether the code
belongs on the other side of that line.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_PREFIXES = ("episode_recorder/build/",)
# First-party: this package, the two submodule packages, the tools' path shim, and the
# test helpers.
FIRST_PARTY = {"episode_recorder", "vcap", "gpb_client", "_path", "harness", "fakes",
               "check_stdlib_only"}


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def code_files() -> list[pathlib.Path]:
    files = sorted((ROOT / "episode_recorder").rglob("*.py"))
    files += sorted((ROOT / "tests").glob("*.py"))
    # The tools have no extension so they read as commands, but they are Python.
    files += sorted(p for p in (ROOT / "tools").iterdir()
                    if p.is_file() and (p.suffix == ".py" or p.name.startswith("er-")))
    return [p for p in files if "__pycache__" not in p.parts]


def main() -> int:
    stdlib = set(sys.stdlib_module_names)
    failures, checked = [], 0
    for path in code_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(ALLOWED_PREFIXES):
            continue
        checked += 1
        for module in sorted(imported_modules(path)):
            if module not in stdlib and module not in FIRST_PARTY:
                failures.append(f"{rel}: imports {module!r}")
    if failures:
        print("  FAIL  non-stdlib imports outside the declared boundary (build/):")
        for line in failures:
            print(f"        {line}")
        return 1
    print(f"  PASS  {checked} files import only the standard library and first-party code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
