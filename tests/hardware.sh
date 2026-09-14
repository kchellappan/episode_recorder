#!/bin/bash
# Checks that need the capture card, a live HDMI source, and a bridge publishing capture to
# this machine. Neither CI nor tests/run_tests.sh can run these. hdmi_capture records a path
# regression that reached main through exactly this gap. Expect the same here.
#
#   tests/hardware.sh <profile.toml> [seconds]
#
# The profile must set capture.bridge_host and, if the bridge signs, control_key_file.
# Preflight asks you to confirm the picture and to move the controls. Keep moving them
# while it records: the bridge publishes once per input read, so an untouched pad may send
# nothing.
set -uo pipefail
cd "$(dirname "$0")/.."
CONFIG="${1:?usage: tests/hardware.sh <profile.toml> [seconds]}"
DURATION="${2:-30}"
TMP="$(mktemp -d)"
export PYTHONPATH="$PWD"

if ! ./tools/er-record --config "$CONFIG" --data-root "$TMP/data" --seconds "$DURATION" \
        --label purpose=hardware-test; then
    echo "  FAIL  er-record exited non-zero; see above. Data kept in $TMP"
    exit 1
fi

python3 - "$TMP/data" <<'PY'
import glob, json, os, sys

import episode_recorder  # noqa: F401
from episode_recorder.capture.control_store import ControlReader
from vcap import Recording

failures = []
def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail != "" else ""))
    if not ok:
        failures.append(name)

sessions = glob.glob(os.path.join(sys.argv[1], "sessions", "*"))
check("exactly one session was written", len(sessions) == 1, sessions)
d = sessions[0]
with open(os.path.join(d, "manifest.json")) as fh:
    m = json.load(fh)
ended = m.get("ended", {})
check("the manifest is finalised with a clean stop", ended.get("clean"), ended.get("stop_reason"))

with Recording(os.path.join(d, "video")) as rec, ControlReader(os.path.join(d, "controls.gpb")) as ctl:
    check("frame timestamps are CLOCK_MONOTONIC", rec.timestamps_are_monotonic)
    fps = m["config"]["capture"]["fps"]
    secs = ended["duration_ns"] / 1e9
    check("frames arrived at roughly the configured rate", len(rec) >= 0.9 * fps * (secs - 3),
          f"{len(rec)} frames in {secs:.1f} s at {fps} fps")
    check("controls were received", len(ctl) > 0, f"{len(ctl)} records")
    if len(rec) and len(ctl):
        f0, f1 = rec.span_ns()
        c0, c1 = ctl.arrival_ns(0), ctl.arrival_ns(-1)
        check("control arrivals and frame timestamps overlap on one clock",
              c0 < f1 and c1 > f0, f"frames {f0}..{f1}, controls {c0}..{c1}")
    print(f"  INFO  flagged frames: {len(rec.flagged())} (expect the first-frame fragment)")
    print(f"  INFO  control flags: {ctl.flag_counts()}")
    print(f"  INFO  receive buffer: {ended['controls']['socket']}")
    print(f"  INFO  control silences over {ended['controls_quiet_warn_ms']} ms: "
          f"{len(ended['controls_quiet_spans'])}")
print(f"\nsession kept at {d}")
sys.exit(1 if failures else 0)
PY
