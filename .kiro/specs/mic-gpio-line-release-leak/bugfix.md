# Bugfix Requirements Document

## Introduction

Running any audio-backed Routine (e.g. `sudo .venv/bin/python src/animatronic.py
--action=blah`) **after** the live mic has been toggled on and then off crashes
the Routine's audio thread with `lgpio.error: 'GPIO busy'`. The crash is raised
in `AudioPlayer.__init__` (`src/audio_player.py`, at
`self.led_eye_light = LED(EYE_LIGHT_PIN)` / GPIO6, and equivalently
`self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)` / GPIO15). When it
happens no Routine audio plays and the jaw/eyes do not move, from both the web
UI and the CLI. The live mic itself still works when re-toggled.

The long-lived mic service process (`src/micwebcontroller.py`, port 5000) claims
GPIO6 (`EYE_LIGHT_PIN`) and GPIO15 (`MOUTH_MOTOR_PIN`) on its **first** mic
session and never releases the underlying lgpio kernel line claim for the rest
of its process lifetime — even though `stop_streaming()` runs `_release_gpio()`,
which calls gpiozero `.off()` + `.close()` on the module-global `jaw_motor` /
`led_eye_light` devices and nulls them. Because the mic service keeps owning the
two kernel lines, the next `animatronic.py` subprocess that tries to claim the
same pins hits `GPIO busy`.

This spec **supersedes** the now-refuted `voice-fx-audio-dropout-fix` spec. That
spec is intentionally left in place and unmodified; see the dedicated note
below for why its hypothesis and its test were a false negative.

### Reproduction evidence (recorded on the running Pi)

These are the direct, hardware-confirmed observations that anchor the bug
condition. They are the reproduction record for this fix and should be
preserved as the ground truth the verification must match.

- The mic service runs as a **single** process (PID 37277), parented to init,
  launched as `/.venv/bin/python3 src/micwebcontroller.py`, with env
  `MICWEB_DEV=0`. The Werkzeug reloader is therefore OFF and there is no forked
  worker. This rules out any fork/reloader double-process cause.
- `/status` reports `is_streaming: false` (mic stopped), yet
  `sudo ls -l /proc/37277/fd` shows the process **still** holds:
  - fd 6 -> `anon_inode:gpio-line`
  - fd 7 -> `anon_inode:gpio-line`  (the two claimed lines: GPIO6 + GPIO15)
  - fd 8 -> `/dev/gpiochip0`
- Across a full start -> stop cycle driven via `curl POST /handler
  {"action":"start"}` then `{"action":"stop"}`, those gpio-line fds are present
  BEFORE start, DURING the stream, and STILL present AFTER a successful stop
  (`"Streaming stopped"`). They never disappear. Their timestamps (13:50)
  predate the test cycle and match the first-ever claim; the process started
  at 13:33.
- An independent probe process doing `from gpiozero import LED; LED(6)` ALSO
  immediately raises `lgpio.error: 'GPIO busy'` while the mic service is up —
  reproducing the exact Routine crash from a separate process and proving the
  mic service is the current pin owner.
- gpiozero's default pin factory is `gpiozero.pins.lgpio.LGPIOFactory`, resolved
  from the SYSTEM install at `/usr/lib/python3/dist-packages` (not the venv).
  The claim is process-wide and exclusive.

**Mechanism:** gpiozero `.close()` on the device — as used in `_release_gpio()`
— is not dropping the lgpio kernel line claim in this long-lived process (the
line/chip handle stays open), so GPIO6 and GPIO15 stay owned by the mic
service's PID until the process is killed. The next `animatronic.py` subprocess
cannot claim them, so `AudioPlayer.__init__` raises `GPIO busy`.

### Note on the previously-investigated (now refuted) cause

The prior spec `.kiro/specs/voice-fx-audio-dropout-fix/` blamed an **in-process
straggler-PortAudio-callback re-grab**: it claimed a late PortAudio callback ran
`talk()` after `stop_streaming()`, lazily re-creating `jaw_motor` /
`led_eye_light` and re-claiming the pins; its fix guarded `talk()` /
`audio_callback` on `is_streaming`.

The hardware evidence refutes this:

- The two kernel lines are claimed **once** (timestamps predate any
  stop/start cycle) and are **never** released — there is no re-grab to prevent.
- `is_streaming` is already `false` while the pins remain held, so an
  `is_streaming` guard changes nothing about who owns the lines.
- The prior exploration test `tests/test_voice_fx_dropout.py` installed a
  **fake** `gpiozero` whose `_FakePinDevice.close()` ALWAYS removes the pin from
  an in-process `_PIN_OWNERS` dict. By construction, in that model `close()`
  frees the pin. That model **structurally cannot represent** the observed
  lgpio behaviour, where `.close()` leaves the kernel line claimed. The fake
  made the test pass green while the bug is fully present on hardware — a false
  negative.

This spec therefore must NOT rely on an in-process `_PIN_OWNERS`-style fake as
the sole proof of a fix. The verification must prove the real kernel line claim
is actually released (see Unchanged/Expected behavior and the design's testing
strategy).

## Bug Analysis

### Current Behavior (Defect)

What currently happens once the mic has been toggled on then off at least once
in the mic service's lifetime (the pins are claimed on the first session and
never released):

1.1 WHEN a mic session has started at least once in the mic service process AND
`stop_streaming()` has returned successfully (`is_streaming` is `false`) THEN
the mic service process still holds the GPIO6 and GPIO15 kernel line claims
(the `anon_inode:gpio-line` fds remain open) instead of releasing them.

1.2 WHEN an `animatronic.py` Routine subprocess then runs and
`AudioPlayer.__init__` constructs `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)`
THEN the construction raises `lgpio.error: 'GPIO busy'` and the Routine's audio
thread crashes.

1.3 WHEN that Routine crashes on the busy pin THEN no Routine audio plays and
the jaw motor and eye LED do not move, whether the Routine was launched from
the web UI or the CLI.

1.4 WHEN any separate process attempts to claim GPIO6 or GPIO15 (e.g. a probe
doing `LED(6)`) while the mic service is running after a stopped session THEN
that claim also raises `lgpio.error: 'GPIO busy'`, because the mic service is
still the exclusive owner of those lines.

### Expected Behavior (Correct)

What should happen instead:

2.1 WHEN `stop_streaming()` returns successfully (`is_streaming` is `false`)
THEN the mic service SHALL have released the GPIO6 and GPIO15 kernel line
claims, so the `anon_inode:gpio-line` fds for those lines are no longer held by
the process.

2.2 WHEN an `animatronic.py` Routine subprocess runs after a stopped mic session
THEN `AudioPlayer.__init__` SHALL successfully claim `EYE_LIGHT_PIN` and
`MOUTH_MOTOR_PIN` without raising `GPIO busy`.

2.3 WHEN a Routine runs after a stopped mic session THEN the Routine SHALL play
its audio and drive the jaw motor and eye LED normally, from both the web UI
and the CLI.

2.4 WHEN any separate process attempts to claim GPIO6 or GPIO15 after the mic
service has stopped streaming THEN that claim SHALL succeed, because the mic
service no longer owns the lines.

### Unchanged Behavior (Regression Prevention)

Existing behavior that must be preserved for the non-buggy paths (i.e. while the
mic genuinely owns the pins, and for all mic functionality itself):

3.1 WHEN the mic stream is live (`is_streaming` is `true`) THEN the mic service
SHALL CONTINUE TO own GPIO6/GPIO15 and drive the jaw motor and eye LED from the
mic envelope exactly as before.

3.2 WHEN the mic is toggled on again after a previous stop THEN the mic service
SHALL CONTINUE TO re-claim the pins and stream mic audio with working jaw/eye
articulation.

3.3 WHEN the mic service applies voice effects, records FX output, loads/saves
style presets, or updates jaw tuning via `/config`, `/effects`, `/effects/save`,
`/effects/revert` THEN the system SHALL CONTINUE TO behave exactly as before
(this fix touches only the pin-release path, not the DSP/effects/config paths).

3.4 WHEN `src/audio_player.py` and `src/performance.py` run a Routine while the
mic service is NOT holding the pins THEN they SHALL CONTINUE TO claim and drive
the GPIO pins exactly as today (these modules are victims, not causes, and
should not require changes — to be confirmed by inspection).

## Deriving the Bug Condition

**Bug Condition Function** — identifies the inputs/states that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X describing a GPIO claim attempt on EYE_LIGHT_PIN/MOUTH_MOTOR_PIN
         while the mic service is running
  OUTPUT: boolean

  // The bug is triggered once the mic service has claimed the lines on its
  // first session and a stop has completed, yet the lines remain held.
  RETURN micServiceRunning
         AND micSessionStartedAtLeastOnce
         AND NOT micCurrentlyStreaming        // stop_streaming() returned
         AND claimTarget IN {EYE_LIGHT_PIN, MOUTH_MOTOR_PIN}
         AND micServiceStillHoldsLine(claimTarget)   // the leak
END FUNCTION
```

**Property Specification (Fix Checking)** — correct behavior for buggy inputs:

```pascal
// Property: Fix Checking - kernel line released after stop
FOR ALL X WHERE isBugCondition(X) DO
  // after the fix, the leak no longer holds:
  ASSERT NOT micServiceStillHoldsLine(X.claimTarget)
  result <- claimPin(X.claimTarget)       // e.g. AudioPlayer.__init__ / LED(6)
  ASSERT succeeded(result) AND NOT raised_GPIO_busy(result)
END FOR
```

Where:
- **F** is the original mic service whose `_release_gpio()` does not drop the
  kernel line claim (the pins stay owned for the process lifetime).
- **F'** is the fixed mic service whose stop path actually releases the GPIO6 /
  GPIO15 kernel lines so another process can claim them.

**Preservation Goal** — non-buggy inputs behave identically:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

That is: while the mic stream is live and owns the pins (3.1/3.2), and for all
effects/recording/config behavior (3.3) and all Routine GPIO use when the mic is
not the owner (3.4), the fixed code behaves exactly as the original.
