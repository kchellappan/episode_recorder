"""events.log: what happened during a session, one JSON object per line.

Anything a person would want to know afterwards goes here as it happens, not just in the
final manifest: a watchdog warning, a receiver that died, the reason recording stopped. A
session killed before it finalised still has this file up to its last line.
"""
from __future__ import annotations

import json
import threading
import time


class EventLog:
    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "x", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def log(self, event: str, *, level: str = "info", **detail) -> None:
        entry = {"ts_rig_mono_ns": time.monotonic_ns(), "ts_realtime_ns": time.time_ns(),
                 "level": level, "event": event}
        entry.update(detail)
        line = json.dumps(entry, default=str)
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_events(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.endswith("\n"):
                out.append(json.loads(line))
    return out
