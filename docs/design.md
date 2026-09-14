# episode_recorder — design

Status: design only. Nothing here has been built or measured. Every number
quoted from a submodule is that submodule's measurement, not this repo's.

---

## 1. What this repo is

A recorder that captures paired video and control streams from a human operator,
segments them into episodes, and emits a LeRobot dataset.

It owns the word **episode**. Neither submodule does, by their own design — see
`hdmi_capture/docs/composition.md`. An episode has a beginning, an end, a task
and an outcome, and all four are facts about data collection rather than about
video or about controllers.

### What it is not

- **Not game-aware.** The word "Mario" must never appear in this repo. Anything
  that requires knowing what is on screen is a plugin implemented elsewhere.
- **Not a training repo.** No torch, no policies, no training loops.
- **Not a replay or evaluation harness.** Those live in game-specific repos and
  import the action codec from here.
- **Not a re-implementation of either submodule.** If frame handling or gamepad
  wire format is being written here, something is wrong.

### Consumers

Game-specific repos submodule this one. Dependency points from specific to
generic and never the reverse:

```
mario_kart_policy/                 game repo (not this repo)
  third_party/
    episode_recorder/              this repo
      third_party/
        hdmi_capture/
        rpi_gamepad_bridge/
  mario_kart/                      Segmenter, LabelExtractor, action spec
  configs/
  data/                            sessions and datasets live in the game repo
```

Cloning requires `--recursive`. A submodule bump is a two-step commit (recorder
pointer, then game repo pointer). That friction is intentional.

---

## 2. Invariants

These are the rules the rest of the design follows from. A change that breaks
one of them is a redesign, not a patch.

1. **Raw capture is append-only and never rewritten.** Every correction happens
   downstream. A session killed by a crash or a full disk is readable up to its
   last complete record.
2. **Annotations live in a sidecar.** Marks, boundaries and labels never touch
   the raw streams.
3. **Only `ts_rig_mono_ns` pairs with video.** See §4.
4. **The submodules never import each other.** Enforced by a test.
5. **This repo never imports a game module.** Enforced by a test.
6. **The action codec is a leaf.** `encode`/`decode` import nothing from capture,
   V4L2, or the receiver. A replay repo must be able to import it alone.
7. **The dataset is derived and disposable.** Rebuilding it from raw plus
   annotations is one idempotent command.
8. **The recorder runs standalone.** With no plugins and no labels it still
   records and still builds a dataset, using fixed-length chunking. This is how
   hardware is brought up and how game number two starts.

---

## 3. Topology

```
  operator
     │ controller (USB)
     ▼
  Raspberry Pi 5 ───── USB HID gadget ──────► console
  (rpi_gamepad_bridge)
     │
     │ UDP, 48-byte GamepadState + HMAC, ~125 Hz, wired gigabit
     ▼
  rig ◄──── HDMI capture card (USB3) ◄──── console (HDMI loop-out ► monitor)
   │
   ├── control receiver  → controls.gpb
   ├── vcap Session      → video/
   └── mark source       → marks.jsonl
```

The console's HDMI goes through the card's **loop-out** to the operator's
monitor. Hardware pass-through adds no latency; no software preview can match
it, so this repo provides none.

The rig is also the inference rig later. It needs USB3, ~10 MB/s sustained
write, and a NIC to the Pi. The GPU is incidental to recording.

---

## 4. Time — the one contract that matters

There are **two unrelated monotonic clocks** in this system.

| Name | Origin | Use |
|---|---|---|
| `ts_pi_mono_ns` | Pi, stamped by `rpi_gamepad_bridge` | Intervals, jitter, drift analysis. **Never** paired with video. |
| `ts_rig_mono_ns` | Rig, stamped on `recvfrom` return | The pairing key. The only timestamp that aligns with frames. |
| `Frame.ts_mono_ns` | Rig kernel, V4L2 buffer | Frame time, carried untouched by `vcap`. |

`hdmi_capture` pairs on `CLOCK_MONOTONIC` and states the constraint plainly:
same machine, or alignment is a lie. Because controls originate on the Pi, the
Pi's stamp does not satisfy that. **Arrival time on the rig does.** Transit on a
wired direct link is ~0.1 ms, which is noise against the ~60 ms capture offset.

Store both. Regressing one against the other offline gives link jitter and a
clear signal when the Pi's loop stalls, for free.

### Offsets

Two distinct corrections, applied in the builder, never baked into raw:

- **`capture_offset_ns`** — frames arrive tens of ms after the pixels reached the
  operator's display. Not applying it trains the policy to act on information it
  has not yet received. `hdmi_capture` deliberately leaves
  `timebase.capture_offset_ns` null and will not fill it in automatically;
  writing a number in is a deliberate act. Measure with `vcap-glass-to-glass`,
  then confirm with a calibration episode (§11).
- **`reaction_offset_ns`** — the human's ~200 ms reaction time. Whether to
  compensate is a modeling question, not a data one. Default 0, switchable at
  build time.

Both live in the build config and are recorded in the emitted dataset metadata.

---

## 5. Layout

```
episode_recorder/
  third_party/
    hdmi_capture/                  submodule, pinned
    rpi_gamepad_bridge/            submodule, pinned
  episode_recorder/
    __init__.py                    also sets up submodule sys.path
    bootstrap.py                   the one place that knows third_party paths
    actions/
      spec.py                      ActionSpec: dims, sources, ranges, defaults
      codec.py                     encode / decode — LEAF, no capture imports
    capture/
      receiver.py                  UDP socket loop, arrival stamping, seq gaps
      control_store.py             fixed-width append-only writer/reader
      marks.py                     MarkSource interface + implementations
      session.py                   orchestration, manifest, preconditions
      preflight.py                 pre-session checks
    annotate/
      model.py                     Annotation / EpisodeSpan types, JSON schema
      segment.py                   runs Segmenter plugins, writes annotations
      cli.py                       chunk / list / edit from the terminal
      server.py                    trimming UI (later; stdlib only)
    build/
      resample.py                  control stream → per-frame action rows
      holes.py                     hole detection and policy
      dataset.py                   LeRobot v3 emission, create/append/finalize
      augment.py                   mirror and other build-time augmentations
      verify.py                    renders paired frames + actions for a human
    plugins/
      base.py                      Segmenter, LabelExtractor, EpisodeFilter
      loader.py                    resolves "module:Class" strings from config
    configs/
      default.toml                 standalone: fixed chunks, no plugins
  tools/
    er-preflight                   check hardware and link without recording
    er-record                      run a session
    er-segment                     marks + plugins → annotations.json
    er-build                       annotations → LeRobot dataset
    er-verify                      render paired frames for eyeballing
    er-archive                     raw session → 480p archival mp4
  tests/
    run_tests.sh                   hardware-free
    hardware.sh                    with card and Pi
    fixtures/golden_session/       synthetic frames + controls, known answer
  docs/
    design.md                      this file
    timebase.md                    the two-clock contract, in detail
    formats.md                     on-disk formats
    plugins.md                     how a game repo extends this
  CLAUDE.md
```

`bootstrap.py` is the only module that knows submodule paths
(`third_party/hdmi_capture/vcap_py`, `third_party/rpi_gamepad_bridge/clients/python`).
Consumers import `episode_recorder` and everything resolves. Without this, the
internal layout leaks into every game repo and can never be reorganised.

---

## 6. On-disk formats

### Data root (in the game repo)

```
data/
  sessions/<session_id>/      raw — deletable after §11 passes
    manifest.json
    preflight.json
    video/                    vcap Session output (.mjpg + .idx + its manifest)
    controls.gpb
    marks.jsonl
    events.log
  annotations/<session_id>.json    retained indefinitely
  archive/<session_id>.mp4         retained — 480p H.264, ~1–2 GB/hr
  datasets/<name>/                 LeRobot v3 — derived, disposable
```

Retention: raw is deleted only after a dataset has been built from it **and a
policy has trained and run**. The archival copy plus annotations keeps a rebuild
possible at a different resolution or action spec afterwards.

### `controls.gpb`

64-byte header, then fixed 64-byte records. Fixed width so timestamp lookup is a
binary search over an `mmap` — the same access pattern `vcap`'s `.idx` gives on
the video side.

```
header (64 B):
  magic        u32   'ERC1'
  version      u16
  record_size  u16   64
  struct_fmt   char[32]  "<IHHIIQQhhhhBB6s"   as advertised by the bridge
  reserved     [..]

record (64 B):
  state        u8[48]   GamepadState, verbatim, unparsed
  arrival_ns   u64      ts_rig_mono_ns
  flags        u32      bit0 seq gap before this record
                       bit1 HMAC failure
                       bit2 stale (seq regression)
  reserved     u32
```

48 + 8 + 4 + 4 = 64 exactly. It fits because everything you would expect to need
room for is already inside the 48-byte struct: `<IHHIIQQhhhhBB6s` is magic(4)
version(2) size(2) seq(4) buttons(4), **two u64 timestamps** (`CLOCK_MONOTONIC`
and `CLOCK_REALTIME`), four axes(8), two u8, 6 pad. So the sequence number and
`ts_pi_mono_ns` ride along at no cost.

The 32-byte HMAC-SHA256 tag is **verified at the socket and discarded** — only
the pass/fail bit is kept. Storing it would not fit and would serve nothing.

The 48 bytes are stored **unparsed**. Parsing is the codec's job and happens
offline. The receiver reads the sequence number only, to detect gaps.

Headroom is 4 reserved bytes, which is thin — a sender address or a receive-path
marker would not fit. That is why `record_size` is a header field: bump
`version`, change `record_size`, readers dispatch. At ~8 KB/s, going to 128-byte
records would cost ~58 MB/hour, so the format should never need a migration.

At ~125 Hz this is ~8 KB/s. Storage is a non-issue; the only real risk on this
path is stalling the socket.

### `marks.jsonl`

One JSON object per line, appended, never rewritten.

```json
{"ts_rig_mono_ns": 123456789, "kind": "episode_start", "payload": {}}
```

Kinds: `episode_start`, `episode_end`, `discard_last`, `flag`, `note`.
Mark latency does not matter — ±1 s is fine, because boundaries are refined
offline.

### `manifest.json`

Written at session **start**, so a crashed session is still identifiable.

```json
{
  "session_id": "2026-09-13T14:02:11Z-a3f9",
  "schema_version": 1,
  "started_at": "2026-09-13T14:02:11Z",
  "fps": 30,
  "submodules": {"hdmi_capture": "<sha>", "rpi_gamepad_bridge": "<sha>"},
  "recorder_commit": "<sha>",
  "config": { "...": "the game config, INLINED not referenced" },
  "bridge_capabilities": { "trigger_mode": "digital", "axes": ["..."] },
  "capture_device": {"usb_id": "345f:2131", "by_id": "/dev/v4l/by-id/..."},
  "assumed_capture_offset_ns": null,
  "session_labels": {"game": "...", "mode": "..."}
}
```

Config is inlined rather than referenced because config files get edited.
`bridge_capabilities` comes from `query_capabilities()` — a client cannot know
whether a target's triggers are analog or digital, and guessing wrong fails
silently, so the answer is recorded with the data.

### `annotations/<session_id>.json`

```json
{
  "schema_version": 1,
  "session_id": "...",
  "generated_by": "er-segment 0.1.0 / mario_kart.segmenter:LapSegmenter",
  "session_labels": {"game": "...", "course": "..."},
  "episodes": [
    {
      "id": "ep-0007",
      "start_ns": 1234, "end_ns": 5678,
      "excluded_spans": [[2000, 2400]],
      "labels": {
        "task": "drive the course",
        "outcome": "success",
        "episode_type": "clean",
        "extra": {"lap_time_s": 92.31, "lap_index": 2}
      },
      "spans": [{"start_ns": 3000, "end_ns": 3600, "text": "..."}]
    }
  ]
}
```

`task`, `outcome` and `episode_type` are **reserved** keys the builder
understands. `extra` is free-form and passed through untouched. Session-level
labels are inherited by every episode and overridden per episode, so `game` and
`course` are typed once rather than 200 times.

`task` must be reserved because in LeRobot v3 task strings map to integer IDs in
`meta/tasks` for task-conditioned policies. A typo does not fail — it silently
creates a second task ID and fragments language conditioning across two labels
that look identical to a human.

A game may ship an optional JSON Schema, validated at **annotation** time and
never at capture time. No schema means no validation, so the recorder stays
agnostic by default.

`excluded_spans` mark stretches *within* an episode that are not demonstration —
menus, pauses, respawns, idle. Boundary-only cutting cannot express these.

---

## 7. Components

### 7.1 Control receiver (`capture/receiver.py`)

`recvfrom` → stamp `CLOCK_MONOTONIC` → read seq → append → loop. Nothing else.
No pairing, no decoding beyond the sequence number, no disk flush in the hot
path. Pairing inside a capture loop is the first of the three mistakes
`composition.md` lists, and it is how frames get dropped.

- Size `SO_RCVBUF` generously (≥ 8 MB). At 8 KB/s that is minutes of slack, so a
  GC pause or a scheduling hiccup cannot lose a datagram.
- **Sequence gaps are the only visibility into loss.** There are two independent
  loss paths: UDP on the wire, and the bridge's bounded ring overflowing — which
  the bridge reports as *sent*, never as delivered, because UDP cannot tell you
  whether anyone received it. Flag the record after a gap and count gaps per
  session.
- Stale datagrams (seq regression) are flagged and kept, not dropped. The
  builder decides.

### 7.2 Preflight (`capture/preflight.py`)

Runs before every session. Refuses to start on failure. Cheapest thing in the
pipeline and the most likely to save a collection day.

- Frames flowing from the card, **and** the card's HDMI input is live. The card
  emits healthy-looking frames with nothing connected; no counter can detect
  this. Prompt the operator to confirm the picture, and save a preview JPEG into
  `preflight.json` so it is auditable after the fact.
- Control datagrams arriving with **advancing** sequence numbers. Video
  recording perfectly while controls are absent, stale, or coming from a bridge
  restarted an hour ago is the exact mirror failure.
- `query_capabilities()` matches the config's action spec — trigger mode and
  axis list.
- Free disk ≥ estimated session size × 1.5. At 30 fps budget ~27 GB/hour.
  `hdmi_capture` lists behaviour as a disk approaches full as untested.
- Device resolved through `/dev/v4l/by-id/`, never `/dev/videoN`.

During the session, a watchdog hard-flags or aborts if either stream goes quiet
for more than ~1 s, and writes the reason to `events.log`.

### 7.3 Session runner (`capture/session.py`)

Starts the `vcap` Session and the receiver as independent paths — neither can
stall the other — writes the manifest first, and stops both on mark, timeout or
signal. Passes `notes={"session_id": ...}` into the vcap manifest so a video
directory is traceable back to a session even if everything else is lost.

Recording is **continuous for the whole session**. Episode boundaries are
annotations, not recording modes. This is what makes a late mark cost nothing,
allows retroactive `discard_last`, and lets one raw session be re-segmented
years later under a different episode definition.

### 7.4 Mark sources (`capture/marks.py`)

```python
class MarkSource(Protocol):
    def poll(self) -> Iterable[Mark]: ...   # (ts_rig_mono_ns, kind, payload)
```

Implementations: `WebMarkSource` (buttons in a browser, first), `EvdevMarkSource`
(footpedal or macro pad, ~40 lines, later), `ChordMarkSource` (controller chord —
needs a bridge-side change to suppress the chord from reaching the console, and
the chord may be unusable mid-game; last).

The UI is a separate process talking to the runner over a Unix socket. The bridge
is a real-time loop and has no business hosting an HTTP server; the same reasoning
applies here. Follow `rpi_gamepad_bridge/web` — stdlib only, no venv, no pip, no
build step, enforced by a test.

With fixed-length chunking (§7.6) marks are mostly quality flags rather than
boundaries, which further reduces how much their timing matters.

### 7.5 Action codec (`actions/`)

**The most important module in the repo**, and a hard leaf.

```python
class ActionSpec:
    dims: list[ActionDim]          # name, source, kind, range
    defaults: dict[str, int|float] # every field NOT in dims

def encode(state: GamepadState, spec: ActionSpec) -> np.ndarray: ...
def decode(action: np.ndarray, spec: ActionSpec) -> GamepadState: ...
```

- `decode` defines **every** unmodeled field explicitly. If throttle was dropped
  as constant, `decode` sets it held — that is a recorded decision, not an
  implicit zero.
- `encode`/`decode` are written as a matched pair in the same module, with a
  round-trip property test over random states.
- The spec is **embedded in the emitted dataset** via the LeRobot feature `names`
  list (`names: ["steer_x", "accel", "drift"]`), so the dataset is
  self-describing and the replay repo never guesses. This is the direct fix for
  the dimension-mismatch failure (`huggingface/lerobot#1839`).

The bridge's premise is that capture and replay are the same pipeline in two
directions and a recorded session replays bit-for-bit. That only holds for your
data if the codec round-trips.

### 7.6 Segmentation (`annotate/`)

Offline. Consumes marks plus `Segmenter` plugin output, emits
`annotations/<id>.json`.

Built-in strategies:

- **`fixed_chunk`** — the default and the standalone path. Non-overlapping
  windows of configurable length. Arbitrary cut points are a *feature* for a
  general policy: diverse initial states rather than always starting from the
  same place. **Do not overlap** — LeRobot samples every frame as a potential
  training start anyway, so overlap adds no coverage and leaks correlated frames
  across a train/val split.
- **`marks`** — `episode_start` / `episode_end` pairs with configurable pre/post
  padding.
- **plugin** — a game repo's `Segmenter`.

Boundary policy for held inputs is explicit config: `snap_to_neutral`,
`trim_lead_in`, or `none`. A chunk that opens mid-manoeuvre shows a held button
with no visible cause.

#### Exclusions split the episode

`excluded_spans` cut their episode into contiguous fragments rather than
punching holes in it. One annotated episode can therefore emit N dataset
episodes.

This keeps the two layers doing what each is good at: **annotations stay
human-meaningful** (one lap is one entry, with the menu pause marked inside it),
while the **dataset gets clean contiguous episodes** with no discontinuities for
the policy to learn from.

Consequences that must be handled:

- **Holes use the same machinery.** `holes.policy = "split"` and exclusions both
  reduce to one function: given a span and a set of cuts, return the contiguous
  runs. Write it once.
- **Minimum fragment length.** `min_episode_seconds` drops fragments too short to
  be useful. A 0.4 s remnant is noise. It must also exceed
  `history_window + action_chunk` to be trainable at all.
- **Fragments carry `parent_id` and `fragment_index`.** Needed for tracing back
  to the annotation, and needed for splitting: fragments of one episode are
  highly correlated and **must not straddle a train/val boundary**, for the same
  reason sessions must not.
- **Not every label survives fragmentation.** `course` applies to any fragment;
  `lap_time_s` describes the whole lap and is meaningless on fragment 2 of 3.
  The label schema must mark keys as `inheritable` or `whole_episode_only`, and
  the builder drops the latter from fragments rather than propagating a number
  that is quietly wrong. Reserved keys default to inheritable except where the
  game schema says otherwise.
- **Report it.** `er-verify` reports how many dataset episodes came from
  fragmentation and how many fragments were dropped as too short. A segmenter
  bug that shreds every episode into 2 s pieces should be loud, not subtle.

Episodes must be comfortably longer than `history_window + action_chunk`. At
30 Hz, 10 s is 300 frames against an ACT chunk of 100.

One raw session yields multiple datasets under different segmentation configs.
That is the payoff of doing this offline.

### 7.7 Resampler (`build/resample.py`)

Control at ~125 Hz → one action row per video frame.

**Window aggregation, not nearest-sample.** A 24 ms button press falls entirely
between two frames at 30 Hz and nearest-sample drops it silently. For each frame
interval:

- buttons: `any` (OR the bits) — default; `last` available
- axes: `last` — default; `mean` available

Configurable, and which was used is recorded in the dataset metadata.

Also emits, as separate columns so training can toggle them without a rebuild:

- `action` — the encoded vector
- `action.prev` — the previous frame's action

`action.prev` exists but **train without it first.** Previous-action-as-input is
the classic copycat setup: the model learns to repeat its last action rather than
read the scene, and looks excellent on validation loss while being useless in
closed loop. Prefer observation history via `delta_timestamps` at load time,
which carries the same temporal information without the shortcut — and do not
store history as denormalised columns, since `delta_timestamps` keeps the window
a training hyperparameter.

### 7.8 Holes (`build/holes.py`)

Not an edge case. The card loses roughly one frame in a thousand — about one
every 16 s at 60 Hz, measured over a 20-minute burn-in — and loss is per frame
rather than per second, so halving the rate halves losses per minute but leaves
any given frame just as likely to be lost. At 10–30 s episodes most episodes will
contain a hole. `Recording.flagged()` lists them, along with the undecodable
first-frame fragment present in nearly every stream.

Policy is config, applied per episode, recorded in metadata:

- `duplicate_previous` — fill and flag
- `reject_episode` — drop the whole episode
- `split` — cut the episode at the hole

LeRobot expects a contiguous uniform-rate sequence and checks timestamps against
the nominal fps grid within a small tolerance. So **write synthesised grid
timestamps (`index / fps`) into the dataset and keep the true kernel timestamps
in a separate column.** Never feed it raw jittered times.

### 7.9 Dataset builder (`build/dataset.py`)

Emits LeRobot v3 via the library, not by hand-writing Parquet. v3 concatenates
many episodes into shared Parquet and MP4 shards with boundaries resolved
through `meta/episodes` rather than filenames, which suits short numerous
episodes.

Flow: `LeRobotDataset.create(...)` → per episode `add_frame` / `save_episode` →
`finalize()`. `finalize()` closes the Parquet writers and writes metadata
footers and must be called before `push_to_hub()`. **Append is a first-class
path**, not a rebuild — required by the retention policy, and a different code
path from build-once.

Feature naming follows LeRobot convention so its policies and visualisers work:
`observation.images.<camera_key>`, `action`, optionally `observation.state`.

ACT can run image-only: its config notes it may optionally work without an
`observation.state` key for proprioceptive state. There is no proprioception in a
game, so image-only is the baseline.

**Degenerate dimension guard.** Compute per-dimension variance and **refuse
loudly** to emit a dimension that never changes. A constant feature breaks
mean/std normalisation. Either drop it from the action space and have `decode`
hold it constant at replay, or switch that feature to min/max via the policy's
`normalization_mapping` — deliberately, not by accident. Do not reason about
which dims are constant; measure.

Downscaling happens **here**, not at capture. Policies train at ~224×224, so raw
stays reusable and changing resolution is a rebuild rather than a recollection.

Split assignment is **by session**, and `session_id` survives into episode
metadata. Consecutive episodes from one sitting share level, strategy and warm-up
state; splitting within a session inflates validation scores.

### 7.10 Augmentation (`build/augment.py`)

Build-time only. Mirror: flip the frame, negate designated axis dims. Free data,
but the HUD and minimap flip too and asymmetric features become fictional. Off by
default; verify visually before trusting it.

---

## 8. Plugin interfaces (`plugins/base.py`)

Game repos implement these. This repo never imports them directly — the config
names an import path and `plugins/loader.py` resolves it:

```toml
segmenter = "mario_kart.segmenter:LapSegmenter"
label_extractors = ["mario_kart.labels:LapTimeOCR"]
```

Naming rather than injecting keeps this repo's CLI usable as-is from a game repo.

```python
class Segmenter(Protocol):
    def segment(self, session: SessionReader, marks: list[Mark]) -> list[EpisodeSpan]: ...

class LabelExtractor(Protocol):
    def extract(self, session: SessionReader, span: EpisodeSpan) -> dict: ...

class EpisodeFilter(Protocol):
    def keep(self, episode: AnnotatedEpisode) -> bool: ...
```

`SessionReader` is the read API over a raw session — video via
`vcap.Recording`, controls via the fixed-width reader, marks, manifest. It is
the same role `vcap.Recording` plays for video, and it is what lets an OCR pass
in the game repo run over raw frames without this repo knowing what OCR is for.

The split is by *what is game knowledge*, not by pipeline stage. Resampling,
hole filling, the timestamp grid and v3 emission are identical across games and
will silently diverge if each game repo owns a copy — exactly the class of bug
that produces plausible training and meaningless policies.

---

## 9. Configuration

One TOML file per game profile. Standalone default ships in
`episode_recorder/configs/default.toml`.

```toml
[capture]
fps = 30
device_match = "MACROSILICON"
control_bind = "0.0.0.0:9001"
max_session_seconds = 3600

[action]
dims = [
  { name = "steer_x", source = "axis.left_x", kind = "continuous", range = [-1.0, 1.0] },
  { name = "accel",   source = "button.a",    kind = "binary" },
  { name = "drift",   source = "button.r",    kind = "binary" },
]
[action.defaults]           # what decode() writes for everything not in dims
"button.b" = 0
"axis.right_x" = 0.0

[segment]
strategy = "fixed_chunk"    # fixed_chunk | marks | plugin
chunk_seconds = 15
min_episode_seconds = 4     # fragments shorter than this are dropped
boundary_policy = "trim_lead_in"
pre_pad_ms = 500
post_pad_ms = 500

[resample]
buttons = "any"             # any | last
axes = "last"               # last | mean

[holes]
policy = "duplicate_previous"
max_holes_per_episode = 3

[build]
resolution = [224, 224]
capture_offset_ns = 0       # MUST be set deliberately after measuring
reaction_offset_ns = 0
include_action_prev = true
mirror_augment = false
task_from = "labels.task"

[labels]
reserved = ["task", "outcome", "episode_type"]
whole_episode_only = []     # keys dropped from fragments, e.g. ["extra.lap_time_s"]
schema = ""                 # optional path to a JSON Schema

[plugins]
segmenter = ""
label_extractors = []
episode_filters = []
```

---

## 10. Hardware notes

- **Rig**: i7 / 64 GB / RX 6800 XT. Adequate for capture and for later inference
  (a ResNet-18-backbone ACT at 30 Hz is undemanding). Recording needs no GPU.
- **Training**: the 6800 XT means ROCm, which is under-tested in this stack —
  fine for inference, a tax for training. LeRobot's hardware guide puts `act` in
  its lightest tier at roughly 2–6 GB peak VRAM at batch size 8, with a laptop
  RTX 3060 listed as a suitable starter GPU; `smolvla` at ~10–16 GB; and the
  large VLAs at ~24–40 GB. So ACT trains on the RTX 2070 box or on a rented L4 /
  A10G for a few dollars a run. Defer any GPU purchase until SmolVLA makes 24 GB
  worth buying. Training convergence in this space is typically 5–10 epochs over
  the dataset, so runs are hours.
- **Scratch**: ~27 GB/hour raw at 30 fps. 1 TB ≈ 35 hours.

---

## 11. Verification

### Calibration episode — run before any real collection

Press something with an unmistakable visual consequence (a pause menu, a weapon
swap) a dozen times. Measure the lag between the control edge and the pixel
change. If it does not match the assumed `capture_offset_ns`, the whole alignment
chain is wrong and you want to know now rather than after twenty hours.

### `er-verify`

Loads the **built** dataset back through the LeRobot library — not through this
repo's own reader — and renders paired frames and actions as a contact sheet or
short video for a human to look at.

This is not optional and it is not replaceable by assertions. Every alignment bug
here — a sign error on the offset, an off-by-one in resampling, the wrong
timebase — is instantly obvious to the eye and nearly invisible to a test.
Segmentation bugs in particular raise no runtime errors; they mis-assign frames
and produce training that looks stable and policies that are meaningless.

Also reported per session: gap count, hole count and positions, per-dimension
action histograms and variance, and episode count by label.

---

## 12. Testing

Both submodules take hardware-free testing seriously — `gpb-fakepad` provides a
uinput virtual gamepad, and synthetic JPEGs stand in for the card. Inherit that.

- **Golden session fixture**, checked in: a few seconds of synthetic frames plus
  a matching control stream with a known correct answer. Tests resampling (a tap
  that falls between frames **must** survive), hole filling, boundary policy, and
  the built dataset's contents. The resampler is exactly the kind of code that
  does the wrong thing silently for months.
- **Codec round-trip**, property-based over random states.
- **Boundary enforcement**: no import of a game module from this repo; no import
  between submodules; the web UI imports stdlib only. Enforced in CI, following
  `hdmi_capture/tests/check_stdlib_only.py`.
- **Crash safety**: truncate `controls.gpb` mid-record and confirm the reader
  recovers everything before it.
- **`tests/hardware.sh`** for anything needing the card or the Pi. Note that
  `hdmi_capture` reports a path regression once reached `main` because CI cannot
  exercise the V4L2 path; expect the same gap here.

---

## 13. Build order

1. `bootstrap.py`, `control_store`, `receiver`, `preflight`, `session` — record a
   session end to end with fixed chunks and no plugins.
2. Calibration episode and `er-verify` on raw pairs. **Do not proceed until
   alignment is confirmed visually.**
3. `actions/` with round-trip tests. Golden session fixture.
4. `resample`, `holes`, `er-segment` with `fixed_chunk`.
5. `build/dataset.py` → a LeRobot dataset; load it back and look at pairs.
6. Train one ACT on it. This is the real acceptance test for everything above.
7. `plugins/`, `er-archive`, append path, mirror augmentation.
8. Trimming UI — last, once experience says which part of trimming is annoying.

---

## 14. Open questions

- Whether `capture_offset_ns` should be per-session (re-measured) or per-rig
  (measured once and pinned). `hdmi_capture` deliberately refuses to fill it in;
  this repo should be equally deliberate.
- Whether to record audio. It is not touched by either submodule, and
  push-to-talk narration transcribed offline would give timestamped language
  annotations nearly free — which is exactly the input a VLA wants and is
  miserable to produce any other way. Deferred, not rejected.
- Multi-camera is not designed for. The LeRobot key is `observation.images.<key>`
  so it is not precluded, but nothing here handles two cards, and
  `hdmi_capture` notes the `by-id` serial may not be unique on this chipset
  family.

---

## 15. Conventions to carry over

From `rpi_gamepad_bridge` and `hdmi_capture`, and they do not survive by
accident:

- **Measured, not asserted.** Numbers in the README come from running against
  the hardware. Where a claim is second-hand, it says so on the spot.
- **Record corrections.** When a measurement contradicts a documented claim, the
  correction goes in the commit message and the claim gets fixed.
- **A specific `Status` section** listing what is known-untested, deliberately
  specific and not claiming to be exhaustive.
- **Name the traps.** Both READMEs have a table of failure modes that cost the
  most time. This repo will earn its own; write them down as they are found.
- **Enforce boundaries with tests**, not with intent.
