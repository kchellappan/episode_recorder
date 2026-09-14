"""Running a session: manifest first, then controls, then video, until stopped.

Recording is continuous for the whole session. Episode boundaries are annotations made
later, so a late mark costs nothing and one raw session can be re-cut under a different
episode definition.

Order matters at both ends. The manifest is written before either stream starts, so a
crashed session still identifies itself. Controls start before video and stop after it,
so every frame has control coverage on both sides.

The two streams are independent. Controls are received and written in a child process
(receiver.ReceiverProcess), and video is captured here by vcap, which writes on its own
thread. A watchdog thread watches both and logs to events.log. It never pairs anything.
"""
from __future__ import annotations

import datetime as _dt
import os
import platform
import secrets
import threading
import time
import traceback
from dataclasses import dataclass

from .. import bootstrap

bootstrap.require("vcap", "gpb_client")

from vcap import Session as VcapSession  # noqa: E402
from vcap import manifest as vcap_manifest  # noqa: E402
from vcap.source import NoSignal, StreamError  # noqa: E402
from vcap.writer import WriterOverrun  # noqa: E402

from .. import config as config_mod  # noqa: E402
from .. import provenance  # noqa: E402
from .events import EventLog  # noqa: E402
from .marks import MarksWriter  # noqa: E402
from .receiver import ReceiverError, ReceiverProcess  # noqa: E402

SCHEMA_VERSION = 1
CLEAN_STOP_PREFIXES = ("signal", "max_session_seconds", "stop_requested")


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def new_session_id(now: _dt.datetime | None = None) -> str:
    """"2026-09-13T140211Z-a3f9". ISO 8601 basic time, without colons, because the
    archive copy of a session may land on exFAT or NTFS, which reject them."""
    now = now or utc_now()
    return f"{now:%Y-%m-%dT%H%M%SZ}-{secrets.token_hex(2)}"


class StopRequest:
    """A stop flag that remembers who asked first."""

    def __init__(self):
        self._event = threading.Event()
        self.reason: str | None = None

    def set(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass
class SessionContext:
    """What a caller can see once both streams are running (for tests and tooling)."""
    session_id: str
    directory: str
    receiver_pid: int
    stop: StopRequest


@dataclass
class SessionResult:
    session_id: str
    directory: str
    stop_reason: str
    clean: bool
    manifest: dict


def vcap_video_factory(device, capture: dict):
    """A factory for vcap.Session using the [capture] settings."""
    def factory(directory: str, *, notes: dict):
        return VcapSession(directory, device, fps=capture["fps"], width=capture["width"],
                           height=capture["height"], notes=notes,
                           patience=capture["video_patience_s"],
                           queue_depth=capture["video_queue_depth"])
    return factory


class Watchdog(threading.Thread):
    """Polls mark sources and watches both streams. Logs; stops only on a dead receiver.

    A quiet control stream is a warning, not an abort. The bridge publishes once per input
    read, not on a heartbeat, so an operator not touching the pad and a dead link can look
    identical from here. The quiet spans are recorded so they can be judged offline, where
    the sequence numbers and video are both available.
    """

    def __init__(self, receiver: ReceiverProcess, events: EventLog, marks: MarksWriter,
                 mark_sources, stop: StopRequest, *, quiet_warn_ms: int,
                 interval_s: float = 0.1):
        super().__init__(name="er-watchdog", daemon=True)
        self.receiver = receiver
        self.events = events
        self.marks = marks
        self.mark_sources = list(mark_sources)
        self.stop_request = stop
        self.quiet_ns = quiet_warn_ms * 1_000_000
        self.interval_s = interval_s
        self.quiet_spans: list[dict] = []
        self._halt = threading.Event()

    def halt(self) -> None:
        self._halt.set()
        self.join(timeout=5.0)

    def run(self) -> None:
        started_ns = time.monotonic_ns()
        watched = ("seq_gaps", "stale", "restarts", "hmac_failures", "malformed")
        previous = dict.fromkeys(watched, 0)
        quiet: dict | None = None

        while not self._halt.wait(self.interval_s):
            for source in self.mark_sources:
                try:
                    for mark in source.poll():
                        self.marks.write(mark)
                        self.events.log("mark", kind=mark.kind, mark_ts_rig_mono_ns=mark.ts_rig_mono_ns)
                except Exception as exc:  # noqa: BLE001 - a bad mark source must not end recording
                    self.events.log("mark_source_error", level="error",
                                    source=type(source).__name__, error=repr(exc))

            if not self.receiver.is_alive():
                self.events.log("receiver_died", level="error",
                                counters=self.receiver.snapshot())
                self.stop_request.set("receiver_died")
                break

            snap = self.receiver.snapshot()
            now = time.monotonic_ns()
            last = snap["last_arrival_ns"] or started_ns
            if now - last > self.quiet_ns:
                if quiet is None:
                    quiet = {"from_ns": last, "to_ns": None,
                             "before_first_datagram": not snap["last_arrival_ns"]}
                    self.events.log("controls_quiet", level="warn", since_ns=last,
                                    threshold_ms=self.quiet_ns // 1_000_000)
            elif quiet is not None:
                # to_ns is the latest arrival seen when the silence was noticed to end: an
                # upper bound on when it ended, within one flush interval. The exact
                # figure is in controls.gpb.
                quiet["to_ns"] = snap["last_arrival_ns"]
                self.quiet_spans.append(quiet)
                self.events.log("controls_resumed", level="warn",
                                quiet_ms=(quiet["to_ns"] - quiet["from_ns"]) // 1_000_000)
                quiet = None

            increments = {k: snap[k] - previous[k] for k in watched if snap[k] != previous[k]}
            if increments:
                self.events.log("control_anomalies", level="warn", increments=increments)
                previous.update({k: snap[k] for k in watched})

        if quiet is not None:
            self.quiet_spans.append(quiet)


def build_manifest(*, session_id: str, cfg: dict, started: dict, key: bytes | None,
                   device_identity: dict | None, bridge_capabilities: dict | None,
                   session_labels: dict, has_preflight: bool) -> dict:
    prov = provenance.collect()
    return {
        "session_id": session_id,
        "schema_version": SCHEMA_VERSION,
        "started_at": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "started": started,
        "fps": cfg["capture"]["fps"],
        "recorder": prov["recorder"],
        "submodules": prov["submodules"],
        "config": cfg,
        "control_auth": "hmac-sha256-trunc16" if key else "none",
        "bridge_capabilities": bridge_capabilities,
        "capture_device": device_identity,
        # Nobody has measured it for this session. Filled in deliberately, never by default.
        "assumed_capture_offset_ns": None,
        "session_labels": session_labels,
        "timebase": {
            "pairing_key": "ts_rig_mono_ns",
            "clock": "CLOCK_MONOTONIC on this host: controls.gpb arrival_ns and video "
                     "Frame.ts_mono_ns",
            "not_for_pairing": "ts_pi_mono_ns inside each GamepadState is the Pi's clock",
        },
        "files": {"video": "video", "controls": "controls.gpb", "marks": "marks.jsonl",
                  "events": "events.log",
                  "preflight": "preflight.json" if has_preflight else None},
        "host": {"hostname": platform.node(), "kernel": platform.release(),
                 "python": platform.python_version()},
    }


def record_session(cfg: dict, *, data_root: str, video_factory, key: bytes | None = None,
                   device_identity: dict | None = None, bridge_capabilities: dict | None = None,
                   preflight: dict | None = None, session_labels: dict | None = None,
                   stop: StopRequest | None = None, mark_sources=(),
                   max_seconds: float | None = None, session_id: str | None = None,
                   on_started=None) -> SessionResult:
    capture = cfg["capture"]
    stop = stop or StopRequest()
    session_id = session_id or new_session_id()
    directory = os.path.join(data_root, "sessions", session_id)
    os.makedirs(directory)   # never reuse a session directory

    if preflight is not None:
        vcap_manifest.write(os.path.join(directory, "preflight.json"), preflight)
    manifest = build_manifest(
        session_id=session_id, cfg=cfg, started=vcap_manifest.clock_pair(), key=key,
        device_identity=device_identity, bridge_capabilities=bridge_capabilities,
        session_labels=dict(session_labels or {}), has_preflight=preflight is not None)
    manifest_path = os.path.join(directory, "manifest.json")
    vcap_manifest.write(manifest_path, manifest)

    events = EventLog(os.path.join(directory, "events.log"))
    marks = MarksWriter(os.path.join(directory, "marks.jsonl"))
    events.log("session_created", session_id=session_id)

    host, port = config_mod.parse_hostport(capture["control_bind"])
    receiver = ReceiverProcess(
        os.path.join(directory, "controls.gpb"), host=host, port=port, key=key,
        rcvbuf_bytes=capture["socket_rcvbuf_bytes"],
        flush_interval_s=capture["flush_interval_ms"] / 1000.0)
    limit_s = max_seconds if max_seconds is not None else capture["max_session_seconds"]

    watchdog = None
    stop_reason = None
    error = None
    video_summary: dict = {}
    pending: BaseException | None = None
    try:
        socket_info = receiver.start()
        events.log("controls_started", socket=socket_info, pid=receiver.pid)
        watchdog = Watchdog(receiver, events, marks, mark_sources, stop,
                            quiet_warn_ms=capture["control_quiet_warn_ms"])
        watchdog.start()
        stop_reason, video_summary = _record_video(
            os.path.join(directory, "video"), video_factory, stop=stop, limit_s=limit_s,
            events=events, on_started=lambda: on_started and on_started(SessionContext(
                session_id, directory, receiver.pid, stop)),
            session_id=session_id)
    except ReceiverError as exc:
        stop_reason, error = "receiver_failed_to_start", str(exc)
        events.log("receiver_failed_to_start", level="error", error=error)
    except BaseException as exc:  # noqa: BLE001 - finalise first, then re-raise
        stop_reason = f"error:{type(exc).__name__}"
        error = traceback.format_exc()
        events.log("session_error", level="error", error=error)
        pending = exc
    finally:
        if watchdog is not None:
            watchdog.halt()
        # A dead receiver outranks whatever the video loop saw: it is why the loop stopped.
        if stop.reason == "receiver_died":
            stop_reason = "receiver_died"
        controls = receiver.stop() if receiver.started else None
        if controls is not None:
            events.log("controls_stopped", **controls)
        ended = vcap_manifest.clock_pair()
        manifest["ended"] = {
            "at": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clock": ended,
            "duration_ns": ended["monotonic_ns"] - manifest["started"]["monotonic_ns"],
            "stop_reason": stop_reason,
            "clean": _is_clean(stop_reason),
            "error": error,
            "controls": {**(controls or {}), "socket": receiver.socket_info},
            "controls_quiet_warn_ms": capture["control_quiet_warn_ms"],
            "controls_quiet_spans": watchdog.quiet_spans if watchdog else [],
            "video": video_summary,
            "marks_written": marks.written,
        }
        vcap_manifest.write(manifest_path, manifest)
        events.log("session_finalized", stop_reason=stop_reason,
                   clean=_is_clean(stop_reason))
        marks.close()
        events.close()
    if pending is not None:
        raise pending
    return SessionResult(session_id, directory, stop_reason, _is_clean(stop_reason), manifest)


def _is_clean(reason: str | None) -> bool:
    return bool(reason) and reason.startswith(CLEAN_STOP_PREFIXES)


def _record_video(video_dir: str, video_factory, *, stop: StopRequest, limit_s: float,
                  events: EventLog, on_started, session_id: str) -> tuple[str, dict]:
    frames = 0
    reason = None
    detail = None
    stats: dict = {}
    t0 = time.monotonic()
    try:
        # notes land in vcap's own manifest, so the video directory can be traced back to
        # its session even if everything around it is lost.
        with video_factory(video_dir, notes={"session_id": session_id}) as video:
            events.log("video_started", granted=(video.manifest or {}).get("granted"))
            on_started()
            for _frame in video.frames():
                frames += 1
                if stop.is_set():
                    reason = stop.reason
                    break
                if time.monotonic() - t0 >= limit_s:
                    reason = "max_session_seconds"
                    break
            else:
                reason = "video_ended"
            stats = video.stats()
    except NoSignal as exc:
        reason, detail = "video_no_signal", str(exc)
    except StreamError as exc:
        reason, detail = "video_stream_error", str(exc)
    except WriterOverrun as exc:
        reason, detail = "video_writer_overrun", str(exc)
    if detail:
        events.log(reason, level="error", error=detail)
    events.log("video_stopped", reason=reason, frames=frames)

    summary = {"frames_seen": frames, "stats": stats, "error": detail}
    try:
        finalized = vcap_manifest.read(os.path.join(video_dir, "manifest.json"))
        for key in ("granted", "frames_written", "effective_fps", "duration_ns", "writer",
                    "counters"):
            summary[key] = finalized.get(key)
    except (OSError, ValueError) as exc:
        summary["manifest_error"] = str(exc)
    return reason, summary


def parse_labels(pairs: list[str]) -> dict:
    labels = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise config_mod.ConfigError(f"label {pair!r} is not key=value")
        labels[key] = value
    return labels


__all__ = ["record_session", "StopRequest", "SessionResult", "SessionContext",
           "new_session_id", "vcap_video_factory", "parse_labels"]
