"""Marks: an operator's notes against the rig clock, in marks.jsonl.

A mark is a hint, not a boundary. Recording runs continuously and episodes are cut
offline, so a mark a second late costs nothing. That is why marks are timestamped when
the recorder sees them rather than by any cleverer means.

Mark sources (browser buttons, a foot pedal, a controller chord) come later. This module
defines what they produce and how it is stored.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

KINDS = ("episode_start", "episode_end", "discard_last", "flag", "note")


@dataclass(frozen=True)
class Mark:
    ts_rig_mono_ns: int
    kind: str
    payload: dict = field(default_factory=dict)

    @classmethod
    def now(cls, kind: str, payload: dict | None = None) -> "Mark":
        return cls(time.monotonic_ns(), kind, payload or {})


class MarkSource(Protocol):
    def poll(self) -> Iterable[Mark]: ...


class MarksWriter:
    """Appends one JSON object per line. Refuses to open an existing file."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "x", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.written = 0

    def write(self, mark: Mark) -> None:
        if mark.kind not in KINDS:
            raise ValueError(f"unknown mark kind {mark.kind!r}; expected one of {KINDS}")
        line = json.dumps({"ts_rig_mono_ns": mark.ts_rig_mono_ns, "kind": mark.kind,
                           "payload": mark.payload}, sort_keys=True)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self.written += 1

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_marks(path: str) -> tuple[list[Mark], list[str]]:
    """Every complete mark, plus a description of each line that could not be read.

    A final line without a newline is what a crash mid-write leaves. It is reported and
    skipped rather than failing the whole file.
    """
    marks: list[Mark] = []
    problems: list[str] = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    trailing = lines.pop()  # "" when the file ends with a newline
    if trailing:
        problems.append(f"line {len(lines) + 1}: incomplete final line (no newline), skipped")
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            marks.append(Mark(int(obj["ts_rig_mono_ns"]), str(obj["kind"]),
                              dict(obj.get("payload") or {})))
        except (ValueError, KeyError, TypeError) as exc:
            problems.append(f"line {number}: {exc}")
    return marks, problems
