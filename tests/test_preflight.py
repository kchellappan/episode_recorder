#!/usr/bin/env python3
"""Preflight refuses each of the failures it exists to catch, and passes a healthy rig.

Video and capabilities are fakes. Controls are real datagrams on loopback.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import shutil
import sys
import tempfile

import fakes
import harness
from harness import free_udp_port

from episode_recorder import config as config_mod
from episode_recorder.capture import preflight as pf

KEY = b"preflight-key"
DIMS = config_mod.load()["action"]["dims"]


def controls(tmp, *, key=KEY, sender=None, seconds=0.6, min_datagrams=10):
    port = free_udp_port()
    s = sender(port) if sender else None
    if s:
        s.start()
    try:
        checks = pf.check_controls("127.0.0.1", port, key=key, seconds=seconds,
                                   min_datagrams=min_datagrams, rcvbuf_bytes=1 << 20,
                                   announce=lambda _m: None)
    finally:
        if s:
            s.stop()
    return {c.name: c for c in checks}["controls"]


def main() -> int:
    t = harness.Checks()
    tmp = tempfile.mkdtemp()
    try:
        # ------------------------------------------------------------ a healthy rig
        port = free_udp_port()
        cfg = config_mod.load()
        cfg["capture"].update(bridge_host="192.0.2.1", control_bind=f"127.0.0.1:{port}",
                              max_session_seconds=60)
        cfg["preflight"].update(video_seconds=0.3, control_seconds=0.6)
        sender = fakes.StreamingSender(port, key=KEY)
        sender.start()
        try:
            report = pf.run_preflight(
                cfg, key=KEY, preview_path=os.path.join(tmp, "preview.jpg"), disk_path=tmp,
                confirm_picture=lambda _p: (True, "test"), announce=lambda _m: None,
                resolve_device=lambda _m: fakes.FakeDevice(),
                open_video=lambda _d, _c: fakes.FakeVideoSource(),
                query_caps=lambda _h, _p: fakes.FakeCaps())
        finally:
            sender.stop()
        statuses = {c.name: c.status for c in report.checks}
        t.check("a healthy rig passes every check", report.passed, pf.format_report(report))
        t.equal("every check ran", sorted(statuses),
                ["capabilities", "controls", "device", "disk", "picture", "socket_buffer", "video"])
        with open(os.path.join(tmp, "preview.jpg"), "rb") as fh:
            preview = fh.read()
        t.check("the preview is a whole JPEG, not the flagged first fragment",
                preview.startswith(b"\xff\xd8") and preview.endswith(b"\xff\xd9"))
        doc = json.loads(json.dumps(report.to_dict()))
        t.equal("the report serialises with the preview inline",
                base64.b64decode(doc["preview_jpeg_base64"]), preview)
        t.equal("the first-frame fragment is noted, not hidden",
                report.check("video").data["first_frame_flagged"], True)

        report = pf.run_preflight(
            cfg, key=KEY, preview_path=os.path.join(tmp, "p2.jpg"), disk_path=tmp,
            confirm_picture=lambda _p: (True, "test"), announce=lambda _m: None,
            resolve_device=lambda _m: (_ for _ in ()).throw(LookupError("nothing plugged in")),
            open_video=lambda _d, _c: fakes.FakeVideoSource(),
            query_caps=lambda _h, _p: fakes.FakeCaps())
        t.check("no capture card fails, and nothing tries to stream from it",
                not report.passed and report.check("device").status == pf.FAIL
                and report.check("video") is None)

        # ---------------------------------------------------------------- video
        check, _ = pf.check_video(lambda: fakes.FakeVideoSource(no_signal=True), seconds=0.2)
        t.equal("no frames fails", check.status, pf.FAIL)
        check, preview = pf.check_video(lambda: fakes.FakeVideoSource(monotonic=False), seconds=0.2)
        t.check("frames not on CLOCK_MONOTONIC fail: they cannot pair with controls",
                check.status == pf.FAIL and "CLOCK_MONOTONIC" in check.detail, check.detail)
        check, preview = pf.check_video(lambda: fakes.FakeVideoSource(corrupt_all=True), seconds=0.2)
        t.check("frames that are never whole JPEGs fail", check.status == pf.FAIL and preview is None)
        t.equal("a device outside /dev/v4l/by-id fails",
                pf.check_device(fakes.FakeDevice("/dev/video4")).status, pf.FAIL)

        check = pf.check_picture(fakes.make_jpeg(), os.path.join(tmp, "p3.jpg"),
                                 lambda _p: (False, "operator said no"))
        t.equal("a picture the operator does not confirm fails", check.status, pf.FAIL)
        t.equal("with no frame there is no picture to confirm",
                pf.check_picture(None, os.path.join(tmp, "p4.jpg"), lambda _p: (True, "")).status,
                pf.SKIPPED)

        # ------------------------------------------------------------- controls
        t.check("healthy controls pass",
                controls(tmp, sender=lambda p: fakes.StreamingSender(p, key=KEY)).status == pf.PASS)
        c = controls(tmp)
        t.check("silence fails, naming publish_host", c.status == pf.FAIL and "publish_host" in c.detail, c.detail)
        c = controls(tmp, sender=lambda p: fakes.StreamingSender(p, key=KEY, repeat_seq=True))
        t.check("a sequence that never advances fails", c.status == pf.FAIL and "not advancing" in c.detail, c.detail)
        c = controls(tmp, key=None, sender=lambda p: fakes.StreamingSender(p, key=KEY))
        t.check("a signing bridge with no key configured fails, naming control_key_file",
                c.status == pf.FAIL and "control_key_file" in c.detail, c.detail)
        c = controls(tmp, key=KEY, sender=lambda p: fakes.StreamingSender(p, key=None))
        t.check("a key configured for a bridge that is not signing fails, naming publish_key",
                c.status == pf.FAIL and "publish_key" in c.detail, c.detail)
        c = controls(tmp, key=KEY, sender=lambda p: fakes.StreamingSender(p, key=b"other"))
        t.check("the wrong key fails as an HMAC mismatch", c.status == pf.FAIL and "HMAC" in c.detail, c.detail)
        c = controls(tmp, sender=lambda p: fakes.StreamingSender(p, key=KEY, rate_hz=4))
        t.check("too few datagrams fails, telling the operator to move the controls",
                c.status == pf.FAIL and "move the controls" in c.detail, c.detail)

        # ----------------------------------------------------------- capabilities
        def caps(dims=DIMS, host="192.0.2.1", **kw):
            return pf.check_capabilities(lambda _h, _p: fakes.FakeCaps(**kw), host, 9871, dims)
        t.equal("the default action spec matches the HORIPAD target", caps().status, pf.PASS)
        t.check("an empty bridge_host fails rather than guessing",
                caps(host="").status == pf.FAIL and "bridge_host" in caps(host="").detail)
        t.equal("a bridge that does not answer fails", caps(ok=False).status, pf.FAIL)
        lt = [{"name": "throttle", "source": "axis.lt"}]
        c = caps(dims=lt)
        t.check("an analog trigger axis on a digital-trigger target fails, suggesting l2/r2",
                c.status == pf.FAIL and "l2/r2" in c.detail, c.detail)
        t.equal("the same axis on an analog target passes",
                caps(dims=lt, trigger_mode="analog").status, pf.PASS)
        t.equal("a button the target lacks fails",
                caps(dims=[{"name": "x", "source": "button.paddle1"}]).status, pf.FAIL)
        t.equal("a vendor-label source (button.a) fails: sources use the bridge's position names",
                caps(dims=[{"name": "a", "source": "button.a"}]).status, pf.FAIL)
        t.equal("a source that is not axis.* or button.* fails",
                caps(dims=[{"name": "q", "source": "lx"}]).status, pf.FAIL)

        # ------------------------------------------------------------------ disk
        usage = collections.namedtuple("usage", "total used free")
        c = pf.check_disk(os.path.join(tmp, "not", "yet", "made"), fps=30, seconds=3600,
                          bytes_per_frame=317_000, headroom=1.5,
                          disk_usage=lambda _p: usage(0, 0, 40 * 10 ** 9))
        t.check("40 GB free for an hour at 30 fps fails (51 GB with headroom)",
                c.status == pf.FAIL and c.data["required_bytes"] == int(30 * 3600 * 317_000 * 1.5), c.detail)
        t.equal("the disk check measures the nearest existing parent", c.data["path"], tmp)
        c = pf.check_disk(tmp, fps=30, seconds=3600, bytes_per_frame=317_000, headroom=1.5,
                          disk_usage=lambda _p: usage(0, 0, 60 * 10 ** 9))
        t.equal("60 GB free for the same session passes", c.status, pf.PASS)

        warn = pf._socket_buffer_check({"rcvbuf_requested": 8 << 20, "rcvbuf_reported": 425984,
                                        "rmem_max": 212992})
        t.check("a capped receive buffer warns, naming rmem_max",
                warn.status == pf.WARN and "rmem_max" in warn.detail, warn.detail)
        t.check("a warning alone does not fail preflight",
                pf.Report("now", [warn, pf.Check("disk", pf.PASS, "")]).passed)
        t.check("an empty report does not pass", not pf.Report("now").passed)
    finally:
        shutil.rmtree(tmp)
    return t.exit_code()


if __name__ == "__main__":
    sys.exit(main())
