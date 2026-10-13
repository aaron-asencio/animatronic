# Mic GPIO Line-Release Leak Bugfix Design

## Overview

The long-lived mic service (`src/micwebcontroller.py`, port 5000) claims the two
jaw/eye GPIO lines — `MOUTH_MOTOR_PIN` (GPIO15) and `EYE_LIGHT_PIN` (GPIO6) — on
its first mic session and never releases the underlying lgpio **kernel line
claim** for the rest of the process's lifetime, even after `stop_streaming()`
runs `_release_gpio()`. gpiozero `.off()` + `.close()` on the two device objects
is not enough: it tears down the Python device wrapper but leaves the lgpio
chip handle (`/dev/gpiochip0`) and the two `anon_inode:gpio-line` fds open and
claimed by the mic process. Because the claim is **process-wide and
exclusive**, the next `animatronic.py` Routine subprocess that constructs
`LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` in
`AudioPlayer.__init__` hits `lgpio.error: 'GPIO busy'`, so no Routine audio
plays and the jaw/eyes do not move.

The fix is to make the stop path actually release the kernel lines. The chosen
approach (Option A below) closes the gpiozero **pin factory** after
`_release_gpio()` has closed and nulled the two devices, which drops the lgpio
chip handle and both gpio-line fds, truly handing the pins back to the OS. A
fresh factory is recreated lazily the next time a device is constructed (the
next mic session), exactly as gpiozero does on first use. The change is confined
to `src/micwebcontroller.py` and runs only on stop, after the stream thread has
joined and `is_streaming` is already `false`.

This spec **supersedes** the refuted `voice-fx-audio-dropout-fix` spec, whose
in-process straggler-callback hypothesis and fake-gpiozero test were a false
negative. That spec is left in place and unmodified.

## Glossary

- **Bug_Condition (C)**: The mic service process still holds the GPIO6/GPIO15
  kernel line claims (the `anon_inode:gpio-line` fds remain open) after a mic
  session has started at least once and `stop_streaming()` has returned with
  `is_streaming == false`. Formalised in Bug Details below.
- **Property (P)**: After the fix, when the bug condition would have held, the
  mic process no longer holds those line claims, so any claimant of GPIO6/GPIO15
  (another process's `AudioPlayer.__init__`, or a probe) succeeds without
  `GPIO busy`.
- **Preservation**: While the mic genuinely owns the pins (live stream), and for
  all mic effects/recording/config behaviour, and for all Routine GPIO use when
  the mic is not the owner, the fixed code behaves identically to the original.
- **`_release_gpio()`**: The stop-path helper in `src/micwebcontroller.py` that
  today calls `.off()` + `.close()` on the module-globals `jaw_motor` /
  `led_eye_light` and nulls them. It frees the Python wrappers but not the lgpio
  kernel line claim.
- **`talk(audio_data, start_time)`**: The per-chunk envelope driver. Lazily
  constructs the two module-global devices (`DigitalOutputDevice(MOUTH_MOTOR_PIN)`,
  `LED(EYE_LIGHT_PIN)`) on the first in-stream frame and drives them on/off from
  the mic envelope. Already guarded to no-op when `is_streaming` is false.
- **gpiozero pin factory**: `gpiozero.pins.lgpio.LGPIOFactory`, resolved from the
  system install `/usr/lib/python3/dist-packages`. `Device.pin_factory` is the
  class-level singleton; it is `None` until the first device is constructed and
  is recreated lazily on next use. Closing it (`Device.pin_factory.close()`)
  releases every pin it reserved **and** the underlying lgpio chip handle — this
  is what actually drops the kernel line claim.
- **lgpio kernel line claim**: The exclusive, process-wide reservation of a GPIO
  line held via `/dev/gpiochip0`. Visible as `anon_inode:gpio-line` fds in
  `/proc/<pid>/fd`. This is what leaks today and what the fix must release.
- **F / F'**: F is the original mic service whose `_release_gpio()` leaks the
  kernel lines; F' is the fixed mic service whose stop path releases them.

## Bug Details

### Bug Condition

The bug manifests once the mic service has claimed GPIO6/GPIO15 on its first
session (lazily, inside `talk()`) and a stop has completed. `_release_gpio()`
closes and nulls the two gpiozero devices, but the lgpio factory keeps the chip
handle and both gpio-line fds open, so the kernel lines stay claimed by the mic
process. The service appears idle (`/status` → `is_streaming: false`) while
still being the exclusive owner of the two lines.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input describing a GPIO claim attempt on EYE_LIGHT_PIN/MOUTH_MOTOR_PIN
         while the mic service process is running
  OUTPUT: boolean

  RETURN micServiceRunning
         AND micSessionStartedAtLeastOnce        // pins were lazily claimed in talk()
         AND NOT micCurrentlyStreaming            // stop_streaming() returned, is_streaming == false
         AND input.claimTarget IN {EYE_LIGHT_PIN, MOUTH_MOTOR_PIN}
         AND micServiceStillHoldsLine(input.claimTarget)   // the leak: gpio-line fd still open
END FUNCTION
```

### Examples

- Mic toggled on then off via `curl POST /handler {"action":"start"}` then
  `{"action":"stop"}`. `/status` reports `is_streaming: false`, yet
  `sudo ls -l /proc/<micpid>/fd` still shows `fd -> anon_inode:gpio-line` for
  both lines plus `fd -> /dev/gpiochip0`. The fds predate the start/stop cycle
  (first-ever claim) and never disappear. **Expected after fix:** those two
  gpio-line fds are gone after stop.
- `sudo .venv/bin/python src/animatronic.py --action=blah` run after a stopped
  mic session: `AudioPlayer.__init__` raises `lgpio.error: 'GPIO busy'` at
  `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)`; no audio, no
  jaw/eye motion. **Expected after fix:** construction succeeds, Routine plays
  and articulates.
- Independent probe `from gpiozero import LED; LED(6)` in a separate process
  while the mic service is up (post-stop): raises `GPIO busy`. **Expected after
  fix:** the probe constructs successfully.
- Isolated prototype on a FREE pin (GPIO26) in a fresh `sudo .venv/bin/python`,
  watching `/proc/self/fd`: `LED(26)` opens TWO fds (`/dev/gpiochip0` + an
  `anon_inode:gpio-line`). Device `.close()` drops NEITHER. Re-claiming the same
  pin in the same process after `.close()` SUCCEEDS (the factory pools the line
  internally). `Device.pin_factory.close()` drops BOTH fds — the line is truly
  released so a separate process can then claim it. **This prototype is the
  backbone of the root-cause analysis and the fix.**

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- While the mic stream is live (`is_streaming` true), the mic service continues
  to own GPIO6/GPIO15 and drive the jaw motor and eye LED from the mic envelope
  exactly as before (bugfix 3.1). The factory close happens only on stop, after
  the stream thread has joined, so a live stream is never disturbed.
- Toggling the mic on again after a previous stop re-claims the pins and streams
  with working jaw/eye articulation (bugfix 3.2) — via the existing lazy device
  creation in `talk()` plus lazy factory recreation.
- Voice effects, FX recording, style preset load/save/revert, and `/config` jaw
  tuning behave exactly as before (bugfix 3.3) — the fix touches only the
  pin-release path, not the DSP/effects/config code.
- `src/audio_player.py` and `src/performance.py` continue to claim and drive the
  GPIO pins exactly as today when the mic is not the owner (bugfix 3.4). They are
  victims, not causes; confirmed by inspection to need no change (see Fix
  Implementation → Confirming the fix is confined).

**Scope:**
All inputs where `isBugCondition` is false must be completely unaffected:
- The whole live-stream path (every in-stream `talk()` / `audio_callback`
  frame while `is_streaming` is true).
- All effects/recording/config endpoints and their persisted state.
- All Routine GPIO use in other processes when the mic service does not own the
  lines.

## Hypothesized Root Cause

The hardware evidence and the isolated GPIO26 prototype make this a
**confirmed** root cause, not a speculative one. The sub-points below map each
mechanism to the bugfix.md Current-Behavior clauses it explains.

1. **gpiozero device `.close()` does not release the lgpio kernel line.** In a
   long-lived process the lgpio factory keeps the chip handle (`/dev/gpiochip0`)
   and the per-line fds open after a device's `.close()`. The GPIO26 prototype
   reproduced this exactly: `.close()` dropped neither the chip fd nor the
   gpio-line fd. So `_release_gpio()`'s `.off()` + `.close()` + null leaves
   GPIO6/GPIO15 claimed. → explains bugfix **1.1** (lines still held after a
   successful stop).

2. **The claim is process-wide and exclusive.** lgpio reserves the line against
   `/dev/gpiochip0` for the owning PID. While the mic process holds it, no other
   process can claim it. → explains bugfix **1.4** (a separate probe hits
   `GPIO busy`) and the downstream **1.2** (`AudioPlayer.__init__` raises
   `GPIO busy`) and **1.3** (no Routine audio, no jaw/eye motion), both of which
   are the same claim failing from the Routine subprocess.

3. **Same-process re-claim masks the leak locally.** The factory pools/keeps the
   line reserved internally, so the mic can re-toggle and re-create its own
   devices fine (the prototype re-claimed GPIO26 in-process after `.close()`).
   This is why the mic "still works" while other processes are blocked — and why
   a same-process in-code check cannot detect the leak.

4. **Only closing the factory (chip handle) releases the line to the OS.** The
   prototype confirmed `Device.pin_factory.close()` drops BOTH the gpio-line fd
   and `/dev/gpiochip0`, after which a separate process can claim the pin. This
   is the lever the fix must pull.

Refuted alternative: the prior spec's "straggler PortAudio callback re-grabs the
pins" hypothesis. The lines are claimed **once** (timestamps predate any
start/stop cycle) and never released, and `is_streaming` is already false while
they stay held — so there is no re-grab to prevent and an `is_streaming` guard
changes nothing about ownership.

## Correctness Properties

Property 1: Bug Condition - Kernel line released after stop

_For any_ input where the bug condition holds (`isBugCondition` returns true —
the mic service has streamed at least once and `stop_streaming()` has returned
with `is_streaming` false), the fixed mic service SHALL have released the GPIO6
and GPIO15 kernel line claims, so (a) the mic process no longer holds the two
`anon_inode:gpio-line` fds, and (b) a separate claimant of either pin
(`AudioPlayer.__init__` / `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)`)
succeeds without raising `lgpio.error: 'GPIO busy'`.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Non-stop paths unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false — a live stream owning the pins, the effects/recording/config endpoints,
or Routine GPIO use in another process while the mic does not own the lines),
the fixed mic service SHALL produce the same result as the original, preserving
live jaw/eye articulation, mic re-toggle re-claim, DSP/effects/config behaviour,
and all other-process GPIO use.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Options evaluated

**Option A — close the gpiozero pin factory on the stop path (CHOSEN).**
After `_release_gpio()` has closed and nulled the two devices, also close the
gpiozero pin factory and null the class attribute so the lgpio chip handle and
both gpio-line fds are dropped and the lines return to the OS. The GPIO26
prototype directly confirms this is what releases the line to other processes,
and that a fresh factory + same-process re-claim works afterward. Concretely, in
`_release_gpio()` after the device-close loop:

```
# After both devices are .off()+.close()+nulled, drop the lgpio chip handle so
# the kernel GPIO lines are released to the OS (device .close() alone leaves them
# claimed for this process's lifetime — bugfix 1.1/1.4). Null the class singleton
# so gpiozero lazily builds a fresh factory on the next device construction
# (next mic session), exactly as it does on first use.
factory = getattr(Device, 'pin_factory', None)
if factory is not None:
    try:
        factory.close()
    except Exception as e:
        print(f"warning: could not close gpiozero pin factory: {e}")
    Device.pin_factory = None
```

This requires importing `Device` from `gpiozero` at the module top (alongside
the existing `PWMLED` / `LED` / `DigitalOutputDevice` imports).

*Process-wide safety analysis (critical — `pin_factory` is a process-global
singleton).* Closing the factory tears down **every** gpiozero device in the
process, so we must confirm nothing else is left dangling across the close:
- The only gpiozero devices ever constructed in `micwebcontroller.py` are the
  two module-globals `jaw_motor` (`DigitalOutputDevice`, GPIO15) and
  `led_eye_light` (`LED`, GPIO6), created lazily in `talk()`. The factory close
  runs **after** `_release_gpio()` has already `.close()`d and nulled both, so
  there is no live gpiozero device at factory-close time.
- `PWMLED` is imported but **never constructed** in this module (confirmed by
  inspection — no `PWMLED(` anywhere). So no PWMLED or other device is live.
- No range sensor, IR illuminator, or other gpiozero user lives in this process
  — those are in `webapp.py`, a **separate** process with its own factory;
  closing this process's factory cannot affect it.
- Therefore closing the factory here is safe: the only things it releases are
  the two lines we intend to release.
*Re-initialisation of the next mic session.* On the next `start_streaming()` the
first in-stream `talk()` frame hits the existing lazy device creation
(`if jaw_motor is None: jaw_motor = DigitalOutputDevice(...)`, likewise for the
LED). gpiozero sees `Device.pin_factory is None` and builds a fresh
`LGPIOFactory`, re-claiming the lines. This is the same code path as the
first-ever session, so no new state machine is introduced. (Nulling the factory
is required because closing it does not auto-reset the class attribute; without
the null, gpiozero would try to reuse a closed factory.)

**Option B — a narrower per-line release without tearing down the factory.**
If gpiozero/lgpio exposed a call that drops just the two lines' kernel claims
(e.g. releasing the specific `pin` objects from the factory's reservation and
closing their line fds) we could avoid the process-wide factory teardown. This
would be marginally safer in a process with other live gpiozero devices.
**Rejected for now:** the prototype only *verified* that `Device.pin_factory.close()`
drops the fds; it did **not** verify any narrower call actually drops the
gpio-line fd (device `.close()` demonstrably does not). Introducing an
unverified narrower release risks re-creating the exact false-negative the prior
spec fell into. Because this process has no other live gpiozero device (see the
safety analysis above), Option A's process-wide scope costs us nothing here, so
we prefer the verified lever. If a narrower, fd-dropping call is confirmed on
hardware in future, it could replace the factory close with no behavioural
change to callers.

**On the existing `is_streaming` guards in `talk()` / `audio_callback`.**
Those guards were added by the refuted prior spec to block a straggler-callback
re-grab. The hardware evidence shows there is no re-grab to prevent (the lines
are claimed once and never released; `is_streaming` is already false while they
stay held), so the guards are **not** the fix. They are, however, **harmless to
keep**: when `is_streaming` is true they are fully transparent (preservation
3.1), and when false they simply make `talk()` a no-op, which is correct either
way. Recommendation: **keep them as-is** to keep this fix minimal and focused on
the real release; do not expand the fix's blast radius by reverting unrelated
code in the same change. (A separate cleanup spec could revisit them, with their
own preservation tests.) This keeps the behaviour change scoped to "actually
release the kernel lines on stop."

### Changes Required

**File**: `src/micwebcontroller.py` (the ONLY file changed)

**Function**: `_release_gpio()` (plus a one-line import)

1. **Import `Device`** from `gpiozero` at the module top, next to the existing
   `PWMLED` / `LED` / `DigitalOutputDevice` imports.
2. **Close the pin factory in `_release_gpio()`** after the existing
   device-close loop and after both globals are nulled: fetch
   `Device.pin_factory`, call `.close()` inside a guarded `try/except` (print a
   warning on failure, never raise — matching the existing per-device guard
   style), then set `Device.pin_factory = None`.
3. **No change to the call site.** `_release_gpio()` is already invoked from
   `stop_streaming()` after the stream thread has joined and `is_streaming` is
   false, which is exactly the safe point to drop the factory. `_reset_audio_dsp_state()`
   still runs after it, unchanged.
4. **No change to `talk()`'s lazy device creation** — it already recreates the
   devices on the next session, which now also triggers lazy factory recreation.

### Confirming the fix is confined

- `src/audio_player.py`: `AudioPlayer.__init__` is a pure consumer — it
  constructs `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` and
  fails today only because the mic process holds the lines. Once the mic
  releases them, this code succeeds unchanged. **No change needed** (bugfix 3.4).
- `src/performance.py`: closes each `AudioPlayer` between tracks to free pins;
  it is downstream of the same claim and needs no change (bugfix 3.4).
- No other module constructs GPIO6/GPIO15; the fix stays inside the mic process.

## Testing Strategy

### Validation Approach

Two phases: first surface a counterexample proving the real kernel line stays
claimed on UNFIXED code, then prove the fix releases it and preserves every
non-stop path. **The verification must prove the REAL kernel line is released**
— an in-process fake-gpiozero model (as used by the refuted prior spec) cannot,
because its fake `close()` always frees the pin and therefore structurally
cannot represent the lgpio leak.

Compile checks and tests are run with the project tools:
`python -m py_compile src/micwebcontroller.py` for syntax/import validation, and
`.venv/bin/python -m pytest -q` for the test suite. Per the workspace
hardware-movement rule, **no SERVO_SIM collision/limit verification** is run —
this bug touches GPIO line ownership, not servo motion, and the operator
validates any hardware movement directly. All in-code GPIO safety stays intact:
device writes still go through the normal gpiozero on/off paths and
`_release_gpio()` still `.off()`s before releasing.

### Hardware-faithful line-release check (the authoritative proof)

The real proof that `isBugCondition` no longer holds must observe the kernel
line, not an in-process model. Two complementary checks, both run as root on the
Pi against real lgpio:

1. **fd inspection** — after a `start_streaming()` → `stop_streaming()` cycle,
   read `/proc/<pid>/fd` (the current process, since the test can drive the mic
   functions in-process) and assert there are **no** `anon_inode:gpio-line` fds
   remaining (and ideally no `/dev/gpiochip0` handle). On UNFIXED code this
   assertion FAILS (the fds persist); after the fix it PASSES.
2. **separate-process re-claim** — spawn a SEPARATE `sudo .venv/bin/python`
   process that constructs `LED(EYE_LIGHT_PIN)` and
   `DigitalOutputDevice(MOUTH_MOTOR_PIN)` after the mic stop and asserts no
   `GPIO busy`. On UNFIXED code this FAILS; after the fix it PASSES. This most
   directly mirrors the real Routine crash (`AudioPlayer.__init__`).

**Gating / skip when off-hardware.** These checks require real lgpio and root on
the Pi. The design REQUIRES they be gated so the suite still runs elsewhere:
skip (e.g. `pytest.mark.skipif` / an early `pytest.skip`) when the process is not
root (`os.geteuid() != 0`) OR `/dev/gpiochip0` does not exist OR the lgpio
factory cannot be constructed. The skip reason must state that the line-release
proof only runs on the Pi as root.

### Note on any retained in-process logic test

If a pure-logic unit test is kept for the stop-path ordering (devices
`.off()`/`.close()`/nulled, then factory closed and `Device.pin_factory` nulled,
in that order), it MUST carry a docstring/comment documenting its limitation:
**it verifies call ordering only and CANNOT prove the kernel line is released**
(a fake or mocked factory frees the pin by construction). This is the explicit
guard against repeating the prior spec's false negative — such a test must never
be presented as proof of release. The authoritative proof is the
hardware-faithful check above.

### Exploratory Bug Condition Checking

**Goal**: Surface a counterexample on UNFIXED code confirming the kernel line
stays claimed after stop, and confirm the root cause.

**Test Plan**: Drive `start_streaming()` then `stop_streaming()` (feeding a few
synthetic in-stream frames through `talk()` so the pins are actually claimed),
then run the hardware-faithful line-release check. Run on UNFIXED code to
observe the leak.

**Test Cases**:
1. **fd-still-present after stop** — assert gpio-line fds are gone after stop
   (FAILS on unfixed: they persist).
2. **separate-process re-claim after stop** — spawn a process doing
   `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` (FAILS on
   unfixed with `GPIO busy`).

**Expected Counterexamples**: both `anon_inode:gpio-line` fds remain open after
a successful stop; a separate claimant raises `lgpio.error: 'GPIO busy'`.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed stop path
releases the lines.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  // after fix, run a start→stop cycle then:
  ASSERT NOT micServiceStillHoldsLine(input.claimTarget)   // no gpio-line fd
  result := claimPin(input.claimTarget)                    // separate process
  ASSERT succeeded(result) AND NOT raised_GPIO_busy(result)
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, F'(input) =
F(input).

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalBehavior(input) = fixedBehavior(input)
END FOR
```

**Testing Approach**: Observe behaviour on UNFIXED code first for the non-stop
paths, then capture it in tests that must still pass after the fix.

**Test Plan / Cases**:
1. **Live-stream ownership (3.1)** — while `is_streaming` is true, `talk()`
   drives the envelope and the two devices exactly as before; the factory is NOT
   closed mid-stream. Observe on unfixed, assert unchanged after fix.
2. **Mic re-toggle re-claim (3.2)** — a second `start → (frames) → stop` cycle
   after the fix re-claims the pins, streams, and releases again. Assert the
   second session works (lazy factory recreation).
3. **Effects/recording/config untouched (3.3)** — the `/effects`, `/effects/save`,
   `/effects/revert`, `/config` handlers and their persisted state behave
   identically (property-based over valid inputs where practical, else
   representative examples). These paths never touch `_release_gpio()`.
4. **Victim modules unchanged (3.4)** — by inspection, `audio_player.py` /
   `performance.py` are unchanged; a focused test that `AudioPlayer.__init__`
   succeeds once the mic has released the pins covers the observable effect.

### Unit Tests

- Stop-path ordering logic in `_release_gpio()` (devices off/close/null, then
  factory close + `Device.pin_factory` null) — **with the documented limitation
  that it does not prove kernel release**.
- `_release_gpio()` is a safe no-op when both devices are already `None`
  (existing behaviour preserved).

### Property-Based Tests

- Preservation of the effects/config endpoints across generated valid inputs
  (e.g. valid `/config` field combinations, valid effect names/amounts) — assert
  observed behaviour is unchanged by the fix.
- Preservation of the envelope decision in `talk()` across generated RMS frame
  sequences while `is_streaming` is true (the stop-path change must not alter
  in-stream behaviour).

### Integration Tests

- Full `start → stream frames → stop → separate-process claim` on the Pi (the
  authoritative hardware-faithful proof), gated to root + `/dev/gpiochip0`.
- Full mic re-toggle (two sessions) on the Pi, asserting the second session
  re-claims and streams.
- A Routine (`AudioPlayer`) constructed in a separate process after a mic stop
  succeeds — the end-to-end reproduction of the original crash, now passing.

## Requirement Traceability

| Requirement | Design element | Verified by |
|-------------|----------------|-------------|
| 1.1 lines still held after successful stop | Root Cause §1; Bug Condition `micServiceStillHoldsLine` | Exploratory fd-inspection (FAILS on unfixed) |
| 1.2 `AudioPlayer.__init__` raises `GPIO busy` | Root Cause §2; Bug Details examples | Exploratory separate-process re-claim (FAILS on unfixed) |
| 1.3 no Routine audio / no jaw+eye motion | Root Cause §2 (same claim failing) | Integration: Routine in separate process (FAILS on unfixed) |
| 1.4 separate process hits `GPIO busy` | Root Cause §2 | Exploratory separate-process re-claim (FAILS on unfixed) |
| 2.1 lines released after stop | Fix: factory close in `_release_gpio()`; Property 1(a) | Fix check: fd inspection (PASSES after fix) |
| 2.2 `AudioPlayer.__init__` claims pins OK | Fix releases lines; Property 1(b) | Fix check: separate-process re-claim (PASSES) |
| 2.3 Routine plays + articulates (web & CLI) | Fix releases lines; victim modules unchanged | Integration: Routine in separate process (PASSES) |
| 2.4 separate process claim succeeds | Fix: factory close drops chip handle; Property 1(b) | Fix check: separate-process re-claim (PASSES) |
| 3.1 live stream still owns + drives pins | Factory close only on stop, after thread join | Preservation: live-stream ownership test |
| 3.2 mic re-toggle re-claims + streams | Lazy device creation + lazy factory recreation | Preservation: second start→stop cycle test |
| 3.3 effects/recording/config unchanged | Fix touches only `_release_gpio()` | Preservation: effects/config PBT + example tests |
| 3.4 `audio_player.py` / `performance.py` unchanged | Confined-fix inspection; no edits to those files | Inspection + `AudioPlayer.__init__` success test |
