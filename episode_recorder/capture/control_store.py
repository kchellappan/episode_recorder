"""controls.gpb: the control stream on disk.

A 64-byte header, then fixed 64-byte records, appended and never rewritten. Fixed width is
what matters: record N is at byte 64 + 64*N, so finding the controls around a frame's
timestamp is a binary search over an mmap, the same access vcap's .idx gives the video.
docs/formats.md is the specification. This module is its only implementation.

Each record holds the bridge's 48-byte GamepadState exactly as it arrived, unparsed, plus
two things only the receiver knows: when it arrived on this machine's CLOCK_MONOTONIC, and
what was wrong with it.

Standard library only, and no vcap or gpb_client, so a machine with neither can still read
a session's controls.
"""
from __future__ import annotations

import bisect
import mmap
import os
import struct
from dataclasses import dataclass

MAGIC = b"ERC1"
VERSION = 1

# magic, version, record_size, struct_fmt (NUL-padded), 24 reserved bytes.
HEADER = struct.Struct("<4sHH32s24x")
HEADER_SIZE = HEADER.size
assert HEADER_SIZE == 64, HEADER_SIZE

# state (GamepadState, verbatim), arrival_ns (ts_rig_mono_ns), flags, reserved.
RECORD = struct.Struct("<48sQII")
RECORD_SIZE = RECORD.size
assert RECORD_SIZE == 64, RECORD_SIZE
STATE_SIZE = 48
ARRIVAL_OFFSET = 48
FLAGS_OFFSET = 56

# Record sizes each version's reader expects. A format change is a new entry here, never
# a migration of files already written.
RECORD_SIZE_BY_VERSION = {1: 64}

FLAG_SEQ_GAP = 1 << 0       # sequence jumped forward: datagrams lost before this one
FLAG_HMAC_FAIL = 1 << 1     # tag did not verify; the state bytes are untrusted
FLAG_STALE = 1 << 2         # sequence did not advance (late, reordered or duplicated)
FLAG_SEQ_RESTART = 1 << 3   # sequence fell far back: the publishing source restarted
FLAG_MALFORMED = 1 << 4     # wrong length, magic or version; state is zero-padded bytes

FLAG_NAMES = {
    FLAG_SEQ_GAP: "seq_gap",
    FLAG_HMAC_FAIL: "hmac_fail",
    FLAG_STALE: "stale",
    FLAG_SEQ_RESTART: "seq_restart",
    FLAG_MALFORMED: "malformed",
}

# Where the bridge's v1 GamepadState keeps the two fields read without a full parse. Its
# layout is a published ABI (append, never reorder), and tests/test_receiver.py pins these
# against gpb_client's own pack().
BRIDGE_V1_FORMAT = "<IHHIIQQhhhhBB6s"
SEQ_OFFSET = 8
PI_MONO_OFFSET = 16

_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")


class FormatError(ValueError):
    """The file is not a controls.gpb this build can read."""


def pack_header(struct_fmt: str) -> bytes:
    fmt = struct_fmt.encode("ascii")
    if len(fmt) > 32:
        raise ValueError(f"struct_fmt {struct_fmt!r} does not fit in 32 bytes")
    return HEADER.pack(MAGIC, VERSION, RECORD_SIZE, fmt)


def pack_record(state: bytes, arrival_ns: int, flags: int) -> bytes:
    if len(state) != STATE_SIZE:
        raise ValueError(f"state is {len(state)} bytes, expected {STATE_SIZE}")
    return RECORD.pack(state, arrival_ns, flags, 0)


@dataclass(frozen=True)
class ControlRecord:
    index: int
    state: bytes
    arrival_ns: int      # ts_rig_mono_ns: the only control timestamp that pairs with video
    flags: int

    @property
    def trusted(self) -> bool:
        """Whether the state bytes came from the bridge intact. Stale and gap flags do
        not affect this: those records are genuine, just out of order or after a loss."""
        return not self.flags & (FLAG_HMAC_FAIL | FLAG_MALFORMED)

    @property
    def seq(self) -> int:
        return _U32.unpack_from(self.state, SEQ_OFFSET)[0]

    @property
    def ts_pi_mono_ns(self) -> int:
        """The Pi's CLOCK_MONOTONIC. A different machine's clock: for jitter analysis
        only, never for pairing with video. See docs/timebase.md."""
        return _U64.unpack_from(self.state, PI_MONO_OFFSET)[0]


def flag_names(flags: int) -> list[str]:
    return [name for bit, name in FLAG_NAMES.items() if flags & bit]


class ControlWriter:
    """Append-only writer. Refuses to open a file that already exists.

    append() moves bytes into this process's buffer. Pushing them to the OS is flush(),
    which the receiver calls on a timer from another thread rather than per record. A
    buffered binary file in CPython serialises writes and flushes with its own lock, so
    those two threads need nothing more.
    """

    def __init__(self, path: str, *, struct_fmt: str):
        self.path = path
        self._fh = open(path, "xb", buffering=64 * 1024)
        self._fh.write(pack_header(struct_fmt))
        # The header is the only way a reader recognises the file, so it is made durable
        # before any record can follow it.
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.records_written = 0

    def append(self, record) -> None:
        self._fh.write(record)
        self.records_written += 1

    def write(self, state: bytes, arrival_ns: int, flags: int = 0) -> None:
        self.append(pack_record(state, arrival_ns, flags))

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if self._fh.closed:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()

    def __enter__(self) -> "ControlWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ControlReader:
    """Random access over a controls.gpb, as of the moment it was opened.

    A trailing partial record means the writer died mid-append. The whole records before
    it are valid, so they are presented and the fragment is reported in `truncated_bytes`
    rather than refusing the file.
    """

    def __init__(self, path: str):
        self.path = path
        self._file = open(path, "rb")
        self._map = None
        try:
            head = self._file.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE:
                raise FormatError(
                    f"{path}: {len(head)} bytes, shorter than the {HEADER_SIZE}-byte header. "
                    f"The session died before its control file was initialised.")
            magic, version, record_size, fmt = HEADER.unpack(head)
            if magic != MAGIC:
                raise FormatError(f"{path}: magic {magic!r}, expected {MAGIC!r}")
            if version not in RECORD_SIZE_BY_VERSION:
                raise FormatError(
                    f"{path}: format version {version}; this build reads "
                    f"{sorted(RECORD_SIZE_BY_VERSION)}")
            if record_size != RECORD_SIZE_BY_VERSION[version]:
                raise FormatError(
                    f"{path}: version {version} declares {record_size}-byte records, "
                    f"expected {RECORD_SIZE_BY_VERSION[version]}")
            self.version = version
            self.record_size = record_size
            self.struct_fmt = fmt.rstrip(b"\0").decode("ascii", "replace")
            size = os.fstat(self._file.fileno()).st_size
            body = size - HEADER_SIZE
            self.count = body // record_size
            self.truncated_bytes = body % record_size
            self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except BaseException:
            self._file.close()
            raise

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        self._file.close()

    def __enter__(self) -> "ControlReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __len__(self) -> int:
        return self.count

    def _offset(self, index: int) -> int:
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        return HEADER_SIZE + index * self.record_size

    def __getitem__(self, index: int) -> ControlRecord:
        at = self._offset(index)
        state, arrival_ns, flags, _ = RECORD.unpack_from(self._map, at)
        return ControlRecord(index % self.count if self.count else index, state,
                             arrival_ns, flags)

    def __iter__(self):
        for i in range(self.count):
            yield self[i]

    def arrival_ns(self, index: int) -> int:
        return _U64.unpack_from(self._map, self._offset(index) + ARRIVAL_OFFSET)[0]

    def flags(self, index: int) -> int:
        return _U32.unpack_from(self._map, self._offset(index) + FLAGS_OFFSET)[0]

    def search(self, ts_rig_mono_ns: int) -> int:
        """Index of the last record that arrived at or before the timestamp; -1 if none.

        Arrival stamps are non-decreasing by construction: one thread takes them from one
        monotonic clock, in the order it receives.
        """
        return bisect.bisect_right(_Arrivals(self), ts_rig_mono_ns) - 1

    def flag_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(FLAG_NAMES.values(), 0)
        for i in range(self.count):
            f = self.flags(i)
            for bit, name in FLAG_NAMES.items():
                if f & bit:
                    counts[name] += 1
        return counts


class _Arrivals:
    """A sequence view of arrival stamps, so bisect can search without a copied list."""

    def __init__(self, reader: ControlReader):
        self._reader = reader

    def __len__(self) -> int:
        return self._reader.count

    def __getitem__(self, index: int) -> int:
        return self._reader.arrival_ns(index)
