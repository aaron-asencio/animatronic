---
inclusion: fileMatch
fileMatchPattern: "src/{movements,animatronic,controller,concurrentMovements}.py"
---
# Authoring a Gesture — animatronic-v2

Cheap, repeatable workflow for adding or tuning a Gesture/Routine. The operator
validates physical servo limits on the hardware, so authoring does NOT need to
re-prove safety on every change — it needs to run clean, return to rest, and not
regress the shared primitives. Follow this by default; it keeps iterations fast
and low-cost.

## 1. Ask for landmarks up front

Most of the cost of a new gesture is discovering pose angles by reading the
codebase. Before exploring, use the landmarks the operator provides. A complete
gesture request should include, per joint:

- **center** and **range/half_range** (or explicit LO/HI angles),
- **rest** angle (default to `constants.REST_POSITIONS[channel]`),
- motion feel (eased `move_to` vs. `randomized_centering_move`, steps/delay).

If a landmark is missing, prefer the documented values already in the codebase
(`constants.REST_POSITIONS`, `constants.SAFE_LIMITS`, the `ARM_DESTINATION_POSES`
dict in `constants.py`, and the nearest existing gesture) over a fresh
full-file search. Only search when no documented value exists.

### Staggered starts are expressed as TIMELINE FRACTIONS, not angles

`move_to`'s `start_fractions` staggers a joint by a fraction of the shared
motion timeline (0.0–1.0), NOT by another joint's angle. The operator drives
these by timeline fraction directly.

So when a request phrases a stagger as an **angle threshold** — e.g. "as
`RT_ELBOW_TILT` reaches 100, start to flex the wrist" — do NOT try to convert
the angle into a fraction (the smoothstep easing makes that conversion
non-linear and error-prone). Instead, **ask the operator for the timeline
fraction** they want the joint to start at, and use that value verbatim in
`start_fractions`. Do not guess or compute a fraction from the angle on their
behalf.

`ARM_DESTINATION_POSES` is the canonical table of each gesture's peak/hold arm
pose (the four right-arm channels) plus the `verified_pose_override` it needs —
read it instead of re-deriving a hand-to-mouth or reach pose. It includes
`yawnCover` and `coverMouth` (the shared cover-mouth hold used by `clearThroat`,
the coughs, `burp`, and `fart`): elbow cover **162**, shoulder tilt 35, rotator
200, forearm 185, under the `RT_SHOULDER_TILT: (35,270)` / `RT_ELBOW_TILT:
(0,170)` override.

## 2. Reuse, don't re-derive

- Compose from existing primitives: `_wave_arm`, `randomized_centering_move`,
  `neck_ellipse`, the shake_no / menacing_reach helpers. Do not re-implement a
  swing/sweep that a primitive already provides.
- Keep arm (channels 4–7) and head (0–1) on **disjoint** channels when running
  concurrently via `asyncio.gather` — never drive one channel from two
  coroutines.
- To change ONE routine that shares a helper (e.g. `startParty` and `evilLaugh`
  both used `wave_and_swivel`), add a NEW gesture and repoint only that routine.
  Do not mutate a shared helper unless every caller should change.

## 3. Always return to rest

End every gesture with the arm and head eased back to `REST_POSITIONS`
(head pan/tilt to 90/90). Match each channel's documented rest exactly — e.g.
`RT_ELBOW_TILT` rests at **5**, not 0. A gesture that leaves a joint off its
rest angle is a bug even if it looks fine on the bench.

## 4. Single sim smoke-check (not the full suite)

One hardware-free run is enough to confirm "runs clean, returns to rest":

```bash
SERVO_SIM=1 .venv/bin/python -c "
import os, sys, asyncio, random
sys.path.insert(0, os.path.abspath('src'))
import constants
from movements import Movements
random.seed(0)
mv = Movements('check')
asyncio.run(mv.<gesture_name>())
for ch in constants.REST_POSITIONS:
    print(constants.servos[ch], mv.trunkController.kit.servo[ch].angle,
          'rest=', constants.REST_POSITIONS[ch])
"
```

`random.seed(0)` makes the run deterministic. Reading back final angles is the
check that catches rest-pose mismatches before the operator ever runs it on the
Pi.

## 5. Run only the RELEVANT test file

Do NOT run the whole `pytest` suite for a motion change — it is slow and mostly
unrelated. Run the targeted file(s):

```bash
SERVO_SIM=1 .venv/bin/python -m pytest tests/test_collision.py -q --maxfail=1
```

Use the performance/collision test that matches what you touched (e.g.
`test_blah_collision.py`, `test_hypnotic_collision.py`, `test_performance.py`).
Only run the full suite when changing a **shared primitive** that many gestures
depend on (`randomized_centering_move`, `move_to`, clamping).

## Why keep the sim and collision tests

The operator validates physical limits by hand, so the sim's angle read-back is
a convenience — but it is what lets a gesture be self-checked without hardware.
The collision/equivalence tests protect the invariants the eye can't see:
`SAFE_LIMITS` clamping, disjoint channels, and the `random.seed`-deterministic
sequence that keeps phased-vs-standalone performances identical. Keep both; they
are cheap to run targeted and are the reason a primitive can be reused across
gestures without re-validating each one on the robot.

## Wiring checklist (per `lang-python.md`)

1. Gesture (no audio) in `movements.py`; register it in `controller.py`'s
   `action_map` for gesture-only CLI testing.
2. For a Routine: add the audio filename to `Animatronic.music`, add a
   `_do_<name>` coroutine and a method calling `run_action_and_audio(...)`,
   and register the action in `action_map` in `animatronic.py`'s `main()`.
3. Add the action name to the appropriate allowlist in `webapp.py`
   (`ROUTINE_ACTIONS` / `MOVEMENT_ACTIONS`) if it should appear in the panel.

## Reference Map — go here instead of re-reading `movements.py`

`movements.py` is large. Do NOT scan it end-to-end to relearn the patterns on
every gesture. Read ONLY the one or two canonical gestures whose shape matches
what you're building, plus the primitive(s) they call. That is enough context to
author a new, well-formed gesture.

### Motion primitives (in `TrunkController` / `Movements`)

| Primitive | Use it for | Signature highlights |
|-----------|-----------|----------------------|
| `move_to(targets, steps, delay, start_fractions=None, ease=True)` | The default. Move several joints to target angles SIMULTANEOUSLY, arriving together, smoothstep-eased. Every write is clamped to `SAFE_LIMITS` / active override. | `targets` = `{channel: angle}`. `start_fractions={ch: 0..1}` staggers a joint to begin part-way through. `ease=False` for linear. |
| `randomized_centering_move(channel, center, half_range, jitter_pct, state_attr, *, steps_range, delay_base, delay_jitter, ease, companion)` | "Randomize within range with centering" — organic oscillation of ONE joint around a center. See `animation-vocabulary.md` for the exact semantics. | Per-gesture `state_attr` string keeps transition state separate. `companion` = zero-arg callable returning `{extra_ch: angle}` eased in the SAME move. Deterministic under `random.seed`. |
| `verified_pose_override({ch: (min, max)})` | A context manager to WIDEN `SAFE_LIMITS` for specific channels when an operator-verified pose dips below/above the global floor/ceiling. Hold it across the whole span (reach → loop → return). | `with TrunkController.verified_pose_override(...):` or an `AsyncExitStack` for phased gestures. |
| `move` / `move_by_direction` | LEGACY linear 1-degree sweeps. **Do not use in new code** — see below. | — |

### Canonical gestures — copy THESE shapes

These are well-formed and current. Match a new gesture to the closest one and
read only it + its primitives:

- **Arm reach + hold + return, with an operator-verified sub-floor pose**:
  `menacing_reach` (and its `_menacing_reach_reach` / `_swing` / `_retract`
  primitives). The template for `verified_pose_override` + a centering swing.
- **Parameterized variant of an existing gesture** (own pose/band/state, shares
  the machinery): `hypnotic_arm` (built on `menacing_reach`'s primitives).
- **Hand-to-face fold with staged ordering** (`start_fractions`): `yawn_cover`,
  `face_palm`.
- **Raise + gentle bob + lower** (centering on one joint): `present_palm`.
- **Head look-around over a duration** (centering pan/tilt): `look_around_random`
  and `hand_visor` (which also shows the ARM FOLLOWING the neck pan via a
  per-glance blend on disjoint channels).
- **Concurrent arm + head over a duration** (`asyncio.gather`, disjoint
  channels): `awaken`, `reach_and_look_smooth`.
- **Startle/reaction with a fast jolt then recover**: `snuck_up`.

### Legacy gestures — DO NOT copy their pattern

`come`, `reach_out`, and the older `wave`/`_wave_arm` internals predate the
current style and use linear `move_by_direction` 1-degree sweeps. They still run,
but they are NOT the pattern for new work. When you need "reach" or "wave"
motion, follow the eased `move_to` versions (`reach_and_look_smooth`,
`wave_and_swivel_smooth`) instead.

### Phased (audio-synced) gestures

A gesture that a Routine drives in sync with audio is split into
`<name>_lead_in` / `<name>_loop_body` / `<name>_return` adapters that call the
SAME shared primitives as the standalone gesture, so a seeded run reproduces an
identical command sequence. If the request is gesture-only, you do NOT need the
phased adapters — add them only when wiring the gesture into an audio Routine.
See `menacing_reach` / `hypnotic_arm` / `present_palm` for the adapter trio.

The cover-mouth family is the most-reused phased gesture: one shared pose + one
hold (`yawn_cover_loop_body`) + one return (`yawn_cover_return`), with FOUR
interchangeable lead-ins that differ only in audio timing
(`yawn_cover_lead_in`, `_settled`, `_gated`, `_delayed`). When tuning a
cover-mouth Routine, change the lead-in or a timing constant — do NOT fork the
pose. The lead-in variants, their timing constants, and which Routine uses each
are tabulated in `audio-sequencing.md` ("Cover-mouth lead-in variants"); the
pose values live in `constants.ARM_DESTINATION_POSES["coverMouth"]` and the
`Movements._YC_*_COVER` constants. The audio-side timing knobs that pair with
these (gating, `followup_audio_files`, `stop_loop_lead_seconds`, and the
"performance waits for audio to drain" rule) are all in `audio-sequencing.md`.
