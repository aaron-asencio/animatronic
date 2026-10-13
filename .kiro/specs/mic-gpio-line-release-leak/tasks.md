# Implementation Plan

> **Scope / confinement.** The fix is confined to `src/micwebcontroller.py`.
> `src/audio_player.py` and `src/performance.py` are **victims, not causes** —
> they are confirmed unchanged by inspection only (no edits). Do NOT modify the
> old, refuted `voice-fx-audio-dropout-fix` spec or its test.
>
> **How to run checks.**
> - Compile checks: `python -m py_compile src/micwebcontroller.py`.
> - Tests: `.venv/bin/python -m pytest <file> -q` (quiet, per workspace testing
>   rule).
> - **NO SERVO_SIM** collision/limit verification anywhere in this plan — this
>   bug is about GPIO line ownership, not servo motion. The operator validates
>   any hardware behavior directly on the Pi.
> - Keep all in-code GPIO safety intact: `_release_gpio()` still `.off()`s each
>   device before releasing; device writes still go through gpiozero on/off.
>
> **Hardware gating.** The authoritative kernel-line-release proof (fd inspection
> + separate-process re-claim) only runs on the Pi **as root** against real
> lgpio. It is skipped elsewhere (`os.geteuid() != 0`, no `/dev/gpiochip0`, or
> the lgpio factory cannot be constructed). On hardware it claims GPIO6/GPIO15,
> so the operator MUST stop the live mic service before running it, otherwise
> the mic service still owns the lines.
>
> **Git policy (execution agents MUST honor).** Do NOT commit, do NOT create or
> switch branches, do NOT merge or push. Leave all changes in the working tree
> for the user to review. No commit unless the user explicitly asks.

- [ ] 1. Write bug-condition exploration test (hardware-faithful, MUST FAIL on unfixed code)
  - **Property 1: Bug Condition** - Kernel line released after stop
  - **CRITICAL**: This test MUST FAIL on UNFIXED code — the failure confirms the
    kernel line leak exists. **DO NOT fix the test or the code when it fails.**
  - **NOTE**: This test encodes the expected behavior; it will validate the fix
    when it PASSES after implementation.
  - **GOAL**: Surface a counterexample proving the mic process still holds the
    GPIO6/GPIO15 kernel lines after a completed stop.
  - **This is the hardware-faithful proof that replaces the refuted prior spec's
    in-process fake.** Do NOT install the `fake-gpiozero` stub for this file — it
    MUST exercise the REAL gpiozero `LGPIOFactory`. A fake `close()` frees the
    pin by construction and structurally cannot represent the lgpio leak (that
    was the prior spec's false negative).
  - Add a new test file `tests/test_mic_gpio_line_release.py`.
  - **Drive the real stop path**: call `micwebcontroller.start_streaming()`, feed
    a few synthetic in-stream frames through `talk()` so GPIO6/GPIO15 are
    actually claimed (lazy device creation), then call
    `micwebcontroller.stop_streaming()`.
  - **Primary check (fd inspection)**: after stop, assert there are NO
    `anon_inode:gpio-line` fds remaining in `/proc/<pid>/fd` for the current
    process (and ideally no `/dev/gpiochip0` handle). **EXPECTED: FAILS on
    unfixed code** (the gpio-line fds persist).
  - **Secondary check (separate-process re-claim)**: spawn a SEPARATE
    `sudo .venv/bin/python` process that constructs `LED(EYE_LIGHT_PIN)` and
    `DigitalOutputDevice(MOUTH_MOTOR_PIN)` and asserts no `lgpio.error:
    'GPIO busy'`. **EXPECTED: FAILS on unfixed code** (`GPIO busy`). This most
    directly mirrors the real `AudioPlayer.__init__` Routine crash.
  - **GATING**: `pytest.skip` / `skipif` when `os.geteuid() != 0` OR
    `/dev/gpiochip0` does not exist OR the lgpio factory cannot be constructed.
    The skip reason MUST state the line-release proof only runs on the Pi as
    root.
  - **CAUTION (document in the task/test text)**: this test claims GPIO6/GPIO15,
    so it must only run when the live mic service is NOT holding them — the
    operator stops the mic service before running on hardware.
  - Document the counterexample as comments (both gpio-line fds remain open after
    a successful stop; a separate claimant raises `GPIO busy`), and note
    explicitly that this hardware-faithful proof replaces the refuted prior
    spec's in-process fake.
  - Run on UNFIXED code. **EXPECTED OUTCOME**: test FAILS (proves the leak).
    Mark complete when the test is written, run, and the failure is documented.
  - _Bug_Condition: isBugCondition(input) — mic streamed once, stop returned
    (is_streaming false), yet micServiceStillHoldsLine(EYE_LIGHT_PIN/MOUTH_MOTOR_PIN)_
  - _Requirements: 1.1, 1.2, 1.3, 1.4 (asserts expected behavior 2.1, 2.2, 2.3, 2.4)_

- [ ] 2. Write preservation tests BEFORE the fix (observation-first, MUST PASS on unfixed code)
  - **Property 2: Preservation** - Non-stop paths unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on
    UNFIXED code for the non-bug-condition paths, then capture it in tests that
    must still pass after the fix.
  - **EXPECTED OUTCOME**: all tests PASS on UNFIXED code (confirms the baseline
    behavior to preserve). Mark complete when written, run, and passing on
    unfixed code.

  - [ ] 2.1 Live-stream ownership preservation (3.1)
    - While `is_streaming` is true, assert `talk()` drives the envelope and
      claims/drives both devices (`DigitalOutputDevice(MOUTH_MOTOR_PIN)`,
      `LED(EYE_LIGHT_PIN)`) exactly as before, and the factory is NOT closed
      mid-stream.
    - May use the in-process logic approach. **If any fake/mocked factory is
      used, the test MUST document (docstring/comment) that it proves
      logic/ordering only and CANNOT prove kernel release.**
    - _Requirements: 3.1_

  - [ ] 2.2 Mic re-toggle re-claim preservation (3.2)
    - A second `start → (synthetic frames) → stop` cycle re-claims the pins and
      streams (lazy factory recreation after a prior release).
    - On the hardware-faithful variant, gate to root exactly as in Task 1
      (`os.geteuid() != 0`, `/dev/gpiochip0` missing, or factory not
      constructible → skip, with the Pi-only-as-root skip reason).
    - _Requirements: 3.2_

  - [ ] 2.3 Effects/recording/config untouched preservation (3.3)
    - Assert `/effects`, `/effects/save`, `/effects/revert`, and `/config`
      endpoints plus their persisted `ConfigStore` state behave identically to
      unfixed code (these paths never touch `_release_gpio()`).
    - Reuse the Flask test-client + temp-store pattern from
      `tests/test_micwebcontroller.py`. Use property-based testing over valid
      inputs where practical (hypothesis, **min 100 examples** where used), else
      representative examples.
    - _Requirements: 3.3_

  - [ ] 2.4 Victim modules unchanged preservation (3.4)
    - By inspection, assert `src/audio_player.py` and `src/performance.py` are
      NOT modified by this fix.
    - A focused check that `AudioPlayer.__init__` succeeds once the mic has
      released the pins covers the observable effect — gate the real-claim part
      to root (as in Task 1).
    - _Requirements: 3.4_

  - [ ] 2.5 In-process stop-path ORDERING logic test (documented limitation)
    - Assert the `_release_gpio()` stop-path ordering: each device
      `.off()` → `.close()` → nulled, THEN the factory closed, THEN
      `Device.pin_factory` nulled, in that order.
    - **REQUIRED docstring**: this test verifies call ORDERING ONLY and CANNOT
      prove the kernel line is released (a fake/mocked factory frees the pin by
      construction). It MUST NOT be presented as proof of release — the
      authoritative proof is the hardware-faithful check in Task 1. This is the
      explicit guard against repeating the prior spec's false negative.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 3. Implement the fix (Option A — close the gpiozero pin factory on the stop path)

  - [ ] 3.1 Import `Device` and close the pin factory in `_release_gpio()`
    - Import `Device` from `gpiozero` at the module top, next to the existing
      `PWMLED` / `LED` / `DigitalOutputDevice` imports.
    - In `_release_gpio()`, AFTER the existing device `.off()`/`.close()`/null
      loop (both globals already nulled), fetch `Device.pin_factory`; if not
      `None`, call `.close()` inside a guarded `try/except` that prints a warning
      on failure and NEVER raises (match the existing per-device guard style),
      then set `Device.pin_factory = None` so gpiozero lazily builds a fresh
      factory on the next device construction.
    - Ground the change in design → Fix Implementation (Option A).
    - **Verification**: `python -m py_compile src/micwebcontroller.py` plus the
      Task 2.5 ordering logic check. NO SERVO_SIM.
    - Keep in-code GPIO safety intact: the `.off()` before release still happens.
    - _Bug_Condition: isBugCondition(input) — leak after stop_
    - _Expected_Behavior: expectedBehavior(result) — line released, claim succeeds (design Property 1)_
    - _Preservation: Preservation Requirements from design (3.1–3.4)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [ ] 3.2 Confirm the call site is unchanged
    - Confirm `_release_gpio()` still runs in `stop_streaming()` AFTER the stream
      thread has joined and `is_streaming` is `false`, and that
      `_reset_audio_dsp_state()` still runs AFTER it. No behavioral change beyond
      the factory close.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`. NO SERVO_SIM.
    - _Preservation: 3.1 (factory close only on stop, after thread join)_
    - _Requirements: 2.1_

  - [ ] 3.3 Keep existing `is_streaming` guards in `talk()`/`audio_callback` as-is
    - Design decision: the guards are harmless and kept to keep the change
      minimal. This is a **no-change confirmation** sub-task — do NOT revert them
      and do NOT expand them.
    - **Verification**: `python -m py_compile src/micwebcontroller.py`. NO SERVO_SIM.
    - _Preservation: 3.1 (guards transparent while streaming)_
    - _Requirements: 2.1_

  - [ ] 3.4 Verify bug-condition exploration test now PASSES
    - **Property 1: Expected Behavior** - Kernel line released after stop
    - **IMPORTANT**: Re-run the SAME test from Task 1 — do NOT write a new test.
    - Run `.venv/bin/python -m pytest tests/test_mic_gpio_line_release.py -q`.
    - **EXPECTED OUTCOME**: PASSES — after stop no `anon_inode:gpio-line` fds
      remain, and the separate-process `LED(EYE_LIGHT_PIN)` /
      `DigitalOutputDevice(MOUTH_MOTOR_PIN)` claim succeeds without `GPIO busy`.
    - Gated to root/Pi as in Task 1 (skipped elsewhere). NO SERVO_SIM.
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [ ] 3.5 Verify preservation tests still PASS
    - **Property 2: Preservation** - Non-stop paths unchanged
    - **IMPORTANT**: Re-run the SAME tests from Task 2 — do NOT write new tests.
    - Run `.venv/bin/python -m pytest tests/test_mic_gpio_line_release.py -q` and
      the preservation checks (live-stream ownership, mic re-toggle re-claim,
      effects/config endpoints, victim-module `AudioPlayer.__init__` success).
    - **EXPECTED OUTCOME**: all PASS — no regressions. Hardware-faithful parts
      gated to root/Pi as in Task 1. NO SERVO_SIM.
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 4. Checkpoint — ensure all checks pass
  - `python -m py_compile src/micwebcontroller.py` (syntax/import).
  - `.venv/bin/python -m pytest tests/test_mic_gpio_line_release.py -q`
    (bug-condition + preservation; hardware-faithful parts skipped off the Pi).
  - `.venv/bin/python -m pytest tests/test_micwebcontroller.py -q` (confirm
    existing endpoint tests are unaffected).
  - State clearly: the authoritative kernel-line-release proof only runs on the
    Pi **as root** and is skipped elsewhere — so on hardware the operator must
    run it with the mic service STOPPED.
  - Confirm both correctness properties hold: Property 1 (Bug Condition — line
    released after stop) and Property 2 (Preservation — non-stop paths
    unchanged).
  - NO SERVO_SIM. Leave changes in the working tree (no commit, no branch).
  - Ask the user if any questions arise.
