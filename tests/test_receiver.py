#!/usr/bin/env python3
"""The receiver against real datagrams on loopback: stamping, sequence classification,
authentication, malformed input, and surviving a SIGKILL."""
from __future__ import annotations

import os
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time

import fakes
import harness
from harness import free_udp_port, wait_until

import gpb_client.state as gpb_state
from gpb_client.auth import sign
from gpb_client.state import GamepadState

from episode_recorder.capture import control_store as cs
from episode_recorder.capture.receiver import (RESTART_THRESHOLD, MemorySink, Receiver,
                                                ReceiverError, ReceiverProcess, open_socket)

KEY = b"test-key"
GAP, STALE, RESTART = cs.FLAG_SEQ_GAP, cs.FLAG_STALE, cs.FLAG_SEQ_RESTART
HMAC, MAL = cs.FLAG_HMAC_FAIL, cs.FLAG_MALFORMED


def receive(key, send, *, sender_key=fakes._DEFAULT, settle=0.2):
    port = free_udp_port()
    sock, info = open_socket("127.0.0.1", port, rcvbuf_bytes=1 << 20)
    sink = MemorySink()
    rx = Receiver(sock, sink, key=key)
    stop = threading.Event()
    thread = threading.Thread(target=rx.run, args=(stop,))
    thread.start()
    sender = fakes.BridgeSender(port, key if sender_key is fakes._DEFAULT else sender_key)
    try:
        send(sender)
        time.sleep(settle)
    finally:
        stop.set()
        thread.join()
        sock.close()
        sender.close()
    return sink.records, rx.counters, info


def flags_of(records):
    return [struct.unpack_from("<I", r, cs.FLAGS_OFFSET)[0] for r in records]


def sends(*seqs):
    return lambda s: [s.send(q) for q in seqs]


def main() -> int:
    t = harness.Checks()

    packed = GamepadState(t_mono_ns=0x1122334455667788).pack(seq=0xDEADBEEF)
    t.equal("seq offset matches the bridge's own pack()",
            struct.unpack_from("<I", packed, cs.SEQ_OFFSET)[0], 0xDEADBEEF)
    t.equal("ts_pi_mono_ns offset matches the bridge's own pack()",
            struct.unpack_from("<Q", packed, cs.PI_MONO_OFFSET)[0], 0x1122334455667788)
    t.equal("the store names the bridge's current struct format",
            gpb_state.FORMAT, cs.BRIDGE_V1_FORMAT)

    sent = []
    t0 = time.monotonic_ns()
    records, counters, info = receive(KEY, lambda s: sent.extend(s.send(q) for q in range(1, 51)))
    t1 = time.monotonic_ns()
    t.equal("every datagram becomes one record", len(records), 50)
    t.equal("a clean stream carries no flags", [f for f in flags_of(records) if f], [])
    t.equal("state bytes are stored verbatim, tag discarded",
            [r[:48] for r in records], [d[:48] for d in sent])
    arrivals = [struct.unpack_from("<Q", r, cs.ARRIVAL_OFFSET)[0] for r in records]
    t.check("arrival stamps are this host's CLOCK_MONOTONIC, taken on receipt",
            all(t0 <= a <= t1 for a in arrivals), (t0, arrivals[:3], t1))
    t.check("arrival stamps never go backwards", arrivals == sorted(arrivals))
    t.check("socket info records the requested and reported buffer",
            info["rcvbuf_requested"] == 1 << 20 and info["rcvbuf_reported"] > 0, info)

    records, counters, _ = receive(KEY, sends(1, 2, 5, 6))
    t.equal("a forward jump flags the record after the gap", flags_of(records), [0, 0, GAP, 0])
    t.equal("and counts what was lost", (counters["seq_gaps"], counters["seq_lost"]), (1, 2))

    # The smallest gap on its own. Mutation testing showed the case above cannot tell a
    # threshold of >1 from >2: only an unrelated test noticed.
    records, counters, _ = receive(KEY, sends(1, 2, 4))
    t.equal("a single lost datagram is a gap",
            (flags_of(records), counters["seq_lost"]), ([0, 0, GAP], 1))

    records, counters, _ = receive(KEY, sends(10, 11, 9, 12))
    t.equal("a late datagram is kept and flagged stale, without moving the baseline",
            flags_of(records), [0, 0, STALE, 0])

    records, _, _ = receive(KEY, sends(10, 10))
    t.equal("a repeated sequence number is stale", flags_of(records), [0, STALE])

    records, counters, _ = receive(KEY, sends(1000, 1001, 1, 2))
    t.equal("a sequence that falls far back is a restart, and the new run is clean",
            flags_of(records), [0, 0, RESTART, 0])
    t.equal("restarts are counted", counters["restarts"], 1)

    records, _, _ = receive(KEY, sends(1000, 1000 - RESTART_THRESHOLD, 1000 - RESTART_THRESHOLD - 1))
    t.equal("the restart threshold is exact", flags_of(records), [0, STALE, RESTART])

    records, _, _ = receive(KEY, sends(0xFFFFFFFE, 0xFFFFFFFF, 0, 1))
    t.equal("a wrapping counter is an ordinary step", flags_of(records), [0, 0, 0, 0])

    records, counters, _ = receive(KEY, lambda s: (s.send(1), s.send(2, key=b"wrong"), s.send(3)))
    t.equal("a bad tag is flagged and does not move the sequence baseline",
            flags_of(records), [0, HMAC, GAP])
    t.equal("hmac failures are counted", counters["hmac_failures"], 1)

    def malformed(s):
        s.send(1)
        s.send(2, key=None)                   # unsigned while a key is configured
        s.send_raw(b"x" * 200)                # oversized
        bad = bytearray(GamepadState().pack(seq=3))
        bad[0:4] = b"XXXX"
        s.send_raw(sign(KEY, bytes(bad)))     # authentic, but not a GamepadState
        s.send(4)
    records, counters, _ = receive(KEY, malformed)
    t.equal("wrong length, oversize and wrong magic are flagged malformed",
            flags_of(records), [0, MAL, MAL, MAL, GAP])
    t.equal("malformed datagrams are counted with the last size seen",
            (counters["malformed"], counters["last_malformed_size"]), (3, 64))

    records, _, _ = receive(KEY, lambda s: (s.send(1, buttons=0xFFFF, lx=-1),
                                            s.send_raw(b"\x01" * 10)))
    t.check("a short datagram is zero-padded, with nothing left over from the previous one",
            records[1][:10] == b"\x01" * 10 and records[1][10:48] == bytes(38), records[1][:48])

    records, _, _ = receive(None, lambda s: (s.send(1), s.send(2, key=KEY)))
    t.equal("with authentication off, a signed datagram is the wrong length",
            flags_of(records), [0, MAL])

    port = free_udp_port()
    held, _ = open_socket("127.0.0.1", port, rcvbuf_bytes=1 << 16)
    try:
        open_socket("127.0.0.1", port, rcvbuf_bytes=1 << 16)[0].close()
        t.check("a second receiver on the same port fails to bind (no SO_REUSEADDR)", False)
    except OSError:
        t.check("a second receiver on the same port fails to bind (no SO_REUSEADDR)", True)
    held.close()

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "controls.gpb")
        port = free_udp_port()
        proc = ReceiverProcess(path, host="127.0.0.1", port=port, key=KEY,
                               rcvbuf_bytes=1 << 20, flush_interval_s=0.05)
        proc.start()
        sender = fakes.BridgeSender(port, KEY)
        for q in range(1, 201):
            sender.send(q)
            if q % 50 == 0:
                time.sleep(0.01)
        t.check("the published snapshot catches up with what was sent",
                wait_until(lambda: proc.snapshot()["received"] == 200, 3.0), proc.snapshot())
        final = proc.stop()
        t.equal("the child reports final counters without error",
                (final["counters"]["received"], final["error"]), (200, None))
        with cs.ControlReader(path) as r:
            t.equal("the child process wrote every record, unflagged",
                    (len(r), sum(r.flag_counts().values())), (200, 0))

        port = free_udp_port()
        blocker, _ = open_socket("127.0.0.1", port, rcvbuf_bytes=1 << 16)
        busy = ReceiverProcess(os.path.join(tmp, "busy.gpb"), host="127.0.0.1", port=port,
                               key=KEY, rcvbuf_bytes=1 << 16, flush_interval_s=0.05)
        try:
            busy.start()
            t.check("a port in use fails start() loudly", False)
        except ReceiverError as exc:
            t.check("a port in use fails start() loudly", "cannot bind" in str(exc), str(exc))
        blocker.close()

        # Crash safety end to end: SIGKILL gives the child no chance to flush on exit.
        path = os.path.join(tmp, "killed.gpb")
        port = free_udp_port()
        victim = ReceiverProcess(path, host="127.0.0.1", port=port, key=KEY,
                                 rcvbuf_bytes=1 << 20, flush_interval_s=0.05)
        victim.start()
        sender.close()
        sender = fakes.BridgeSender(port, KEY)
        for q in range(1, 101):
            sender.send(q)
        # The snapshot is published right after a flush, so seeing 100 means 100 are
        # with the OS -- then wait one more interval to be past any in-progress flush.
        t.check("the receiver about to be killed has received everything",
                wait_until(lambda: victim.snapshot()["received"] == 100, 3.0), victim.snapshot())
        time.sleep(0.1)
        os.kill(victim.pid, signal.SIGKILL)
        victim.stop(timeout=5.0)
        with cs.ControlReader(path) as r:
            t.equal("after SIGKILL, every record flushed before the kill is readable",
                    (len(r), r.truncated_bytes), (100, 0))
        sender.close()
    finally:
        shutil.rmtree(tmp)
    return t.exit_code()


if __name__ == "__main__":
    sys.exit(main())
