"""Checks run before every session. Any failure refuses to start.

Every check here reflects a way a full collection day can be lost:

  * The card sends healthy-looking frames with nothing on its HDMI input. No counter can
    see that, so a person looks at a preview of what the card is capturing.
  * Video can record perfectly while controls are absent, unsigned, signed with the wrong
    key, or from a sender whose sequence is not advancing.
  * A client cannot know whether the target's triggers are analog or digital, and
    recording the wrong one fails silently. So the bridge is asked.
  * A disk that fills mid-session is behaviour hdmi_capture lists as untested.

The checks take their I/O as arguments (a device resolver, a video opener, a capability
query, a confirmation prompt), so the logic is tested without hardware. What that cannot
test is the hardware itself. See tests/hardware.sh.
"""
from __future__ import annotations

import base64
import datetime as _dt
import os
import shutil
import struct
import threading
import time
from dataclasses import asdict, dataclass, field

from .. import bootstrap

bootstrap.require("vcap", "gpb_client")

from gpb_client.capabilities import query_capabilities  # noqa: E402
from vcap import VideoSource, find_capture_card  # noqa: E402
from vcap.source import NoSignal, StreamError  # noqa: E402

from .. import config as config_mod  # noqa: E402
from .control_store import FLAGS_OFFSET, SEQ_OFFSET  # noqa: E402
from .receiver import MemorySink, Receiver, open_socket  # noqa: E402

PASS, WARN, FAIL, SKIPPED = "pass", "warn", "fail", "skipped"
SCHEMA_VERSION = 1

_U32 = struct.Struct("<I")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class Report:
    started_at: str
    checks: list[Check] = field(default_factory=list)
    preview_jpeg: bytes | None = None
    device: object = None          # the resolved vcap Device; not serialised

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not any(c.status == FAIL for c in self.checks)

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "started_at": self.started_at,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
            "preview_jpeg_base64": (base64.b64encode(self.preview_jpeg).decode("ascii")
                                    if self.preview_jpeg else None),
        }


# --------------------------------------------------------------------------- disk

def check_disk(path: str, *, fps: float, seconds: float, bytes_per_frame: int,
               headroom: float, disk_usage=shutil.disk_usage) -> Check:
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    free = disk_usage(probe).free
    estimate = int(fps * seconds * bytes_per_frame)
    need = int(estimate * headroom)
    data = {"path": probe, "free_bytes": free, "estimated_bytes": estimate,
            "required_bytes": need, "fps": fps, "seconds": seconds,
            "bytes_per_frame": bytes_per_frame, "headroom": headroom}
    gb = 1e9
    if free < need:
        return Check("disk", FAIL,
                     f"{free / gb:.1f} GB free at {probe}; a {seconds:g} s session at "
                     f"{fps:g} fps is budgeted at {estimate / gb:.1f} GB, x{headroom:g} "
                     f"headroom = {need / gb:.1f} GB", data)
    return Check("disk", PASS, f"{free / gb:.1f} GB free, {need / gb:.1f} GB required", data)


# ------------------------------------------------------------------ capabilities

def required_controls(dims: list[dict]) -> tuple[set[str], set[str], list[str]]:
    axes, buttons, errors = set(), set(), []
    for dim in dims:
        kind, _, name = dim.get("source", "").partition(".")
        if kind == "axis" and name:
            axes.add(name)
        elif kind == "button" and name:
            buttons.add(name)
        else:
            errors.append(f"action dim {dim.get('name')!r}: source {dim.get('source')!r} is "
                          f"not axis.<name> or button.<name>")
    return axes, buttons, errors


def check_capabilities(query, host: str, port: int, dims: list[dict]) -> Check:
    if not host:
        return Check("capabilities", FAIL,
                     "capture.bridge_host is empty. The bridge must be asked what it "
                     "presents; a guessed trigger mode fails silently.")
    caps = query(host, port)
    if not caps:
        return Check("capabilities", FAIL,
                     f"no capability reply from {host}:{port}. Is gpbridge running, and "
                     f"is its source udp (the query is answered on the control port)?")
    data = {"host": host, "port": port, "capabilities": caps.raw}
    axes, buttons, problems = required_controls(dims)

    trigger_axes = axes & {"lt", "rt"}
    if trigger_axes and caps.trigger_mode != "analog":
        problems.append(
            f"action spec records {sorted(trigger_axes)} but the target's trigger_mode is "
            f"{caps.trigger_mode!r}; record button.l2/r2 instead")
    missing_axes = sorted((axes - {"lt", "rt"}) - set(caps.axes))
    if missing_axes:
        problems.append(f"axes not on the target: {missing_axes} (target has {caps.axes})")
    missing_buttons = sorted(buttons - set(caps.buttons))
    if missing_buttons:
        problems.append(f"buttons not on the target: {missing_buttons}")

    if problems:
        return Check("capabilities", FAIL, "; ".join(problems), data)
    return Check("capabilities", PASS,
                 f"{caps.target!r}, trigger_mode={caps.trigger_mode}, all "
                 f"{len(axes) + len(buttons)} action sources present", data)


# ------------------------------------------------------------------------- video

def check_device(device) -> Check:
    identity = device.identity()
    if not str(device.path).startswith("/dev/v4l/by-id/"):
        return Check("device", FAIL,
                     f"resolved to {device.path}, not a /dev/v4l/by-id/ link. /dev/videoN "
                     f"numbering moves between boots and replugs.", {"identity": identity})
    return Check("device", PASS, str(device.path), {"identity": identity})


def check_video(open_video, *, seconds: float) -> tuple[Check, bytes | None]:
    frames = ok = 0
    first_flagged = None
    first_ts = last_ts = None
    preview = None
    try:
        with open_video() as source:
            negotiated = source.negotiated
            deadline = time.monotonic() + seconds
            for frame in source.frames(timeout=2.0):
                frames += 1
                if first_flagged is None:
                    first_flagged = not frame.ok
                if frame.ok:
                    ok += 1
                    preview = frame.data
                first_ts = frame.ts_mono_ns if first_ts is None else first_ts
                last_ts = frame.ts_mono_ns
                if time.monotonic() >= deadline:
                    break
    except NoSignal as exc:
        return Check("video", FAIL, f"no frames from the card: {exc}"), None
    except (StreamError, OSError) as exc:
        return Check("video", FAIL, f"could not stream from the card: {exc}"), None

    measured = ((frames - 1) * 1e9 / (last_ts - first_ts)
                if frames > 1 and last_ts > first_ts else None)
    data = {
        "frames": frames, "ok_frames": ok, "first_frame_flagged": first_flagged,
        "measured_fps": measured,
        "granted": {"pixelformat": negotiated.pixelformat, "width": negotiated.width,
                    "height": negotiated.height, "fps": negotiated.fps_granted},
        "timestamp_clock": negotiated.timestamp_clock,
        "timestamps_are_monotonic": negotiated.timestamps_are_monotonic,
    }
    if not negotiated.timestamps_are_monotonic:
        return Check("video", FAIL,
                     f"frame timestamps are on {negotiated.timestamp_clock!r}, not "
                     f"CLOCK_MONOTONIC; they cannot be paired with controls", data), None
    if ok == 0:
        return Check("video", FAIL, f"{frames} frames, none decodable as whole JPEGs",
                     data), None
    granted_fps = negotiated.fps_granted
    detail = (f"{frames} frames in {seconds:g} s ({measured or 0:.1f} fps measured, "
              f"{granted_fps} granted). Frames arriving says nothing about the HDMI input; "
              f"that is the picture check.")
    if granted_fps and measured and measured < 0.9 * granted_fps:
        return Check("video", WARN, "below the granted rate. " + detail, data), preview
    return Check("video", PASS, detail, data), preview


def check_picture(preview: bytes | None, preview_path: str, confirm) -> Check:
    if preview is None:
        return Check("picture", SKIPPED, "no frame to show; see the video check")
    with open(preview_path, "wb") as fh:
        fh.write(preview)
    confirmed, method = confirm(preview_path)
    data = {"preview_path": preview_path, "method": method}
    if not confirmed:
        return Check("picture", FAIL,
                     f"picture not confirmed ({method}). The card emits frames with nothing "
                     f"connected; only a person can tell.", data)
    return Check("picture", PASS, f"confirmed: {method}", data)


# ---------------------------------------------------------------------- controls

def check_controls(host: str, port: int, *, key: bytes | None, seconds: float,
                   min_datagrams: int, rcvbuf_bytes: int, announce) -> list[Check]:
    try:
        sock, info = open_socket(host, port, rcvbuf_bytes=rcvbuf_bytes)
    except OSError as exc:
        return [Check("controls", FAIL,
                      f"cannot bind {host}:{port}: {exc}. Is another recorder running?")]

    announce(f"Move a stick and press some buttons for the next {seconds:g} s.")
    sink = MemorySink()
    rx = Receiver(sock, sink, key=key)
    stop = threading.Event()
    thread = threading.Thread(target=rx.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join()
    sock.close()

    checks = [_socket_buffer_check(info), _analyse_controls(
        sink.records, rx.counters, host=host, port=port, key=key, seconds=seconds,
        min_datagrams=min_datagrams)]
    return checks


def _socket_buffer_check(info: dict) -> Check:
    requested, reported = info["rcvbuf_requested"], info["rcvbuf_reported"]
    if reported < 2 * requested:
        return Check("socket_buffer", WARN,
                     f"receive buffer capped: asked for {requested}, kernel reports "
                     f"{reported} (it reports double what it applied). Raise "
                     f"net.core.rmem_max to at least {requested} for the full margin.", info)
    return Check("socket_buffer", PASS, f"{requested} bytes requested and granted", info)


def _analyse_controls(records: list[bytes], counters: dict, *, host, port, key, seconds,
                      min_datagrams) -> Check:
    clean_seqs = [_U32.unpack_from(r, SEQ_OFFSET)[0] for r in records
                  if _U32.unpack_from(r, FLAGS_OFFSET)[0] == 0]
    data = {"bind": f"{host}:{port}", "seconds": seconds, "received": len(records),
            "clean": len(clean_seqs), "authenticated": key is not None}
    data.update({k: counters[k] for k in ("seq_gaps", "seq_lost", "stale", "restarts",
                                          "hmac_failures", "malformed")})
    if clean_seqs:
        data["seq_first"], data["seq_last"] = clean_seqs[0], clean_seqs[-1]

    if not records:
        return Check("controls", FAIL,
                     f"no datagrams on {host}:{port} in {seconds:g} s. Check that the "
                     f"bridge's publish_host/publish_port point at this machine and "
                     f"gpbridge is running.", data)
    if not clean_seqs:
        size = counters["last_malformed_size"]
        if counters["hmac_failures"]:
            reason = ("every datagram failed HMAC verification: control_key_file does "
                      "not match the bridge's publish_key")
        elif key is None and size == 64:
            reason = ("datagrams are 64 bytes, so the bridge is signing them; set "
                      "capture.control_key_file to a file holding its publish_key")
        elif key is not None and size == 48:
            reason = ("datagrams are 48 bytes, so the bridge is not signing; set "
                      "publish_key on the bridge or clear capture.control_key_file")
        else:
            reason = f"no datagram was a valid GamepadState (last size {size} bytes)"
        return Check("controls", FAIL, reason, data)
    if len(set(clean_seqs)) == 1:
        return Check("controls", FAIL,
                     f"sequence number is not advancing (every datagram has seq "
                     f"{clean_seqs[0]}): something is replaying a single state", data)
    if len(clean_seqs) < min_datagrams:
        return Check("controls", FAIL,
                     f"only {len(clean_seqs)} valid datagrams in {seconds:g} s, need "
                     f"{min_datagrams}. The bridge publishes once per input read, so an "
                     f"untouched pad may send nothing; move the controls during the check.",
                     data)
    return Check("controls", PASS,
                 f"{len(clean_seqs)} valid datagrams, seq {clean_seqs[0]}..{clean_seqs[-1]}",
                 data)


# ---------------------------------------------------------------------------- run

def _open_vcap_source(device, capture: dict):
    return VideoSource(device, "MJPG", capture["width"], capture["height"],
                       fps=capture["fps"])


def run_preflight(cfg: dict, *, key: bytes | None, preview_path: str, disk_path: str,
                  confirm_picture, announce=print, session_seconds: float | None = None,
                  resolve_device=find_capture_card, open_video=_open_vcap_source,
                  query_caps=query_capabilities) -> Report:
    capture, pf = cfg["capture"], cfg["preflight"]
    report = Report(started_at=_dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"))

    report.checks.append(check_disk(
        disk_path, fps=capture["fps"],
        seconds=session_seconds if session_seconds is not None else capture["max_session_seconds"],
        bytes_per_frame=capture["budget_bytes_per_frame"], headroom=capture["disk_headroom"]))

    report.checks.append(check_capabilities(
        query_caps, capture["bridge_host"], capture["bridge_caps_port"],
        cfg.get("action", {}).get("dims", [])))

    try:
        device = resolve_device(capture["device_match"])
    except Exception as exc:  # noqa: BLE001 - any failure to resolve is a failed check
        report.checks.append(Check("device", FAIL,
                                   f"no capture card matching {capture['device_match']!r}: {exc}"))
        device = None
    if device is not None:
        report.device = device
        report.checks.append(check_device(device))
        video, preview = check_video(lambda: open_video(device, capture),
                                     seconds=pf["video_seconds"])
        report.checks.append(video)
        report.preview_jpeg = preview
        report.checks.append(check_picture(preview, preview_path, confirm_picture))

    host, port = config_mod.parse_hostport(capture["control_bind"])
    report.checks.extend(check_controls(
        host, port, key=key, seconds=pf["control_seconds"],
        min_datagrams=pf["min_control_datagrams"],
        rcvbuf_bytes=capture["socket_rcvbuf_bytes"], announce=announce))
    return report


def terminal_confirm(asserted: bool):
    """A confirm_picture for the command line."""
    import sys

    def confirm(preview_path: str) -> tuple[bool, str]:
        if asserted:
            return True, "asserted with --picture-confirmed"
        if not sys.stdin.isatty():
            return False, ("no terminal to ask on; look at the preview, then rerun with "
                           "--picture-confirmed")
        print(f"\nOpen {preview_path}: it is what the card is capturing now. The card sends "
              f"a placeholder image when nothing is connected.")
        answer = input("Does it show the console's picture? [y/N] ")
        return answer.strip().lower() in ("y", "yes"), "operator answered at the terminal"

    return confirm


def format_report(report: Report) -> str:
    lines = []
    for c in report.checks:
        lines.append(f"  {c.status.upper():7s} {c.name:14s} {c.detail}")
    lines.append(f"\npreflight {'PASSED' if report.passed else 'FAILED'}")
    return "\n".join(lines)
