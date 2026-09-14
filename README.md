# episode_recorder

> **This repository is AI-generated and has had no independent human review.** Claude
> (Anthropic) wrote the code, tests and documentation, working from a human-written design
> ([docs/design.md](docs/design.md)) and rules ([CLAUDE.md](CLAUDE.md)). Read it before you
> depend on it.
>
> What that means in practice: **nothing here has run against the capture card or the Pi.**
> The hardware-free suite passes in a container, and its checks were confirmed by mutation
> to fail when the behaviour they cover is broken (see [Status](#status)). Several claims in
> the design were corrected by reading the submodules' source. Each correction is marked in
> the design doc and says it was read, not measured.

Captures paired video and control streams from a human operator, segments them into
episodes, and emits a LeRobot dataset. **Today it does the first part**: it records a
session, which is build order step 1 of the design. Everything after that waits on an
alignment check against the real hardware.

It owns the word *episode*. Neither submodule does, by their own design.
[`hdmi_capture`](https://github.com/kchellappan/hdmi_capture) knows frames and time.
[`rpi_gamepad_bridge`](https://github.com/kchellappan/rpi_gamepad_bridge) knows controls and
time. This repo pairs them. It is not game-aware, not a training repo, and not a replay
harness.

```bash
git clone --recursive <this repo>          # the submodules are the capture layers
cd episode_recorder
./tests/run_tests.sh                       # no hardware, no pip, no build step
```

## Recording a session

On the Pi, point the bridge's capture at the rig (in its network config):

```ini
publish_host = 192.168.1.100     ; this machine
publish_port = 9872
publish_key  = <the key>
```

On the rig, a profile. It is merged over
[`episode_recorder/configs/default.toml`](episode_recorder/configs/default.toml):

```toml
[capture]
bridge_host = "192.168.1.50"                       # the Pi, asked for its capabilities
control_key_file = "~/.config/episode_recorder/bridge.key"
```

The key lives in a file, never in the profile: the profile is inlined into every session
manifest, and a test checks that the key appears in no session file.

```bash
./tools/er-preflight --config profile.toml                  # checks only
./tools/er-record --config profile.toml --data-root data --label operator=me
```

`er-record` runs preflight first and refuses to record if anything fails:

| check | why |
|---|---|
| `disk` | free space ≥ the session's budget × 1.5 |
| `capabilities` | the bridge's own answer to what its target has: trigger mode and axes against the profile's action sources. Guessing fails silently. |
| `device` | resolved through `/dev/v4l/by-id/`, never `/dev/videoN` |
| `video` | frames flowing, on `CLOCK_MONOTONIC`, whole JPEGs among them |
| `picture` | **a person** confirms the preview shows the console. The card sends a placeholder with nothing connected, and no counter can see that. |
| `controls` | datagrams arriving, authenticating, with an advancing sequence. It names the misconfigured side when they don't. |
| `socket_buffer` | warns when the kernel capped the receive buffer |

Recording is continuous until Ctrl-C or the time limit. A session is a directory:

```
data/sessions/2026-09-13T140211Z-a3f9/
  manifest.json   written before either stream starts
  preflight.json
  controls.gpb    every control datagram: 48-byte state + rig arrival time + flags
  video/          a vcap recording
  marks.jsonl
  events.log
```

[docs/formats.md](docs/formats.md) specifies all of it.

## The one rule about time

Controls pair with video on **`arrival_ns`**, the rig's `CLOCK_MONOTONIC` when a datagram
arrived. Frames carry the same clock. The Pi's timestamp inside each state is a different
machine's clock: use it for jitter analysis, never for pairing.
[docs/timebase.md](docs/timebase.md).

## What is in here

| | |
|---|---|
| `episode_recorder/bootstrap.py` | The only code that knows where the submodules live. |
| `episode_recorder/config.py` | Profiles: merge, validate, read the key file. |
| `episode_recorder/capture/control_store.py` | `controls.gpb` writer and reader. Standard library only, and needs neither submodule. |
| `episode_recorder/capture/receiver.py` | recvfrom, stamp, classify the sequence number, append, in its own process. |
| `episode_recorder/capture/preflight.py` | The checks above, with their I/O injectable so the logic is tested without hardware. |
| `episode_recorder/capture/session.py` | Manifest, receiver, vcap session and watchdog, in that order. |
| `episode_recorder/capture/marks.py`, `events.py` | `marks.jsonl` and `events.log`. |
| `tools/er-preflight`, `tools/er-record` | The command line. |
| `tests/` | `run_tests.sh` (no hardware), `docker.sh` (the same suite in a container), `hardware.sh`. |

Capture, storage and read-back import only the standard library, and
`tests/check_stdlib_only.py` enforces it. A rig records with no venv and no pip. The dataset
builder will be the declared exception (`build/`: numpy, pillow, lerobot), and
`tests/check_boundaries.py` already fails if `capture/` imports it.

## Tests

```bash
./tests/run_tests.sh                 # python3 >= 3.11, submodules checked out; ~30 s
./tests/docker.sh                    # the same, read-only checkout, no network
./tests/hardware.sh profile.toml     # card + live HDMI + bridge publishing here
```

The suite drives the receiver with real datagrams over loopback, built with gpb_client's own
`pack()` and `sign()`. Video comes from fake frames written through vcap's own writer and
manifest, so the recording read back is a genuine vcap recording. Recorders are SIGINTed and
SIGKILLed as real processes.

It cannot cover the V4L2 path, the bridge's C++ publisher, a real network, or the rig's
kernel limits. That is what `hardware.sh` is for, and CI cannot run it. hdmi_capture reports a
path regression reaching `main` through exactly that gap. Assume the same blind spot here.

## Traps

Found while building step 1. Add to this table as more are found.

| Trap | Consequence | Where it is handled |
|---|---|---|
| **The bridge publishes once per source read, not on its heartbeat** (read from source) | An untouched pad may send nothing, so at capture time silence cannot be told apart from a dead link. Aborting on silence would end sessions for no reason. | The watchdog logs quiet spans and never aborts. Preflight tells the operator to move the controls. |
| A bridge read that drains several input reports publishes only the last, and `seq` advances by one | Intermediate states vanish **without a sequence gap**. A lost state is not re-sent until the input changes again. | Documented in formats.md. Not detectable from this side. |
| A controller disconnect releases the console to neutral without publishing it | The control stream can end with a button held that the console already released | Documented. Not yet handled downstream. |
| `multiprocessing.Event` shared with a process that gets SIGKILLed | The survivor's next `set()` can block forever on a lock the dead process held. The recorder would have hung on a dead receiver instead of finalising. Found by the SIGKILL test. | Lock-free shared flag (`receiver._ChildStop`) |
| `SO_REUSEADDR` on a UDP receiver (gpb_client's `CaptureReceiver` sets it) | A second recorder binds the same port, and datagrams go to only one of the two, silently | Not set. A test requires the second bind to fail. |
| `SO_RCVBUF` is capped at `net.core.rmem_max`, silently, and reported doubled | An "8 MB" buffer can be ~200 KB | Granted size recorded. Preflight warns. |
| The bridge signs but the rig has no key, or the reverse | Every datagram is the wrong length and nothing records | Preflight names the side to fix from the datagram size |
| An HMAC key in the profile | Copied into every manifest and every dataset built from one | Refused at load. A test searches every session file for the key. |
| Colons in session ids | exFAT and NTFS, likely homes for archive copies, reject them | `2026-09-13T140211Z-xxxx` |
| Inherited from hdmi_capture: frames with nothing connected, a fragment as frame 0, ~1 frame in 1000 lost | See its README | Preflight's picture check. vcap flags frames; nothing is dropped. |

## Status

**Implemented:** build order step 1: bootstrap, profiles, `controls.gpb`, the receiver,
preflight, the session runner, `er-preflight`, `er-record`, and the hardware-free suite with
its container runner.

**Verified by mutation:** 20 deliberate breakages, each confirmed to fail the suite. They
cover:

- sequence classification: gap threshold, restart baseline
- zero-padding of malformed datagrams
- the periodic flush, via the SIGKILL recovery test
- `SO_REUSEADDR`
- the orphaned-receiver check
- `controls.gpb` truncation handling, exclusive create and timestamp search
- quiet controls aborting a session
- the key leaking into the manifest
- the manifest being written late
- the config key refusal
- preflight's trigger-mode, by-id, monotonic-clock and picture checks
- `sys.path` use outside bootstrap, a non-stdlib import in `capture/`, and `capture/`
  importing `build/`

One mutation was initially caught only by an unrelated test, and the test it belonged to was
strengthened. This is not every line of the code.

**Known untested.** Deliberately specific, and not exhaustive:

- **Everything against hardware.** `vcap.Session` inside `er-record`, device resolution, the
  interactive preflight prompts, the bridge's C++ publisher, and a real network link.
  `tests/hardware.sh` has been syntax-checked and never run.
- **CI.** `.github/workflows/ci.yml` is written but has never run. There is no remote.
- **Throughput.** Loopback tests stream at 125 Hz for seconds. Nothing tests a pad reporting
  at 1000 Hz, bursts, or the receiver's CPU cost.
- **Duration.** The longest test session is about 2.4 s. The default limit is an hour.
- **Power loss** (only process kills are tested), a disk filling, IPv6 (the receiver binds
  IPv4 only), and a rig with several NICs.
- **Whether an untouched pad publishes at all.** Stick drift past the evdev fuzz threshold
  is unmeasured on this project's pads. It decides whether a quiet span means anything.
- **A bridge restarting mid-session** is classified from synthetic sequences only. A restart
  before `seq` passes 64 flags up to 64 records stale before recovering.
- **Mark sources.** The protocol and `marks.jsonl` exist, but no source does: no web
  buttons, pedal or chord.
- **Python 3.11,** the minimum for `tomllib`. Tested on 3.12.3 (host) and 3.12.14 (container).
- **Not built:** build order steps 2–8: calibration and `er-verify`, the action codec, the
  golden session fixture, resampling, holes, segmentation, the dataset builder, plugins,
  archival, the trimming UI.

### A proposed bridge change, not made

Publishing capture on the heartbeat as well as on each source read would make silence at
the rig mean a dead link. It would re-send a lost state within one heartbeat period and let
the watchdog abort safely. Publishing neutral on disconnect would close the held-button case.

There is a cost. Heartbeat publishes need their own sequence increments, or the receiver
here classifies them as stale. The capture rate becomes the heartbeat rate even when idle.
That change belongs in rpi_gamepad_bridge, and whether to make it is its maintainer's call.

## License

Not yet chosen. Both submodules are MIT.
