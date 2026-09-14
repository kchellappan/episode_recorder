"""The control receiver: recvfrom, stamp, append. Nothing else.

This is the path composition.md warns about. Every datagram costs a clock read, an HMAC
check, a sequence comparison and a buffered write. There is no parsing beyond the sequence
number, no pairing and no disk flush. Flushing happens on a timer, on another thread.

The arrival stamp is taken the moment recvfrom returns, on this machine's CLOCK_MONOTONIC.
That, not the Pi's timestamp inside the state, is what pairs with video. See
docs/timebase.md.

What the sequence number can and cannot tell you (read from rpi_gamepad_bridge's source,
not measured):

  * The bridge publishes once per source read, not on its heartbeat. A read that coalesced
    several input reports publishes only the last one, and seq advances by one. So a
    FLAG_SEQ_GAP means datagrams were lost between the Pi and here, but intermediate
    states the bridge never published leave no gap at all.
  * Published capture is fire-and-forget. A lost press is not re-sent until the input
    changes again.

The receiver runs in its own process (ReceiverProcess), so a stalled video path cannot
starve it of the interpreter, and a crash in one cannot take the other down.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import signal
import socket
import struct
import threading
import time
import traceback

from .. import bootstrap

bootstrap.require("gpb_client")

from gpb_client.auth import TAG_BYTES, verify  # noqa: E402
from gpb_client.state import FORMAT as STATE_FORMAT  # noqa: E402
from gpb_client.state import MAGIC as STATE_MAGIC  # noqa: E402
from gpb_client.state import SIZE as STATE_SIZE  # noqa: E402
from gpb_client.state import VERSION as STATE_VERSION  # noqa: E402

from .control_store import (ARRIVAL_OFFSET, FLAG_HMAC_FAIL, FLAG_MALFORMED,  # noqa: E402
                            FLAG_SEQ_GAP, FLAG_SEQ_RESTART, FLAG_STALE, RECORD_SIZE,
                            SEQ_OFFSET, ControlWriter)

# A sequence that falls back further than this is a restarted source, not a late datagram.
# On a direct cable, reordering is a handful of datagrams at most.
RESTART_THRESHOLD = 64

# How long recvfrom waits before checking for a stop request.
POLL_S = 0.1

COUNTER_FIELDS = (
    "received", "seq_gaps", "seq_lost", "stale", "restarts", "hmac_failures",
    "malformed", "last_malformed_size", "first_arrival_ns", "last_arrival_ns",
)

_HEAD = struct.Struct("<IH")
_SEQ = struct.Struct("<I")
_TAIL = struct.Struct("<QII")


class ReceiverError(RuntimeError):
    pass


def open_socket(host: str, port: int, *, rcvbuf_bytes: int,
                timeout_s: float = POLL_S) -> tuple[socket.socket, dict]:
    """A bound UDP socket plus what the kernel actually granted.

    No SO_REUSEADDR. gpb_client's CaptureReceiver sets it, and on Linux that lets a second
    process bind the same UDP port. Unicast datagrams then go to only one of the two, so
    two recorders started by mistake would split the stream silently. Failing to bind is
    the better outcome.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf_bytes)
        reported = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        sock.bind((host, port))
        sock.settimeout(timeout_s)
    except BaseException:
        sock.close()
        raise
    return sock, {
        "bind": f"{host}:{port}",
        "rcvbuf_requested": rcvbuf_bytes,
        # Linux reports twice the value it applied, and applies at most
        # net.core.rmem_max. Both are recorded, so the doubling is not mistaken for room.
        "rcvbuf_reported": reported,
        "rmem_max": _read_int("/proc/sys/net/core/rmem_max"),
    }


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


class MemorySink:
    """A sink that keeps records in memory, for preflight and tests."""

    def __init__(self):
        self.records: list[bytes] = []

    def append(self, record) -> None:
        self.records.append(bytes(record))


class Receiver:
    """Receives capture datagrams from a bound socket into a sink, until stopped."""

    def __init__(self, sock: socket.socket, sink, *, key: bytes | None):
        self.sock = sock
        self.sink = sink
        self.key = key or None
        self.counters = dict.fromkeys(COUNTER_FIELDS, 0)
        self._have_seq = False
        self._last_seq = 0

    def run(self, stop) -> None:
        sock, sink, counters = self.sock, self.sink, self.counters
        # One byte of headroom, so an oversized datagram shows up as a length mismatch
        # instead of being truncated to a plausible size.
        buf = bytearray(STATE_SIZE + TAG_BYTES + 1)
        view = memoryview(buf)
        record = bytearray(RECORD_SIZE)
        expected = STATE_SIZE + (TAG_BYTES if self.key else 0)

        while not stop.is_set():
            try:
                n, _ = sock.recvfrom_into(buf)
            except TimeoutError:
                continue
            arrival_ns = time.monotonic_ns()

            flags = self._classify(buf, view, n, expected)
            if flags & FLAG_MALFORMED:
                # Never let bytes from the previous datagram leak into this record.
                record[:STATE_SIZE] = bytes(view[:min(n, STATE_SIZE)]).ljust(STATE_SIZE, b"\0")
            else:
                record[:STATE_SIZE] = view[:STATE_SIZE]
            _TAIL.pack_into(record, ARRIVAL_OFFSET, arrival_ns, flags, 0)
            sink.append(record)

            counters["received"] += 1
            if not counters["first_arrival_ns"]:
                counters["first_arrival_ns"] = arrival_ns
            counters["last_arrival_ns"] = arrival_ns

    def _classify(self, buf: bytearray, view: memoryview, n: int, expected: int) -> int:
        counters = self.counters
        if n != expected:
            counters["malformed"] += 1
            counters["last_malformed_size"] = n
            return FLAG_MALFORMED
        if self.key is not None and verify(self.key, bytes(view[:n]), STATE_SIZE) is None:
            counters["hmac_failures"] += 1
            return FLAG_HMAC_FAIL
        magic, version = _HEAD.unpack_from(buf, 0)
        if magic != STATE_MAGIC or version != STATE_VERSION:
            counters["malformed"] += 1
            counters["last_malformed_size"] = n
            return FLAG_MALFORMED

        # Untrusted and malformed datagrams returned above, so they never move the
        # sequence baseline.
        seq = _SEQ.unpack_from(buf, SEQ_OFFSET)[0]
        if not self._have_seq:
            self._have_seq = True
            self._last_seq = seq
            return 0
        # Wrapped difference, as the bridge's own UdpSource computes it, so a counter
        # wrapping past 2^32 is an ordinary step.
        delta = (seq - self._last_seq) & 0xFFFFFFFF
        if delta >= 0x80000000:
            delta -= 0x100000000
        if delta == 1:
            self._last_seq = seq
            return 0
        if delta > 1:
            counters["seq_gaps"] += 1
            counters["seq_lost"] += delta - 1
            self._last_seq = seq
            return FLAG_SEQ_GAP
        if -delta > RESTART_THRESHOLD:
            counters["restarts"] += 1
            self._last_seq = seq
            return FLAG_SEQ_RESTART
        # Kept and flagged, not dropped. Deciding what a late state means is the builder's
        # job, and it can only decide about records that exist.
        counters["stale"] += 1
        return FLAG_STALE


class _ChildStop:
    """The stop signal inside the receiver process: a shared byte plus a local flag.

    Deliberately not a multiprocessing.Event. An Event is backed by a cross-process lock,
    and a process SIGKILLed while holding it -- which the hot loop's is_set() does on every
    datagram -- leaves it held forever. The parent's next set() then blocks for good. That
    happened in tests/test_receiver.py: the recorder would have hung on a dead receiver
    instead of finalising the session. A single byte needs no lock to write or read.
    """

    def __init__(self, flag):
        self._flag = flag
        self._local = False

    def set(self) -> None:
        self._local = True

    def is_set(self) -> bool:
        return self._local or self._flag.value != 0


def _child_main(path, host, port, key, rcvbuf_bytes, flush_interval_s, shared, stop_flag,
                conn) -> None:
    stop = _ChildStop(stop_flag)
    # The terminal's Ctrl-C reaches the whole process group. The parent decides when
    # recording stops, so the child ignores SIGINT and treats SIGTERM as a stop request.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    parent = os.getppid()

    try:
        sock, info = open_socket(host, port, rcvbuf_bytes=rcvbuf_bytes)
    except OSError as exc:
        conn.send({"error": f"cannot bind {host}:{port}: {exc}"})
        return
    try:
        writer = ControlWriter(path, struct_fmt=STATE_FORMAT)
    except OSError as exc:
        sock.close()
        conn.send({"error": f"cannot create {path}: {exc}"})
        return

    rx = Receiver(sock, writer, key=key)

    def publish() -> None:
        for i, name in enumerate(COUNTER_FIELDS):
            shared[i] = rx.counters[name]

    def flusher() -> None:
        while not stop.is_set():
            time.sleep(flush_interval_s)
            writer.flush()
            publish()
            # A parent killed with SIGKILL cannot tell this process to stop. Left running,
            # it would hold the port and block the next session from binding it.
            if os.getppid() != parent:
                stop.set()

    thread = threading.Thread(target=flusher, name="er-controls-flush", daemon=True)
    thread.start()
    conn.send({"ready": True, "socket": info, "pid": os.getpid()})

    error = None
    try:
        rx.run(stop)
    except BaseException:  # noqa: BLE001 - reported to the parent, not swallowed
        error = traceback.format_exc()
    finally:
        stop.set()
        thread.join(timeout=flush_interval_s + 1.0)
        try:
            writer.close()
        finally:
            sock.close()
            publish()
            try:
                conn.send({"final": dict(rx.counters), "error": error})
            except OSError:
                pass


class ReceiverProcess:
    """A Receiver writing controls.gpb in a child process.

    Counters are published to shared memory each flush interval, so snapshot() is at most
    that stale. It is for a watchdog, not a hot path.
    """

    def __init__(self, path: str, *, host: str, port: int, key: bytes | None,
                 rcvbuf_bytes: int, flush_interval_s: float):
        ctx = mp.get_context("spawn")
        self.path = path
        # Both shared objects are lock-free on purpose; see _ChildStop.
        self._shared = ctx.RawArray("q", len(COUNTER_FIELDS))
        self._stop_flag = ctx.RawValue("b", 0)
        self._conn, child_conn = ctx.Pipe(duplex=False)
        self._child_conn = child_conn
        self._proc = ctx.Process(
            target=_child_main, name="er-receiver", daemon=True,
            args=(path, host, port, key, rcvbuf_bytes, flush_interval_s, self._shared,
                  self._stop_flag, child_conn))
        self.socket_info: dict | None = None
        self.pid: int | None = None
        self.final: dict | None = None

    def start(self, timeout: float = 15.0) -> dict:
        self._proc.start()
        self._child_conn.close()
        try:
            if not self._conn.poll(timeout):
                raise ReceiverError(f"control receiver did not start within {timeout:g}s")
            msg = self._conn.recv()
        except EOFError:
            msg = {"error": f"control receiver exited during startup "
                            f"(exit code {self._proc.exitcode})"}
        except ReceiverError:
            self._proc.kill()
            self._proc.join()
            raise
        if not msg.get("ready"):
            self._proc.join(5.0)
            raise ReceiverError(msg.get("error", "control receiver failed to start"))
        self.socket_info = msg["socket"]
        self.pid = msg["pid"]
        return self.socket_info

    @property
    def started(self) -> bool:
        return self.pid is not None

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def snapshot(self) -> dict:
        return dict(zip(COUNTER_FIELDS, self._shared[:]))

    def stop(self, timeout: float = 10.0) -> dict:
        self._stop_flag.value = 1
        counters, error = None, None
        try:
            if self._conn.poll(timeout):
                msg = self._conn.recv()
                counters, error = msg.get("final"), msg.get("error")
        except Exception:  # noqa: BLE001 - a child killed mid-send leaves a partial message
            pass
        self._proc.join(timeout)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join()
            error = (error or "") + "control receiver did not exit when asked; killed"
        if counters is None:
            counters = self.snapshot()
            error = error or (
                f"control receiver exited without a final report (exit code "
                f"{self._proc.exitcode}); counters are its last published snapshot")
        self.final = {"counters": counters, "error": error, "exitcode": self._proc.exitcode}
        return self.final
