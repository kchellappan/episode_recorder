#!/usr/bin/env python3
"""One session with fake video and a streaming fake bridge, as a real process.

test_session.py runs this so it can send SIGINT and SIGKILL to an actual recorder:
signals and orphaned children behave differently in-process.

    session_driver.py <data_root> <port> <max_seconds>
"""
from __future__ import annotations

import json
import signal
import sys

import fakes
import harness  # noqa: F401

from episode_recorder import config as config_mod
from episode_recorder.capture import session as sess


def main() -> int:
    data_root, port, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    cfg = config_mod.load()
    cfg["capture"].update(control_bind=f"127.0.0.1:{port}", flush_interval_ms=50,
                          socket_rcvbuf_bytes=1 << 20)
    stop = sess.StopRequest()
    signal.signal(signal.SIGINT, lambda _s, _f: stop.set("signal:SIGINT"))
    sender = fakes.StreamingSender(port)
    sender.start()
    try:
        result = sess.record_session(cfg, data_root=data_root,
                                     video_factory=fakes.video_factory(fps=30), stop=stop,
                                     max_seconds=seconds)
    finally:
        sender.stop()
    print(json.dumps({"session_id": result.session_id, "stop_reason": result.stop_reason,
                      "clean": result.clean}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
