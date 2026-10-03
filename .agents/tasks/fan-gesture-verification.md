# Verification Note — `fan` Gesture

Standalone Gesture `fan` added to `src/movements.py` and wired into the
`src/controller.py` CLI allowlist. Implemented from scratch (no
`fan-gesture-review.json` present — first iteration).

## What was implemented

- **`Movements.fan`** (`src/movements.py`), inserted in the ARM-gestures region
  immediately after `present_palm_return` (before `come_here`). Three phases:
  1. **Start pose** — one eased `move_to` driving all seven involved channels to
     the operator pose `{NECK_PAN:90, NECK_TILT:85, RT_WRIST_TILT:90,
     RT_ELBOW_ROTATOR:0, RT_ELBOW_TILT:5, RT_SHOULDER_TILT:55,
     RT_SHOULDER_ROTATOR:0}`. Seeds `_fan_wrist_pos=90` and `_fan_rotator_pos=5`
     so each centering joint starts from a known band center.
  2. **Concurrent centering** — two per-joint loops (7 swings each) run via
     `asyncio.gather` on DISJOINT channels, each reusing the existing
     `randomized_centering_move` primitive with its OWN `state_attr`:
     - `RT_WRIST_TILT` (ch 3): center 90, half_range 80 → band [10, 170].
     - `RT_SHOULDER_ROTATOR` (ch 7): center 5, half_range 5 → band [0, 10].
  3. **Return to rest** — one eased `move_to` driving all seven channels to
     `constants.REST_POSITIONS`.
- Band constants added as class attributes (`_FAN_WRIST_CENTER`,
  `_FAN_WRIST_HALF_RANGE`, `_FAN_ROTATOR_CENTER`, `_FAN_ROTATOR_HALF_RANGE`,
  `_FAN_JITTER_PCT`, `_FAN_SWINGS`), matching the `_MR_*` / `_PP_*` style.
- Google-style docstring documents the start pose, the two concurrent centering
  bands, and the return-to-rest, in the style of neighboring gestures.
- **`src/controller.py`**: added `'fan': mv.fan,` to the `action_map` allowlist
  under the ARM-gestures block, so `sudo python3 src/controller.py --action=fan`
  dispatches to the new coroutine.

## Channel-to-constant mapping (verified against `src/constants.py`)

| Key | Constant | Start val | SAFE_LIMITS | In range? |
|-----|----------|-----------|-------------|-----------|
| 0 | `NECK_PAN` | 90 | (5, 175) | yes |
| 1 | `NECK_TILT` | 85 | (30, 160) | yes |
| 3 | `RT_WRIST_TILT` | 90 | (10, 230) | yes |
| 4 | `RT_ELBOW_ROTATOR` | 0 | (0, 270) | yes |
| 5 | `RT_ELBOW_TILT` | 5 | (0, 160) | yes |
| 6 | `RT_SHOULDER_TILT` | 55 | (45, 270) | yes |
| 7 | `RT_SHOULDER_ROTATOR` | 0 | (0, 270) | yes |

Both centering bands are inside their channel's SAFE_LIMITS (wrist [10,170] ⊂
(10,230); rotator [0,10] ⊂ (0,270)). No `verified_pose_override` needed; no
SAFE_LIMITS widened.

## Hard constraints honored

- Every servo write goes through `move_to` / `randomized_centering_move` (which
  clamp to SAFE_LIMITS). No direct `kit.servo[n].angle` writes.
- Concurrent joints on disjoint channels (3 and 7).
- No audio / jaw path touched — `fan` is a Gesture.
- All randomness comes from the shared `random` module via
  `randomized_centering_move`.
- Returns every involved joint to `REST_POSITIONS` at the end.

## Verification run (results)

- `.venv/bin/python -m py_compile src/movements.py src/controller.py` → `py_compile OK` (exit 0).
- `.venv/bin/python -c "...; assert inspect.iscoroutinefunction(mv.fan); ..."` →
  `fan OK: coroutine on Movements` (exit 0).
- Controller allowlist dispatch check (static grep for `'fan':`/`mv.fan` +
  runtime coroutine assertion) → `controller dispatch OK: --action=fan ->
  Movements.fan coroutine` (exit 0).

## Hardware verification policy

Per steering, did NOT run `SERVO_SIM` collision/limit verification, CLAMPED-warning
inspection, commanded-angle-range validation against SAFE_LIMITS, or
FORBIDDEN_COMBINATIONS checks. The operator validates travel limits and
collisions on the physical robot. In-code safety guards (set_angle/move_to
clamping and return-to-rest) remain in the code.

No git commit was created (per instructions).
