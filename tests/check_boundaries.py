#!/usr/bin/env python3
"""Structural rules from CLAUDE.md, enforced rather than intended.

  * This repo never imports a game module. check_stdlib_only.py covers static imports.
    Here, dynamic import machinery is allowed only in the plugin loader, the one place
    config strings may name code elsewhere.
  * Submodule paths are known only to bootstrap.py.
  * capture/ never imports build/.
  * The submodules never import each other. That is their rule; a submodule bump here
    must not be the thing that breaks it.
  * control_store.py stays readable on a machine with neither submodule.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import harness
from check_stdlib_only import code_files, imported_modules

from episode_recorder import bootstrap

ROOT = harness.REPO_ROOT
SELF = pathlib.Path(__file__).resolve()
DYNAMIC_IMPORT_ALLOWED = {"episode_recorder/plugins/loader.py"}
SYS_PATH_ALLOWED = {"episode_recorder/bootstrap.py", "tools/_path.py", "tests/harness.py"}
failures: list[str] = []


def rel(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def check(name: str, problems: list[str]) -> None:
    if problems:
        print(f"  FAIL  {name}")
        for p in problems:
            print(f"        {p}")
        failures.append(name)
    else:
        print(f"  PASS  {name}")


def dynamic_imports(path: pathlib.Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in ("import_module", "__import__"):
                lines.append(node.lineno)
    return lines


def sys_path_mutations(path: pathlib.Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Attribute) and node.attr in ("insert", "append", "extend")
                and isinstance(node.value, ast.Attribute) and node.value.attr == "path"
                and getattr(node.value.value, "id", None) == "sys"):
            lines.append(node.lineno)
    return lines


def imports_build(path: pathlib.Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and module.startswith("episode_recorder.build"):
                return True
            if node.level > 0 and (module == "build" or module.startswith("build.")
                                   or (not module and any(a.name == "build" for a in node.names))):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.startswith("episode_recorder.build") for a in node.names):
                return True
    return False


def submodule_python(root: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for p in root.rglob("*"):
        if ".git" in p.parts or "__pycache__" in p.parts or not p.is_file():
            continue
        if p.suffix == ".py" or (p.parent.name == "tools" and p.name.startswith(("vcap-", "gpb-"))):
            try:
                ast.parse(p.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            out.append(p)
    return out


def main() -> int:
    files = code_files()

    check("dynamic imports only in the plugin loader",
          [f"{rel(p)}:{n}" for p in files if rel(p) not in DYNAMIC_IMPORT_ALLOWED
           for n in dynamic_imports(p)])

    marker = "third" + "_party"
    check("only bootstrap.py knows submodule paths",
          [rel(p) for p in files + [ROOT / "tests" / "run_tests.sh", ROOT / "tests" / "hardware.sh"]
           if p.resolve() != SELF and rel(p) != "episode_recorder/bootstrap.py"
           and marker in p.read_text()])

    check("sys.path is modified only by bootstrap and the entry-point shims",
          [f"{rel(p)}:{n}" for p in files if rel(p) not in SYS_PATH_ALLOWED
           for n in sys_path_mutations(p)])

    check("capture/ never imports build/",
          [rel(p) for p in (ROOT / "episode_recorder" / "capture").rglob("*.py")
           if imports_build(p)])

    store = ROOT / "episode_recorder" / "capture" / "control_store.py"
    stdlib = set(sys.stdlib_module_names)
    check("control_store.py imports only the standard library",
          sorted(imported_modules(store) - stdlib))

    hdmi = pathlib.Path(bootstrap.SUBMODULES["hdmi_capture"])
    bridge = pathlib.Path(bootstrap.SUBMODULES["rpi_gamepad_bridge"])
    check("hdmi_capture does not import the control library",
          [p.relative_to(hdmi).as_posix() for p in submodule_python(hdmi)
           if imported_modules(p) & {"gpb_client"}])
    check("rpi_gamepad_bridge does not import the video library",
          [p.relative_to(bridge).as_posix() for p in submodule_python(bridge)
           if imported_modules(p) & {"vcap"}])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
