# Bugfix Requirements Document

## Introduction

Operators report that after using the live mic (Voice-FX / Stream) on the
dashboard, the next Routine launched from the control panel is dead: no Routine
audio plays and the Routine's jaw and eyes never move. The figure performs
normally again only after the `animatronic-micweb` service is restarted or the
Pi is rebooted. A Routine run from a freshly started service (with no prior mic
session) works fine; the failure appears **only after a live mic session**.

**Note on the previously-investigated (now refuted) cause.** An earlier pass
blamed `micwebcontroller.stop_streaming()` tearing down PyAudio without an
orderly `stop_stream()`/`close()`, plus leaked DSP/effect state, leaving the
shared ALSA output device degraded. That fix was already applied in the working
tree (stream-handle capture + orderly stream teardown in `stop_streaming()`, and
`_reset_audio_dsp_state()`), **and the bug still reproduces** — so that was not
the cause. The ALSA/JACK error burst seen in the logs is just PortAudio
enumerating devices during `pyaudio.PyAudio()` initialisation (normal Pi noise),
**not** the defect. The already-applied orderly PyAudio teardown and DSP reset
are correct and harmless, so they are **preserved** (see Regression Prevention),
but they do not fix this bug.

**Confirmed root cause: a cross-process GPIO ownership leak.** The Routine's
audio thread crashes *before any audio plays*, raising `lgpio.error: 'GPIO busy'`
from `AudioPlayer.__init__` when it constructs `LED(EYE_LIGHT_PIN)`
(`src/audio_player.py:75`), reached from `src/performance.py` `_play_chain()`
(`player = AudioPlayer(**options)`, ~line 331). The underlying failure is
`lgpio.gpio_claim_input` on GPIO6.

Grounding in the current implementation:

- `src/constants.py`: `EYE_LIGHT_PIN = 6` (GPIO6) and `MOUTH_MOTOR_PIN = 15`.
- The long-lived mic service process (`src/micwebcontroller.py`, port 5000)
  claims these pins inside `talk()` during a live mic stream: it lazily
  constructs the module-global `jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)`
  and `led_eye_light = LED(EYE_LIGHT_PIN)` on first use.
- Routines run in a **separate** `animatronic.py` subprocess. Its
  `AudioPlayer.__init__` constructs `LED(EYE_LIGHT_PIN)` and
  `DigitalOutputDevice(MOUTH_MOTOR_PIN)` up front — *before* opening any audio
  stream. If the mic process still holds GPIO6, this construction raises
  `lgpio 'GPIO busy'`, which kills the performance audio thread: no Routine
  audio, and the Routine jaw/eyes never move. This matches the exact symptoms.
- Reproduction/isolation: the Routine fails **only after a mic session**, and
  **both** a full reboot and simply restarting the `animatronic-micweb` service
  fix it. The leaked pins are therefore held by the mic **service process
  itself**; killing that process frees them. There is no kernel/hardware latch.

**Reloader hypothesis ruled out.** `deploy/animatronic-micweb.service` sets
`Environment=MICWEB_DEV=0`, and `src/micwebcontroller.py` `__main__` disables the
Werkzeug reloader when `MICWEB_DEV` is `0/false/no/off`
(`app.run(..., use_reloader=dev_reload)`). It is a **single** process — `talk()`
and `_release_gpio()` run in the same process — so the leak is not a forked
reloader child retaining the pins.

**Leading mechanism to confirm during design.** `start_streaming()` runs
`def _run_stream(): audio_state['stream'] = stream_mic()` on a worker thread. But
`stream_mic()` opens a **callback (non-blocking)** PyAudio stream, calls
`stream.start_stream()`, and **returns immediately**; the audio callback
(`audio_callback` → `talk()`) then fires on PortAudio's **own internal thread**,
not the worker thread. So `stop_streaming()`'s `thread.join()` returns almost at
once (the worker already finished), while the PortAudio callback may still be
running `talk()`, which lazily **re-creates** `jaw_motor` / `led_eye_light`.
`stop_streaming()` then stops/closes the stream and calls `_release_gpio()`
(`device.off()`/`close()`, nulls the globals). The race between the still-active
callback re-grabbing the pins via `talk()` and `_release_gpio()` closing them is
the likely reason GPIO6 ends up still-claimed after the stream stops — and why
only a service restart (process death) reliably frees it. This is the primary
hypothesis the design phase must confirm.

## Bug Analysis

### Current Behavior (Defect)

What currently happens when the bug is triggered.

1.1 WHEN a live mic session has run and been stopped, THEN the mic service process (`micwebcontroller.py`) continues to hold `EYE_LIGHT_PIN` (GPIO6) and `MOUTH_MOTOR_PIN` (GPIO15) rather than relinquishing them in-process.

1.2 WHEN a Routine subprocess (`animatronic.py`) is started after such a mic session, THEN `AudioPlayer.__init__` raises `lgpio.error: 'GPIO busy'` while constructing `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)`, before any audio stream is opened.

1.3 WHEN that `GPIO busy` error is raised in the performance audio thread (`performance.py` `_play_chain` → `AudioPlayer(**options)`), THEN the system produces no Routine audio AND the Routine jaw motor and eye LED never move (the whole audio-driven path dies at construction).

1.4 WHEN the operator attempts to recover without a restart (e.g. by re-launching the Routine), THEN the system CONTINUES to fail with the same `GPIO busy` error, because the mic process still owns the pins — only killing/restarting the mic service process or rebooting frees them.

### Expected Behavior (Correct)

What should happen instead.

2.1 WHEN a live mic session is stopped (`stop_streaming()` returns), THEN the system SHALL fully relinquish both `EYE_LIGHT_PIN` (GPIO6) and `MOUTH_MOTOR_PIN` (GPIO15) in-process, so neither pin is held by the mic service after the stream ends.

2.2 WHEN a Routine subprocess is started after a stopped mic session, THEN the system SHALL allow `AudioPlayer.__init__` to construct `LED(EYE_LIGHT_PIN)` and `DigitalOutputDevice(MOUTH_MOTOR_PIN)` without an `lgpio 'GPIO busy'` error.

2.3 WHEN that Routine runs after a mic session, THEN the system SHALL play the Routine audio audibly AND drive the Routine jaw motor and eye LED from the audio envelope, exactly as a first-run Routine does.

2.4 WHEN the operator repeatedly hands off between the live mic and Routines (mic → Routine → mic → Routine …), THEN the system SHALL succeed on every hand-off without any `animatronic-micweb` service restart or reboot.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved.

3.1 WHEN the live mic is actively streaming, THEN the system SHALL CONTINUE TO pass the mic through to the speaker and drive the jaw motor (GPIO15) and eye LED (GPIO6) from the mic envelope via `talk()` for the duration of the stream — the pins are legitimately owned by the mic process *while streaming*.

3.2 WHEN a Routine is run from a freshly started mic service with no prior mic session, THEN the system SHALL CONTINUE TO play its audio and drive the jaw/eyes (the known-good first-run behavior is unaffected).

3.3 WHEN `stop_streaming()` tears down the stream, THEN the system SHALL CONTINUE TO stop/close the live PyAudio stream in order before `terminate()` (the already-applied orderly teardown) and SHALL CONTINUE TO call `_reset_audio_dsp_state()` — these are correct and preserved, even though they are not the fix.

3.4 WHEN a voice-style preset is selected, saved, or reverted, THEN the system SHALL CONTINUE TO load/persist the correct effect chain (saved tuned override else factory preset) and apply it to the live stream, unchanged.

3.5 WHEN a Routine or Mode is requested while the mic is streaming, THEN the system SHALL CONTINUE TO auto-stop/preempt the mic so the Routine/Mode can own the jaw/audio path, exactly as today (the Stream and a Routine never co-own the path).

3.6 WHEN a servo movement runs as part of a Routine, THEN the system SHALL CONTINUE TO write every servo angle through `set_angle`/`move_to` (clamped to `SAFE_LIMITS`), return to rest after the routine/on error, and respect `FORBIDDEN_COMBINATIONS` — this GPIO fix must not touch the servo safety path.

## Deriving the Bug Condition

**Definitions**

- **F**: the original (unfixed) mic-stop path — `stop_streaming()` runs
  `thread.join()` on a worker that has already returned, while PortAudio's
  callback thread may still be running `talk()` and re-creating the GPIO
  devices; `_release_gpio()` then races that callback, so the mic process can
  retain `EYE_LIGHT_PIN` / `MOUTH_MOTOR_PIN` after the stream stops.
- **F'**: the fixed path — after `stop_streaming()`, the callback can no longer
  run `talk()` (so it cannot re-create the devices), the stream is stopped
  before GPIO is released, and both pins are verifiably freed in-process, so a
  separate Routine subprocess can claim them without `GPIO busy`.

**Bug Condition Function** — identifies inputs that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT:  X of type GpioHandoff
          // X describes a mic -> routine hand-off: a live mic session was
          // started and stopped in the micwebcontroller process, then a
          // separate animatronic.py Routine subprocess is launched and
          // attempts to construct an AudioPlayer (claiming GPIO6 + GPIO15).
  OUTPUT: boolean

  RETURN X.mic_session_ran = TRUE          // a live mic stream was started
     AND X.mic_session_stopped = TRUE      // and then stopped (stop_streaming)
     AND X.routine_started_after = TRUE    // a Routine subprocess then launches
     AND NOT mic_process_released_pins(X)  // mic process still owns GPIO6/GPIO15
END FUNCTION
```

**Property Specification** — defines correct behavior for buggy inputs:

```pascal
// Property: Fix Checking — the Routine can claim the pins and perform
FOR ALL X WHERE isBugCondition(X) DO
  result ← performHandoff'(X)   // F' = fixed path
  ASSERT audio_player_constructed_without_error(result)   // no lgpio 'GPIO busy'
     AND audible(result.routine_audio)
     AND jaw_and_eyes_driven(result)
     AND holds_across_repeated_handoffs(result)           // no service restart
END FOR
```

**Preservation Goal** — non-buggy inputs behave identically to the original:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // e.g. an actively streaming mic still drives GPIO6/GPIO15 via talk(); a
  // Routine from a fresh service still plays and moves; preset load/persist/
  // revert, the orderly PyAudio teardown + _reset_audio_dsp_state(), and the
  // servo safety path all behave exactly as before.
END FOR
```
