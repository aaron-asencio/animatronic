# Implementation Plan

> ## ✅ RESOLUTION NOTE — the plan below is superseded
>
> This plan (Tasks 3.1–3.3) targeted hypothesis H1 by KEEPING `_dsp_lock` and
> only shrinking the work under it. That did **not** fix the hardware wedge.
> The actual fix (confirmed via the PortAudio `Status: 6` log) **removed
> `_dsp_lock` from the real-time callback entirely** (lock-free atomic config
> publish + callback-owned DSP state via `_dsp_reset_generation`), **vectorised
> `_fx_reverb`** (a per-sample Python loop that was ~22 ms/frame, over the
> 21.3 ms deadline), and **added duplex-wedge recovery**. The checkboxes are
> left as-is for history; see the RESOLUTION section in `bugfix.md` and the
> `live-audio-troubleshooting` steering doc for the current ground truth. The
> text below that says to preserve `_dsp_lock` is historical, not current.

## Overview

This plan fixes a bug where the live microphone goes permanently silent (no crash) at the moment of
a voice-FX change, recoverable only by restarting the process. The work follows the exploratory
bugfix methodology: write a bug-condition exploration test that FAILS on the unfixed code (Property
1), establish preservation tests that PASS on the unfixed code (Property 2), implement the fix in
`src/micwebcontroller.py`, then re-run both to confirm the bug is resolved with no regressions. The
tasks run as a linear sequence (Task 1 → Task 2 → Task 3 → Task 4).

> **Scope & conventions (read first).**
> - The fix is **confined to `src/micwebcontroller.py`**. No servo/movement code is touched, so there
>   are **no `SERVO_SIM`** concerns. Do not touch `audio_player.py`, `performance.py`, or any
>   servo/routine code.
> - This bug is **distinct** from `voice-fx-style-change-stream-crash` (a crash, already fixed) and
>   `voice-fx-audio-dropout-fix` (mic→routine GPIO leak). Here the live mic goes **permanently silent
>   with no crash** at the moment of an FX change, recovered only by a process restart.
> - **Write the exploration test BEFORE implementing the fix.** Run it on the **UNFIXED** code first
>   to confirm the stall/mask (Task 1 must FAIL), and run the preservation tests on unfixed code to
>   confirm the baseline (Task 2 must PASS) before any source change.
> - The faults involve a **real-time deadline** and a **shared duplex ALSA device**. The device-level
>   wedge needs **hardware confirmation on the Pi**; the automated tests target the mechanisms the fix
>   changes in a **device-independent** way: under-lock I/O, callback lock-hold time under concurrent
>   request-thread mutation, and recurring-exception masking. A single-threaded test cannot surface
>   the contention — the exploration test MUST run **two real threads**.
> - **Tests:** `.venv/bin/python -m pytest -q` (add `--maxfail=1` for focused runs).
>   **Compile:** `python -m py_compile src/micwebcontroller.py`.
> - New test modules install hardware stubs BEFORE importing `micwebcontroller`, mirroring
>   `tests/test_micwebcontroller.py`. The **gpiozero stub MUST provide `Device`** (plus `PWMLED`,
>   `LED`, `DigitalOutputDevice`) because `micwebcontroller` does `from gpiozero import Device`; the
>   **pyaudio stub** must provide `paInt16`, `paContinue`, `PyAudio`.
> - **CHUNK deadline:** 1024 frames / 48000 Hz ≈ **21.3 ms**. Any callback-side work (including
>   blocking on `_dsp_lock` or I/O under it) must stay well inside this.
> - **Preserve the landed `voice-fx-style-change-stream-crash` fix:** the `_dsp_lock` serialization,
>   the whole-chain atomic rebinds, and the build-new-then-atomic-rebind `_reset_audio_dsp_state` must
>   stay intact; the "deque mutated during iteration" race must NOT be reintroduced.
> - **Git policy for execution agents:** do NOT commit, do NOT create or switch branches, leave all
>   changes in the working tree. Commit only if the user explicitly asks.

## Tasks

- [x] 1. Write bug-condition exploration test (MUST FAIL on unfixed code)
  - **Property 1: Bug Condition** — FX change keeps live mic audible (no stall, no masked error)
  - **CRITICAL**: This test MUST FAIL on unfixed code — the failure confirms the silent-stall /
    masked-exception mechanisms exist. **DO NOT attempt to fix the test or the code when it fails.**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after
    implementation (re-run unchanged in Task 3.5).
  - **GOAL**: Surface counterexamples that demonstrate the bug on the UNFIXED module and
    disambiguate H1 (lock-induced underrun / under-lock I/O) vs H2 (masked recurring exception). A
    single-threaded test cannot surface the contention.
  - **Scoped PBT approach**: the device-level duplex wedge (H3) needs the Pi; scope the automated
    property to the device-independent proxies below and document the hardware step as a manual
    integration check (Task 4).
  - Create a new test file (e.g. `tests/test_voice_fx_change_mic_silence.py`). Install the hardware
    stubs BEFORE importing `micwebcontroller`, mirroring `tests/test_micwebcontroller.py` (`pyaudio`
    stub with `paInt16`/`paContinue`/`PyAudio`; `gpiozero` stub providing `Device` **as well as**
    `PWMLED` / `LED` / `DigitalOutputDevice`).
  - **Variant A — under-lock I/O latency / lock-hold under contention (H1, the FAIL signal):**
    - Spin a **CONSUMER thread** that mimics the real-time callback: in a tight bounded loop acquire
      `_dsp_lock` around `talk(...)` then call `apply_effects(frame)` (which acquires `_dsp_lock`
      itself), with a fixed `int16[CHUNK]` frame; measure the max time the callback-side critical
      section is blocked / held per iteration.
    - Spin a **PRODUCER thread** that issues style changes in a bounded loop
      (`_apply_chain_to_live(...)` then `_reset_audio_dsp_state()`), as the Flask request thread does.
    - Redirect stdout to a **slow/piped sink** so the per-frame `print` in `talk()` (run under the
      lock) and the request-thread `print(f"... {effects_config}")` are realistically costly.
    - Run both concurrently with an **iteration cap + join timeout** so it reproduces reliably yet
      stays fast/deterministic and never hangs.
    - **Assertion (encodes expected behavior)**: the measured callback-side lock-hold / block time
      stays well under the CHUNK deadline (≈21 ms). On UNFIXED code this FAILS — the per-frame
      under-lock `print` plus request-thread contention pushes it over the deadline.
  - **Variant B — masked recurring exception (H2):**
    - Force `apply_effects` to raise **every** frame (e.g. apply a style whose `_fx_pitch.amount`
      drives `n_src = int(len/amount)` to a pathological value, or otherwise make the chain raise).
    - Drive the callback-emulation loop and capture stdout.
    - **Assertion (encodes expected behavior)**: a recurring per-frame exception produces a
      **visible, rate-limited** diagnostic (not swallowed silently). On UNFIXED code this FAILS — the
      unconditional `try/except` returns passthrough and emits no distinguishable recurring signal.
  - Document the counterexamples as comments (e.g. "callback holds `_dsp_lock` across a per-frame
    `print` to a slow sink → lock-hold exceeds the 21 ms CHUNK deadline → duplex underrun/wedge";
    "`apply_effects` raising every frame is swallowed into permanent passthrough/silence with no
    operator-visible signal"), and note that the device-level wedge is confirmed on hardware (Task 4).
  - Run on UNFIXED code. **EXPECTED OUTCOME**: Test FAILS (confirms the bug). Mark complete when the
    test is written, run, and the failure is documented.
  - _Requirements: 1.1, 1.2, 1.3, 1.4 (asserts Expected Behavior 2.1, 2.2, 2.3, 2.4, 2.5)_

- [x] 2. Write preservation property tests BEFORE the fix (observation-first; MUST PASS on unfixed code)
  - **Property 2: Preservation** — Non-FX-change / non-live-stream behavior unchanged
  - **IMPORTANT**: Follow observation-first methodology — run the UNFIXED code for inputs where
    `isBugCondition` is false, record the actual outputs, then assert those recorded baselines.
    Property-based testing (hypothesis, ≥100 examples) is recommended for the universal preservation
    claims so edge cases a few unit tests would miss are caught.
  - **EXPECTED OUTCOME (whole task)**: all preservation tests PASS on unfixed code.

  - [x] 2.1 DSP output equality (single-threaded + PBT)
    - With a fixed, fully-applied chain and a clean DSP state (call `_reset_audio_dsp_state()` first
      for a clean slate) and a fixed seeded DSP state, feed a fixed input sequence through
      `apply_effects` on UNFIXED code and record the exact output bytes; assert equality to the
      recorded baseline, and that output is `int16` of length `CHUNK`.
    - Property-based variant (hypothesis ≥100 examples): random valid chain + random `int16` input →
      output is `int16[CHUNK]` and deterministic for a fixed seeded state.
    - _Requirements: 3.2_

  - [x] 2.2 Endpoint behavior (Flask test-client + temp `ConfigStore`)
    - Reuse the fixture pattern from `tests/test_micwebcontroller.py` (hardware stubs before import;
      per-test temp `ConfigStore`).
    - Observe then assert `/effects` (style + single-effect), `/effects/save`, `/effects/revert`, and
      `/config` return the same statuses/payloads and persist the same `ConfigStore` state.
    - PBT over valid effect names/amounts and valid style names (≥100 examples); invalid bodies →
      rejected without mutation.
    - _Requirements: 3.3_

  - [x] 2.3 Stream lifecycle & GPIO unaffected
    - Assert `start_streaming` / `stop_streaming` / `_release_gpio` observable behavior is unchanged
      at the stub level, including the `is_streaming` callback short-circuit, the GPIO line-release,
      and that the mic→routine GPIO hand-off (`voice-fx-audio-dropout-fix`) is undisturbed.
    - _Requirements: 3.4_

  - [x] 2.4 Stream-crash fix intact (regression guard)
    - Re-run (or mirror) the `voice-fx-style-change-stream-crash` thread-faithful concurrency harness:
      a consumer `apply_effects` loop vs a producer `_reset_audio_dsp_state`/chain-swap loop.
    - Assert NO `RuntimeError: deque mutated during iteration`, continuous `int16[CHUNK]` output, and
      only complete chains observed — proving the `_dsp_lock` serialization / atomic rebinds are not
      reopened.
    - Also assert `_reset_audio_dsp_state` still yields a fresh zero echo deque (len 3, `maxlen=3`), a
      fresh zero reverb buffer (len 9600) at `pos 0`, zeroed tremolo/ring phases, and a reset jaw
      envelope.
    - _Requirements: 3.1_

  - [x] 2.5 Routine audio & servo paths untouched
    - Assert (at the stub level / by inspection) that no change affects the `AudioPlayer` routine path
      and that the fix touches no servo/movement code (no `SERVO_SIM`).
    - _Requirements: 3.5, 3.6_

  - Run all of Task 2 on UNFIXED code; mark complete when written, run, and passing.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Fix the voice-FX change → live-mic silence (remove under-lock I/O; surface recurring errors)

  - [x] 3.1 Remove the per-frame `print` from `talk()`'s locked hot path (H1)
    - `talk()` runs inside the callback's `with _dsp_lock:` block and currently does a per-frame
      `print(f"RMS: ...; Jaw: ...")`. **Preferred:** delete the per-frame diagnostic `print` entirely.
      If any diagnostic must remain, gate it behind an off-by-default debug flag and build/emit the
      string **after** releasing the lock — never do stdout I/O under `_dsp_lock`.
    - Leave the jaw-envelope math, GPIO drive, and `is_streaming` short-circuit unchanged — only the
      under-lock I/O is removed.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: isBugCondition(input) — FX change around a live duplex stream (per-frame I/O under lock delays the callback past the CHUNK deadline → underrun/wedge)_
    - _Expected_Behavior: expectedBehavior(result) — no permanent silence, no stream wedge (2.1, 2.3)_
    - _Preservation: Preservation Requirements from design (jaw/GPIO/DSP math unchanged)_
    - _Requirements: 2.1, 2.3_

  - [x] 3.2 Keep both callback critical sections pure CPU, no blocking I/O (H1)
    - In `audio_callback`, keep the `is_streaming` short-circuit FIRST (unchanged — preserves 3.4).
      The callback acquires `_dsp_lock` twice per frame (around `talk()`, then inside
      `apply_effects()`); keep the two-acquire structure (`threading.Lock` is not reentrant and
      `apply_effects` acquires the lock itself) but ensure **nothing** blocking (no `print`, no
      allocation, no I/O) runs inside either critical section.
    - This keeps each callback well inside the ≈21 ms CHUNK deadline even while a request-thread
      mutation is in flight.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: isBugCondition(input) — callback contends with request-thread mutation on _dsp_lock_
    - _Expected_Behavior: expectedBehavior(result) — stream keeps producing audio, no wedge (2.1, 2.3)_
    - _Preservation: _dsp_lock serialization intact; stream-crash fix not reopened (3.1)_
    - _Requirements: 2.1, 2.3_

  - [x] 3.3 Request-thread mutators — allocate and log OFF-lock; lock covers only the rebind (H1)
    - Confirm `_reset_audio_dsp_state()` builds the fresh echo deque + reverb buffer BEFORE taking
      `_dsp_lock`, and that the locked region is only the rebinds + phase/envelope zeroing (no I/O, no
      allocation).
    - Confirm `_apply_chain_to_live(chain)` builds the new dict off-lock and rebinds under the lock.
    - **Move the large `print(f"... {effects_config}")` in the `/effects` style and single-effect
      branches to run only AFTER all lock-held work completes**, and verify no logging happens between
      the two acquisitions. Prefer a compact summary (style name + tuned/factory) over dumping the
      whole config dict.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: isBugCondition(input) — request-thread allocation/logging under or between lock acquisitions contends with the callback_
    - _Expected_Behavior: expectedBehavior(result) — request-thread mutation never blocks the callback past the CHUNK deadline (2.3)_
    - _Preservation: whole-chain atomic rebinds preserved; endpoint payloads/persistence unchanged (3.1, 3.3)_
    - _Requirements: 2.3_

  - [x] 3.4 Surface a RECURRING callback exception instead of masking it (H2/H3)
    - Keep the `try/except Exception` so a single unforeseen error returns
      `(in_data, pyaudio.paContinue)` and never propagates (preserves "never silently die").
    - Add a module-level **consecutive-error counter**, reset to 0 on any successful frame. On each
      caught exception increment it and emit a **rate-limited** diagnostic (first occurrence, then
      every Nth, with the exception detail) so an operator/log sees that FX processing is failing
      every frame. The counter increment and the `print` live **outside** `_dsp_lock` (the `except`
      runs after the lock is released). This changes only error-path observability; the happy path
      and the lock semantics are untouched.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: isBugCondition(input) — a recurring per-frame exception after an FX change is swallowed into permanent silence_
    - _Expected_Behavior: expectedBehavior(result) — recurringCallbackExceptionSwallowed = FALSE (2.4)_
    - _Preservation: one-off errors still return passthrough; lock semantics unchanged (3.1)_
    - _Requirements: 2.4, 2.5_

  - [x] 3.5 Verify bug-condition exploration test now PASSES
    - **Property 1: Expected Behavior** — FX change keeps live mic audible
    - **IMPORTANT**: Re-run the SAME test from Task 1 — do NOT write a new test. The test encodes the
      expected behavior; when it passes it confirms the expected behavior is satisfied.
    - Run with `.venv/bin/python -m pytest tests/test_voice_fx_change_mic_silence.py -q`.
    - **EXPECTED OUTCOME**: Test PASSES — callback lock-hold stays well under the CHUNK deadline with
      no under-lock I/O, and a recurring exception is surfaced (rate-limited) rather than swallowed.
    - _Requirements: Expected Behavior Properties from design — 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.6 Verify preservation tests still PASS
    - **Property 2: Preservation** — Non-FX-change / non-live-stream behavior unchanged
    - **IMPORTANT**: Re-run the SAME tests from Task 2 — do NOT write new tests.
    - **EXPECTED OUTCOME**: all PASS (no regressions in DSP output, endpoints/`ConfigStore`, lifecycle
      /GPIO, the stream-crash fix, or routine/servo paths).
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4. Checkpoint — ensure all tests pass
  - `python -m py_compile src/micwebcontroller.py`.
  - `.venv/bin/python -m pytest tests/test_voice_fx_change_mic_silence.py -q`.
  - `.venv/bin/python -m pytest tests/test_micwebcontroller.py -q` (confirm existing endpoint tests
    are unaffected).
  - `.venv/bin/python -m pytest -q` (full suite; confirm the stream-crash and audio-dropout specs'
    tests still pass).
  - **Hardware confirmation (manual, on the Pi):** start a live duplex mic stream and apply a sequence
    of style and single-effect changes both BEFORE starting and DURING the stream; confirm the
    passthrough stays audible with the new chain applied for every change, repeated changes need **no**
    micweb-service restart, and routine `AudioPlayer` audio is unaffected (bugfix 2.1, 2.2, 2.5, 3.5).
  - Confirm both correctness properties hold (Property 1 fix checking; Property 2 preservation).
  - **No `SERVO_SIM`.** Leave changes in the working tree (no commit / no branch). Ask the user if
    questions arise.

## Notes

- **Linear sequence.** The tasks run in order: write the FAIL-first exploration test (Task 1), then
  establish the PASS-first preservation baseline (Task 2), both on UNFIXED code, before any source
  change; implement the fix (Task 3); then checkpoint (Task 4). Within Task 3, apply the source
  changes in order (3.1 → 3.2 → 3.3 → 3.4), then re-run the unchanged Task 1 and Task 2 tests
  (3.5 and 3.6) to confirm the fix and preservation.
- **Fix is confined to `src/micwebcontroller.py`.** No servo/movement code is touched, so there are
  no `SERVO_SIM` concerns. Do not touch `audio_player.py`, `performance.py`, or any servo/routine
  code.
- **This bug is distinct** from `voice-fx-style-change-stream-crash` (a crash, already fixed) and
  `voice-fx-audio-dropout-fix` (mic→routine GPIO leak). Here the live mic goes permanently silent
  with no crash at the moment of an FX change, recovered only by a process restart.
- **Preserve the landed `voice-fx-style-change-stream-crash` fix:** the `_dsp_lock` serialization,
  the whole-chain atomic rebinds, and the build-new-then-atomic-rebind `_reset_audio_dsp_state` must
  stay intact; the "deque mutated during iteration" race must NOT be reintroduced.
- **CHUNK deadline:** 1024 frames / 48000 Hz ≈ 21.3 ms. Any callback-side work (including blocking on
  `_dsp_lock` or I/O under it) must stay well inside this.
- **Tests:** `.venv/bin/python -m pytest -q` (add `--maxfail=1` for focused runs).
  **Compile:** `python -m py_compile src/micwebcontroller.py`.
- **Git policy for execution agents:** do NOT commit, do NOT create or switch branches, leave all
  changes in the working tree. Commit only if the user explicitly asks.