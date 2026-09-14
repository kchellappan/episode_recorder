"""Shared scaffolding for the hardware-free suite.

Each test file is a script. It prints one line per check and exits non-zero if any failed,
so run_tests.sh needs no test framework.
"""
from __future__ import annotations

import pathlib
import socket
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import episode_recorder  # noqa: E402,F401  -- puts the pinned submodules on sys.path


class Checks:
    def __init__(self):
        self.failed = 0
        self.passed = 0

    def check(self, name: str, ok, detail="") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}")
            if detail != "":
                print(f"        {detail}")
        return bool(ok)

    def equal(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got {got!r}, want {want!r}")

    def exit_code(self) -> int:
        return 1 if self.failed else 0


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
