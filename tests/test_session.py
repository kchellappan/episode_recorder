#!/usr/bin/env python3
"""A whole session, end to end, without hardware: layout, ordering, both streams on one
clock, and each way a session ends -- limit, signal, lost video, dead receiver, busy port,
and the recorder itself being killed."""
from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import fakes
import harness
from harness import free_udp_port, port_is_free, wait_until

from episode_recorder import config as config_mod
from episode_recorder.capture import session as sess
from episode_recorder.capture.control_store import ControlReader
from episode_recorder.capture.events import read_events
from episode_recorder.capture.marks import Mark, read_marks
from vcap import Recording

KEY = b"session-key-4f1c9a"
DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_driver.py")


class OneMark:
    def __init__(self):
        self.done = False

    def poll(self):
        if self.done:
            return []
        self.done = True
        return [Mark.now("flag", {"why": "test"})]


def profile(tmp: str, port: int, *, quiet_ms: int = 1000) -> dict:
    cfg = config_mod.load()
    key_file = os.path.join(tmp, "bridge.key")
    with open(key_file, "wb") as fh:
        fh.write(KEY + b"\n")
    cfg["capture"].update(control_bind=f"127.0.0.1:{port}", flush_interval_ms=50,
                          control_quiet_warn_ms=quiet_ms, socket_rcvbuf_bytes=1 << 20,
                          control_key_file=key_file)
    return cfg


def run(tmp: str, name: str, *, quiet_ms=1000, sender_kw=None, **kw):
    port = free_udp_port()
    cfg = profile(tmp, port, quiet_ms=quiet_ms)
    sender = fakes.StreamingSender(port, key=KEY, **(sender_kw or {}))
    sender.start()
    kw.setdefault("video_factory", fakes.video_factory(fps=30))
    try:
        started = time.monotonic()
        result = sess.record_session(cfg, data_root=os.path.join(tmp, name),
                                     key=config_mod.read_key(cfg), **kw)
        return result, time.monotonic() - started, sender
    finally:
        sender.stop()


def event_names(directory: str) -> list[str]:
    return [e["event"] for e in read_events(os.path.join(directory, "events.log"))]


def main() -> int:
    t = harness.Checks()
    tmp = tempfile.mkdtemp()
    try:
        # ------------------------------------------------------------ a normal session
        seen = {}

        def on_started(ctx):
            with open(os.path.join(ctx.directory, "manifest.json")) as fh:
                seen["manifest"] = json.load(fh)

        result, _, _ = run(tmp, "normal", max_seconds=1.5, mark_sources=[OneMark()],
                           on_started=on_started, device_identity=fakes.FakeDevice().identity(),
                           bridge_capabilities=fakes.FakeCaps().raw,
                           preflight={"passed": True, "checks": []},
                           session_labels={"operator": "test"})
        m0 = seen.get("manifest", {})
        t.check("the manifest identifies the session before video starts",
                m0.get("session_id") == result.session_id and "ended" not in m0, m0.keys())
        t.check("the manifest records each submodule's commit",
                all(v.get("commit") for v in m0.get("submodules", {}).values()), m0.get("submodules"))
        t.equal("assumed_capture_offset_ns is null until someone measures it",
                m0.get("assumed_capture_offset_ns", "absent"), None)
        t.equal("the session ends at its time limit, cleanly",
                (result.stop_reason, result.clean), ("max_session_seconds", True))
        t.check("the session id has no colons (exFAT and NTFS reject them)",
                ":" not in result.session_id, result.session_id)
        d = result.directory
        t.equal("the session directory layout",
                sorted(os.listdir(d)),
                ["controls.gpb", "events.log", "manifest.json", "marks.jsonl", "preflight.json", "video"])

        with Recording(os.path.join(d, "video")) as rec, ControlReader(os.path.join(d, "controls.gpb")) as ctl:
            n_controls = len(ctl)
            t.check("video was recorded", len(rec) >= 30, len(rec))
            t.equal("the video manifest carries the session id",
                    rec.manifest.get("notes", {}).get("session_id"), result.session_id)
            t.check("controls were recorded", n_controls >= 100, n_controls)
            t.equal("a clean stream leaves no control flags", sum(ctl.flag_counts().values()), 0)
            first_frame, last_frame = rec.entry(0).ts_mono_ns, rec.entry(-1).ts_mono_ns
            t.check("controls start before the first frame", ctl.arrival_ns(0) < first_frame)
            t.check("and end after the last", ctl.arrival_ns(-1) > last_frame)
            mid = rec.entry(len(rec) // 2).ts_mono_ns
            i = ctl.search(mid)
            t.check("a control arrived within 20 ms before a mid-session frame (one clock)",
                    i >= 0 and mid - ctl.arrival_ns(i) < 20_000_000, (i, mid))

        ended = result.manifest["ended"]
        t.equal("the final manifest counts every control record",
                ended["controls"]["counters"]["received"], n_controls)
        t.equal("the config is inlined", result.manifest["config"]["capture"]["flush_interval_ms"], 50)
        t.equal("authentication is recorded without the key",
                result.manifest["control_auth"], "hmac-sha256-trunc16")
        leaked = [p for p in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                  if os.path.isfile(p) and KEY in open(p, "rb").read()]
        t.equal("the HMAC key appears in no session file", leaked, [])
        names = event_names(d)
        t.check("events.log records the session's course",
                all(n in names for n in ("session_created", "controls_started", "video_started",
                                         "mark", "video_stopped", "controls_stopped",
                                         "session_finalized")), names)
        marks, problems = read_marks(os.path.join(d, "marks.jsonl"))
        t.check("a polled mark is appended to marks.jsonl",
                len(marks) == 1 and marks[0].kind == "flag" and not problems, (marks, problems))
        t.equal("and counted", ended["marks_written"], 1)
        t.equal("no quiet spans on a continuous stream", ended["controls_quiet_spans"], [])

        # ------------------------------------------------ quiet controls warn, not abort
        holder = {}

        def pause_later(_ctx):
            threading.Timer(0.4, holder["sender"].pause).start()
            threading.Timer(1.4, holder["sender"].resume).start()

        port = free_udp_port()
        cfg = profile(tmp, port, quiet_ms=300)
        holder["sender"] = fakes.StreamingSender(port, key=KEY)
        holder["sender"].start()
        try:
            result = sess.record_session(cfg, data_root=os.path.join(tmp, "quiet"), key=KEY,
                                         video_factory=fakes.video_factory(fps=30),
                                         max_seconds=2.4, on_started=pause_later)
        finally:
            holder["sender"].stop()
        spans = result.manifest["ended"]["controls_quiet_spans"]
        t.equal("a silent control stream does not stop the session",
                result.stop_reason, "max_session_seconds")
        t.check("the silence is recorded as one span of about a second",
                len(spans) == 1 and spans[0]["to_ns"]
                and 0.6e9 < spans[0]["to_ns"] - spans[0]["from_ns"] < 1.6e9, spans)
        names = event_names(result.directory)
        t.check("and logged as it starts and ends",
                "controls_quiet" in names and "controls_resumed" in names, names)

        # ------------------------------------------------------------ video lost
        result, _, _ = run(tmp, "novideo", max_seconds=5,
                           video_factory=fakes.video_factory(fps=30, stall_after=10))
        t.equal("losing the HDMI signal stops the session, not cleanly",
                (result.stop_reason, result.clean), ("video_no_signal", False))
        with ControlReader(os.path.join(result.directory, "controls.gpb")) as ctl:
            t.check("controls up to that point are intact", len(ctl) > 0 and ctl.truncated_bytes == 0)
        t.check("the manifest is still finalised", "ended" in result.manifest)

        # -------------------------------------------------------- receiver killed
        def kill_receiver(ctx):
            threading.Timer(0.3, lambda: os.kill(ctx.receiver_pid, signal.SIGKILL)).start()

        result, elapsed, _ = run(tmp, "rxdead", max_seconds=10, on_started=kill_receiver)
        t.equal("a dead control receiver stops the session", result.stop_reason, "receiver_died")
        t.check("promptly", elapsed < 3.0, f"{elapsed:.1f} s")
        t.check("and says so in the manifest and the log",
                result.manifest["ended"]["controls"]["error"] and "receiver_died" in event_names(result.directory))

        # -------------------------------------------------------------- port busy
        port = free_udp_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))
        try:
            cfg = profile(tmp, port)
            result = sess.record_session(cfg, data_root=os.path.join(tmp, "busy"), key=KEY,
                                         video_factory=fakes.video_factory(), max_seconds=5)
        finally:
            blocker.close()
        t.check("a control port already in use stops before video starts, with a manifest",
                result.stop_reason == "receiver_failed_to_start"
                and "video" not in os.listdir(result.directory) and "ended" in result.manifest,
                (result.stop_reason, os.listdir(result.directory)))

        # ------------------------------------------------------------- SIGINT
        root = os.path.join(tmp, "sigint")
        port = free_udp_port()
        proc = subprocess.Popen([sys.executable, DRIVER, root, str(port), "30"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        recording = lambda: any(os.path.getsize(p) > 32 * 10 for p in  # noqa: E731
                                glob.glob(os.path.join(root, "sessions", "*", "video", "session.idx")))
        wait_until(recording, 20.0)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        try:
            summary = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            summary = {"stdout": out, "stderr": err[-2000:]}
        t.equal("SIGINT stops a recorder cleanly",
                (proc.returncode, summary.get("stop_reason"), summary.get("clean")),
                (0, "signal:SIGINT", True))

        # ------------------------------------------------------------- SIGKILL
        root = os.path.join(tmp, "sigkill")
        port = free_udp_port()
        proc = subprocess.Popen([sys.executable, DRIVER, root, str(port), "60"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def far_enough():
            ctl = glob.glob(os.path.join(root, "sessions", "*", "controls.gpb"))
            idx = glob.glob(os.path.join(root, "sessions", "*", "video", "session.idx"))
            return (ctl and idx and os.path.getsize(ctl[0]) > 64 + 64 * 50
                    and os.path.getsize(idx[0]) > 32 * 15)
        t.check("the recorder under test got going", wait_until(far_enough, 20.0))
        proc.kill()
        proc.wait()
        t.check("the orphaned receiver notices and releases the port",
                wait_until(lambda: port_is_free(port), 5.0))
        d = glob.glob(os.path.join(root, "sessions", "*"))[0]
        with open(os.path.join(d, "manifest.json")) as fh:
            manifest = json.load(fh)
        t.check("a killed session still identifies itself, unfinalised",
                manifest.get("session_id") and "ended" not in manifest)
        with ControlReader(os.path.join(d, "controls.gpb")) as ctl, Recording(os.path.join(d, "video")) as rec:
            t.check("its controls read back up to the kill", len(ctl) > 50, len(ctl))
            t.check("its video reads back up to the kill", len(rec) > 15, len(rec))
        t.check("its event log reads back", "controls_started" in event_names(d))
    finally:
        shutil.rmtree(tmp)
    return t.exit_code()


if __name__ == "__main__":
    sys.exit(main())
