# Bugfix Requirements Document

## Introduction

The live microphone audio path in `src/micwebcontroller.py` stops producing sound whenever a
Voice-FX **style** is changed — either just **before** starting the live mic stream, or **during**
an active live mic session. The operator observes the live mic going silent for the rest of the
session. Routine audio (the `animatronic.py` / `AudioPlayer` path) is unaffected.

The defect is a **thread-safety race** on the shared DSP state. `micwebcontroller.py` runs its
PyAudio stream with a non-blocking callback (`stream_callback=audio_callback`) that executes on
PortAudio's own thread, while Flask runs with `threaded=True`, so `/effects` requests run on a
separate request thread. There is **no lock** guarding the shared mutable DSP state. When a style
is selected, the `/effects` style handler calls `_reset_audio_dsp_state()` on the request thread,
which **rebinds** `audio_state['echo_buffer']` to a brand-new `deque` (and swaps the reverb buffer
and zeroes effect phase) at the same moment the callback thread is iterating that very deque inside
`_fx_echo` (`for i, old in enumerate(audio_state['echo_buffer'])`). Mutating/replacing a `deque`
while it is being iterated raises `RuntimeError: deque mutated during iteration` **inside the audio
callback**. An unhandled exception in a PortAudio callback stops the stream, so the live mic goes
silent.

This is a separate defect from the already-fixed GPIO line-release leak (`mic-gpio-line-release-leak`)
and from `voice-fx-audio-dropout-fix`. The fix will be confined to `src/micwebcontroller.py`
(the stream callback, the shared effects/DSP state, the `/effects` style handler, and
`_reset_audio_dsp_state`). `audio_player.py` and `performance.py` are not involved.

### Bug Condition (informal)

The bug condition `isBugCondition(X)` holds when a style/effects mutation on the Flask request
thread — specifically the style branch that calls `_reset_audio_dsp_state()`, replacing the shared
`echo_buffer` deque and reverb buffer and zeroing effect phase — runs **concurrently** with an
active PortAudio callback that is reading/iterating that same shared DSP state inside
`apply_effects` / `_fx_echo` / `_fx_reverb`.

```pascal
FUNCTION isBugCondition(X)
  INPUT:  X describing concurrent access to the shared DSP state
          (the live callback thread vs. the /effects style-change request thread)
  OUTPUT: boolean

  RETURN  X.callback_active                       // a PortAudio callback is in-flight,
          AND X.callback_reads_shared_dsp_state   //   iterating/reading echo_buffer / reverb /
                                                  //   effects_config / effect_state, AND
          X.request_mutates_dsp_state             // the /effects style path (request thread)
                                                  //   calls _reset_audio_dsp_state() /
                                                  //   _apply_chain_to_live(), swapping/mutating
                                                  //   that same shared state, with NO lock
                                                  //   serialising the two threads
END FUNCTION
```

```pascal
// Property: Fix Checking — style change never crashes the live callback
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)                 // F' = fixed micwebcontroller
  ASSERT no RuntimeError escapes the callback
         AND live stream keeps producing output
         AND no torn effects_config read
END FOR
```

```pascal
// Property: Preservation Checking — non-racing behaviour is unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)             // F = original, F' = fixed
END FOR
```

Here **F** is the current (unfixed) `micwebcontroller.py` and **F'** is the fixed version. A
reproduced counterexample is: driving `_fx_echo` / `apply_effects` in a loop on one thread while a
second thread calls `_reset_audio_dsp_state()` raises `RuntimeError: deque mutated during iteration`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a Voice-FX style is selected via `/effects` `{"style": ...}` while (or just before) the live mic stream is running THEN `_reset_audio_dsp_state()` on the Flask request thread swaps the shared `echo_buffer` deque concurrently with the PyAudio callback thread iterating it in `_fx_echo`, raising `RuntimeError: deque mutated during iteration` inside the callback.

1.2 WHEN that `RuntimeError` is raised inside the PortAudio callback THEN the live mic stream stops producing audio (goes silent) for the remainder of the session.

1.3 WHEN the style is changed either before starting the mic or during a live session THEN the failure occurs in both cases (the first callbacks after start race the just-reset state; a mid-session change swaps the buffer under the live callback).

1.4 WHEN a style is changed THEN Routine audio (`AudioPlayer`) is unaffected, because it does not use the shared effects engine, `effects_config`, or the shared echo/reverb buffers and plays single-threaded per track.

1.5 WHEN the callback thread and the `/effects` request thread access the shared DSP state (`echo_buffer`, `reverb_buffer`/`reverb_pos`, `effects_config`, `effect_state`) THEN they do so with no lock, so in addition to the deque fault, `effects_config` mutated in place mid-chain can yield a torn read (an effect toggled on while its parameters are half-updated). (The deque iteration-mutation is the stream-killing fault; the torn read is a secondary non-crash hazard noted here, not conflated with the crash.)

### Expected Behavior (Correct)

2.1 WHEN a style is changed before or during a live mic session THEN the system SHALL keep the live mic stream producing audio without interruption (no silent stream, no stopped callback).

2.2 WHEN a style is changed mid-stream THEN the system SHALL apply the new effect chain on subsequent frames without crashing the callback — no `RuntimeError: deque mutated during iteration` and no torn `effects_config` read.

2.3 WHEN the Flask request thread and the PyAudio callback thread access the shared DSP state (`echo_buffer`, `reverb_buffer`/`reverb_pos`, `effects_config`, `effect_state`) THEN the system SHALL serialise/snapshot that access (e.g. a lock and/or an atomic swap or per-callback snapshot) so neither a buffer swap nor a config update can corrupt an in-flight callback.

2.4 WHEN a style is changed THEN the system SHALL still clear stale echo/reverb energy and effect phase (the current `_reset_audio_dsp_state()` intent) WITHOUT racing the callback thread.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a fully-applied effect chain processes audio (no concurrent style change in flight) THEN the system SHALL CONTINUE TO produce the same DSP output as before — the fix is synchronization, not effect math.

3.2 WHEN the `/effects`, `/effects/save`, `/effects/revert`, and `/config` endpoints are used THEN the system SHALL CONTINUE TO behave as before, including their persisted `ConfigStore` state.

3.3 WHEN Routine audio runs (`audio_player.py` / `performance.py`) THEN the system SHALL CONTINUE TO behave exactly as before, since it is not involved in this fix.

3.4 WHEN the stream is started or stopped THEN the system SHALL CONTINUE TO behave as before, including the just-fixed GPIO line-release behavior (`mic-gpio-line-release-leak`).
