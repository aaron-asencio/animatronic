# Implementation Plan — `fan` Gesture

Add a new standalone Gesture `fan` to `src/movements.py` and wire it into the
`src/controller.py` CLI allowlist so it runs via
`sudo python3 src/controller.py --action=fan`. No audio, no jaw/audio path.

## Design decisions (grounded in the codebase)

### Verified channel-to-constant mapping (from `src/constants.py`)

The operator's start pose `{0: 90, 1: 85, 3: 90, 4: 0, 5: 5, 6: 55, 7: 0}` maps
to named constants and is checked against `SAFE_LIMITS`:

| Key | Constant (`constants.py`) | Value | `SAFE_LIMITS` | In range? | Note |
|-----|---------------------------|-------|---------------|-----------|------|
| 0 | `NECK_PAN` | 90 | (5, 175) | yes | centered (`NECK_CENTER`) |
| 1 | `NECK_TILT` | 85 | (30, 160) | yes | near level (rest is 90) |
| 3 | `RT_WRIST_TILT` | 90 | (10, 230) | yes | rest value |
| 4 | `RT_ELBOW_ROTATOR` | 0 | (0, 270) | yes | palm-down end |
| 5 | `RT_ELBOW_TILT` | 5 | (0, 160) | yes | rest value (elbow straight) |
| 6 | `RT_SHOULDER_TILT` | 55 | (45, 270) | yes | rest value |
| 7 | `RT_SHOULDER_ROTATOR` | 0 | (0, 270) | yes | rest value |

The task brief's annotation said "0 and 1 = the two neck channels". Confirmed:
channel 0 is `NECK_PAN`, channel 1 is `NECK_TILT`. All seven start-pose values
lie inside their channel's `SAFE_LIMITS`, so **no `verified_pose_override` is
needed** — do not widen any limit.

### Concurrent-motion decision

Per the operator spec, after reaching the start pose, run two
"randomize within range with centering" loops concurrently on **disjoint**
channels via `asyncio.gather`, reusing the existing
`randomized_centering_move` primitive (the same one `_menacing_reach_swing`,
`_hypnotic_arm_swing`, and `_present_palm_bob` call):

- **RT_WRIST_TILT (channel 3)**: band 10–170 → `center=90`, `half_range=80`.
  Within `SAFE_LIMITS` (10, 230). Own `state_attr="_fan_wrist_pos"`.
- **RT_SHOULDER_ROTATOR (channel 7)**: band 0–10 → `center=5`,
  `half_range=5`. Within `SAFE_LIMITS` (0, 270). Own
  `state_attr="_fan_rotator_pos"`.

Channels 3 and 7 are disjoint, so gathering two per-joint loop coroutines is a
safe concurrent gesture (same contract as `menacing_reach`'s
`gather(_swings(), _menacing_reach_wrist_flex())`, which drives disjoint
channels 6/7 vs 3).

Rationale for `center`/`half_range`: `randomized_centering_move` oscillates in
`[center - half_range, center + half_range]`, so the band endpoints the operator
gave (10–170 and 0–10) are expressed as center ± half_range exactly.

### FORBIDDEN_COMBINATIONS check

The only rule (hand-to-face) matches when `RT_ELBOW_TILT ∈ (150,270)` AND
`RT_SHOULDER_ROTATOR ∈ (210,270)` simultaneously. In `fan` the elbow tilt holds
at 5 and the rotator stays in [0,10], so the rule never triggers. (Per steering,
do NOT run a `SERVO_SIM` collision/limit simulation to prove this — the operator
validates on hardware; the in-code `set_angle`/`move_to` clamps stay.)

### Swing count

Match the looping style of existing gestures (`menacing_reach`: 8 swings,
`present_palm`: 4 bobs). Use **7 iterations per joint** (within the 6–8 the brief
suggested). Each joint's loop is independent and both run concurrently.

### Pattern to copy

Follow the `present_palm` shape most closely (raise/pose → centering loop →
lower), but with the concurrent two-joint `asyncio.gather` structure from
`menacing_reach`. All randomness comes only from the shared `random` module via
`randomized_centering_move` (no separate RNG), so a seeded run is reproducible.

### `fan` coroutine structure (target)

```
async def fan(self):
    # 1. START POSE: move all seven channels to the operator's start pose
    #    in one eased move_to (every write clamped to SAFE_LIMITS).
    #    Also initialize both centering state_attrs to their band centers
    #    so the first swing of each joint starts from a known position.
    #    self._fan_wrist_pos = 90   (RT_WRIST_TILT center)
    #    self._fan_rotator_pos = 5  (RT_SHOULDER_ROTATOR center)
    await self.trunkController.move_to(
        {
            constants.NECK_PAN: 90,
            constants.NECK_TILT: 85,
            constants.RT_WRIST_TILT: 90,
            constants.RT_ELBOW_ROTATOR: 0,
            constants.RT_ELBOW_TILT: 5,
            constants.RT_SHOULDER_TILT: 55,
            constants.RT_SHOULDER_ROTATOR: 0,
        },
        steps=45, delay=0.02,
    )

    # 2. CONCURRENT CENTERING on disjoint channels 3 and 7.
    async def _wrist_swings():
        for _ in range(7):
            await self.randomized_centering_move(
                constants.RT_WRIST_TILT,
                center=90, half_range=80, jitter_pct=0.25,
                state_attr="_fan_wrist_pos",
                steps_range=(18, 24), delay_base=0.02, delay_jitter=0.005,
            )

    async def _rotator_swings():
        for _ in range(7):
            await self.randomized_centering_move(
                constants.RT_SHOULDER_ROTATOR,
                center=5, half_range=5, jitter_pct=0.25,
                state_attr="_fan_rotator_pos",
                steps_range=(18, 24), delay_base=0.02, delay_jitter=0.005,
            )

    await asyncio.gather(_wrist_swings(), _rotator_swings())

    # 3. RETURN TO REST for every involved joint (match REST_POSITIONS exactly:
    #    NECK_PAN 90, NECK_TILT 90, RT_WRIST_TILT 90, RT_ELBOW_ROTATOR 150,
    #    RT_ELBOW_TILT 5, RT_SHOULDER_TILT 55, RT_SHOULDER_ROTATOR 0).
    await self.trunkController.move_to(
        {
            constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
            constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT],
            constants.RT_WRIST_TILT: constants.REST_POSITIONS[constants.RT_WRIST_TILT],
            constants.RT_ELBOW_ROTATOR: constants.REST_POSITIONS[constants.RT_ELBOW_ROTATOR],
            constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
            constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
            constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
        },
        steps=45, delay=0.02,
    )
```

Jitter/timing values (`jitter_pct=0.25`, `steps_range=(18,24)`, `delay_base=0.02`,
`delay_jitter=0.005`) mirror the existing centering callers; the implementer may
keep these as-is. The start pose and rest moves use `steps=45, delay=0.02` to
match `_present_palm_raise`/`_lower`.

Note: the return-to-rest move drives `RT_ELBOW_ROTATOR` from its start-pose
value 0 to its rest value 150 and `NECK_TILT` from 85 to 90 — this is correct
(the gesture leaves every joint on its documented `REST_POSITIONS` value, per
the authoring-gestures "always return to rest" rule). The controller's error
path also calls `return_to_rest()`, so an aborted run still recovers.

## Steps

- [ ] 1. Add the `async def fan(self)` coroutine to the `Movements` class in
      `src/movements.py`, in the ARM-gestures region near `present_palm`
      (e.g. after `present_palm_return`). Implement the three phases above:
      (a) one eased `move_to` to the start pose that also seeds
      `self._fan_wrist_pos = 90` and `self._fan_rotator_pos = 5`; (b) two
      per-joint loop coroutines (7 iterations each) that each call
      `randomized_centering_move` with distinct `state_attr`s on disjoint
      channels 3 and 7, run via `asyncio.gather`; (c) one eased `move_to`
      returning all seven channels to `constants.REST_POSITIONS`. Every servo
      write goes through `move_to` (clamped) — never touch `kit.servo[n].angle`.
      Write a Google-style docstring listing the channels owned
      (NECK_PAN 0, NECK_TILT 1, RT_WRIST_TILT 3, RT_ELBOW_ROTATOR 4,
      RT_ELBOW_TILT 5, RT_SHOULDER_TILT 6, RT_SHOULDER_ROTATOR 7) and noting no
      `verified_pose_override` is needed (all values inside `SAFE_LIMITS`).
      Files: `src/movements.py`
      Verify: `.venv/bin/python -m py_compile src/movements.py` exits 0 (no
      syntax/import error).

- [ ] 2. Register the gesture in the `action_map` in `src/controller.py`'s
      `main()`, under the `--- ARM gestures ---` block, as `'fan': mv.fan,`.
      This is the security allowlist boundary — add the key explicitly; do not
      use `getattr`/`eval` on raw `--action`.
      Files: `src/controller.py`
      Verify: `.venv/bin/python -m py_compile src/controller.py` exits 0; and
      `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import controller"`
      imports cleanly.

- [ ] 3. Confirm `fan` is dispatchable from the CLI allowlist without running it
      on hardware or in a collision sim. Load the controller module and assert
      `'fan'` is present in the `action_map` keys and maps to a coroutine
      function on `Movements`.
      Files: (no change) `src/controller.py`, `src/movements.py`
      Verify: run
      `.venv/bin/python -c "import sys,inspect; sys.path.insert(0,'src'); from movements import Movements; mv=Movements('t'); assert inspect.iscoroutinefunction(mv.fan); print('fan OK')"`
      and confirm it prints `fan OK`.

## Explicitly out of scope (per steering)

- Do NOT run `SERVO_SIM` collision/limit verification, `CLAMPED`-warning
  inspection, `SAFE_LIMITS` range validation of commanded angles, or
  `FORBIDDEN_COMBINATIONS` checks via simulation. The operator validates servo
  travel limits and collisions on the physical robot. The in-code safety guards
  (`move_to`/`set_angle` clamping and return-to-rest) remain in the code — this
  only means not simulating to verify angles.
- No audio: `fan` is a Gesture, not a Routine. Do not add anything to
  `Animatronic.music`, `animatronic.py`'s `action_map`, phased lead-in/loop/
  return adapters, or the `webapp.py` panel allowlists. (If later wired into the
  control panel as a Movement, it would go in `MOVEMENT_ACTIONS` in
  `webapp.py` — not part of this task.)

## Assumptions

- Swing count fixed at 7 per joint (brief allowed 6–8); adjust if the operator
  prefers a different count.
- `NECK_TILT` start value 85 is intentional (slightly off the 90 rest); the
  gesture still returns it to the documented rest of 90 at the end.
