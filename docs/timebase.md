# Time

hdmi_capture's `docs/timebase.md` defines what a frame timestamp means. This file covers the
control side and how the two meet. Code that needs to reason about time points here rather
than restating it.

## Three timestamps, two clocks

| name | where it is stamped | clock | use |
|---|---|---|---|
| `Frame.ts_mono_ns` | rig kernel, per V4L2 buffer, carried untouched by vcap | rig `CLOCK_MONOTONIC` | frame time |
| `arrival_ns` in `controls.gpb` (`ts_rig_mono_ns`) | rig, when `recvfrom` returns | rig `CLOCK_MONOTONIC` | **the pairing key** |
| `ts_pi_mono_ns`, inside each `GamepadState` | the Pi | Pi `CLOCK_MONOTONIC` | intervals and jitter on the Pi side. **Never pair it with video.** |

The first two are the same clock on the same machine, so aligning a control with a frame is
subtraction. The third belongs to a different machine. There is no shared epoch, and
comparing it with either of the others is meaningless, however plausible the numbers look.

`CLOCK_REALTIME` values (`t_real_ns` in the state, `ts_realtime_ns` in events, the clock
pairs in manifests) exist for rendering dates. They are not for pairing either.

## Why arrival time

hdmi_capture's alignment rests on one constraint: video and controls are timestamped on the
same machine. Controls originate on the Pi, so the Pi's stamp does not meet it. The moment a
datagram reaches the rig does. rpi_gamepad_bridge's README puts transit on a wired gigabit
link at about 0.1 ms, against a capture offset hdmi_capture measured at about 60 ms. Both
figures are from those repos' measurements, not this one's.

## What arrival time includes

Between the operator's input and `arrival_ns`, in order:

1. The controller to the Pi: USB polling and evdev. For an evdev source, `ts_pi_mono_ns` is
   the Pi kernel's event time, which is the closest stamp to the physical input.
2. The bridge's loop, up to its `sendto`. Read from source: it publishes once per source
   read. A read that drains several input reports publishes only the newest.
3. The network.
4. Waiting in the rig's socket receive queue until the receiver process calls `recvfrom`.
   A stalled receiver makes stamps late. It does not make them wrong-clock.

Items 2 to 4 are what regressing `arrival_ns` against `ts_pi_mono_ns` offline measures, and a
stalled Pi loop shows up there as a step. Nothing has been measured yet: this repo has not
run against the hardware.

## The capture offset

A frame's timestamp lags the moment its pixels reached the operator's screen by tens of
milliseconds. The correction is applied by the dataset builder (build order step 5, not
written), never to raw data:

- `vcap`'s manifest leaves `timebase.capture_offset_ns` `null`.
- This repo's session manifest leaves `assumed_capture_offset_ns` `null`.

Neither is filled in automatically. A corrected timestamp cannot be told apart from an
uncorrected one, so a correction folded into raw data could never be re-corrected once the
measurement improves. Whether the offset is per session or per rig is an open question in
docs/design.md §14.

## Watching for silence

The in-session watchdog compares the newest `arrival_ns` against the rig's own monotonic
clock. That is one machine and one clock. A gap longer than `control_quiet_warn_ms` is
logged, never used to stop the session. The bridge does not publish on its heartbeat, so a
pad nobody is touching may send nothing, and silence cannot be told apart from a dead link
at capture time. Offline, with sequence numbers and video both available, it usually can.
