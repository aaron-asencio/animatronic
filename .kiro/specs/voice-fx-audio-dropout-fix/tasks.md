# Implementation Plan

Fix the Voice-FX → Routine failure caused by a **cross-process GPIO ownership
leak**. After a live mic session, the long-lived mic service process
(`src/micwebcontroller.py`, port 5000) keeps holding `EYE_LIGHT_PIN` (GPIO6) and
`MOUTH_MOTOR_PIN` (GPIO15), so a separate `animatronic.py` Routine subprocess
raises `lgpio.error: 'GPIO busy'` in `AudioPlayer.__init__` before any audio
plays. The fix is confined to the mic-stop/callback path in
`src/micwebcontroller.py`:

- Guard the lazy device (re)creation in `talk()` / short-circuit
  `audio_callback` on `is_streaming`, so a straggler PortAudio callback cannot
  re-grab GPIO6/GPIO15 after `_release_gpio()`.
- In `stop_streaming()`, stop AND close the live PyAudio stream (no more
  callbacks can fire) BEFORE calling `_release_gpio()`.
- `_release_gpio()` then `off()`+`close()`es both devices and nulls the globals,
  freeing the pins in-process.

`src/audio_player.py` and `src/performance.py` are **NOT** modified — they are
the victim of the leak and the preservation reference (confirmed by inspection
only). The already-applied orderly PyAudio teardown + `_reset_audio_dsp_state()`
are **preserved** (requirement 3.3), not the fix.

Testing reuses the `sys.modules` fake `pyaudio` / `gpiozero` stub pattern from
`tests/test_micwebcontroller.py` (install the fakes BEFORE `import
micwebcontroller`). The fake `gpiozero` `LED` / `DigitalOutputDevice` model pin
ownership: constructing a device "claims" its pin and raises a `GPIO busy`-style
error if that pin is already held by a live device; `close()` releases it. This
lets a test stand in for the Routine subprocess's `AudioPlayer` claiming the
same pin. Property tests use `hypothesis` (min 100 examples). Run tests quietly:

```bash
pytest tests/test_voice_fx_dropout.py -q --maxfail=1
```

> Per the workspace hardware-movement rule, do NOT add `SERVO_SIM`
> collision/limit verification tasks — the servo path is unchanged by this fix.
> The servo safety preservation (3.6) is verified by inspection (no servo code
> is touched). Each task below instead runs `python -m py_compile` compile
> checks plus GPIO/audio-logic verification.

---

- [x] 1. Write bug condition exploration test (GPIO leak via post-stop callback)
  - **Property 1: Bug Condition** - Routine Claims GPIO6/GPIO15 After a Mic Session
  - **CRITICAL**: This test MUST FAIL on the unfixed code - failure confirms the bug exists.
  - **DO NOT attempt to fix the test or the code when it fails** at this step.
  - **NOTE**: This test encodes the Expected Behavior (2.1-2.4); it will validate the fix once it passes after implementation.
  - **GOAL**: Surface a counterexample that reproduces the callback-vs-release race on unfixed code, confirming the "PortAudio callback re-creating devices after stop" mechanism in design.md ("Hypothesized Root Cause"). If a counterexample refutes it, re-hypothesize before implementing.
  - Reuse/replace `tests/test_voice_fx_dropout.py` (it already exists from the prior, now-refuted pass — overwrite its contents to target the GPIO leak). Install fake `pyaudio` and `gpiozero` modules in `sys.modules` BEFORE `import micwebcontroller` (mirror the stub pattern in `tests/test_micwebcontroller.py`), then import `micwebcontroller`.
  - **Fake gpiozero** must model pin ownership: `LED(pin)` / `DigitalOutputDevice(pin)` construction "claims" `pin` and raises a `GPIO busy`-style error if `pin` is already claimed by a live device; `off()` is recorded and `close()` releases the pin. **Fake PyAudio**: `open(...)` must capture the `stream_callback` (the `audio_callback`) so the test can fire it manually, and return a fake stream recording `start_stream`/`stop_stream`/`close`.
  - **Scoped PBT Approach**: this is a deterministic race, so scope the property to the concrete failing hand-off (one mic session, one straggler callback, one Routine claim) rather than a broad input space.
  - **Simulate the race**: call `micwebcontroller.start_streaming()`, then `micwebcontroller.stop_streaming()`. Capture the `audio_callback` passed to the fake stream's `open()` and **invoke it once more AFTER `stop_streaming()` returns** to stand in for a straggler PortAudio callback still running `talk()`.
  - Assertion A (**1.1**): after the post-stop callback, the module globals `micwebcontroller.jaw_motor` and `micwebcontroller.led_eye_light` are `None` AND each was `close()`d during `_release_gpio()`. **EXPECTED OUTCOME: FAILS** — unfixed `talk()` consults neither `is_streaming`, so the straggler callback re-creates the devices, leaving the globals non-`None` and the pins claimed.
  - Assertion B (**1.2 / 1.3**): after the post-stop callback, construct a fresh fake `LED(EYE_LIGHT_PIN)` and `DigitalOutputDevice(MOUTH_MOTOR_PIN)` (standing in for the Routine subprocess's `AudioPlayer.__init__`) and assert no simulated `GPIO busy` is raised. **EXPECTED OUTCOME: FAILS** — the mic process still owns GPIO6/GPIO15, so the construction raises.
  - Document the concrete counterexamples found as comments in the test (e.g. "post-stop callback left `jaw_motor` non-`None`; `LED(6)` raised simulated `GPIO busy`") and note that they confirm the callback-re-creation mechanism.
  - **Verification**: run `python -m py_compile src/micwebcontroller.py tests/test_voice_fx_dropout.py`; run the test and confirm it FAILS for the documented reason.
  - Mark complete when the test is written, run on unfixed code, and the failure/counterexamples are documented.
  - _Requirements: 1.1, 1.2, 1.3, 1.4 (Bug Condition); asserts 2.1, 2.2, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Streaming Drive, First-Run, and Safety Paths Unchanged
  - **IMPORTANT**: Follow the observation-first methodology — run the UNFIXED code for non-bug-condition inputs (`isBugCondition(X)` false), record the actual outputs, then write tests asserting those observed outputs. All of these MUST PASS on the unfixed code (they are the baseline to preserve).
  - Add these to `tests/test_voice_fx_dropout.py` using the same `sys.modules` fakes and the Flask test client / temp-store fixture pattern from `tests/test_micwebcontroller.py`.
  - **Streaming drive preservation (3.1)** — *property, hypothesis*: with `is_streaming` true, feed random in-stream chunk sequences through `talk(...)` and capture the jaw-motor (GPIO15) / eye-LED (GPIO6) on/off decisions and the lazy device creation on first frame; assert they are reproduced exactly (the `is_streaming` guard is transparent while streaming). Effects use `numpy`/deterministic math (no `random` module) — pin any needed seed. Min 100 examples. **EXPECTED: PASSES on unfixed code.**
  - **First-run Routine (3.2)** — *unit*: standing in for a fresh service with no prior mic session, construct the fake `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` (as `AudioPlayer.__init__` would) with no live mic owner and assert it succeeds — the known-good first-run claim is unaffected. **EXPECTED: PASSES.**
  - **Orderly teardown preservation (3.3)** — *unit*: `start_streaming()` then `stop_streaming()` still records `stop_stream()` → `close()` on the live stream before `terminate()` on the PyAudio instance, and still calls `_reset_audio_dsp_state()`. **EXPECTED: PASSES.**
  - **GPIO-release preservation (3.3)** — *unit*: after a normal `start_streaming()`/`stop_streaming()` (no straggler callback), `_release_gpio()` still `off()`s + `close()`s both fake devices and nulls `jaw_motor` / `led_eye_light`. **EXPECTED: PASSES.**
  - **Effect-chain load/persist/revert (3.4)** — *unit*: selecting / saving / reverting a style via the `/effects` endpoints still resolves saved-override-else-factory and persists through `ConfigStore`, with `effects_config`, `saved_voice_styles`, and `current_style` identical to today. **EXPECTED: PASSES.**
  - **Mic preempt (3.5)** — *unit*: requesting a Routine/Mode while streaming still auto-stops/preempts the mic (the preempt path still calls `stop_streaming()`); the stop-path change does not alter the trigger. **EXPECTED: PASSES.**
  - **Servo safety (3.6)** — verify by inspection only: assert in a comment that no servo code is modified by this spec; do NOT run `SERVO_SIM`.
  - **Verification**: run `python -m py_compile` on the test file; run the preservation tests and confirm they PASS on unfixed code.
  - Mark complete when the tests are written, run, and passing on unfixed code.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6 (Preservation)_

- [x] 3. Fix the cross-process GPIO ownership leak in the mic process

  - [x] 3.1 Guard `talk()` / `audio_callback` device (re)creation on `is_streaming`
    - In `src/micwebcontroller.py`, make `talk()` only lazily construct the module-global `jaw_motor` (`DigitalOutputDevice(MOUTH_MOTOR_PIN)`) and `led_eye_light` (`LED(EYE_LIGHT_PIN)`) — and only drive them — when `is_streaming` is true. Equivalently, short-circuit `audio_callback` to a no-op (returning the normal `paContinue` tuple) when `is_streaming` is false before calling `talk()`. Once `stop_streaming()` sets `is_streaming = False`, a straggler PortAudio callback can never re-create or re-grab GPIO6/GPIO15.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; GPIO-logic check that a post-stop callback leaves `jaw_motor` / `led_eye_light` `None` while an in-stream call still creates/drives them.
    - _Bug_Condition: isBugCondition(X) — mic process must not re-grab GPIO6/GPIO15 after stop_
    - _Expected_Behavior: expectedBehavior(result): post-stop callback does not re-create devices_
    - _Preservation: in-stream `talk()` creation/drive unchanged while `is_streaming` true (3.1)_
    - _Requirements: 1.1, 1.4, 2.1, 2.4_

  - [x] 3.2 Close the live stream BEFORE releasing GPIO in `stop_streaming()`
    - Reorder `stop_streaming()` so the sequence is: set `is_streaming = False` (callbacks become no-ops per 3.1); join the worker thread (fast, as today); if `audio_state['stream']` is not `None`, `stop_stream()` then `close()` it (each guarded so cleanup never raises) so PortAudio fires no further callbacks; `terminate()` the PyAudio instance; THEN `_release_gpio()`; THEN `_reset_audio_dsp_state()`. The stream is closed and the callback is dead before any GPIO release.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; audio-logic check via the fake-PyAudio ordering assertion (`is_streaming=False` → join → `stop_stream` → `close` → `terminate` → `_release_gpio` → `_reset_audio_dsp_state`).
    - _Bug_Condition: isBugCondition(X) — no callback may fire after _release_gpio()_
    - _Expected_Behavior: expectedBehavior(result): stream stopped+closed before GPIO released_
    - _Preservation: orderly `stop_stream`/`close`/`terminate` + `_reset_audio_dsp_state()` kept (3.3)_
    - _Requirements: 1.1, 1.2, 2.1, 2.2_

  - [x] 3.3 Confirm `_release_gpio()` frees the pins in-process and nothing re-grabs them
    - Confirm `_release_gpio()` calls `off()` then `close()` on each of `jaw_motor` / `led_eye_light` (freeing the underlying lgpio pin) and nulls both module globals and jaw state. Given 3.1 and 3.2, verify nothing re-claims the pins afterward (no straggler callback can run `talk()`; the stream is closed). No behavioral change to `_release_gpio()` itself beyond running after the stream is closed and the callback is dead.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; GPIO-logic check that after `stop_streaming()` both fake devices are closed, globals are `None`, and a fresh `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` construction succeeds (no simulated `GPIO busy`).
    - _Bug_Condition: isBugCondition(X) — mic process must relinquish GPIO6/GPIO15 on stop_
    - _Expected_Behavior: audio_player_constructed_without_error(result) — Routine can claim the pins_
    - _Preservation: `_release_gpio()` off()+close()+null behavior unchanged (3.3)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.4 Keep the orderly PyAudio teardown + `_reset_audio_dsp_state()` intact (preserve)
    - Confirm (no behavioral change) that the already-applied orderly stream teardown (`stop_stream()` → `close()` before `terminate()`) and the `_reset_audio_dsp_state()` call remain in `stop_streaming()`; 3.2 only fixes their ordering relative to `_release_gpio()` (stream closed before GPIO release), not their own behavior. Do NOT remove them even though they are not the fix.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; audio-logic check that the teardown order and the `_reset_audio_dsp_state()` call are still present and unchanged.
    - _Preservation: orderly PyAudio teardown + `_reset_audio_dsp_state()` preserved (3.3)_
    - _Requirements: 3.3_

  - [x] 3.5 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Routine Claims GPIO6/GPIO15 After a Mic Session
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test. It encodes the expected behavior; passing confirms the leak is fixed.
    - Run the task-1 test. **EXPECTED OUTCOME: PASSES** — after `stop_streaming()` and a straggler callback, `jaw_motor` / `led_eye_light` are `None` and were `close()`d, and the fresh `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` construction (standing in for the Routine subprocess) succeeds with no simulated `GPIO busy`. Confirm this holds across repeated start → stop → claim cycles with no "service restart" (no re-import) — 2.4.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; run the task-1 test quietly and confirm PASS.
    - _Requirements: 2.1, 2.2, 2.3, 2.4 (Expected Behavior Properties)_

  - [x] 3.6 Verify preservation tests still pass
    - **Property 2: Preservation** - Streaming Drive, First-Run, and Safety Paths Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests.
    - Run the preservation tests. **EXPECTED OUTCOME: PASS** — in-stream `talk()` drive of GPIO6/GPIO15, first-run Routine claim, orderly PyAudio teardown + `_reset_audio_dsp_state()`, GPIO release, preset load/persist/revert, and mic preempt are all unchanged (no regressions). Servo safety (3.6) remains verified by inspection.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`; run the preservation tests quietly and confirm PASS.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run `python -m py_compile src/micwebcontroller.py` (compile check — no syntax/import errors before the operator runs anything on the robot). `src/audio_player.py` and `src/performance.py` are unchanged, so no edits to verify there.
  - Run the file quietly: `pytest tests/test_voice_fx_dropout.py -q --maxfail=1`. Also run `pytest tests/test_micwebcontroller.py -q` to confirm the existing endpoint tests are unaffected.
  - Confirm both correctness properties hold: Property 1 (Bug Condition / Expected Behavior) passes on fixed code, Property 2 (Preservation) still passes.
  - Do NOT run `SERVO_SIM` collision/limit verification — the servo path is unchanged; 3.6 stays verified by inspection.
  - Ask the user if any question or ambiguity arises.
