# On-disk formats

This file is the specification. Where it and the code disagree, one of them has a bug.

## A session directory

Written by `tools/er-record`:

```
<data-root>/
  sessions/<session_id>/
    manifest.json    written before either stream starts; `ended` added when it stops
    preflight.json   the checks that allowed this session to start
    controls.gpb     the control stream: 64-byte header, 64-byte records, append-only
    video/           a vcap recording, untouched (hdmi_capture docs/formats.md)
    marks.jsonl      operator marks, append-only
    events.log       what happened during the session, append-only
  preflight/<session_id>/
    preflight.json   kept whether or not preflight passed
    preview.jpg      what the card was capturing when the operator was asked
```

**Session id**: `2026-09-13T140211Z-a3f9`. UTC time in ISO 8601 basic format (no colons),
then four random hex digits. docs/design.md originally used `14:02:11`, but exFAT and NTFS
reject colons in names, and an archive copy of a session may well land on one of them.

**Never rewritten.** `controls.gpb`, `marks.jsonl`, `events.log` and everything in
`video/` are only ever appended to. The writers open with exclusive create, so a second
session cannot append into an old one. `manifest.json` is the one exception, and in only
one way: it is replaced atomically once, at stop, to add `ended`. This is vcap's pattern.
A reader must treat a missing `ended` as normal, because that is what a crashed session
looks like.

## `controls.gpb`

Little-endian throughout. Fixed-width, so record *N* is at byte `64 + 64N` and a timestamp
lookup is a binary search over an `mmap`.

### Header, 64 bytes

| offset | type | field | value |
|---|---|---|---|
| 0 | char[4] | `magic` | `ERC1` |
| 4 | u16 | `version` | 1 |
| 6 | u16 | `record_size` | 64 |
| 8 | char[32] | `struct_fmt` | the bridge's `GamepadState` format, NUL-padded: `<IHHIIQQhhhhBB6s` |
| 40 | 24 bytes | reserved | zero |

The header is `fsync`ed when the file is created, before any record can follow it.

### Record, 64 bytes

| offset | type | field | meaning |
|---|---|---|---|
| 0 | u8[48] | `state` | the `GamepadState` exactly as received. The HMAC tag, if any, is verified and discarded. |
| 48 | u64 | `arrival_ns` | `ts_rig_mono_ns`: this machine's `CLOCK_MONOTONIC` when `recvfrom` returned |
| 56 | u32 | `flags` | see below |
| 60 | u32 | reserved | zero |

Two fields inside `state` are read without a full parse, at offsets pinned by a test against
gpb_client's own `pack()`: `seq` (u32 at 8) and `ts_pi_mono_ns` (u64 at 16). The second is
the Pi's clock. See [timebase.md](timebase.md) before using it.

`arrival_ns` never decreases within a file. One thread takes it from one monotonic clock in
receive order.

### Flags

| bit | name | meaning | `state` trusted |
|---|---|---|---|
| 0 | `seq_gap` | `seq` jumped forward by *k* > 1: *k*−1 published datagrams never arrived | yes |
| 1 | `hmac_fail` | the tag did not verify | **no** |
| 2 | `stale` | `seq` did not advance, falling back by at most 64: late, reordered or duplicated. The sequence baseline is unchanged. | yes |
| 3 | `seq_restart` | `seq` fell back by more than 64: the publishing source restarted. The baseline resets. | yes |
| 4 | `malformed` | wrong length, magic or version. `state` holds the first ≤48 bytes received, zero-padded. | **no** |

Every datagram received becomes exactly one record. Nothing is dropped at capture. A
`hmac_fail` or `malformed` record never moves the sequence baseline. Sequence comparison is a
32-bit wrapped difference, the same as the bridge's own `UdpSource`.

docs/design.md specified bits 0–2. Bits 3 and 4 were added before any file was written, so
the version stays 1.

**What `seq_gap` does not catch.** Read from rpi_gamepad_bridge's source, not measured.
The bridge publishes once per successful source read, not on its heartbeat. A read that
drains several input reports publishes only the last one, and `seq` advances by one. So
intermediate states the bridge never published leave no gap. And a lost datagram is not
re-sent: the state it carried is simply absent until the input next produces a read.

### Reading

`ControlReader` validates magic, dispatches on `version`, requires the `record_size` that
version declares, and tolerates a partial trailing record (`truncated_bytes`). A new layout
is a new version and a new entry in `RECORD_SIZE_BY_VERSION`, with the old reader path kept,
never a migration of files already written.

### Durability

The receiver pushes buffered records to the OS every `capture.flush_interval_ms` (250 ms by
default), not per record. A SIGKILL of the receiver or of the recorder loses at most that
much. Both cases are tested. Power loss can additionally lose whatever the page cache held,
because records are `fsync`ed only at close. That is untested.

## `marks.jsonl`

One JSON object per line, with keys sorted:

```json
{"kind": "flag", "payload": {"why": "respawn"}, "ts_rig_mono_ns": 123456789}
```

Kinds: `episode_start`, `episode_end`, `discard_last`, `flag`, `note`. `read_marks()` skips
and reports an incomplete final line.

## `events.log`

One JSON object per line: `ts_rig_mono_ns`, `ts_realtime_ns`, `level` (`info`, `warn`,
`error`), `event`, then event-specific fields. Written and flushed as each event happens.

| event | when |
|---|---|
| `session_created` | directory and manifest exist |
| `controls_started` | receiver bound; includes the granted socket buffer |
| `video_started` | vcap session open; includes what the driver granted |
| `mark` | a mark source produced a mark |
| `controls_quiet` / `controls_resumed` | no datagram for `control_quiet_warn_ms`, and its end. A warning, never a stop. |
| `control_anomalies` | gap, stale, restart, HMAC or malformed counters increased |
| `receiver_died` | the receiver process exited during the session; recording stops |
| `video_no_signal`, `video_stream_error`, `video_writer_overrun` | vcap raised; recording stops |
| `video_stopped`, `controls_stopped`, `session_finalized` | shutdown, in that order |

## `manifest.json`

Written at start:

| field | |
|---|---|
| `session_id`, `schema_version` (1), `started_at` | |
| `started` | a `CLOCK_MONOTONIC` / `CLOCK_REALTIME` pair sampled together, with its spread |
| `fps` | requested; what the driver granted is in `video/manifest.json` and `ended.video` |
| `recorder`, `submodules` | `{commit, dirty, error}` for this repo and each submodule |
| `config` | the merged profile, **inlined**, since config files get edited |
| `control_auth` | `hmac-sha256-trunc16` or `none`. The key is never recorded anywhere, and a test checks every session file for it. |
| `bridge_capabilities` | the bridge's own answer to `query_capabilities()` |
| `capture_device` | vcap's device identity, resolved through `/dev/v4l/by-id/` |
| `assumed_capture_offset_ns` | `null`. Written in deliberately, never defaulted. |
| `session_labels` | from `--label key=value` |
| `timebase`, `files`, `host` | |

Added under `ended` at stop: `at`, `clock`, `duration_ns`, `stop_reason`, `clean`,
`error`, `controls` (final counters, socket buffer, receiver exit status),
`controls_quiet_spans`, `video` (frames written, effective fps, granted mode, writer queue
high water), `marks_written`.

`stop_reason` is one of `max_session_seconds`, `signal:SIGINT`, `signal:SIGTERM` (all
clean), or `video_no_signal`, `video_stream_error`, `video_writer_overrun`,
`video_ended`, `receiver_died`, `receiver_failed_to_start`, `error:<Exception>`.

`controls_quiet_spans[].to_ns` is an upper bound, within one flush interval, on when a
silence ended. The exact figure is in `controls.gpb`.

## `preflight.json`

`schema_version`, `started_at`, `passed`, `checks[]` (`name`, `status`, `detail`, `data`)
and `preview_jpeg_base64`. Statuses are `pass`, `warn`, `fail` and `skipped`. Any `fail`
refuses the session; `warn` does not.
