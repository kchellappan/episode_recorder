#!/bin/bash
# The hardware-free suite. Needs python3 >= 3.11 and the submodules checked out. No capture
# card, no Pi, no pip. tests/docker.sh runs this same script in a container.
#
# NOT covered here, and cannot be: the V4L2 capture path, a real bridge on a real network,
# the recording machine's receive-buffer limit, and whether the card's HDMI input is live.
# Those are tests/hardware.sh.
set -uo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1

PASS=0; FAIL=0

run() {
    local name="$1"; shift
    local out
    if out="$("$@" 2>&1)"; then
        echo "$out"
        PASS=$((PASS+1))
    else
        echo "$out"
        echo "        ^ $name failed"
        FAIL=$((FAIL+1))
    fi
}

echo "episode_recorder test suite ($(python3 --version 2>&1))"
echo
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "python3 >= 3.11 is required (tomllib)" >&2
    exit 1
fi

echo "submodules"
run "submodules" python3 -c "
import harness
from episode_recorder import bootstrap
bootstrap.require('vcap', 'gpb_client')
print('  PASS  vcap and gpb_client import from the pinned submodules')"
echo

echo "boundaries"
run "stdlib only" python3 check_stdlib_only.py
run "boundaries" python3 check_boundaries.py
echo

echo "config"
run "config" python3 test_config.py
echo

echo "controls"
run "control store" python3 test_control_store.py
run "receiver" python3 test_receiver.py
echo

echo "preflight"
run "preflight" python3 test_preflight.py
echo

echo "session"
run "session" python3 test_session.py
echo

echo "tools parse and respond to --help"
for tool in ../tools/er-*; do
    name="$(basename "$tool")"
    if out="$(python3 "$tool" --help 2>&1)"; then
        echo "  PASS  $name --help"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $name --help"
        echo "$out" | sed 's/^/        /'
        FAIL=$((FAIL+1))
    fi
done
echo

echo "shell scripts parse"
for f in ./*.sh; do
    if bash -n "$f"; then
        echo "  PASS  $(basename "$f")"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $(basename "$f")"
        FAIL=$((FAIL+1))
    fi
done
echo

echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
