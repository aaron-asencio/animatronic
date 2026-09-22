# Implementation Plan: Audio-Synced Concurrent Gestures

## Overview

Build the reusable Performance Framework in a new `src/performance.py` module, then
expose phase adapters on `Movements` for `menacing_reach` and `look_around_random`,
and finally register the `brains` performance in `animatronic.py`. The framework's
coordination logic (concurrency, sequencing, gated audio start, loop-until-audio,
return-to-rest) lives in one place — `PerformanceRunner` — so new performances are
declarative `PerformanceDefinition`s rather than new code.

Work proceeds bottom-up: data models and validation first (so channel-ownership is
enforced before any motion), then the async runner, then the `Movements` phase
adapters, then `brains` wiring. Property tests (Hypothesis, `SERVO_SIM=1`, fake
movements + fake `PlaybackController`) are placed next to the code they validate to
catch coordination errors early. All servo-touching tests run in simulation mode so
no hardware moves. Run tests quietly: `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`.

## Tasks

- [x] 1. Create the framework module skeleton and data models
  - [x] 1.1 Create `src/performance.py` with data models and channel-ownership validation
    - Create `src/performance.py` (module docstring, Google-style; `print()` for debug per project convention).
    - Define `ChannelOwnershipError(Exception)`.
    - Define frozen dataclasses: `MovementSpec` (name, `owned_channels: frozenset[int]`, `lead_in`, `loop_body`, `do_return`, `supplies_gate: bool = False`), `GateSpec` (`movement_name: str`), `ConcurrentGroup` (`movements: tuple[MovementSpec, ...]`), `PerformanceStep` (`group`, `loop_for_audio: bool = False`), `PerformanceDefinition` (name, `audio_file`, `steps`, `gate: GateSpec | None = None`).
    - In `ConcurrentGroup`, validate at construction (`__post_init__`) that members' `owned_channels` are pairwise disjoint; raise `ChannelOwnershipError` naming the shared channel(s) otherwise.
    - _Requirements: 3.1, 3.2, 4.2, 10.1_

  - [x]* 1.2 Write property test for channel-ownership enforcement
    - **Property 3: Channel ownership is enforced before any motion**
    - **Validates: Requirements 3.2, 4.2, 10.1**
    - Hypothesis-generate movement specs with disjoint vs. deliberately overlapping channel sets; assert `ConcurrentGroup` construction succeeds iff disjoint and raises `ChannelOwnershipError` before any servo write.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 1.3 Write example test for definition shape
    - Construct a `PerformanceDefinition` and assert it exposes ordered steps, audio track, gate, and per-movement start timing (`supplies_gate`).
    - _Requirements: 3.1_

- [x] 2. Implement the audio bridge (PlaybackController)
  - [x] 2.1 Implement `PlaybackController` in `src/performance.py`
    - `__init__(self, audio_path: str)`; `start()` spawns `AudioPlayer().play_audio_file` in a `daemon=True` thread and marks active; `is_active()` returns `thread.is_alive()` (Playback_Active source of truth); `wait_finished(timeout=None)` joins the thread.
    - Reuse the existing pattern from `Animatronic.run_action_and_audio`; exactly one track, never restarted.
    - _Requirements: 5.1, 5.2, 7.1_

  - [ ]* 2.2 Write property test for single audio start
    - **Property 4: Exactly one audio track per performance**
    - **Validates: Requirements 5.1, 5.2**
    - Use a fake `PlaybackController` recording `start()` calls; run the runner over random definitions; assert `start()` is called exactly once and the track is never restarted/switched between steps.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 4: Exactly one audio track per performance`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

- [x] 3. Implement the generic PerformanceRunner
  - [x] 3.1 Implement `PerformanceRunner.run()` — validation, ordered steps, concurrency
    - `__init__(self, definition, movements, audio_dir)`; async `run()`.
    - Validate every group's Channel_Ownership before issuing any motion.
    - Execute `PerformanceStep`s in declared order; each step completes before the next; run a step's `ConcurrentGroup` members concurrently via `asyncio.gather`; the final step ending ends the performance.
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 2.4, 4.1_

  - [ ]* 3.2 Write property test for concurrent-group execution
    - **Property 1: Concurrent group runs its members together**
    - **Validates: Requirements 2.1, 4.1**
    - Fake movements record loop-body invocations; assert every member of a concurrent group makes progress within the same step (interleaved, not serialized to completion one-by-one).
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 1: Concurrent group runs its members together`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 3.3 Write property test for ordered steps and termination
    - **Property 2: Steps run in order and the performance terminates**
    - **Validates: Requirements 2.2, 2.3, 2.4**
    - Assert steps execute in declared order, no cross-step overlap, and `run()` returns after the final step.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 2: Steps run in order and the performance terminates`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [x] 3.4 Implement gated audio start in `run()`
    - Start ungated movements' loops at time zero; run a gated movement's `lead_in`; when the gate signals (its `lead_in` completing or an explicit `asyncio.Event`), `start()` the `PlaybackController` — with no fixed idle sleep between the gate signal and `start()`.
    - `gate is None` ⇒ start audio at time zero.
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [ ]* 3.5 Write property test for gated audio start
    - **Property 5: Audio start is gated on movement progress**
    - **Validates: Requirements 6.1, 6.3, 6.4**
    - Random definitions with/without a gate; assert audio begins only after the gate signal (never before), begins at t=0 when no gate, and ungated movements begin at t=0.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 5: Audio start is gated on movement progress`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 3.6 Write example test for prompt-start after gate
    - Structural assertion that no fixed idle `sleep` sits between the gate signal and `PlaybackController.start()`.
    - _Requirements: 6.2_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`. Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement Loop_Until_Audio and loop targeting
  - [x] 5.1 Implement loop-until-audio in `run()`
    - For each looping movement, repeat `loop_body` while `playback.is_active()`, checking the flag **between** whole iterations so a started iteration always completes; when playback becomes inactive, stop starting new iterations and proceed to the return phase.
    - Apply looping only to `PerformanceStep`s flagged `loop_for_audio`; unflagged steps run their bodies exactly once.
    - _Requirements: 5.3, 7.1, 7.2, 7.3, 7.4_

  - [x] 5.2 Write property test for loop-until-audio (no mid-move cutoff)
    - **Property 6: Loop bodies repeat for the audio duration and are never cut off**
    - **Validates: Requirements 5.3, 7.1, 7.2, 7.3**
    - Fake `PlaybackController` whose `is_active()` flips false after a random number of iterations; assert loop bodies repeat while active, the flag is checked only between whole iterations, and no new iteration starts once inactive.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 6: Loop bodies repeat for the audio duration and are never cut off`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 5.3 Write property test for loop targeting
    - **Property 7: Loop targeting follows the definition**
    - **Validates: Requirements 7.4**
    - Random multi-step definitions; assert only `loop_for_audio` steps repeat and every unflagged step runs its bodies exactly once.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 7: Loop targeting follows the definition`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

- [x] 6. Implement return-to-rest and safe-rest recovery
  - [x] 6.1 Implement return phases, inter-step rest, and exception safe-rest in `run()`
    - After looping, run each movement's `do_return`; then call `trunkController.return_to_rest()` for residual channels; between sequential steps return driven channels to a known rest before the next step's lead-in as needed.
    - Wrap the body in try/except: on any exception, log the offending phase and drive all servos to safe rest via `trunkController.return_to_rest()` (mirroring `Animatronic._safe_rest`); do not swallow the error silently.
    - _Requirements: 8.1, 8.3, 8.4, 10.4_

  - [x]* 6.2 Write property test for every owned channel ending at rest
    - **Property 8: Every owned channel ends at rest**
    - **Validates: Requirements 8.1, 8.3, 8.4, 10.4**
    - Include runs where a phase raises; assert every driven channel's final commanded angle equals its `REST_POSITIONS` value, and driven channels return to a known rest between sequential steps. Assert against the captured `SERVO_SIM` command log.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 8: Every owned channel ends at rest`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

- [x] 7. Checkpoint - Ensure all tests pass
  - Run `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`. Ensure all tests pass, ask the user if questions arise.

- [x] 8. Add Movements phase adapters
  - [x] 8.1 Add `menacing_reach` phase adapters to `Movements`
    - Add `menacing_reach_lead_in` (REACH out; opens the audio gate), `menacing_reach_loop_body` (ONE menace swing 25<->55), `menacing_reach_return` (RETRACT to rest), reusing the existing `move_to` keyframes/values. No audio logic in any phase.
    - Hold the `verified_pose_override` for shoulder tilt across lead_in→loop→return via an `AsyncExitStack` owned by the movement adapter, closing it in the return phase.
    - Keep the existing standalone `menacing_reach()` working (delegating to the same primitives): reach, swing 3×, retract.
    - _Requirements: 9.1, 9.2, 9.3, 9.5_

  - [x]* 8.2 Write property test for phased-vs-standalone equivalence
    - **Property 10: Phased composition reproduces standalone behavior**
    - **Validates: Requirements 9.3**
    - Assert `lead_in` + `loop_body` × 3 + `return` produces the same `(channel, angle)` command sequence as standalone `menacing_reach()`. Compare `SERVO_SIM` command logs.
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 10: Phased composition reproduces standalone behavior`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 8.3 Write property test for override scoping and clamping
    - **Property 9: Verified-pose overrides are scoped and clamped**
    - **Validates: Requirements 8.2, 9.5**
    - Run definitions whose movements open a `verified_pose_override`; assert `TrunkController._limit_overrides` is restored after the run and every commanded angle lies within the effective clamp (widened only while active, global `SAFE_LIMITS` otherwise).
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 9: Verified-pose overrides are scoped and clamped`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [x] 8.4 Add `look_around_random` phase adapters to `Movements`
    - Add `look_scan_lead_in` (center the head), `look_scan_loop_body` (ONE random glance + dwell, reusing existing per-glance random pan/tilt bounds and dwell), `look_scan_return` (neck to center). No audio logic. `owned_channels = {NECK_PAN, NECK_TILT}`.
    - Keep the existing standalone `look_around_random()` working.
    - _Requirements: 9.1, 9.2, 9.4_

  - [ ]* 8.5 Write property test for scan bounds invariant
    - **Property 11: Scan glances stay within neck bounds**
    - **Validates: Requirements 9.4**
    - For any number of `look_scan_loop_body` invocations, assert every commanded neck-pan/tilt angle lies within the declared scan bounds (pan ∈ [PAN_MIN, PAN_MAX], tilt ∈ [TILT_MIN, TILT_MAX]).
    - Tag: `# Feature: audio-synced-concurrent-gestures, Property 11: Scan glances stay within neck bounds`
    - `@settings(max_examples=100)`; `SERVO_SIM=1`.

  - [ ]* 8.6 Write example test for phase protocol / no audio logic
    - Assert both adapters expose the phase callables and `owned_channels`; structural check that phase methods do not reference `AudioPlayer` / `PlaybackController`.
    - _Requirements: 9.1, 9.2_

- [x] 9. Checkpoint - Ensure all tests pass
  - Run `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`. Ensure all tests pass, ask the user if questions arise.

- [x] 10. Define and register the `brains` performance
  - [x] 10.1 Build the `BRAINS` PerformanceDefinition and wire it into `animatronic.py`
    - Add `'brains.wav'` to `Animatronic.music` with an index comment.
    - Add a `brains()` method that builds the single-step `BRAINS` `PerformanceDefinition` (concurrent group of `menacing_reach` (channels 4-7) + `look_around_random` (channels 0-1), `loop_for_audio=True`, `gate=GateSpec("menacing_reach")`, `supplies_gate=True` on `menacing_reach`, ungated head scan at t=0) and runs it via `asyncio.run(PerformanceRunner(BRAINS, mv, audio_dir).run())`.
    - Register `'brains': a.brains` in `action_map` inside `main()`, dispatched by the existing allowlist under `servo_lock()`.
    - _Requirements: 1.5, 2.5, 4.3, 6.5, 7.5, 8.4, 11.1, 11.2, 11.3, 11.4_

  - [ ]* 10.2 Write example test for the `brains` instance
    - Run the BRAINS definition against a short fake track: single concurrent group runs, audio gated on `menacing_reach`, head scan starts at t=0, both bodies loop, ends with arm retracted and neck centered (assert against `SERVO_SIM` log).
    - _Requirements: 2.5, 4.3, 6.5, 7.5, 8.4_

  - [ ]* 10.3 Write example tests for registration and execution model
    - Assert `'brains'` is in `animatronic.py`'s `action_map` and dispatches the runner; `'brains.wav'` is in `Animatronic.music`; dispatch acquires `servo_lock()` (fail-fast when busy) via the same path as existing routines; the runner is the sole coordination entry point.
    - _Requirements: 1.1, 1.2, 1.5, 11.1, 11.2, 11.3, 11.4_

- [ ] 11. Opaque-movement extensibility coverage
  - [ ]* 11.1 Write example tests for opaque movements / extensibility
    - Drive the runner with a stub movement exposing only the phase shape; register a second trivial definition through the same runner unchanged; assert two definitions can share the same `MovementSpec` by reference.
    - _Requirements: 1.3, 1.4, 3.3, 3.4_

- [x] 12. Collision-safety integration coverage
  - [x]* 12.1 Write integration test for combined extreme poses (collision model)
    - Feed the `brains` combined extremes — arm fully out (`menacing_reach` reach pose) with the neck at its scan corners — to `kinematics.CollisionModel.is_pose_safe(...)` and assert every combination is SAFE.
    - _Requirements: 10.2, 10.3_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Run `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Each task references specific requirements for traceability.
- Checkpoints ensure incremental validation.
- Property tests (Hypothesis, ≥100 examples) validate the universal coordination
  invariants; each runs with `SERVO_SIM=1` against the captured servo command log
  with fake movements and a fake `PlaybackController` — no hardware, no real audio.
- Example/integration tests cover the specific `brains` instance, registration/
  execution model, phase protocol, and offline collision safety.
- Run tests quietly and stop on first failure: `SERVO_SIM=1 pytest -q --maxfail=1 -k performance`.
- Real-audio end-to-end verification on the Pi is manual (requires the audio device) and is not part of the automated suite.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 4, "tasks": ["3.5", "3.6", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "6.1"] },
    { "id": 6, "tasks": ["6.2", "8.1", "8.4"] },
    { "id": 7, "tasks": ["8.2", "8.3", "8.5", "8.6", "10.1"] },
    { "id": 8, "tasks": ["10.2", "10.3", "11.1", "12.1"] }
  ]
}
```
