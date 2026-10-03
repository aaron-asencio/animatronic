# New `fan` Gesture — wrist + shoulder-rotator centering over a held start pose

The change adds a standalone, audio-free `fan` Gesture to `Movements` (`src/movements.py`) and registers it in the gesture-only CLI allowlist in `src/controller.py`. `fan` eases all seven involved channels to an operator-specified start pose, then runs two concurrent "randomize within range with centering" loops on disjoint channels — `RT_WRIST_TILT` (ch 3) over the band [10, 170] and `RT_SHOULDER_ROTATOR` (ch 7) over [0, 10] — via `asyncio.gather`, each reusing the shared `randomized_centering_move` primitive with its own transition-state attribute. It finishes by easing every involved joint back to `constants.REST_POSITIONS`. No `constants.py` change was needed; the band constants live as `_FAN_*` class attributes alongside the coroutine.

Watch for: nothing blocking. The implementation matches the plan and the operator spec exactly, every write goes through `move_to` / `randomized_centering_move` (both SAFE_LIMITS-clamped), no audio/jaw path is touched, and all randomness is drawn from the shared `random` module (confirmed).

**Verdict**: APPROVED

## High-level view

The start pose is applied as a single eased `move_to` with every integer channel from the operator's spec (`{0:90,1:85,3:90,4:0,5:5,6:55,7:0}`) mapped to the correct named constant from `constants.py` — `NECK_PAN`, `NECK_TILT`, `RT_WRIST_TILT`, `RT_ELBOW_ROTATOR`, `RT_ELBOW_TILT`, `RT_SHOULDER_TILT`, `RT_SHOULDER_ROTATOR` respectively. All seven values sit inside their channel's `SAFE_LIMITS`, so no limit is widened and no `verified_pose_override` is used.

The concurrent motion is two per-joint loops gathered together on disjoint channels 3 and 7. The wrist band is expressed as `center=90, half_range=80` (= [10, 170]) and the rotator as `center=5, half_range=5` (= [0, 10]), which is the exact center±half_range decomposition of the operator's ranges. Each loop runs seven swings (within the 6–8 the brief allowed) and carries a distinct `state_attr` (`_fan_wrist_pos` / `_fan_rotator_pos`), seeded to its band center by the start-pose phase so the two joints never share transition state.

The return-to-rest phase drives all seven channels to their documented `REST_POSITIONS` values, including the two that differ from the start pose (`NECK_TILT` 85→90, `RT_ELBOW_ROTATOR` 0→150), so no joint is left off rest. The CLI allowlist entry `'fan': mv.fan` dispatches to the coroutine, and the controller's error path already drives `return_to_rest()` on an aborted run.

The coder's verification note records a `py_compile` pass and a dispatch/coroutine assertion for both files. Per project steering, SERVO_SIM collision/limit simulation was deliberately skipped (operator validates on hardware); its absence is not a finding.

<details>
<summary>Issues (0)</summary>

No blocking or non-blocking issues identified.

</details>

<details>
<summary>Details</summary>

## Start pose: channel-to-constant mapping

The start-pose `move_to` names each channel explicitly rather than using raw integers, and the mapping is correct against `constants.py`: channel 0 → `NECK_PAN` (90), 1 → `NECK_TILT` (85), 3 → `RT_WRIST_TILT` (90), 4 → `RT_ELBOW_ROTATOR` (0), 5 → `RT_ELBOW_TILT` (5), 6 → `RT_SHOULDER_TILT` (55), 7 → `RT_SHOULDER_ROTATOR` (0). Each value lies inside the corresponding `SAFE_LIMITS` entry — notably `NECK_TILT` 85 ⊂ (30,160) and `RT_SHOULDER_TILT` 55 ⊂ (45,270) — so the eased move cannot be clamped away from the operator's intent, and no `verified_pose_override` is required. The two centering state attributes are seeded (`_fan_wrist_pos=90`, `_fan_rotator_pos=5`) before the move, so each joint's first swing classifies from its true band center (confirmed).

## Concurrent centering on disjoint channels

The two swing loops are defined as inner coroutines and run under `asyncio.gather(_wrist_swings(), _rotator_swings())`. Channels 3 and 7 are disjoint, so no servo channel is driven from two coroutines at once — the same contract `menacing_reach` uses. The band decomposition is exact: wrist [10,170] = 90±80 ⊂ SAFE_LIMITS (10,230); rotator [0,10] = 5±5 ⊂ (0,270). Each call carries a distinct `state_attr`, so the shared `randomized_centering_move` keeps the two joints' transition state independent — the primitive's documented requirement for concurrent reuse. Both loops run `_FAN_SWINGS = 7` iterations, inside the 6–8 the brief specified.

## Return to rest

The closing `move_to` reads targets straight from `constants.REST_POSITIONS` for all seven involved channels, so the gesture lands every joint on its documented rest — including `RT_ELBOW_ROTATOR` (0 → 150) and `NECK_TILT` (85 → 90), the two whose rest differs from the start pose. This satisfies the "always return to rest, match each channel's documented rest exactly" rule. If the run aborts mid-gesture, `controller.py`'s `except` path invokes `mv.trunkController.return_to_rest()`, so recovery is covered there as well.

## Safety invariants and audio boundary

Every servo write in `fan` goes through `move_to` or `randomized_centering_move`, both of which clamp to `SAFE_LIMITS`; there is no direct `kit.servo[n].angle` assignment. The gesture touches no audio or jaw path (`MOUTH_MOTOR_PIN`), consistent with it being a Gesture rather than a Routine. All randomness enters only through `randomized_centering_move`, which draws from the shared `random` module, preserving `random.seed()` determinism. No `SAFE_LIMITS` entry was widened and no start-pose value is out of limits.

## CLI allowlist wiring

`'fan': mv.fan` is added under the ARM-gestures block of `controller.py`'s `action_map`, the explicit allowlist that is the security boundary for `--action`. Dispatch goes through `action_map[args.action]()` with no `getattr`/`eval` on the raw argument, so the allowlist pattern is preserved. The docstring is Google-style and matches the structure of neighboring gestures (channel list, start pose, concurrent-centering description, return-to-rest), and the `_FAN_*` constants mirror the `_MR_*` / `_PP_*` naming style.

## Verification evidence

The coder's verification note records `.venv/bin/python -m py_compile src/movements.py src/controller.py` (exit 0), a coroutine-identity assertion on `Movements.fan`, and a controller-dispatch check confirming `--action=fan` resolves to the coroutine. Per the testing and authoring-gestures steering, the SERVO_SIM collision/limit simulation and FORBIDDEN_COMBINATIONS checks were intentionally not run — the operator validates travel limits on hardware. That omission is explicitly not a finding. The in-code guards (clamping via `move_to`, return-to-rest) remain intact, which is what the policy requires.

</details>

<details>
<summary>File map</summary>

- `src/movements.py` — new `_FAN_*` band/jitter/swing class constants and the `async def fan(self)` coroutine (start pose → gathered wrist/rotator centering loops → return to rest), placed in the ARM-gestures region before `come_here`.
- `src/controller.py` — added `'fan': mv.fan` to the `action_map` allowlist under the ARM-gestures block.
- `constants.py` — unchanged (no new constant needed; all start-pose and band values already within existing `SAFE_LIMITS` / `REST_POSITIONS`).

Full diff: `git diff main -- src/movements.py src/controller.py`

</details>
