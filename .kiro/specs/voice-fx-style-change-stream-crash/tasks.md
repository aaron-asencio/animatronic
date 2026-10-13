# Implementation Plan

> **Scope & conventions (read first).**
> - The fix is **confined to `src/micwebcontroller.py`**. `audio_player.py` and `performance.py`
>   are **uninvolved** — do not touch them.
> - **Write exploration tests BEFORE implementing the fix.** Run them on the **UNFIXED** code first
>   to confirm the bug exists (Task 1 must FAIL, Task 2 must PASS) before any source change.
> - This defect is a **thread race**: a single-threaded test cannot surface it. The exploration
>   test MUST run **two real threads**.
> - **Tests:** `.venv/bin/python -m pytest -q` (add `--maxfail=1` for focused runs).
>   **Compile:** `python -m py_compile src/micwebcontroller.py`.
> - New test modules install hardware stubs BEFORE importing `micwebcontroller`, mirroring
>   `tests/test_micwebcontroller.py`. The **gpiozero stub MUST provide `Device`** (plus `PWMLED`,
>   `LED`, `DigitalOutputDevice`) because `micwebcontroller` does `from gpiozero import Device`.
> - **No `SERVO_SIM`** — this fix touches no servo/movement code, so no collision/limit simulation
>   is involved. Keep in-code GPIO safety intact (writes through gpiozero devices in `talk`; pins
>   released in `_release_gpio`).
> - **Git policy for execution agents:** do NOT commit, do NOT create or switch branches, leave all
>   changes in the working tree. Commit only if the user explicitly asks.

- [x] 1. Write thread-faithful bug-condition exploration test (MUST FAIL on unfixed code)
  - **Property 1: Bug Condition** — Style change never crashes or stalls the live callback
  - **CRITICAL**: This test MUST FAIL on unfixed code — the failure confirms the deque race exists.
    **DO NOT attempt to fix the test or the code when it fails.**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes
    after implementation (re-run unchanged in Task 3.6).
  - **GOAL**: Surface `RuntimeError: deque mutated during iteration` (and the torn-config read) on
    the UNFIXED module, confirming the root cause. A single-threaded test cannot surface this race.
  - Create a new test file (e.g. `tests/test_voice_fx_style_change.py`). Install the hardware stubs
    BEFORE importing `micwebcontroller`, mirroring `tests/test_micwebcontroller.py`:
    - `pyaudio` stub (`paInt16`, `paContinue`, `PyAudio`), and a `gpiozero` stub that provides
      `Device` **as well as** `PWMLED` / `LED` / `DigitalOutputDevice` (import requires `Device`).
  - **Primary variant — deque race (the FAIL signal):**
    - Enable echo: `effects_config['echo']['enabled'] = True` with a non-zero amount; keep the rest
      of the chain simple so `_fx_echo`'s iterate+append loop is tight.
    - **CONSUMER thread**: call `micwebcontroller.apply_effects(frame)` in a tight bounded loop with
      a fixed `int16` frame of length `CHUNK`; capture any exception into a shared slot.
    - **PRODUCER thread**: repeatedly call `micwebcontroller._reset_audio_dsp_state()` (the
      style-change reset that rebinds `echo_buffer`) in a tight bounded loop.
    - Run both concurrently with an **iteration cap + join timeout** (a few thousand iterations per
      thread) so it reproduces reliably yet stays fast/deterministic and never hangs.
    - **Assertion**: no `RuntimeError` escapes the consumer AND it keeps returning `int16[CHUNK]`.
  - **Second variant — torn-config read (softer check):** producer alternates
    `_apply_chain_to_live` between two presets; consumer asserts it only ever observes a COMPLETE
    preset chain. Document that on unfixed in-place edits a torn mix can appear; the deque
    `RuntimeError` remains the primary FAIL signal.
  - Document the counterexample as comments (e.g. "`_fx_echo` iterating `echo_buffer` while
    `_reset_audio_dsp_state()` rebinds it → `RuntimeError: deque mutated during iteration`"), and
    note that single-threaded tests cannot surface this race.
  - Run on UNFIXED code. **EXPECTED OUTCOME**: Test FAILS (confirms the bug). Mark complete when the
    test is written, run, and the failure is documented.
  - _Requirements: 1.1, 1.2, 1.3, 1.5 (asserts Expected Behavior 2.1, 2.2, 2.3)_

- [x] 2. Write preservation tests BEFORE the fix (observation-first; MUST PASS on unfixed code)
  - **Property 2: Preservation** — Non-racing behaviour is unchanged
  - **IMPORTANT**: Follow observation-first methodology — observe behavior on UNFIXED code, then
    assert it. All sub-tests MUST PASS on unfixed code (this is the baseline to preserve).
  - **EXPECTED OUTCOME (whole task)**: all preservation tests PASS on unfixed code.

  - [x] 2.1 DSP output equality (single-threaded + PBT)
    - With a fixed, fully-applied chain and a clean DSP state (call `_reset_audio_dsp_state()` first
      for a clean slate), feed a fixed input sequence through `apply_effects` and record the exact
      output bytes; assert equality to the recorded baseline.
    - Property-based variant (hypothesis ≥100 examples): random valid chain + random `int16` input →
      output is `int16` of length `CHUNK` and deterministic for a fixed seeded state.
    - _Requirements: 3.1_

  - [x] 2.2 Endpoint behavior (Flask test-client + temp `ConfigStore`)
    - Reuse the fixture pattern from `tests/test_micwebcontroller.py` (hardware stubs before import;
      per-test temp `ConfigStore`).
    - Assert `/effects` (style + single-effect), `/effects/save`, `/effects/revert`, and `/config`
      return the same statuses/payloads and persist the same `ConfigStore` state.
    - PBT over valid effect names/amounts and valid style names (≥100 examples); invalid-body
      rejection without mutation.
    - _Requirements: 3.2_

  - [x] 2.3 Lifecycle unaffected
    - Assert `start_streaming` / `stop_streaming` / `_release_gpio` observable behavior is unchanged
      at the stub level, including the `is_streaming` short-circuit and the GPIO line-release.
    - _Requirements: 3.4_

  - [x] 2.4 `_reset_audio_dsp_state` intent preserved
    - Assert it still yields a fresh zero echo deque (len 3, `maxlen=3`), a fresh zero reverb buffer
      (len 9600) at `pos 0`, zeroed tremolo/ring phases, and a reset jaw envelope.
    - _Requirements: 2.4_

  - Run all of Task 2 on UNFIXED code; mark complete when written, run, and passing.
  - _Requirements: 2.4, 3.1, 3.2, 3.4_

- [x] 3. Fix the voice-FX style-change stream crash (synchronization only — DSP math untouched)

  - [x] 3.1 Add the module-level DSP lock
    - Define `_dsp_lock = threading.Lock()` near the other module globals.
    - Add a docstring stating it guards all shared DSP state — `audio_state` echo/reverb entries,
      `effects_config`, and `effect_state` — across the callback thread and the request thread.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: isBugCondition(input) from design (no lock serialises the two threads)_
    - _Expected_Behavior: 2.3 — serialise shared DSP access_
    - _Requirements: 2.3_

  - [x] 3.2 Guard the callback's DSP work
    - In `audio_callback`, keep the `is_streaming` short-circuit FIRST (unchanged — preserves 3.4),
      then hold `_dsp_lock` around `talk()` + `apply_effects()` (the state-reading work).
    - Keep the critical section short (pure CPU DSP, no I/O). The `is_recording` append of
      already-computed bytes may stay OUTSIDE the lock (it touches only `record_frames`).
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: callback reads shared DSP state concurrently with request-thread mutation_
    - _Expected_Behavior: 2.1, 2.3 — stream keeps producing audio; access serialised_
    - _Requirements: 2.1, 2.3_

  - [x] 3.3 Make `effects_config` writes whole-chain atomic rebinds under the lock
    - Rewrite `_apply_chain_to_live` to build a complete new `effects_config` dict and rebind the
      module global in one assignment under `_dsp_lock` (whole-chain swap; replaces the per-key
      in-place loop).
    - Make the `/effects` single-effect branch (toggle `enabled` / set `amount`) also rebuild and
      rebind under the lock, so even a single-field change is atomic from the callback's view.
      (Rule: all `effects_config` writes go through a rebind under the lock.)
    - Keep the style branch order: `_apply_chain_to_live(...)` then `_reset_audio_dsp_state()`;
      `current_style` / logging unchanged. Closes the torn-read window (1.5 / 2.2).
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: torn effects_config read (in-place per-key edits mid-chain)_
    - _Expected_Behavior: 2.2 — no torn config read; whole chain swapped atomically_
    - _Requirements: 1.5, 2.2_

  - [x] 3.4 Make `_reset_audio_dsp_state` thread-safe (build-new-then-atomic-rebind)
    - Build the fresh echo `deque` and fresh reverb buffer FIRST, then take `_dsp_lock` to perform
      the rebinds + tremolo/ring phase zeroing + jaw-envelope reset.
    - Preserve the intent exactly (fresh zero echo deque, fresh zero reverb buffer at `pos 0`, zeroed
      phases, reset envelope) — only the write becomes serialised. Guard any other request-thread DSP
      mutation with the same lock.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Bug_Condition: deque rebind races callback iteration of echo_buffer_
    - _Expected_Behavior: 2.1, 2.4 — stale energy/phase still cleared, race-free_
    - _Requirements: 2.1, 2.4_

  - [x] 3.5 (Secondary / belt-and-suspenders) Wrap locked DSP in try/except passthrough
    - Wrap the locked `talk()` + `apply_effects()` in a `try/except Exception` that prints (project
      convention — no `logging`) and returns passthrough `(in_data, pyaudio.paContinue)` so a FUTURE
      unknown error can't silently kill the stream.
    - **Explicitly secondary**: this must NOT mask the race — the lock is the real fix. The design
      recommends including it; implement it unless the reviewer specified lock-only.
    - Verify: `python -m py_compile src/micwebcontroller.py`.
    - _Expected_Behavior: 2.1 — stream never silently dies (defense-in-depth)_
    - _Requirements: 2.1_

  - [x] 3.6 Verify bug-condition exploration test now PASSES
    - **Property 1: Expected Behavior** — Style change never crashes or stalls the live callback
    - **IMPORTANT**: Re-run the SAME test from Task 1 — do NOT write a new test.
    - Run with `.venv/bin/python -m pytest tests/test_voice_fx_style_change.py -q`.
    - **EXPECTED OUTCOME**: Test PASSES — the lock serialises the threads; no `RuntimeError`;
      continuous `int16[CHUNK]` output; only complete chains observed.
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.7 Verify preservation tests still PASS
    - **Property 2: Preservation** — Non-racing behaviour is unchanged
    - **IMPORTANT**: Re-run the SAME tests from Task 2 — do NOT write new tests.
    - **EXPECTED OUTCOME**: all PASS (no regressions in DSP output, endpoints/`ConfigStore`,
      lifecycle, or `_reset_audio_dsp_state` intent).
    - _Requirements: 2.4, 3.1, 3.2, 3.4_

- [x] 4. Checkpoint — ensure all tests pass
  - `python -m py_compile src/micwebcontroller.py`.
  - `.venv/bin/python -m pytest tests/test_voice_fx_style_change.py -q`.
  - `.venv/bin/python -m pytest tests/test_micwebcontroller.py -q` (confirm existing endpoint tests
    are unaffected).
  - Confirm both correctness properties hold (Property 1 fix checking; Property 2 preservation).
  - **No `SERVO_SIM`.** Leave changes in the working tree (no commit / no branch). Ask the user if
    questions arise.
