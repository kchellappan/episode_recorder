#!/bin/bash
# Run the hardware-free suite in a container.
#
# The checkout is mounted read-only and the container has no network. A test that writes
# into the repo, or reaches off-host, fails here instead of passing by accident. Loopback
# still works, which is all the receiver tests need.
#
# Runs as the calling user so the mounted checkout's ownership matches, with git told to
# trust it (manifests record commits).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${ER_TEST_IMAGE:-episode-recorder-test}"

# A shell started before this user joined the docker group cannot reach the daemon
# directly, but `sg docker` can.
if docker info >/dev/null 2>&1; then
  dk() { docker "$@"; }
elif sg docker -c "docker info" >/dev/null 2>&1; then
  dk() { sg docker -c "$(printf '%q ' docker "$@")"; }
else
  echo "cannot reach the docker daemon: is this user in the docker group?" >&2
  exit 1
fi

dk build -q -t "$IMAGE" - < Dockerfile >/dev/null
dk run --rm --network none \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  -v "$PWD:/src:ro" -w /src \
  "$IMAGE" ./tests/run_tests.sh
