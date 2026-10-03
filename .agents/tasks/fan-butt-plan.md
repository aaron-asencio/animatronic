# Fan Butt Gesture — Implementation Plan

A single Gesture (no audio, never touches the jaw/audio path): the right hand
fans at the side to waft away an unpleasant smell. Three fan joints oscillate
between an "up" and a "down" keyframe, 3 reps, at speed 8, then return to rest.

## Base branch (RECORDED — read before merging)

**The base branch for this entire task is `main`** (worktree branch
`feat/fan-butt-gesture` was re-created from `main` tip `f4986f3`, which carries
the real `src/` architecture). `origin/HEAD` points at the STALE 2021 `master`
(`cf3c4bd`, pre-`src/`) — that is a trap. The final merge/finalize step MUST
rebase onto and fast-forward **`main`**, never `master`. Do not let any step
fast-forward `master`.

## Grounding (what exists in the committed base, verified by reading src/)

- Code lives under `src/`. Gestures are `async` methods on the `Movements`
  class in `src/movements.py`; method names are **snake_case**
  (`come_here`, `menacing_reach`, `yawn_cover`).
- `src/controller.py` dispatches `--action` through an explicit `action_map`
  dict (camelCase key -> bound `Movements` method), inside a `servo_lock()`,
  and on any exception calls `mv.trunkController.return_to_rest()`. This
  allowlist is the security boundary — keep it a dict, never `getattr`/`eval`
  on `args.action`.
- `TrunkController.move_to(targets, steps=60, delay=0.02, start_fractions=None,
  ease=True)` interpolates all listed channels from their current angle to the
  targets over one shared `steps`, so they ARRIVE TOGETHER. Every write goes
  through `set_angle()` -> clamp to `SAFE_LIMITS` (and any active
  `verified_pose_override`). This is the correct primitive for "concurrent
  joints at the same speed in one call".
- `TrunkController.return_to_rest()` drives every configured channel to
  `constants.REST_POSITIONS`. Standalone gestures themselves end by `move_to`-ing
  their joints back toward rest; `controller.py` additionally calls
  `return_to_rest()` on the error path.
- `constants.py` channel map matches the spec exactly: `NECK_PAN=0`,
  `NECK_TILT=1`, `RT_WRIST_TILT=3`, `RT_ELBOW_ROTATOR=4`, `RT_ELBOW_TILT=5`,
  `RT_SHOULDER_TILT=6`, `RT_SHOULDER_ROTATOR=7`.

### IMPORTANT base gap: `speed_to_steps` is NOT in the committed base

The animation-vocabulary steering and this task REQUIRE authoring motion with
the speed dial via `trunkcontroller.speed_to_steps(distance_deg, speed,
delay=0.02)`. That helper (and `speed_to_deg_per_sec`) is **absent** from the
committed `main` `src/trunkcontroller.py` this worktree is on. (It exists only
as uncommitted edits in a parallel `feat/fan-butt` attempt in the main
checkout — do not depend on that.) Steering outranks "match existing committed
code," and existing gestures hand-tune `steps` only because the helper
post-dates them. **Decision: add the speed helper to this worktree's
`trunkcontroller.py` as step 1, then author `fan_butt` with it.** The formula is
fully specified by the steering (anchors + geometric ratio), so this is a
faithful port, not a new design.

## Safety verification (done against the real SAFE_LIMITS / FORBIDDEN_COMBINATIONS)

All commanded angles are inside the global `SAFE_LIMITS`, so **no
`verified_pose_override` is needed**:

| Channel | targets used | SAFE_LIMITS | ok |
|---|---|---|---|
| NECK_PAN (0) | 90 | (5,175) | yes |
| NECK_TILT (1) | 85 | (30,160) | yes |
| RT_WRIST_TILT (3) | 90 (start), 10 (up), 160 (down) | (10,230) | yes (10 is the floor, clamps cleanly) |
| RT_ELBOW_ROTATOR (4) | 25 (start) | (0,270) | yes |
| RT_ELBOW_TILT (5) | 60 (start/down), 5 (up) | (0,160) | yes |
| RT_SHOULDER_TILT (6) | 55 (start) | (45,270) | yes |
| RT_SHOULDER_ROTATOR (7) | 0 (start/down), 10 (up) | (0,270) | yes (0 is the floor) |

`FORBIDDEN_COMBINATIONS` has one rule: hand-to-face when `RT_ELBOW_TILT ∈
[150,270]` AND `RT_SHOULDER_ROTATOR ∈ [210,270]`. The fan's elbow max is 60 and
shoulder-rotator max is 10 — the rule can never match. No override, no forbidden
combination. Do NOT relax `SAFE_LIMITS`.

Fan joints (ch 3, 5, 7) are disjoint, as required for concurrent motion in one
`move_to`. Channels 0,1,4,6 are set once in the start pose and are not driven
during the fanning oscillation.

## Decisions (recorded)

- **Method name:** `fan_butt` (snake_case) on `Movements`.
- **Action name:** `fanButt` (camelCase) in `controller.py`'s `action_map`.
- **One `move_to` per stroke:** all three fan joints share speed 8, so each
  stroke is a single `move_to` with one `steps`, sized from the LONGEST-travel
  joint (the wrist), so they arrive together (vocabulary steering).
- **Speed sizing (speed 8, delay 0.02):** per-stroke wrist travel is 150°
  (90->10 settle differs; the up<->down legs are |160-10|=150 for the wrist,
  |60-5|=55 for the elbow, |10-0|=10 for the shoulder rotator). `speed_to_steps(150,
  8, delay=0.02)` ≈ **31 steps** (speed 8 ≈ 245 °/s -> 150/245 ≈ 0.61s -> 31
  steps). Compute `steps` in code from the max leg travel; do not hardcode 31.
  Pass the SAME `delay=0.02` to both `speed_to_steps` and the matching `move_to`.
- **Start pose settle:** issue one `move_to` to the full 7-channel start pose
  (`{0:90,1:85,3:90,4:25,5:60,6:55,7:0}`) before fanning, sized at speed 8 from
  that move's longest leg (or a fixed modest `steps` is acceptable for the
  initial settle — keep it a single `move_to`).
- **Return to rest:** after 3 reps, `move_to` the fan joints toward
  `REST_POSITIONS` (shoulder rotator 0, elbow tilt 5, wrist 90); leave neck /
  elbow-rotator / shoulder-tilt where the start pose set them (the
  controller/`return_to_rest` path settles everything to rest). Wrap the body so
  a failure still yields to the controller's error-path `return_to_rest`.

## Verification evidence (recorded for the reviewer)

Ran from the worktree; `python3` used for compile/sim (worktree has no local
`.venv`; the collision/kinematics/hardware-validation test suites were
deliberately NOT run per the testing steering's Hardware Movement Verification
rule — this is a new movement, validated on hardware by the operator).

- Compile check (exit 0):
  `python3 -m py_compile src/movements.py src/controller.py src/trunkcontroller.py` -> `COMPILE_OK`
- Speed helper sanity (speed 8, delay 0.02):
  `SERVO_SIM=1 python3 -c "import trunkcontroller as t; print(round(t.speed_to_deg_per_sec(8),1), t.speed_to_steps(150,8), t.speed_to_steps(55,8), t.speed_to_steps(10,8))"`
  -> `244.5 31 11 2` (speed-8 velocity ~245 deg/s; wrist leg = 31 steps).
- Flow / dispatch only (NOT angle/limit inspection):
  `SERVO_SIM=1 python3 controller.py --action=fanButt` -> `EXIT=0`, 8 `move_to`
  lines: start-pose settle (steps=40), 3 up/down reps (steps=31 each), return
  toward rest (steps=40). Confirms `fanButt` resolves to `fan_butt` through the
  controller allowlist and the gesture oscillates only channels 3/5/7.
- No unit test covers controller `--action` wiring; `tests/test_cli.py` exercises
  the kinematic collision-model CLI (a SERVO_SIM/collision check), which the
  testing steering says to skip for new movements. Wiring is verified by the
  dispatch run above instead.

## Items

- [x] 1. Add the speed-dial helpers to `src/trunkcontroller.py`.
      Add module-level `SPEED_MIN=1`, `SPEED_MAX=10`, `SPEED_MIN_DPS=20.0`,
      `SPEED_MAX_DPS=500.0`, `SPEED_RATIO=(SPEED_MAX_DPS/SPEED_MIN_DPS)**(1/(SPEED_MAX-SPEED_MIN))`,
      `speed_to_deg_per_sec(speed)` (clamp speed to [1,10], return
      `SPEED_MIN_DPS*SPEED_RATIO**(speed-1)`), and
      `speed_to_steps(distance_deg, speed, delay=0.02, min_steps=1)` returning
      `max(min_steps, round(abs(distance_deg)/speed_to_deg_per_sec(speed)/delay))`.
      Place them near the top of the module (module-level functions, matching
      the steering's documented API). Add a short docstring citing the
      animation-vocabulary speed section.
      Files: `src/trunkcontroller.py`
      Verify: `cd src && SERVO_SIM=1 python3 -c "import trunkcontroller as t;
      print(round(t.speed_to_deg_per_sec(8),1), t.speed_to_steps(150,8),
      t.speed_to_steps(55,8), t.speed_to_steps(10,8))"` prints the speed-8
      velocity (~245.0) and positive step counts (wrist leg ~31). Also
      `python -m py_compile src/trunkcontroller.py` exits 0.

- [x] 2. Add the `fan_butt` gesture coroutine to the `Movements` class in
      `src/movements.py`, following the `come_here` idiom (start/closed pose
      dicts + a `for rep in range(3)` loop of two `move_to` calls per rep).
      Import the helper (e.g. `from trunkcontroller import speed_to_steps` or
      call `trunkcontroller.speed_to_steps`, matching the module's existing
      import style). Steps:
        a. Define the up keyframe `{RT_SHOULDER_ROTATOR:10, RT_ELBOW_TILT:5,
           RT_WRIST_TILT:10}` and down keyframe `{RT_SHOULDER_ROTATOR:0,
           RT_ELBOW_TILT:60, RT_WRIST_TILT:160}` using `constants.*` channel names.
        b. Settle the start pose with one `move_to`
           ({NECK_PAN:90, NECK_TILT:85, RT_WRIST_TILT:90, RT_ELBOW_ROTATOR:25,
           RT_ELBOW_TILT:60, RT_SHOULDER_TILT:55, RT_SHOULDER_ROTATOR:0}).
        c. Size each stroke `steps = speed_to_steps(max_leg_travel, 8, delay=0.02)`
           where `max_leg_travel` is the largest per-joint delta for that stroke
           (150 for the wrist on both legs); pass `delay=0.02` to the matching
           `move_to`.
        d. Loop `for _ in range(3)`: `move_to(up, steps=..., delay=0.02)` then
           `move_to(down, steps=..., delay=0.02)`.
        e. Return the fan joints toward rest with a final `move_to`
           ({RT_SHOULDER_ROTATOR:0, RT_ELBOW_TILT:5, RT_WRIST_TILT:90}).
      No `verified_pose_override` (all targets within SAFE_LIMITS). Add a
      Google-style docstring tagged with the channels it owns (3,5,7 for the fan;
      0,1,4,6 set once in the start pose) and noting speed 8 / 3 reps.
      Files: `src/movements.py`
      Verify: `python -m py_compile src/movements.py` exits 0.

- [x] 3. Register the gesture in `src/controller.py`'s `action_map`.
      Add `'fanButt': mv.fan_butt,` to the ARM-gestures section of the dict (keep
      the explicit-allowlist pattern; do not touch the dispatch/lock/error logic).
      Optionally add `fanButt` to the module docstring's ARM gesture list.
      Files: `src/controller.py`
      Verify: `python -m py_compile src/controller.py` exits 0; and
      `cd src && SERVO_SIM=1 python3 controller.py --action=fanButt` runs the
      gesture end-to-end against the fake kit and exits 0 (prints the `move_to`
      lines for the settle, 3 up/down reps, and the rest return).

- [x] 4. Final flow + non-safety logic check (no new files).
      Confirm `fanButt` resolves to `fan_butt`, the gesture only oscillates
      channels 3/5/7, and it starts and ends near rest.
      Files: none (verification only)
      Verify: `cd src && SERVO_SIM=1 python3 controller.py --action=fanButt`
      completes without error.
      NOTE (testing steering — Hardware Movement Verification): do NOT run
      SERVO_SIM collision/limit verification passes for this new movement — do
      not grep the sim log for `CLAMPED`, do not validate commanded angles
      against `SAFE_LIMITS`, and do not check `FORBIDDEN_COMBINATIONS` in sim.
      The operator validates travel limits and collisions on the physical robot.
      The SERVO_SIM runs above are for FLOW/DISPATCH only (does it run and
      dispatch cleanly), not angle inspection. The in-code safety guards
      (set_angle/move_to clamping, return-to-rest, no relaxed SAFE_LIMITS) stay
      intact in the code regardless.

## Notes / assumptions
- Delay fixed at 0.02 everywhere (steering default; same value to
  `speed_to_steps` and `move_to`).
- If `src/movements.py` imports trunkcontroller as `from trunkcontroller import
  TrunkController`, add `speed_to_steps` to that import (or call
  `trunkcontroller.speed_to_steps`); match whatever import form the file uses.
- The gesture adds no audio and must not import or touch any audio/jaw path.
