#!/usr/bin/env python3
"""controls.gpb: the exact layout, round trip, timestamp search, and crash recovery."""
from __future__ import annotations

import os
import random
import shutil
import struct
import sys
import tempfile

import harness

from episode_recorder.capture import control_store as cs


def raises(fn, exc_type, needle: str = "") -> bool:
    try:
        fn()
    except exc_type as exc:
        return needle in str(exc)
    return False


def main() -> int:
    t = harness.Checks()
    tmp = tempfile.mkdtemp()
    try:
        fmt = cs.BRIDGE_V1_FORMAT
        path = os.path.join(tmp, "controls.gpb")
        rng = random.Random(1234)
        originals = []
        arrival = 10 ** 12
        with cs.ControlWriter(path, struct_fmt=fmt) as w:
            for _ in range(1000):
                state = bytes(rng.getrandbits(8) for _ in range(48))
                arrival += rng.choice([0, 1, 8_000_000, 20_000_000])
                flags = rng.choice([0, 0, 0, cs.FLAG_SEQ_GAP, cs.FLAG_STALE])
                w.write(state, arrival, flags)
                originals.append((state, arrival, flags))

        with open(path, "rb") as fh:
            raw = fh.read()
        t.equal("a 64-byte header then 64-byte records", len(raw), 64 + 64 * 1000)
        t.equal("magic ERC1 at 0", raw[0:4], b"ERC1")
        t.equal("version u16 at 4", struct.unpack_from("<H", raw, 4)[0], 1)
        t.equal("record_size u16 at 6", struct.unpack_from("<H", raw, 6)[0], 64)
        t.equal("the bridge's struct format, NUL-padded, at 8", raw[8:40],
                fmt.encode().ljust(32, b"\0"))
        t.equal("reserved header bytes are zero", raw[40:64], bytes(24))
        rec1 = raw[128:192]
        t.equal("record: state[48], arrival_ns u64, flags u32, reserved u32",
                (rec1[:48], struct.unpack_from("<QII", rec1, 48)),
                (originals[1][0], (originals[1][1], originals[1][2], 0)))

        arrivals = [a for _, a, _ in originals]
        with cs.ControlReader(path) as r:
            t.equal("reader count", len(r), 1000)
            t.equal("reader reports the struct format", r.struct_fmt, fmt)
            t.equal("every record round-trips",
                    [i for i in range(1000) if (r[i].state, r[i].arrival_ns, r[i].flags) != originals[i]],
                    [])
            t.equal("negative indexing", r[-1].arrival_ns, arrivals[-1])

            def expected(ts):
                return max((j for j in range(1000) if arrivals[j] <= ts), default=-1)
            probes = [arrivals[i] + d for i in rng.sample(range(1000), 60) for d in (-1, 0, 1)]
            wrong = [(ts, r.search(ts), expected(ts)) for ts in probes if r.search(ts) != expected(ts)]
            t.equal("search returns the last record at or before a timestamp", wrong, [])
            t.equal("search before the first record", r.search(arrivals[0] - 1), -1)
            t.equal("search after the last record", r.search(arrivals[-1] + 10 ** 9), 999)

        # Crash safety: a writer killed mid-append leaves a partial record.
        with open(path, "r+b") as fh:
            fh.truncate(64 + 64 * 573 + 23)
        with cs.ControlReader(path) as r:
            t.equal("a file truncated mid-record reads every whole record", len(r), 573)
            t.equal("and reports the fragment", r.truncated_bytes, 23)
            t.equal("records before the cut are intact",
                    [(x.state, x.arrival_ns, x.flags) for x in r], originals[:573])

        header_only = os.path.join(tmp, "empty.gpb")
        cs.ControlWriter(header_only, struct_fmt=fmt).close()
        with cs.ControlReader(header_only) as r:
            t.equal("a header-only file is a valid empty stream", (len(r), r.search(10 ** 12)), (0, -1))

        def corrupt(name: str, data: bytes) -> str:
            p = os.path.join(tmp, name)
            with open(p, "wb") as fh:
                fh.write(data)
            return p

        t.check("a partial header is a FormatError",
                raises(lambda: cs.ControlReader(corrupt("short.gpb", raw[:40])), cs.FormatError, "header"))
        t.check("a wrong magic is a FormatError",
                raises(lambda: cs.ControlReader(corrupt("magic.gpb", b"XXXX" + raw[4:200])),
                       cs.FormatError, "magic"))
        t.check("an unknown version is refused, not guessed at",
                raises(lambda: cs.ControlReader(corrupt("v2.gpb", raw[:4] + struct.pack("<H", 2) + raw[6:200])),
                       cs.FormatError, "version 2"))
        t.check("a record size that does not match the version is refused",
                raises(lambda: cs.ControlReader(corrupt("size.gpb", raw[:6] + struct.pack("<H", 128) + raw[8:200])),
                       cs.FormatError, "128"))
        t.check("the writer refuses to reopen an existing file (raw is never rewritten)",
                raises(lambda: cs.ControlWriter(path, struct_fmt=fmt), FileExistsError))
        t.equal("flag names", cs.flag_names(cs.FLAG_SEQ_GAP | cs.FLAG_MALFORMED),
                ["seq_gap", "malformed"])
    finally:
        shutil.rmtree(tmp)
    return t.exit_code()


if __name__ == "__main__":
    sys.exit(main())
