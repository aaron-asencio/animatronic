# Bugfix Requirements Document

> ## ✅ RESOLUTION (what actually fixed it) — read this first
>
> The body below (hypotheses H1/H2/H3, and the task plan) is the ORIGINAL
> analysis. It is kept for history, but it is **superseded** by the fix that
> actually landed. Where the body says to KEEP the `_dsp_lock` and only shrink
> the work under it, that is **no longer accurate** — the real fix REMOVED the
> lock from the real-time path entirely.
>
> **Root cause (confirmed on hardware via the PortAudio `Status: 6` log =
> `paOutputUnderflow | paInputOverflow` every frame):** the real-time audio
> callback acquired `_dsp_lock` every frame, and a voice-FX change on the Flask
> request thread took that same lock. Under the GIL the lock hand-off delayed
> the callback past the ~21 ms CHUNK deadline, the shared duplex ALSA output
> ring underran, and the stream wedged silent for the rest of the session.
> A **second, independent** underrun source was found: `_fx_reverb` ran a
> per-sample Python loop (~22 ms/frame — over the deadline by itself), so any
> reverb-enabled preset could wedge the stream too.
>
> **The fix (in `src/micwebcontroller.py`):**
> 1. **Made the callback lock-free.** `_dsp_lock` was REMOVED. `effects_config`
>    is published by a single atomic reference rebind; the callback reads it once
>    per frame with no lock (no torn read). The per-frame DSP state (echo deque,
>    reverb buffer, phases) is owned EXCLUSIVELY by the callback thread; a style
>    change bumps `_dsp_reset_generation` and the callback resets itself at the
>    top of a frame. The old deque-mutation race is thus closed structurally
>    (single owner), not by a lock.
> 2. **Vectorised `_fx_reverb`** (numpy gather/scatter, provably equivalent to
>    the loop because the 9600-sample delay buffer is longer than one 1024 block)
>    — ~22 ms → ~0.8 ms/frame, byte-identical output.
> 3. **Added duplex-wedge recovery** (`_note_stream_status` /
>    `_stream_needs_recovery`): a persistent output underflow makes the stream
>    thread reopen the stream to re-prime ALSA without dropping `is_streaming`.
> 4. Kept the rate-limited recurring-exception diagnostic (the H2 work) — that
>    part was correct and remains.
>
> **Invariant going forward:** there is intentionally **no lock** on the live
> audio path; `apply_effects` / the DSP buffers are single-owner (callback only).
> See the `live-audio-troubleshooting` steering doc. Any requirement/task text
> below that asserts a preserved `_dsp_lock` is historical, not current.

## Introduction

Changing the voice FX during live mic performance kills the live mic audio. When an operator selects a style preset, or toggles/slides a single effect, through the voice-FX dashboard (proxied from `webapp.py` on port 8000 to `micwebcontroller.py` on port 5000) either **before** or **during** a live mic Stream, the live mic passthrough goes silent and **stays** silent for the remainder of the process life. Re-issuing FX changes does not recover it. The only recovery is restarting the micweb service (killing and respawning the `micwebcontroller.py` process), which proves the broken state lives **in process memory**, not in persisted config and not in a permanently latched hardware/ALSA device.

Impact: this breaks Puppeteer / live-mic performance. The moment the operator reaches for the voice FX — the whole point of a live performance — the figure goes mute until someone restarts a service.

This bug is **distinct** from two existing specs and must not be conflated with either:

- **`voice-fx-style-change-stream-crash` (already fixed)** — a thread race where `_fx_echo` iterated `echo_buffer` while `_reset_audio_dsp_state` rebound it, raising `RuntimeError: deque mutated during iteration` and crashing the callback. That is fixed via a module-level `_dsp_lock` (the callback holds it around `talk()` and separately inside `apply_effects()`; `effects_config` writes are whole-chain atomic rebinds; `_reset_audio_dsp_state` is build-new-then-atomic-rebind) plus a secondary try/except passthrough in the callback. **That bug crashed; this one does not crash — the mic simply goes quiet.**
- **`voice-fx-audio-dropout-fix`** — a mic→Routine GPIO ownership leak (routine audio dead after a mic session). That is the opposite direction (mic breaks the later routine). This bug breaks the **live mic itself**, mid-session.

This bug: the **live mic passthrough** itself goes silent at the moment of an FX change, with **no crash**, recovered only by a process restart. Routine audio (the `animatronic.py` `AudioPlayer` path) is unaffected.

### Grounding in current code (`src/micwebcontroller.py`)

- The mic stream is opened **duplex and non-blocking (callback mode)**: `audio_state['audio'].open(..., input=True, output=True, frames_per_buffer=CHUNK=1024, stream_callback=audio_callback)`, then `stream.start_stream()`. PortAudio drives `audio_callback` on its own real-time thread.
- `audio_callback` first short-circuits `if not audio_state['is_streaming']: return (in_data, paContinue)`, then:

  ```
  try:
      with _dsp_lock:
          talk(audio, ...)
      processed = apply_effects(audio)   # apply_effects ACQUIRES _dsp_lock itself
  except Exception as e:
      print(...); return (in_data, paContinue)   # SECONDARY passthrough
  # then returns (processed.tobytes(), paContinue)
  ```

  So the callback takes `_dsp_lock` **twice per frame** (once for `talk()`, once inside `apply_effects()`).
- A **style change** (`POST /effects {"style": ...}`) runs on the Flask request thread: `_apply_chain_to_live(...)` (acquires `_dsp_lock` for a whole-dict rebind) → `_reset_audio_dsp_state()` (acquires `_dsp_lock` again; allocates a fresh 9600-float32 reverb buffer + a 3×CHUNK deque) → `print(f"... {effects_config}")` printing the whole chain.
- A **single-effect change** (`POST /effects {"effect": ...}`) similarly rebuilds the whole dict and rebinds under `_dsp_lock`.

### Hypotheses to confirm during design (do not over-commit to one)

- **H1 — lock-induced ALSA underrun.** The real-time callback needs `_dsp_lock` every frame (twice). An FX change on the request thread takes `_dsp_lock` (two separate acquisitions) + allocates buffers + prints the whole config dict. On the Pi's shared duplex ALSA device, blocking the callback past its CHUNK deadline underruns the output ring; a duplex stream can wedge so it keeps firing the callback but emits silence for the rest of the session — only reopening (process restart) recovers it. The prior design assumed "pure CPU DSP, never blocks the callback past one CHUNK period"; that assumption is the suspect.
- **H2 — masked per-frame exception.** The secondary try/except returns passthrough on **any** exception and only `print()`s. If an FX change makes `apply_effects` raise every frame (e.g. a shape/length edge case, or `_fx_pitch` with a pathological amount — note `_fx_pitch` does `n_src = int(len/amount)` and the saved "natural" style has `pitch.amount = 1.3`), the stream would not crash — it would silently pass through or go quiet every frame forever until restart. The try/except may be **masking** the real post-fix failure.
- **H3 — passthrough still silent.** Even the `(in_data, paContinue)` passthrough could be silent if the FX change leaves `is_streaming` or the device in a state where the callback's output is dropped.

Because reproduction depends on the real PyAudio + shared duplex ALSA device on the Pi, **hardware confirmation of the mechanism (H1 vs H2 vs H3) is expected during the design phase.** This change touches **no servo/movement code**, so there are no `SERVO_SIM` concerns; the in-code audio safety and the existing stream-crash guards must stay intact.

## Bug Analysis

### Current Behavior (Defect)

What currently happens when an FX change is applied around a live mic Stream.

1.1 WHEN the operator changes the voice FX (selecting a style preset OR toggling/sliding a single effect) WHILE a live mic Stream is active THEN the system silences the live mic passthrough.

1.2 WHEN the operator changes the voice FX BEFORE starting a live mic Stream AND then starts the stream THEN the system produces no live mic audio.

1.3 WHILE the live mic passthrough is silenced THEN the system KEEPS it silent for the rest of the process life — re-issuing further FX changes does NOT recover the audio.

1.4 WHEN the live mic passthrough has been silenced THEN the system still does NOT crash the callback or the micweb service — the stream keeps running but emits silence.

1.5 WHEN only the micweb service is restarted (the `micwebcontroller.py` process is killed and respawned) THEN the system restores live mic audio — confirming the broken state is in-process memory, NOT persisted config and NOT a permanently latched hardware/ALSA device.

1.6 WHILE the live mic passthrough is silenced THEN the system leaves Routine audio (the `animatronic.py` `AudioPlayer` path) unaffected — only the LIVE MIC path is broken.

### Expected Behavior (Correct)

What should happen instead, for the same FX-change conditions.

2.1 WHEN the operator changes the voice FX (style OR single effect) WHILE a live mic Stream is active THEN the system SHALL continue producing live mic passthrough audio with the new effect chain applied, with at most a brief (sub-perceptible / single-CHUNK) transient — never permanent silence.

2.2 WHEN the operator changes the voice FX BEFORE starting a live mic Stream AND then starts the stream THEN the system SHALL produce live mic passthrough audio with the selected effect chain applied.

2.3 WHEN an FX change is applied during a live mic Stream THEN the system SHALL NOT cause the duplex PortAudio/ALSA stream to underrun or wedge; the request-thread mutation SHALL NOT block the real-time callback past its buffer (CHUNK) deadline — e.g. minimize or eliminate work done while holding `_dsp_lock` on the request thread, pre-allocate buffers off-lock, and avoid heavy logging (printing the whole config dict) under the lock.

2.4 IF the DSP work raises an exception on a frame THEN the system SHALL NOT silently swallow a RECURRING callback exception indefinitely — a per-frame error that repeats SHALL be surfaced (logged/printed in a way an operator can see), and the secondary try/except SHALL NOT be allowed to mask a real failure into permanent silence.

2.5 WHEN the operator makes repeated FX changes (style and/or single-effect, before and during the stream) over the course of one session THEN the system SHALL keep the live mic passthrough audible for all of them with NO micweb service restart required.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved by the fix.

3.1 WHEN a style or single-effect change rebinds the live effect chain concurrently with the callback THEN the system SHALL CONTINUE TO be free of the "deque mutated during iteration" crash — the `_dsp_lock` serialization and the whole-chain atomic rebinds (the landed `voice-fx-style-change-stream-crash` fix) SHALL stay intact and the race SHALL NOT be reintroduced.

3.2 WHEN a frame is processed through the effect chain THEN the system SHALL CONTINUE TO apply the same `apply_effects` DSP math and signal-chain order, and SHALL CONTINUE TO output `int16` samples of length `CHUNK`.

3.3 WHEN a client calls `/effects`, `/effects/save`, `/effects/revert`, or `/config` THEN the system SHALL CONTINUE TO return the same endpoint behavior and payloads and SHALL CONTINUE TO persist tuning through the existing `ConfigStore` unchanged.

3.4 WHEN the stream is started or stopped THEN the system SHALL CONTINUE TO follow the existing stream lifecycle (`start_streaming` / `stop_streaming`), the `is_streaming` short-circuit in the callback, and `_release_gpio` / GPIO behavior — and SHALL CONTINUE TO preserve the mic→routine GPIO hand-off fix (`voice-fx-audio-dropout-fix`) without disturbance.

3.5 WHEN a Routine plays audio through the `AudioPlayer` path THEN the system SHALL CONTINUE TO behave exactly as before (this bug and its fix are audio-mic-path only).

3.6 WHEN any routine or gesture drives servos THEN the system SHALL CONTINUE TO use the existing servo safety path unchanged — this fix touches no servo/movement code and introduces no `SERVO_SIM` concerns.

## Bug Condition Methodology

### Bug Condition Function

Identifies the inputs that trigger the bug: an FX change (style or single-effect) applied while — or just before — a live duplex mic Stream is active. The observable output modeled is whether the live passthrough stays audible afterward **without** a process restart.

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type FxChangeEvent
    // X.kind        : one of {STYLE, SINGLE_EFFECT}
    // X.timing      : one of {BEFORE_STREAM, DURING_STREAM}
    // X.streamIsDuplexLive : boolean  // a live duplex mic Stream is (or becomes) active
  OUTPUT: boolean

  // The bug is triggered whenever a voice-FX change is applied around a live
  // duplex mic stream -- whether the stream is already running (DURING_STREAM)
  // or is started right after the change (BEFORE_STREAM). Routine-only audio
  // and FX changes with no live mic stream are NOT bug conditions.
  RETURN (X.kind = STYLE OR X.kind = SINGLE_EFFECT)
         AND (X.timing = DURING_STREAM OR X.timing = BEFORE_STREAM)
         AND X.streamIsDuplexLive
END FUNCTION
```

### Property Specification (Fix Checking)

Defines the correct behavior for every buggy input. `F` is the current (unfixed) code; `F'` is the fixed code.

```pascal
// Property: Fix Checking -- an FX change around a live mic stream must leave
// the live passthrough audible (new chain applied), with no permanent silence
// and no required service restart.
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT livePassthroughAudibleAfter(result) = TRUE
     AND newEffectChainApplied(result) = TRUE
     AND permanentSilence(result) = FALSE
     AND serviceRestartRequired(result) = FALSE
     AND streamWedged(result) = FALSE            // no duplex underrun/wedge (H1)
     AND recurringCallbackExceptionSwallowed(result) = FALSE   // not masked (H2)
END FOR
```

### Preservation Goal (Preservation Checking)

For every input that is NOT a bug condition, the fixed code must behave identically to the original.

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

This covers, among others: Routine audio through `AudioPlayer`; `/effects`, `/effects/save`, `/effects/revert`, `/config` endpoint payloads and `ConfigStore` persistence; the stream start/stop lifecycle and `is_streaming` short-circuit; `_release_gpio` / GPIO behavior and the mic→routine hand-off; the `apply_effects` DSP math/order and `int16[CHUNK]` output; the `_dsp_lock` serialization that keeps the `voice-fx-style-change-stream-crash` fix intact; and all servo/movement behavior.
