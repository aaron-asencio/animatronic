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
(`constants.REST_POSITIONS`, `constants.SAFE_LIMITS`, the ARM DESTINATION POSES
comment in `constants.py`, and the nearest existing gesture) over a fresh
full-file search. Only search when no documented value exists.

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
