# CLAUDE.md — episode_recorder

Read `docs/design.md` before writing code. This file is the short version: the
rules that must not be broken and the conventions this project holds itself to.

---

## What this repo is

Captures paired video and control streams from a human operator, segments them
into episodes, and emits a LeRobot dataset.

It owns the word **episode**. Neither submodule does, by their own design —
`third_party/hdmi_capture/docs/composition.md` says so explicitly.

## What this repo is not

- **Not game-aware.** No game name, no game mechanic, no game-specific heuristic
  appears anywhere in this repo. Ever.
- **Not a training repo.** No torch, no policies, no training loops.
- **Not a replay or evaluation harness.** Those are separate repos that import
  the action codec from here.
- **Not a reimplementation of the submodules.** If you are writing V4L2 ioctls or
  gamepad wire-format parsing, stop — it already exists below you.

---

## Hard rules

Breaking one of these is a redesign, not a patch. If a task seems to require it,
say so and stop rather than working around it.

1. **Raw capture is append-only and never rewritten.** Corrections happen
   downstream. A session killed mid-write must be readable up to its last
   complete record.
2. **Annotations live in a sidecar.** Marks, boundaries and labels never touch
   the raw streams.
3. **Only `ts_rig_mono_ns` pairs with video.** `ts_pi_mono_ns` comes from a
   different machine's clock and is for jitter analysis only. Never compare them
   as if they share an epoch. See §4 of the design doc.
4. **This repo never imports a game module.** There is a test. Keep it passing.
5. **The submodules never import each other.** Their rule; do not break it from
   above.
6. **`actions/codec.py` is a leaf.** It imports nothing from capture, V4L2 or the
   receiver. A replay repo must be able to import it alone.
7. **Nothing but `bootstrap.py` knows submodule paths.** No `sys.path.insert` for
   `third_party/...` anywhere else.
8. **The recorder runs standalone.** With no plugins, no labels and fixed-length
   chunking it still records and still builds a dataset. Do not add a required
   plugin.
9. **Never do per-sample work inside a capture loop.** Receive, stamp, append,
   loop. Pairing, parsing and disk flushes happen offline. This is the first of
   the three mistakes `composition.md` names.

---

## Language and dependencies

Python. The control stream is ~8 KB/s and the video path never decodes — there is
no performance argument for C++ here, and `third_party/hdmi_capture/vcap_cpp`
already exists if one ever appears.

- Core capture, storage and read-back: **standard library only**. Enforced by a
  test, following `hdmi_capture/tests/check_stdlib_only.py`. A machine should be
  able to record a session with no venv, no pip and no build step.
- The web UI: **standard library only**, separately enforced. `rpi_gamepad_bridge/web`
  is the model — no venv, no pip, no build step, no framework.
- `build/` may depend on numpy, pillow/opencv, ffmpeg and lerobot. That boundary
  is explicit and one-directional: nothing in `capture/` may import from
  `build/`.

Do not add a dependency without saying why the standard library cannot do it.

---

## Conventions inherited from the sibling repos

These do not survive by accident. Maintain them.

- **Measured, not asserted.** Any number in a README or doc comes from running
  against real hardware. If a claim is second-hand or estimated, say so on the
  spot, in the same sentence.
- **Record corrections.** When a measurement contradicts a documented claim, fix
  the claim and put what was measured and what was wrong in the commit message.
- **A specific `Status` section.** List what is known-untested, deliberately
  specific, and say it is not exhaustive.
- **Name the traps.** Both sibling READMEs carry a table of the failure modes
  that cost the most time. This repo will earn its own; write them down as they
  are found, not later.
- **Enforce boundaries with tests, not intent.** Every rule above that can be
  tested, is.
- **Say when something is AI-generated** and has not had independent human
  review. Both sibling repos do this at the top of the README.

---

## Code conventions

- Fixed-width binary records for anything that needs timestamp lookup, so it is a
  binary search over an `mmap`. `hdmi_capture`'s 32-byte `.idx` is the model;
  `controls.gpb` uses 64.
- Self-describing formats: magic, version and record size in the header, so a
  format change is a version bump and a dispatch, never a migration.
- Protocols (`typing.Protocol`) for plugin interfaces, not ABCs.
- Plugins are named in config as `"module:Class"` strings and resolved by
  `plugins/loader.py`. Do not require a game repo to construct and inject
  objects — that would make the CLI unusable from outside.
- Explicit config over inferred behaviour. Every choice that affects the emitted
  dataset (resampling mode, hole policy, offsets, boundary policy) is written
  into the dataset metadata so a dataset says how it was built.
- Errors that matter are loud. A degenerate action dimension, a missing capture
  offset, a shredded episode set — these fail or warn visibly. Silence is the
  failure mode this whole project is guarding against.

---

## Testing

`./tests/run_tests.sh` must pass with no hardware, no card and no Pi.

- **Golden session fixture** (`tests/fixtures/golden_session/`): synthetic frames
  plus a matching control stream with a known correct answer. Every change to
  resampling, hole handling or segmentation is tested against it. A button tap
  that falls between two frames **must** survive resampling — that test is not
  optional.
- **Codec round-trip**, property-based over random states. `encode` then `decode`
  must reproduce a valid `GamepadState` with every unmodeled field at its
  declared default.
- **Crash safety**: truncate `controls.gpb` mid-record; the reader recovers
  everything before the truncation.
- **Boundary tests**: no game imports, no cross-submodule imports, stdlib-only
  where declared.
- Confirm a new test fails when the behaviour it covers is broken.
  `hdmi_capture` verified its suite by mutation; do the same rather than
  assuming.

`./tests/hardware.sh` covers anything needing the card or the Pi. CI cannot run
it — `hdmi_capture` notes a path regression reached `main` because of exactly
this gap. Assume the same blind spot here and say so in `Status`.

---

## Things that look like bugs and are not

- **The first frame of nearly every recording is an undecodable fragment.** It is
  the back half of a frame in flight when `STREAMON` landed. Flagged, not hidden.
  Filter on `Recording.flagged()`.
- **The card emits frames with nothing connected to its HDMI input.** No counter
  can detect this. Preflight asks the operator to confirm the picture.
- **Control capture reports "sent", never "delivered".** UDP cannot tell you
  whether anyone received it. Sequence numbers are the only visibility into loss.
- **~1 frame in 1000 is lost** and loss is per-frame, not per-second. At episode
  lengths here, most episodes contain a hole. That is the common case, not an
  edge case.
- **`timebase.capture_offset_ns` is null** and stays null until someone measures
  it and writes it in deliberately. Do not auto-populate it.

---

## When making changes

- Read `docs/design.md` first. It carries reasoning this file compresses away.
- If a change alters an on-disk format, bump the version, update `docs/formats.md`,
  and keep the old reader path.
- If a change alters how a dataset is built, it must be reflected in the emitted
  metadata. A dataset that cannot say how it was built is not reproducible.
- Prefer failing a build loudly over emitting a dataset that trains without
  complaint and produces a meaningless policy. That specific failure — stable
  loss, useless policy — is the one this design exists to prevent, and it is
  invisible without deliberate effort.
