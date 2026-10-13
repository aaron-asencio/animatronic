# Voice-FX Change → Live-Mic Silence — Bugfix Design

## Overview

The live-mic path in `src/micwebcontroller.py` goes **permanently silent** the moment an
operator changes the voice FX — selecting a style preset or toggling/sliding a single effect —
either just **before** or **during** an active duplex mic stream. There is **no crash**: the
PortAudio callback keeps firing but the duplex output emits silence for the rest of the process
life, and re-issuing FX changes does not recover it. Only restarting the `micwebcontroller.py`
process restores audio, which proves the broken state lives **in process memory** — not in
persisted config and not in a permanently latched ALSA/hardware device.

This bug is **distinct** from the two related specs and must not be conflated with either:

- **`voice-fx-style-change-stream-crash` (landed).** A real thread race (`_fx_echo` iterating
  `echo_buffer` while `_reset_audio_dsp_state` rebound it) that **crashed** the callback with
  `RuntimeError: deque mutated during iteration`. The landed fix introduced the module-level
  `_dsp_lock` (callback holds it around `talk()`, then `apply_effects()` acquires it itself; all
  `effects_config` writes are whole-chain atomic rebinds; `_reset_audio_dsp_state` is
  build-new-then-atomic-rebind) plus a secondary `try/except` passthrough in the callback. **That
  bug crashed; this one does not — the mic simply goes quiet.**
- **`voice-fx-audio-dropout-fix`.** A mic→Routine GPIO ownership leak (the *later* routine audio
  dies after a mic session). That is the opposite direction. This bug breaks the **live mic
  itself**, mid-session; routine audio (`animatronic.py` `AudioPlayer`) is unaffected.

The strong suspect — grounded in the landed code — is that the stream-crash fix **traded a crash
for a silent stall**. The real-time PortAudio callback now acquires `_dsp_lock` **twice per
frame** (once around `talk()`, once inside `apply_effects()`), and `talk()` itself runs a
per-frame `print(...)` **inside** that locked region. An FX change on the Flask request thread
takes `_dsp_lock` (two separate acquisitions, via `_apply_chain_to_live` then
`_reset_audio_dsp_state`), **allocates** a fresh 9600-float32 reverb buffer and a 3×CHUNK deque,
and does a large `print(f"... {effects_config}")`. On the Pi's shared **duplex** ALSA device,
blocking (or merely delaying) the real-time callback past its CHUNK deadline (1024/48000 ≈ 21 ms)
underruns the output ring; a duplex stream can wedge so it keeps firing the callback but emits
silence until the stream is reopened (process restart). The secondary `try/except` can also
**mask** a recurring per-frame exception into permanent silence.

Because reproduction depends on the real PyAudio + shared duplex ALSA device on the Pi,
**hardware confirmation of the mechanism (H1 vs H2 vs H3) is expected during this design phase.**
The fix must (a) keep the request-thread FX mutation from ever blocking the real-time callback
past its CHUNK deadline, (b) stop the per-frame work-under-lock in the hot path, and (c) make a
recurring callback exception visible instead of silently swallowed — all **without** reopening the
`voice-fx-style-change-stream-crash` race and without touching any servo/movement code.

## Glossary

- **Bug_Condition (C)** — a voice-FX change (style preset OR single-effect toggle/slide) applied
  on the Flask request thread while a live **duplex** mic stream is active (`DURING_STREAM`) or is
  started immediately after the change (`BEFORE_STREAM`). Formalised in
  [Bug Condition](#bug-condition).
- **Property (P)** — for buggy inputs, the live passthrough stays **audible** with the new chain
  applied, with at most a sub-perceptible single-CHUNK transient; **no** permanent silence, **no**
  stream wedge/underrun, **no** required service restart, and **no** silently-swallowed recurring
  callback exception.
- **Preservation** — for every non-buggy input, the fixed module behaves identically to the
  original: routine audio, the `/effects`/`/effects/save`/`/effects/revert`/`/config` payloads and
  `ConfigStore` persistence, the stream start/stop lifecycle, `_release_gpio`/GPIO behaviour, the
  `apply_effects` DSP math and `int16[CHUNK]` output, and the landed `_dsp_lock` serialization that
  keeps the stream-crash fix intact.
- **Callback thread** — PortAudio's own real-time thread, which invokes `audio_callback` once per
  CHUNK (1024 frames ≈ 21 ms at RATE=48000). It must return the next output buffer before that
  deadline or the duplex device underruns.
- **Request thread** — a Flask worker thread (`threaded=True`) handling `POST /effects`
  (style or single-effect), which mutates the shared DSP state.
- **`_dsp_lock`** — the module-level `threading.Lock` added by the stream-crash fix, held by the
  callback (around `talk()`, then again inside `apply_effects()`) and by the request-thread
  mutators (`_apply_chain_to_live`, `_reset_audio_dsp_state`, the single-effect rebind).
- **Underrun / wedge** — the callback misses its CHUNK deadline so the duplex output ring starves;
  on a shared ALSA duplex device the stream can keep firing the callback yet emit only silence
  until the stream is reopened.
- **`talk()`** — the per-frame jaw-envelope + GPIO driver; currently runs a per-frame
  `print(f"RMS: ...")` **inside** the `_dsp_lock` critical section held by the callback.
- **CHUNK deadline** — the real-time budget for one callback invocation: 1024 frames / 48000 Hz ≈
  **21.3 ms**. Any callback-side work (including blocking on `_dsp_lock` or I/O under it) must stay
  well inside this.

## Bug Details

### Bug Condition

The bug manifests when a voice-FX change is applied on the request thread around a live duplex
mic stream. The failure is one (or a combination) of: the request-thread mutation **blocking the
real-time callback** on `_dsp_lock` (and/or the per-frame `print` under the lock) past the CHUNK
deadline so the duplex stream underruns and wedges silent (H1); a recurring per-frame exception in
`apply_effects` being **masked** by the secondary `try/except` into permanent passthrough/silence
(H2); or the `(in_data, paContinue)` passthrough itself being silent in the wedged state (H3).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT:  X of type FxChangeEvent
    // X.kind               : one of {STYLE, SINGLE_EFFECT}
    // X.timing             : one of {BEFORE_STREAM, DURING_STREAM}
    // X.streamIsDuplexLive : boolean   // a live duplex mic stream is / becomes active
  OUTPUT: boolean

  RETURN (X.kind = STYLE OR X.kind = SINGLE_EFFECT)
         AND (X.timing = DURING_STREAM OR X.timing = BEFORE_STREAM)
         AND X.streamIsDuplexLive
END FUNCTION
```

The observable modelled is whether the live passthrough stays audible afterward **without** a
process restart. FX changes with no live mic stream, and routine-only audio, are NOT bug
conditions.

### Expected (correct) behavior for buggy inputs

```
FUNCTION expectedBehavior(result)
  INPUT:  result -- the live-stream outcome after the FX change under F'
  OUTPUT: boolean

  RETURN livePassthroughAudibleAfter(result) = TRUE
     AND newEffectChainApplied(result)       = TRUE
     AND permanentSilence(result)            = FALSE
     AND serviceRestartRequired(result)      = FALSE
     AND streamWedged(result)                = FALSE   // no duplex underrun/wedge  (H1)
     AND recurringCallbackExceptionSwallowed(result) = FALSE   // not masked        (H2)
END FUNCTION
```

### Examples

- **Mid-session style change (primary).** Mic streaming the `demon` chain; operator selects
  `ghost`. The request thread takes `_dsp_lock` for the whole-chain rebind, releases it, takes it
  again for `_reset_audio_dsp_state` (allocating a 9600-float reverb buffer + a 3×CHUNK deque),
  then `print`s the whole config dict. Expected: the next frames apply the `ghost` chain and audio
  keeps flowing. Actual: the live passthrough goes silent and stays silent until the service is
  restarted.
- **Single-effect toggle mid-session.** Operator slides `reverb.amount` while streaming. Expected:
  reverb depth changes, audio continues. Actual: permanent silence.
- **FX change just before start.** Operator picks `possessed`, then starts the stream. Expected:
  stream starts producing the `possessed` chain. Actual: no live mic audio from the first frame.
- **Repeated recovery attempts.** After the mic goes silent, the operator re-issues several FX
  changes. Expected (post-fix): any single change already keeps audio flowing. Actual (pre-fix):
  none of them recover it; only a process restart does.
- **Edge — `natural` style with `pitch.amount = 1.3` saved override (H2 probe).** `_fx_pitch` does
  `n_src = int(len/amount)`; a pathological amount could make `apply_effects` raise every frame,
  which the secondary `try/except` would swallow into permanent passthrough. The design must make
  such a **recurring** error visible rather than silent.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors (must continue to work exactly as before):**
- The landed `voice-fx-style-change-stream-crash` fix stays intact: no "deque mutated during
  iteration" crash, the `_dsp_lock` serialization and whole-chain atomic rebinds are preserved, and
  the race is **not** reintroduced (bugfix 3.1).
- `apply_effects` applies the **same DSP math and signal-chain order** and still outputs `int16`
  samples of length `CHUNK` (bugfix 3.2).
- `/effects`, `/effects/save`, `/effects/revert`, and `/config` return the same payloads and
  persist tuning through `ConfigStore` unchanged (bugfix 3.3).
- The stream lifecycle (`start_streaming` / `stop_streaming`), the `is_streaming` callback
  short-circuit, `_release_gpio`/GPIO behaviour, and the mic→routine GPIO hand-off
  (`voice-fx-audio-dropout-fix`) are undisturbed (bugfix 3.4).
- Routine audio through the `AudioPlayer` path behaves exactly as before (bugfix 3.5).
- Servo/movement behaviour is untouched — no `SERVO_SIM` concerns (bugfix 3.6).

**Scope:** Any input that does NOT involve an FX change around a live duplex mic stream
(`NOT isBugCondition`) must be completely unaffected. This includes routine-only audio, FX changes
issued when no stream is live, the config/profile endpoints, and the stream start/stop paths.

> Note: the actual expected *correct* behavior for buggy inputs is defined in
> [Correctness Properties](#correctness-properties) (Property 1). This section captures what must
> NOT change.

## Hypothesized Root Cause

The landed stream-crash fix is strongly suspected to have **traded a crash for a silent stall**.
Confirmed by code inspection of the current `src/micwebcontroller.py`:

1. **H1 — Lock-induced ALSA underrun (primary suspect).**
   - The callback acquires `_dsp_lock` **twice per frame**: once in a `with _dsp_lock:` around
     `talk()`, then again inside `apply_effects()` (which `with _dsp_lock:` the whole chain).
   - `talk()` runs a **per-frame `print(f"RMS: {level:.0f}; avg: ...; Jaw: ...; below: ...")`
     while still holding `_dsp_lock`** (the callback's first critical section). `print` to a
     possibly-redirected/piped stdout can block for an unbounded time; doing it under the lock on
     the real-time thread is a latency hazard independent of contention.
   - A style change on the request thread takes `_dsp_lock` for `_apply_chain_to_live`, releases
     it, then takes it again for `_reset_audio_dsp_state` — and does a large
     `print(f"... {effects_config}")`. The buffer allocation (9600 float32 + a 3×CHUNK deque) is
     built off-lock, but the two acquisitions plus the request-thread work still contend with the
     callback's two per-frame acquisitions.
   - On the shared **duplex** ALSA device, if the callback blocks on the lock (or on its own
     under-lock `print`) past the CHUNK deadline (≈21 ms), the output ring underruns; a duplex
     stream can wedge so it keeps firing the callback but emits silence for the rest of the
     session. The prior design's assumption — "pure CPU DSP, no I/O, stays well inside one CHUNK
     period" — is **false** because of the per-frame `print` under the lock (I/O in the critical
     section).

2. **H2 — Masked recurring per-frame exception.**
   - The callback's secondary `try/except Exception` returns `(in_data, paContinue)` and only
     `print`s on error. If an FX change puts `apply_effects` into a state where it raises **every**
     frame (a shape/length edge, or `_fx_pitch` with a pathological `amount` — the saved `natural`
     style can carry `pitch.amount = 1.3`), the stream never crashes; it silently passes through
     (or emits silence) every frame forever until restart. The `try/except` can therefore **mask**
     the real post-change failure into permanent silence.

3. **H3 — Passthrough still silent.**
   - Even the `(in_data, paContinue)` passthrough can be silent if the FX change leaves the duplex
     device/output ring in an underrun state where the callback's output is dropped — i.e. once
     wedged (H1), returning the input bytes does not restore audible output.

**Confirmation plan (hardware, during design):** reproduce on the Pi with the real duplex device,
then disambiguate H1/H2/H3 by (a) removing the per-frame `print` from the locked region and
confirming recovery (points at H1), (b) instrumenting the `try/except` to count consecutive
exceptions and surface them (points at H2), and (c) checking whether passthrough audio returns
after a change without reopening the stream (distinguishes H3). The chosen fix below is robust to
all three and does not depend on which dominates.

## Correctness Properties

Property 1: Bug Condition — FX change keeps live mic audible

_For any_ input where the bug condition holds (`isBugCondition` returns true) — a style or
single-effect change applied before or during a live duplex mic stream — the fixed code SHALL keep
the live passthrough **audible** with the new effect chain applied (at most a sub-perceptible,
single-CHUNK transient), with **no** permanent silence, **no** duplex underrun/wedge, **no**
required micweb-service restart, and **no** recurring callback exception silently swallowed.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation — Non-FX-change / non-live-stream behavior unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns false), the fixed
code SHALL produce the same result as the original — preserving routine `AudioPlayer` audio; the
`/effects`/`/effects/save`/`/effects/revert`/`/config` payloads and `ConfigStore` persistence; the
`apply_effects` DSP math/order and `int16[CHUNK]` output; the stream start/stop lifecycle,
`is_streaming` short-circuit, `_release_gpio`/GPIO behaviour and the mic→routine hand-off; the
landed `_dsp_lock` serialization that keeps the stream-crash fix intact; and all servo/movement
behaviour.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Design intent

Keep the stream-crash fix's **correctness** (the shared-DSP races stay closed) while removing every
way the request-thread FX change can block, delay, or silently stall the real-time callback. Three
coordinated changes, all confined to `src/micwebcontroller.py`:

1. **Remove all I/O from the callback's locked region (H1).** No `print` (or any blocking call) may
   run while the callback holds `_dsp_lock`.
2. **Keep the request-thread critical sections tiny and off-lock for everything expensive (H1).**
   Buffer allocation and all logging happen **outside** `_dsp_lock`; the lock covers only the
   atomic rebinds.
3. **Stop masking a *recurring* callback exception (H2/H3).** The secondary `try/except` must still
   prevent a one-off unforeseen error from being fatal, but a per-frame error that **repeats** must
   be surfaced (rate-limited log) rather than swallowed into permanent silence.

### Changes Required

**File:** `src/micwebcontroller.py` (only).

**1. `talk()` — remove the per-frame `print` from the locked hot path (H1).**
`talk()` runs inside the callback's `with _dsp_lock:` block and currently does a per-frame
`print(f"RMS: ...; Jaw: ...")`. Options, in order of preference:
   - **Preferred:** delete the per-frame diagnostic `print` entirely (it fires every ≈21 ms and is
     pure noise during a performance), OR
   - gate it behind an off-by-default debug flag and, if ever emitted, build the string and emit it
     **after** releasing the lock (never do stdout I/O under `_dsp_lock`).
   The jaw-envelope math, GPIO drive, and `is_streaming` short-circuit are unchanged — only the
   under-lock I/O is removed. This directly removes the per-frame blocking call from the real-time
   critical section.

**2. Minimise the callback's time under `_dsp_lock` (H1).**
   The callback acquires `_dsp_lock` twice per frame (around `talk()`, then inside
   `apply_effects()`). With the per-frame `print` gone from `talk()`, both critical sections become
   pure CPU DSP with no I/O. Keep the two-acquire structure (it is required because
   `threading.Lock` is not reentrant and `apply_effects` acquires the lock itself), but ensure
   **nothing** blocking is added inside either. This keeps each callback well inside the ≈21 ms
   CHUNK deadline even while a request-thread mutation is in flight.

**3. Request-thread mutators — allocate and log OFF-lock; hold `_dsp_lock` only for the rebind
   (H1).**
   - `_reset_audio_dsp_state()` already builds the fresh deque/reverb buffer before taking the
     lock — keep that. Confirm the locked region is only the rebinds + phase/envelope zeroing (no
     I/O, no allocation).
   - `_apply_chain_to_live(chain)` already builds the new dict off-lock and rebinds under the lock
     — keep that.
   - **Move the large `print(f"... {effects_config}")` in the `/effects` style and single-effect
     branches to run only AFTER all lock-held work completes** (it already runs after the mutators
     return, but verify no logging happens between the two acquisitions). Prefer logging a compact
     summary (style name + tuned/factory) rather than dumping the whole config dict, to keep the
     request handler from doing heavy stdout work that competes with the callback on a loaded Pi.

**4. Surface a *recurring* callback exception instead of masking it (H2/H3).**
   Replace the unconditional "swallow + passthrough on every exception" with a guarded version that
   still protects against a one-off error but makes a repeating one visible:
   - Keep the `try/except Exception` so a single unforeseen error returns
     `(in_data, pyaudio.paContinue)` and never propagates (PortAudio-fatal). The stream-crash fix's
     "never silently die" posture is preserved.
   - Add a small module-level **consecutive-error counter** (reset to 0 on any successful frame).
     On each caught exception increment it, and emit a **rate-limited** diagnostic (e.g. the first
     occurrence, then every Nth, with the exception detail) so an operator/log sees that FX
     processing is failing every frame — turning H2's "silent forever" into a visible, diagnosable
     condition. The counter/print live **outside** `_dsp_lock` (the `except` runs after the lock is
     released). This changes only error-path observability; the happy path and the lock semantics
     are untouched.

**5. Keep everything else byte-identical.** No change to any `_fx_*` math, `apply_effects`
   ordering/clipping/`int16[CHUNK]` output, `start_streaming`/`stop_streaming`/`_release_gpio`
   logic, the GPIO line-release fix, the mic→routine hand-off, the `/config`/`/effects/save`/
   `/effects/revert` persistence, or the in-code GPIO safety. No servo/movement code is touched
   (no `SERVO_SIM`).

### Why this satisfies the properties

- **Property 1 (fix):** removing under-lock I/O (change 1) and keeping both callback critical
  sections pure CPU (change 2), combined with tiny off-lock request-thread work (change 3), means a
  request-thread FX change can no longer delay the callback past the CHUNK deadline → no duplex
  underrun/wedge (H1). Surfacing a recurring exception (change 4) removes H2's silent-forever mask.
  Together they keep the live passthrough audible with the new chain applied.
- **Property 2 (preservation):** the DSP math, endpoint payloads/persistence, lifecycle, GPIO, and
  the landed `_dsp_lock` serialization are unchanged. The only removed behaviour is per-frame
  diagnostic noise and the swallowing of a *recurring* error — neither is observable correct
  behaviour that any non-buggy input depends on.

## Testing Strategy

### Validation Approach

Two phases: first **surface the stall/silence** on the current code (ideally on the Pi with the
real duplex device, plus a thread-faithful in-process harness for CI), confirming H1/H2/H3; then
verify the fix keeps the live passthrough audible across FX changes while preserving DSP output,
endpoint behaviour, and the landed stream-crash fix. Because the fault involves a real-time
deadline and a shared duplex ALSA device, **hardware confirmation of the mechanism is expected**;
the automated tests target the mechanisms the fix changes (under-lock I/O, lock-hold time, recurring
exception masking) in a device-independent way.

### Exploratory Bug Condition Checking

**Goal:** surface counterexamples that demonstrate the silent stall BEFORE implementing the fix, and
confirm or refute H1/H2/H3. If refuted, re-hypothesize.

**Test Plan:** combine on-hardware observation with a thread-faithful in-process harness that
imports `micwebcontroller` with the existing gpiozero/pyaudio stubs (see
`tests/test_micwebcontroller.py`).

**Test Cases:**
1. **Under-lock I/O latency (H1).** Instrument/measure the time the callback holds `_dsp_lock`
   while `talk()` runs its per-frame `print`, with stdout redirected to a slow/piped sink. Show the
   locked region can exceed the ≈21 ms CHUNK deadline on the UNFIXED code (will exceed on unfixed).
2. **Request-vs-callback contention (H1).** Run a consumer loop calling `apply_effects`/`talk`
   under the real lock while a producer loop issues style changes (`_apply_chain_to_live` +
   `_reset_audio_dsp_state`); measure max callback stall and show it crosses the deadline on
   unfixed code (will exceed on unfixed).
3. **Masked recurring exception (H2).** Force `apply_effects` to raise every frame (e.g. a style
   with a pathological `_fx_pitch` amount); on UNFIXED code assert the error is swallowed with no
   visible, rate-limited signal (demonstrates the mask).
4. **Hardware wedge (H1/H3).** On the Pi, change FX during a live duplex stream and confirm the
   passthrough goes permanently silent until process restart; confirm `(in_data, paContinue)`
   passthrough does not restore audio once wedged (distinguishes H3).

**Expected Counterexamples:**
- Callback lock-hold time exceeds the CHUNK deadline due to under-lock `print` and/or contention.
- A recurring per-frame exception is swallowed with no visible signal.
- Possible causes: under-lock stdout I/O (H1), request-thread lock contention (H1), masked
  recurring exception (H2), silent passthrough in a wedged duplex stream (H3).

### Fix Checking

**Goal:** for all inputs where the bug condition holds, the fixed code produces the expected
behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT livePassthroughAudibleAfter(result) = TRUE
     AND newEffectChainApplied(result)       = TRUE
     AND permanentSilence(result)            = FALSE
     AND serviceRestartRequired(result)      = FALSE
     AND streamWedged(result)                = FALSE
     AND recurringCallbackExceptionSwallowed(result) = FALSE
END FOR
```

Automated proxies for the device-dependent assertions: the callback's `_dsp_lock` critical sections
contain **no** stdout/blocking I/O; measured lock-hold time under concurrent request-thread
mutation stays well under the CHUNK deadline; and a forced recurring exception produces a visible,
rate-limited signal (not silent).

### Preservation Checking

**Goal:** for all inputs where the bug condition does NOT hold, the fixed code produces the same
result as the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Testing Approach:** property-based testing (hypothesis, ≥100 examples) is well suited to the
universal preservation claims — DSP output equality across random valid chains and inputs, and
endpoint accept/reject across valid/invalid bodies — because it generates many cases and catches
edge cases a few unit tests would miss. Observe behaviour on the UNFIXED code first (recorded
baselines), then assert the fixed code matches.

**Test Cases:**
1. **DSP output equality.** With a fixed, fully-applied chain and a seeded DSP state, feed a fixed
   input sequence through `apply_effects` on unfixed code, record the exact output bytes, then
   assert the fixed code produces identical bytes (and still `int16` length `CHUNK`).
2. **Endpoint behaviour.** With the Flask test client + a temp `ConfigStore`, observe that
   `/effects` (style + single-effect), `/effects/save`, `/effects/revert`, and `/config` return the
   same statuses/payloads and persist the same state on unfixed code, then assert unchanged after
   the fix.
3. **Stream-crash fix intact (regression).** Re-run the `voice-fx-style-change-stream-crash`
   thread-faithful concurrency harness (consumer `apply_effects` loop vs producer
   `_reset_audio_dsp_state`/chain-swap loop) and assert it still PASSES — no "deque mutated during
   iteration", continuous `int16[CHUNK]` output — proving the race was not reopened.

### Unit Tests

- `talk()` performs no stdout I/O inside the `_dsp_lock`-held region (and, if a debug flag exists,
  any emission happens after the lock is released).
- The callback's `_dsp_lock` critical sections contain only CPU DSP (no `print`, no allocation, no
  blocking call).
- The recurring-exception path: on repeated forced errors the consecutive-error counter increments
  and a rate-limited diagnostic is emitted; a successful frame resets it; a one-off error is still
  swallowed into passthrough.
- `_reset_audio_dsp_state` still yields a fresh zero echo deque (len 3, maxlen 3), fresh zero reverb
  buffer (len 9600) at pos 0, zeroed phases, reset envelope — allocation off-lock, rebind under
  lock.

### Property-Based Tests

- Random valid effect chains + random `int16` input → `apply_effects` returns `int16[CHUNK]` and is
  deterministic for a fixed seeded state, byte-identical to the unfixed baseline (preservation 3.2).
- Random valid `/effects` and `/config` bodies → accepted and persisted identically; invalid bodies
  → rejected without mutation (preservation 3.3).
- Across many randomized FX-change sequences driven against a mocked callback loop, the measured
  callback lock-hold stays under the CHUNK deadline and no recurring exception is silently swallowed
  (fix, Property 1).

### Integration Tests

- **Full live-session flow (hardware).** On the Pi, start a duplex mic stream and apply a sequence
  of style and single-effect changes (before and during the stream); assert the passthrough stays
  audible with the new chain applied for every change and that **no** micweb-service restart is
  required (bugfix 2.1, 2.2, 2.5).
- **Context/order coverage.** Cover both orderings — FX change `BEFORE_STREAM` then start, and
  `DURING_STREAM` — and repeated changes within one session.
- **Routine audio unaffected.** Confirm a Routine playing through the `AudioPlayer` path behaves
  exactly as before, in isolation and after a mic session (bugfix 3.5, and no disturbance to the
  `voice-fx-audio-dropout-fix` hand-off, 3.4).

### Tooling, Stubs, and Safety

- **Compile check:** `python -m py_compile src/micwebcontroller.py` before running tests.
- **Run tests:** `.venv/bin/python -m pytest -q` (use `--maxfail=1` for focused runs).
- **Stubs:** import `micwebcontroller` with the gpiozero/pyaudio stubs from
  `tests/test_micwebcontroller.py`; any new test module installing its own gpiozero stub MUST set
  `Device`, `PWMLED`, `LED`, and `DigitalOutputDevice` so the import succeeds.
- **No `SERVO_SIM`:** this fix touches no servo/movement code; in-code audio safety and the landed
  stream-crash guards stay intact.
