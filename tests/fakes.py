"""Stand-ins for the card and the bridge, so capture is testable with neither.

The bridge side is real: datagrams are built with gpb_client's own pack() and sign() and
sent over loopback UDP, so the receiver is exercised against the exact bytes the bridge's
Python client produces. The video side writes through vcap's own SegmentWriter and
manifest, so what a session reads back is a genuine vcap recording.

What this cannot stand in for: V4L2, the kernel's frame timestamps, a real network, and
the bridge's C++ publisher. tests/hardware.sh covers those.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import harness  # noqa: F401

from gpb_client.auth import sign
from gpb_client.state import GamepadState
from vcap import manifest as vcap_manifest
from vcap.frame import FLAG_CORRUPT, Frame
from vcap.source import NoSignal
from vcap.writer import SegmentWriter

HORIPAD_AXES = ["lx", "ly", "rx", "ry"]
HORIPAD_BUTTONS = ["south", "east", "west", "north", "l1", "r1", "l2", "r2", "select",
                   "start", "l3", "r3", "guide", "misc1", "dup", "ddown", "dleft", "dright"]

_DEFAULT = object()


# ----------------------------------------------------------------------------- bridge

class BridgeSender:
    def __init__(self, port: int, key: bytes | None = None, host: str = "127.0.0.1"):
        self.addr = (host, port)
        self.key = key
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def datagram(self, seq: int, *, key=_DEFAULT, buttons: int = 0, lx: int = 0) -> bytes:
        k = self.key if key is _DEFAULT else key
        return sign(k, GamepadState(buttons=buttons, lx=lx).pack(seq=seq))

    def send(self, seq: int, **kw) -> bytes:
        data = self.datagram(seq, **kw)
        self.sock.sendto(data, self.addr)
        return data

    def send_raw(self, data: bytes) -> None:
        self.sock.sendto(data, self.addr)

    def close(self) -> None:
        self.sock.close()


class StreamingSender(threading.Thread):
    """Publishes at a fixed rate with an advancing sequence, like a bridge under a moving
    stick. pause() simulates an untouched pad or a dead link: indistinguishable here too."""

    def __init__(self, port: int, *, key: bytes | None = None, rate_hz: float = 125.0,
                 start_seq: int = 1, repeat_seq: bool = False):
        super().__init__(daemon=True)
        self.sender = BridgeSender(port, key)
        self.interval = 1.0 / rate_hz
        self.seq = start_seq
        self.repeat_seq = repeat_seq
        self.sent = 0
        self._halt = threading.Event()
        self._paused = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            if not self._paused.is_set():
                self.sender.send(self.seq, lx=self.seq % 32767)
                self.sent += 1
                if not self.repeat_seq:
                    self.seq += 1
            time.sleep(self.interval)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2.0)
        self.sender.close()


class FakeCaps:
    def __init__(self, *, ok: bool = True, trigger_mode: str = "digital",
                 axes=HORIPAD_AXES, buttons=HORIPAD_BUTTONS):
        self.ok = ok
        self.sink = "ns_hid"
        self.target = "HORIPAD for Nintendo Switch"
        self.trigger_mode = trigger_mode
        self.axes = list(axes)
        self.buttons = list(buttons)
        self.raw = {"sink": self.sink, "target": self.target, "trigger_mode": trigger_mode,
                    "axes": self.axes, "buttons": self.buttons, "notes": "fake"}

    def __bool__(self) -> bool:
        return self.ok


# ------------------------------------------------------------------------------ video

def make_jpeg(payload: int = 120, *, headless: bool = False) -> bytes:
    """Structurally a JPEG (SOI, SOF0, filler, EOI), as vcap's checks read one. Headless
    reproduces the card's first frame after STREAMON: the back half of a frame."""
    out = bytearray(b"\xff\xd8\xff\xc0\x00\x11\x08")
    out += (48).to_bytes(2, "big") + (64).to_bytes(2, "big")
    out += b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    body = b"\x00" * payload
    out += b"\xff\xfe" + (len(body) + 2).to_bytes(2, "big") + body + b"\xff\xd9"
    return bytes(out[len(out) // 2:]) if headless else bytes(out)


class FakeDevice:
    def __init__(self, path: str = "/dev/v4l/by-id/usb-fake_capture-video-index0"):
        self.path = path
        self.node = "/dev/video99"

    def identity(self) -> dict:
        return {"path": self.path, "node_at_capture": self.node, "driver": "fake",
                "card": "fake capture", "bus_info": "fake", "usb": None}


class FakeNegotiated:
    def __init__(self, *, fps: float = 30.0, monotonic: bool = True):
        self.pixelformat = "MJPG"
        self.width, self.height = 64, 48
        self.sizeimage = 0
        self.fps_requested = self.fps_granted = fps
        self.timestamp_clock = "monotonic" if monotonic else "realtime"
        self.timestamp_source = "start-of-frame"

    @property
    def timestamps_are_monotonic(self) -> bool:
        return self.timestamp_clock == "monotonic"


def _paced(fps: float):
    period = 1.0 / fps
    due = time.monotonic() + period
    while True:
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        due += period
        yield time.monotonic_ns()


class FakeVideoSource:
    """What preflight reads: vcap.VideoSource's context manager, negotiated and frames()."""

    def __init__(self, *, fps: float = 30.0, no_signal: bool = False, monotonic: bool = True,
                 corrupt_all: bool = False):
        self.negotiated = FakeNegotiated(fps=fps, monotonic=monotonic)
        self.fps = fps
        self.no_signal = no_signal
        self.corrupt_all = corrupt_all

    def __enter__(self) -> "FakeVideoSource":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def frames(self, timeout: float = 2.0):
        if self.no_signal:
            raise NoSignal("fake: nothing connected")
        for i, ts in enumerate(_paced(self.fps)):
            bad = i == 0 or self.corrupt_all
            yield Frame(make_jpeg(headless=bad), ts, i, FLAG_CORRUPT if bad else 0)


class FakeVideoSession:
    """What record_session drives: vcap.Session's surface, writing a real vcap recording."""

    def __init__(self, directory: str, *, notes: dict, fps: float = 30.0,
                 stall_after: int | None = None):
        self.directory = directory
        self.notes = notes
        self.fps = fps
        self.stall_after = stall_after
        self.manifest: dict | None = None
        self.count = 0

    def __enter__(self) -> "FakeVideoSession":
        self._writer = SegmentWriter(self.directory, "session", segment_bytes=0)
        self.manifest = vcap_manifest.build(
            session="session", device=FakeDevice(), negotiated=FakeNegotiated(fps=self.fps),
            requested={"pixelformat": "MJPG", "width": 64, "height": 48, "fps": self.fps},
            stream_file="session.mjpg", index_file="session.idx", notes=self.notes)
        self.manifest["segments"] = self._writer.segments
        vcap_manifest.write(os.path.join(self.directory, "manifest.json"), self.manifest)
        return self

    def frames(self):
        for ts in _paced(self.fps):
            if self.stall_after is not None and self.count >= self.stall_after:
                raise NoSignal("fake: signal lost")
            first = self.count == 0
            frame = Frame(make_jpeg(headless=first), ts, self.count,
                          FLAG_CORRUPT if first else 0)
            self._writer.write(frame)
            self.count += 1
            yield frame

    def stats(self) -> dict:
        return {"frames": self.count, "driver_dropped": 0, "driver_errors": 0,
                "corrupt": min(self.count, 1)}

    def __exit__(self, *exc) -> None:
        self._writer.close()
        vcap_manifest.finalize(self.manifest, stats=self.stats(),
                               frames_written=self._writer.frames_written,
                               bytes_written=self._writer.bytes_written)
        self.manifest["segments"] = self._writer.segments
        vcap_manifest.write(os.path.join(self.directory, "manifest.json"), self.manifest)


def video_factory(**kw):
    return lambda directory, *, notes: FakeVideoSession(directory, notes=notes, **kw)
