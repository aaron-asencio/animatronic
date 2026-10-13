# Voice-FX Audio Dropout Fix Bugfix Design

## Overview

After an operator uses the live mic (Voice-FX / Stream) on the dashboard, the
next Routine launched from the control panel is dead: no Routine audio plays and
the Routine's jaw and eyes never move. The figure only works again after the
`animatronic-micweb` service is restarted or the Pi is rebooted. A Routine from
a freshly started service (no prior mic session) works fine — the failure
appears **only after a live mic session**.

**The confirmed root cause is a cross-process GPIO ownership leak**, not the
ALSA/PyAudio-teardown degradation an earlier pass blamed. The long-lived mic
service process (`src/micwebcontroller.py`, port 5000, a **single** process:
`MICWEB_DEV=0` disables the Werkzeug reloader in
`deploy/animatronic-micweb.service`) claims `EYE_LIGHT_PIN` (GPIO6) and
`MOUTH_MOTOR_PIN` (GPIO15) lazily inside `talk()` during a live stream, via the
module-global `jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)` and
`led_eye_light = LED(EYE_LIGHT_PIN)`. After the mic stops, those pins are not
reliably relinquished in-process. A separate `animatronic.py` Routine subprocess
then runs `src/performance.py` `_play_chain()`, which constructs
`AudioPlayer(**options)` on the daemon audio thread; `AudioPlayer.__init__`
(`src/audio_player.py:75`) constructs `LED(EYE_LIGHT_PIN)` /
`DigitalOutputDevice(MOUTH_MOTOR_PIN)` **up front, before any audio stream is
opened**. If the mic process still holds GPIO6, that construction raises
`lgpio.error: 'GPIO busy'`, which kills the whole performance audio thread: no
Routine audio and the jaw/eyes never move.

**The leak is a callback-vs-release race.** `start_streaming()` runs
`def _run_stream(): audio_state['stream'] = stream_mic()` on a worker thread,
but `stream_mic()` opens a **callback (non-blocking)** PyAudio stream, calls
`stream.start_stream()`, and **returns immediately**. The audio callback
(`audio_callback` → `talk()`) then fires on PortAudio's **own internal thread**,
not the worker. So `stop_streaming()`'s `thread.join()` returns almost at once
(the worker already finished), while the PortAudio callback may still be running
`talk()`, which lazily **re-creates** `jaw_motor` / `led_eye_light`. The current
`stop_streaming()` stops/closes the stream and then calls `_release_gpio()`
(`device.off()`/`close()`, nulls the globals), but nothing prevents a
still-in-flight callback from re-grabbing the pins via `talk()` **after**
`_release_gpio()` has freed them — so GPIO6 ends up still-claimed when the stream
stops, and only process death (service restart) reliably frees it.

The fix guarantees the mic process relinquishes GPIO6 + GPIO15 **in-process**
after a stream, by closing the race: once stop is signalled, the PortAudio
callback can no longer run `talk()` to re-create the devices, and the pins are
verifiably freed. The fix touches only the mic-stop path in
`src/micwebcontroller.py`. It must not alter the servo safety path
(`SAFE_LIMITS` clamping, return-to-rest, `FORBIDDEN_COMBINATIONS`), the preset
load/persist logic, the mic-preemption behavior, or the mic-while-streaming
GPIO drive — all explicit preservation requirements.

**On the previously-applied (correct but non-fixing) teardown.** The working
tree already contains the orderly PyAudio stream teardown in `stop_streaming()`
(capture the stream handle, `stop_stream()` → `close()` before `terminate()`)
and `_reset_audio_dsp_state()`. These were the earlier (refuted) fix for an
assumed ALSA degradation. They are correct and harmless, so they are
**preserved** (requirement 3.3) — but they do not fix this bug and are not the
subject of this design.

## Glossary

- **Bug_Condition (C)**: A mic → routine hand-off where a live mic session ran
  and stopped in the `micwebcontroller.py` process, a separate `animatronic.py`
  Routine subprocess then launches and tries to construct an `AudioPlayer`
  (claiming GPIO6 + GPIO15), and the mic process has **not** released those
  pins. See `isBugCondition` in `bugfix.md`.
- **Property (P)**: The desired behavior for a buggy input — the Routine's
  `AudioPlayer` constructs without `lgpio 'GPIO busy'`, its audio is audible,
  its jaw/eyes are driven from the envelope, and this holds across repeated
  mic → routine hand-offs with no service restart.
- **Preservation**: Behavior that must stay unchanged — the mic-while-streaming
  GPIO drive via `talk()`, a first-run Routine from a fresh service, the
  already-applied orderly PyAudio teardown + `_reset_audio_dsp_state()`, the
  preset load/persist/revert logic, mic preemption, and the servo
  `SAFE_LIMITS`/rest/`FORBIDDEN_COMBINATIONS` safety path.
- **F** / **F'**: The original (unfixed) mic-stop path, where the PortAudio
  callback can re-create the GPIO devices via `talk()` after `_release_gpio()`
  races it, leaving the pins owned / the fixed path, where after
  `stop_streaming()` the callback can no longer run `talk()` and both pins are
  verifiably freed in-process.
- **`talk()`**: The function in `src/micwebcontroller.py` that, per audio
  frame, lazily constructs the module-global `jaw_motor`
  (`DigitalOutputDevice(MOUTH_MOTOR_PIN)`) and `led_eye_light`
  (`LED(EYE_LIGHT_PIN)`) on first use and drives them from the mic envelope.
  Called from `audio_callback` on PortAudio's internal callback thread.
- **`stream_mic()`**: The function that opens the **callback (non-blocking)**
  input+output PyAudio stream, calls `start_stream()`, and returns the stream
  immediately. The callback thread, not the worker thread, runs `audio_callback`
  → `talk()`.
- **`start_streaming()` / `_run_stream()`**: `start_streaming()` resets DSP
  state, opens PyAudio, sets `is_streaming = True`, and starts a worker thread
  whose target `_run_stream()` does `audio_state['stream'] = stream_mic()` and
  then exits (because `stream_mic()` returns immediately).
- **`stop_streaming()`**: The function that stops the mic: sets
  `is_streaming = False`, joins the worker thread (which has already exited),
  stops/closes the live stream, terminates PyAudio, calls `_release_gpio()`,
  then `_reset_audio_dsp_state()`.
- **`_release_gpio()`**: The function that drives `jaw_motor` / `led_eye_light`
  off, `close()`s them (freeing GPIO6/GPIO15), and nulls the module globals and
  jaw state.
- **`AudioPlayer.__init__`**: The constructor in `src/audio_player.py` that,
  when `drive_eyes`/`drive_jaw` are true (the default), constructs
  `LED(EYE_LIGHT_PIN)` / `DigitalOutputDevice(MOUTH_MOTOR_PIN)` **up front**,
  before opening any stream. This is where `lgpio 'GPIO busy'` is raised when
  the mic process still owns the pins.
- **`_play_chain()`**: The method in `src/performance.py` that, on the daemon
  audio thread, constructs `AudioPlayer(**options)` per track and calls the
  blocking `play_audio_file`. The `AudioPlayer(**options)` construction is where
  the `GPIO busy` raise lands and kills Routine audio.
- **`audio_state`**: The module-level dict holding the PyAudio instance,
  stream, `is_streaming` flag, worker thread, and DSP buffers.

## Bug Details

### Bug Condition

The bug manifests on a mic → routine hand-off. A live mic session ran and was
stopped in the `micwebcontroller.py` process; the PortAudio callback re-created
(or never released) the module-global `jaw_motor` / `led_eye_light`, so the mic
process still owns GPIO6 + GPIO15 after `stop_streaming()` returns. A separate
`animatronic.py` Routine subprocess then constructs an `AudioPlayer`, whose
`__init__` tries to claim those same pins and raises `lgpio 'GPIO busy'`.

**Formal Specification:**
```
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

Where `NOT mic_process_released_pins(X)` is realised in F by the PortAudio
callback re-creating `jaw_motor`/`led_eye_light` via `talk()` after (or racing)
`_release_gpio()`, so the pins stay claimed for the process's lifetime.

### Examples

- **Mic → routine hand-off (1.1 / 1.2 / 1.3 → 2.1 / 2.2 / 2.3):** Operator
  streams the live mic, stops it, then starts a Routine (e.g. `blah`). Expected:
  `AudioPlayer.__init__` claims GPIO6/GPIO15 cleanly, Routine audio is audible,
  the jaw and eyes move from the envelope. Actual: `AudioPlayer.__init__` raises
  `lgpio.error: 'GPIO busy'` on `LED(EYE_LIGHT_PIN)`, the performance audio
  thread dies at construction, and nothing plays or moves.
- **Retry without restart (1.4 → 2.4):** Operator re-launches the same Routine
  without restarting the service. Expected: it works. Actual: it fails again
  with the same `GPIO busy` — the mic process still owns the pins; only killing
  that process frees them.
- **Repeated hand-offs (2.4):** mic → Routine → mic → Routine … Expected: every
  hand-off succeeds with no service restart or reboot. Edge case: the fix must
  hold across an arbitrary number of hand-offs, not just the first.
- **Isolation evidence:** both a reboot and simply restarting
  `animatronic-micweb` fix it; the leaked pins are held by the mic **service
  process itself** — there is no kernel/hardware latch and no forked reloader
  child (single process, `MICWEB_DEV=0`).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- While the mic is actively streaming, `talk()` continues to pass the mic to the
  speaker and drive the jaw motor (GPIO15) and eye LED (GPIO6) from the mic
  envelope for the duration of the stream — the pins are legitimately owned by
  the mic process *while streaming* (3.1).
- A Routine from a freshly started mic service with no prior mic session
  continues to play audio and drive the jaw/eyes — the known-good first-run
  behavior is unaffected (3.2).
- `stop_streaming()` continues to stop/close the live PyAudio stream in order
  before `terminate()` (the already-applied orderly teardown) and continues to
  call `_reset_audio_dsp_state()` — correct and preserved, though not the fix
  (3.3).
- Selecting, saving, or reverting a voice-style preset continues to
  load/persist the correct effect chain (saved tuned override else factory
  preset) and apply it to the live stream, unchanged (3.4).
- Requesting a Routine or Mode while the mic is streaming continues to
  auto-stop/preempt the mic so the Routine/Mode owns the jaw/audio path (3.5).
- Every servo angle in a Routine is still written through `set_angle`/`move_to`
  (clamped to `SAFE_LIMITS`), returns to rest after the routine/on error, and
  respects `FORBIDDEN_COMBINATIONS` — this GPIO fix never touches the servo
  safety path (3.6).

**Scope:**
All inputs where `isBugCondition(X)` is false must behave exactly as before.
This includes:
- A live mic stream running steadily (the mic legitimately owning GPIO6/GPIO15).
- A single Routine played from a fresh process with no prior mic session.
- The preset selection/save/revert logic and jaw-tuning `/config` logic.
- The mic auto-stop/preempt trigger.
- The entire servo safety path.

## Hypothesized Root Cause

The confirmed cause (per `bugfix.md`) is a cross-process GPIO ownership leak in
the mic process. The design must confirm the precise mechanism and the following
contributing factors before implementing:

1. **PortAudio callback re-creating devices after stop (primary).** Neither
   `audio_callback` nor `talk()` consults `is_streaming`. `talk()` lazily
   constructs `jaw_motor` / `led_eye_light` whenever they are `None`. Because
   `stream_mic()` returns a non-blocking callback stream, `_run_stream()` exits
   immediately and `stop_streaming()`'s `thread.join()` returns at once, while a
   callback can still be in flight on PortAudio's thread. That in-flight
   `talk()` can re-create the devices **after** `_release_gpio()` has freed them,
   leaving the pins claimed. This directly explains 1.1 and 1.4.

2. **Release ordering vs. callback liveness.** The current `stop_streaming()`
   stops/closes the stream and then calls `_release_gpio()`, but there is no
   guarantee the PortAudio callback has fully quiesced before `_release_gpio()`
   runs — `stop_stream()`/`close()` should prevent further callbacks, but the
   lazy re-creation in `talk()` means any single callback that slips through
   after release re-grabs the pins. The fix must make it **impossible** for a
   post-release callback to re-create the devices (guard `talk()` on
   `is_streaming`, and/or close the stream before releasing GPIO so no callback
   can fire afterward).

3. **Cross-process claim on the same pins.** Routines run in a separate
   `animatronic.py` subprocess whose `AudioPlayer.__init__` claims GPIO6/GPIO15
   up front via gpiozero/lgpio. If the mic process still owns them, the claim
   raises `lgpio 'GPIO busy'` — explaining why the failure is specifically a
   mic → routine hand-off (1.2, 1.3) and why only mic-process death
   (service restart) frees the pins (1.4).

The exploratory phase simulates a callback still firing after stop to reproduce
the device re-creation race on the UNFIXED code. If the counterexample refutes
this mechanism, we re-hypothesize before implementing.

## Correctness Properties

Property 1: Bug Condition - Routine Claims GPIO6/GPIO15 After a Mic Session

_For any_ input where the bug condition holds (`isBugCondition` returns true) —
a live mic session that ran and stopped, followed by a Routine subprocess that
constructs an `AudioPlayer` claiming GPIO6 + GPIO15 — the fixed code SHALL have
the mic process relinquish both `EYE_LIGHT_PIN` (GPIO6) and `MOUTH_MOTOR_PIN`
(GPIO15) in-process when `stop_streaming()` returns, so the Routine's
`AudioPlayer.__init__` constructs `LED(EYE_LIGHT_PIN)` /
`DigitalOutputDevice(MOUTH_MOTOR_PIN)` without an `lgpio 'GPIO busy'` error, the
Routine audio is audible, the jaw and eyes are driven from the envelope, and
this holds across repeated mic → routine hand-offs with no service restart.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Streaming Drive, First-Run, and Safety Paths Unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false) — an actively streaming mic, or a Routine from a fresh service with no
prior mic session — the fixed code SHALL produce the same result as the original
code, preserving the mic-while-streaming jaw/eye drive via `talk()`, the
first-run Routine behavior, the already-applied orderly PyAudio teardown +
`_reset_audio_dsp_state()`, the preset load/persist/revert logic, the mic
auto-stop/preempt behavior, and the servo
`SAFE_LIMITS`/rest/`FORBIDDEN_COMBINATIONS` safety path.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, the change is confined to the
mic-stop path in `src/micwebcontroller.py`. No change is needed in
`audio_player.py` or `performance.py` — they are the (correct) victim of the
leak and the preservation reference for how pins should be claimed/released.

**File**: `src/micwebcontroller.py`

**Functions**: `stop_streaming()`, `talk()` (and `audio_callback` inside
`stream_mic()`), with `_release_gpio()` as the verified release point.

**Specific Changes**:

1. **Stop the callback from re-creating devices once stop is signalled
   (addresses 1.1, 1.4 / 2.1, 2.4).** Make it impossible for a late PortAudio
   callback to re-grab the pins:
   - **Guard the lazy device creation in `talk()` on `is_streaming`.** `talk()`
     must only construct `jaw_motor` / `led_eye_light` when `is_streaming` is
     true. Once `stop_streaming()` sets `is_streaming = False`, a straggler
     callback running `talk()` becomes a no-op for device creation (and should
     not drive devices either), so it can never re-claim GPIO6/GPIO15 after
     release. (Equivalently, short-circuit `audio_callback` when
     `is_streaming` is false before calling `talk()`.)
   - This closes the race regardless of exact callback timing, which is the
     property we need (not merely "usually released").

2. **Close the stream BEFORE releasing GPIO (addresses 2.1).** Reorder
   `stop_streaming()` so the live stream is stopped and closed (no further
   callbacks can fire) **before** `_release_gpio()` runs, rather than relying on
   `thread.join()` (which returns immediately for a non-blocking stream). The
   sequence becomes:
   - set `is_streaming = False` (callbacks become no-ops per change 1);
   - join the worker thread (fast, as today);
   - `stop_stream()` then `close()` the live stream (each guarded) so PortAudio
     fires no more callbacks;
   - `terminate()` the PyAudio instance;
   - `_release_gpio()` — now with no possibility of a callback re-creating the
     devices afterward;
   - `_reset_audio_dsp_state()` (unchanged, preserved).

3. **Verify `_release_gpio()` actually frees the pins in-process (addresses
   2.1).** Confirm `_release_gpio()` calls `device.off()` then `device.close()`
   on each of `jaw_motor` / `led_eye_light` (freeing the underlying lgpio pin)
   and nulls the module globals, and that — given changes 1 and 2 — nothing
   re-grabs them afterward. No behavioral change to `_release_gpio()` itself is
   expected beyond ensuring it runs after the stream is closed and the callback
   is dead.

4. **Hold across repeated hand-offs (addresses 2.4).** Because `talk()` only
   creates devices while `is_streaming` is true and `stop_streaming()` closes
   the stream before releasing, each mic session owns the pins only for its own
   duration and frees them on stop. A subsequent mic session re-creates them on
   its first in-stream `talk()` (3.1 preserved), and each intervening Routine
   subprocess can claim them. This must be verified across multiple
   start → stop → (routine) → start cycles with no restart.

**Preservation notes (no behavioral change intended):**

- **`talk()` while streaming (3.1):** the `is_streaming` guard is true for the
  whole live stream, so lazy creation on first frame and envelope-driven
  on/off are byte-for-byte as today. The guard only changes behavior for
  callbacks that fire *after* stop — which should never have driven hardware.
- **Orderly teardown + DSP reset (3.3):** the `stop_stream()`/`close()` before
  `terminate()` and the `_reset_audio_dsp_state()` call are kept exactly; change
  2 only fixes their ordering relative to `_release_gpio()` (stream closed
  before GPIO release), which does not alter their own behavior.
- **Preset logic (3.4), mic preempt (3.5), servo safety (3.6):** untouched. The
  change is isolated to the mic-stop/callback path; no preset, dispatch, or
  servo code is modified. 3.6 is verified by inspection (no servo code changes).

**File**: `src/audio_player.py` — **no change.** `AudioPlayer.__init__`
constructing the devices up front and `close()` releasing them per track are the
correct, preserved behavior; they are the victim of the leak, and the
preservation reference for 3.2. Confirmed by inspection only.

**File**: `src/performance.py` — **no change.** `_play_chain()` constructing
`AudioPlayer(**options)` per track on the daemon audio thread (where the raise
currently lands) is unchanged; once the mic process frees the pins, this path
works as it does on a fresh service.

## Testing Strategy

Per the workspace hardware-movement rule, do **not** run `SERVO_SIM`
collision/limit verification for this fix. The servo path is unchanged, so the
servo safety path (3.6) is verified by **inspection** (no servo code is
modified) rather than by simulation. Static and logic verification still apply:
`python -m py_compile` on every touched module, plus the audio/GPIO-logic tests
below.

### Validation Approach

Two-phase: first surface a counterexample that reproduces the GPIO re-creation
race on the UNFIXED code (confirming the mechanism), then verify the fix frees
the pins in-process and preserves all non-handoff behavior.

Because the real path drives physical PyAudio/ALSA hardware and GPIO, the tests
exercise the fixable seams with fakes installed into `sys.modules` **before**
importing `micwebcontroller`, following the stub pattern already in
`tests/test_micwebcontroller.py`:
- a fake `pyaudio` (`paInt16`/`paContinue` constants + a `PyAudio()` factory
  whose `open()` returns a fake stream recording `start_stream`/`stop_stream`/
  `close` calls), and
- fake `gpiozero` `LED` / `DigitalOutputDevice` that model pin ownership: each
  construction "claims" its pin and raises a `GPIO busy`-style error if that pin
  is already claimed by a live device; `close()` releases it. This lets a test
  simulate a second owner (the Routine subprocess's `AudioPlayer`) claiming the
  same pin and asserting success/failure without a Pi.

### Exploratory Bug Condition Checking

**Goal**: Surface a counterexample that demonstrates the leak BEFORE
implementing the fix, and confirm the callback-re-creation mechanism. If
refuted, re-hypothesize.

**Test Plan**: Drive `start_streaming()` then `stop_streaming()` with the fake
PyAudio/gpiozero. Capture the `audio_callback` passed to the fake stream's
`open()`, and **invoke that callback once more after `stop_streaming()` has
returned** to simulate a straggler PortAudio callback. Then simulate the Routine
subprocess by constructing a fresh `LED(EYE_LIGHT_PIN)` /
`DigitalOutputDevice(MOUTH_MOTOR_PIN)` on the fake gpiozero and assert whether it
succeeds.

**Test Cases**:
1. **Post-stop callback re-creation (will FAIL on unfixed code).** After
   `stop_streaming()`, fire the captured callback once; assert the mic module
   globals `jaw_motor` / `led_eye_light` are still `None` (devices not
   re-created). On unfixed code `talk()` re-creates them, so the pins stay
   claimed — the assertion fails, reproducing the leak.
2. **Routine claim after mic session (will FAIL on unfixed code).** After the
   post-stop callback, construct new fake devices on GPIO6/GPIO15 (standing in
   for `AudioPlayer.__init__` in the Routine subprocess) and assert no
   `GPIO busy` is raised. On unfixed code the pins are still held, so the
   construction raises — reproducing 1.2/1.3.

**Expected Counterexamples**:
- After `stop_streaming()`, a single further callback leaves `jaw_motor` /
  `led_eye_light` non-`None` (pins re-claimed in-process).
- A subsequent fake-device construction on GPIO6 raises `GPIO busy`.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
code frees the pins and the Routine can claim them.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := performHandoff_fixed(X)
  ASSERT audio_player_constructed_without_error(result)   // no lgpio 'GPIO busy'
     AND audible(result.routine_audio)
     AND jaw_and_eyes_driven(result)
     AND holds_across_repeated_handoffs(result)           // no service restart
END FOR
```

Concretely: after the fix, firing the captured callback post-`stop_streaming()`
leaves the module globals `None` (change 1), and constructing fresh fake devices
on GPIO6/GPIO15 succeeds. Repeating start → stop → claim across several cycles
succeeds every time.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed code behaves identically to the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT performHandoff_original(X) = performHandoff_fixed(X)
END FOR
```

**Testing Approach**: Property-based testing (`hypothesis`, per the repo PBT
convention) is well suited to preservation checking: it generates many chunk
sequences and jaw-tuning profiles automatically and gives strong confidence that
the mic-while-streaming envelope behavior is unchanged. Effect randomness comes
from `numpy`/deterministic math (no `random` module), so pin seeds where a test
needs reproducibility.

**Test Plan**: Observe UNFIXED behavior for the non-handoff cases first (a steady
in-stream `talk()` sequence, the orderly teardown call order, the GPIO release),
then assert it is unchanged after the fix.

**Test Cases**:
1. **Streaming drive preservation (3.1):** With `is_streaming` true, a sequence
   of `talk()` calls lazily creates the devices on first frame and drives
   on/off from the envelope exactly as the unfixed code (the `is_streaming`
   guard is transparent while streaming).
2. **Orderly teardown preservation (3.3):** `stop_streaming()` still records
   `stop_stream()` → `close()` → `terminate()` in order and still calls
   `_reset_audio_dsp_state()`; the only observable change is that
   `_release_gpio()` runs after the stream is closed and the callback is dead.
3. **GPIO-release preservation (3.3):** `_release_gpio()` still `off()`s +
   `close()`s both devices and nulls the globals.
4. **Preset load/persist preservation (3.4):** Selecting / saving / reverting a
   style still resolves saved-override-else-factory and persists via
   `ConfigStore`, identical to before (the fix does not touch `set_effects()`).
5. **Mic preempt preservation (3.5):** Requesting a Routine/Mode while streaming
   still auto-stops the mic (the stop-path change does not alter the preempt
   trigger).

### Unit Tests

- `stop_streaming()` teardown ordering: `is_streaming=False` → thread join →
  `stop_stream()` → `close()` → `terminate()` → `_release_gpio()` →
  `_reset_audio_dsp_state()`, via the fake PyAudio.
- `talk()` guarded on `is_streaming`: does NOT construct devices when
  `is_streaming` is false; DOES construct and drive them when true.
- `_release_gpio()` closes both fake devices and nulls `jaw_motor` /
  `led_eye_light`.
- A fresh fake-device construction on GPIO6/GPIO15 succeeds after
  `stop_streaming()` (simulating the Routine subprocess).

### Property-Based Tests

- For random in-stream chunk sequences (with `is_streaming` true), the guarded
  `talk()` jaw/eye on/off decisions equal the unfixed `talk()` decisions
  (preservation 3.1).
- For random repeated start → stop → claim cycles, the Routine-subprocess claim
  succeeds on every cycle (fix checking 2.4) and the module globals are `None`
  after each stop.

### Integration Tests

- Full start → (post-stop straggler callback) → stop → simulated Routine claim
  on fake hardware: after the fix the module globals are `None` and the Routine
  claim succeeds; without the fix the claim raises `GPIO busy`.
- Repeated mic → routine → mic → routine hand-offs: the Routine claim succeeds
  on every hand-off with no "service restart" (no process re-import), confirming
  2.4.
