"""Bug-condition exploration test for the Voice-FX audio dropout fix.

Spec: .kiro/specs/voice-fx-audio-dropout-fix  (Task 1)

PURPOSE
-------
This is a BUG-CONDITION EXPLORATION test. It encodes the Expected Behavior
(bugfix.md 2.1-2.4) for the confirmed root cause: a **cross-process GPIO
ownership leak** in the long-lived mic service process
(``src/micwebcontroller.py``). After a live mic session ends, a straggler
PortAudio callback can still run ``talk()`` and re-create the module-global
``jaw_motor`` (``DigitalOutputDevice(MOUTH_MOTOR_PIN)`` / GPIO15) and
``led_eye_light`` (``LED(EYE_LIGHT_PIN)`` / GPIO6) **after** ``_release_gpio()``
has freed them -- so the mic process keeps owning GPIO6/GPIO15 for its lifetime,
and a separate ``animatronic.py`` Routine subprocess's ``AudioPlayer.__init__``
raises ``lgpio 'GPIO busy'`` when it tries to claim the same pins.

This test MUST FAIL on the current (unfixed) code -- that failure confirms the
bug exists, and the counterexamples it surfaces confirm the "PortAudio callback
re-creating devices after stop" mechanism in design.md ("Hypothesized Root
Cause"). After the fix (task 3) the SAME test validates the repair (task 3.5)
and must PASS unchanged.

The prior contents of this file targeted a now-REFUTED ALSA/DSP-teardown cause
(see bugfix.md "Note on the previously-investigated (now refuted) cause"); they
have been overwritten to target the real GPIO leak.

Scoped PBT approach
-------------------
The leak is a deterministic race, so the test scopes the property to the one
concrete failing hand-off (bugfix.md ``isBugCondition``): one mic session, one
straggler PortAudio callback after stop, one Routine claim of GPIO6/GPIO15 --
rather than a broad generated input space. A counterexample here is a concrete,
reproducible call sequence.

OBSERVED COUNTEREXAMPLES ON UNFIXED CODE (recorded this run)
------------------------------------------------------------
Assertion A (bugfix.md 1.1 -> 2.1) -- post-stop callback re-creates the devices:
  * Counterexample: after ``start_streaming()`` -> ``stop_streaming()`` ->
    (one straggler ``audio_callback`` fired), ``micwebcontroller.jaw_motor`` and
    ``micwebcontroller.led_eye_light`` are NON-``None`` again. ``_release_gpio()``
    had nulled + ``close()``d them, but the straggler ``talk()`` re-created fresh
    ``DigitalOutputDevice(15)`` / ``LED(6)`` because unfixed ``talk()`` consults
    neither ``is_streaming`` nor any stop flag -- it re-creates whenever the
    globals are ``None``.
    => CONFIRMS mechanism #1: "PortAudio callback re-creating devices after
       stop". The pins are re-claimed in-process after release.

Assertion B (bugfix.md 1.2 / 1.3 -> 2.2) -- Routine claim after the mic session:
  * Counterexample: constructing a fresh ``LED(EYE_LIGHT_PIN=6)`` /
    ``DigitalOutputDevice(MOUTH_MOTOR_PIN=15)`` (standing in for the Routine
    subprocess's ``AudioPlayer.__init__``) raises the simulated
    ``GPIOBusyError: GPIO busy: pin 6 already claimed`` -- because the straggler
    callback left the mic process owning GPIO6 (and GPIO15).
    => CONFIRMS 1.2/1.3: the Routine's ``AudioPlayer.__init__`` cannot claim the
       pins, so no Routine audio plays and the jaw/eyes never move.

Together these confirm the design's primary hypothesis: a late PortAudio
callback re-creates the GPIO devices via ``talk()`` after ``_release_gpio()``,
leaving GPIO6/GPIO15 claimed by the mic service process until it is killed.

Run quietly:
    pytest tests/test_voice_fx_dropout.py -q --maxfail=1
"""

import os
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

# Ensure src/ is importable when pytest runs from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# ---------------------------------------------------------------------------
# Install hardware/dependency stubs BEFORE importing micwebcontroller.
# Mirrors the stub pattern in tests/test_micwebcontroller.py: micwebcontroller
# imports pyaudio + gpiozero at module top and builds a module-level
# ConfigStore() at import time, which would fail with no sound card / GPIO.
#
# Difference from test_micwebcontroller.py: here the fakes are not bare
# MagicMocks. The fake gpiozero MODELS PIN OWNERSHIP (so a second claim of an
# already-held pin raises a simulated 'GPIO busy', standing in for the Routine
# subprocess), and the fake PyAudio CAPTURES the stream_callback so the test can
# fire a straggler callback by hand after stop.
# ---------------------------------------------------------------------------

# Point the jaw-config store at a temp path from the first import so the
# module-level ConfigStore() never touches a real home-directory file.
os.environ.setdefault(
    "ANIMATRONIC_JAW_CONFIG",
    os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        ".pytest_import_jaw_config.json",
    ),
)


def _install_stub(name, module):
    if name not in sys.modules:
        sys.modules[name] = module


# ---- Fake gpiozero that models cross-process pin ownership ------------------
# A single process-wide registry records which pin each LIVE device holds.
# Constructing a device "claims" its pin and raises GPIOBusyError if the pin is
# already held by a live device; close() releases it. This lets the test stand
# in for the Routine subprocess's AudioPlayer claiming the same GPIO6/GPIO15 the
# mic process is (incorrectly) still holding.


class GPIOBusyError(Exception):
    """Simulated lgpio 'GPIO busy' raised when a claimed pin is re-claimed."""


# pin -> the live device instance currently owning it.
_PIN_OWNERS = {}


class _FakePinDevice:
    """Base fake gpiozero device modelling exclusive pin ownership."""

    def __init__(self, pin, **kwargs):
        self.pin = pin
        self.closed = False
        self.off_called = False
        self.on_called = False
        if pin in _PIN_OWNERS:
            # Mirrors lgpio.error: 'GPIO busy' when another live device (here
            # the mic process) still owns the pin.
            raise GPIOBusyError(f"GPIO busy: pin {pin} already claimed")
        _PIN_OWNERS[pin] = self

    def on(self):
        self.on_called = True

    def off(self):
        self.off_called = True

    def close(self):
        # Idempotent: releases the pin only if this device still owns it.
        if not self.closed and _PIN_OWNERS.get(self.pin) is self:
            del _PIN_OWNERS[self.pin]
        self.closed = True


class _FakeLED(_FakePinDevice):
    """Fake gpiozero.LED(pin)."""


class _FakeDigitalOutputDevice(_FakePinDevice):
    """Fake gpiozero.DigitalOutputDevice(pin)."""


_gpiozero_stub = types.ModuleType("gpiozero")
_gpiozero_stub.PWMLED = MagicMock(name="PWMLED")
_gpiozero_stub.LED = _FakeLED
_gpiozero_stub.DigitalOutputDevice = _FakeDigitalOutputDevice
_gpiozero_stub.GPIOBusyError = GPIOBusyError
_install_stub("gpiozero", _gpiozero_stub)


# ---- Fake PyAudio that captures the stream_callback -------------------------
# open() captures the stream_callback (micwebcontroller's audio_callback) so the
# test can fire a straggler callback manually AFTER stop_streaming() returns,
# standing in for a PortAudio callback still in flight on its own thread.


class _FakeStream:
    """Fake PyAudio stream recording start/stop/close and holding the callback."""

    def __init__(self):
        self.callback = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start_stream(self):
        self.started = True

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def is_active(self):
        return self.started and not self.stopped


class _FakePyAudio:
    """Fake pyaudio.PyAudio(): open() captures the callback and returns a stream."""

    def __init__(self):
        self.last_stream = None
        self.terminated = False

    def open(self, *args, **kwargs):
        stream = _FakeStream()
        stream.callback = kwargs.get("stream_callback")
        self.last_stream = stream
        return stream

    def terminate(self):
        self.terminated = True

    def get_default_output_device_info(self):
        return {"index": 2}


_pyaudio_stub = types.ModuleType("pyaudio")
_pyaudio_stub.paInt16 = 8
_pyaudio_stub.paContinue = 0
_pyaudio_stub.paComplete = 1
_pyaudio_stub.PyAudio = lambda: _FakePyAudio()
_install_stub("pyaudio", _pyaudio_stub)

# --- utils.audio_utils stub: try real first, fall back to a stub. ------------
try:  # pragma: no cover - depends on environment
    import utils.audio_utils  # noqa: F401
except Exception:  # pragma: no cover - fall back to a stub
    _utils_pkg = sys.modules.get("utils")
    if _utils_pkg is None:
        _utils_pkg = types.ModuleType("utils")
        _utils_pkg.__path__ = []
        sys.modules["utils"] = _utils_pkg
    _audio_utils_stub = types.ModuleType("utils.audio_utils")
    _audio_utils_stub.AudioUtils = MagicMock(name="AudioUtils")
    sys.modules["utils.audio_utils"] = _audio_utils_stub

# numpy is a real runtime dependency and safe to import; no stub needed.

import micwebcontroller  # noqa: E402
from constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset micwebcontroller globals + the pin-owner registry per test.

    hypothesis/pytest reuse the process, so the module globals (audio_state,
    the jaw_motor / led_eye_light device handles, jaw envelope) and the
    process-wide _PIN_OWNERS registry persist across tests. Zero them at the
    START of each test so each starts from a known, isolated state with no pins
    held by anyone.
    """
    _PIN_OWNERS.clear()
    micwebcontroller.jaw_motor = None
    micwebcontroller.led_eye_light = None
    micwebcontroller.jaw_open = False
    micwebcontroller.below_count = 0
    micwebcontroller.avg_level = 0.0
    micwebcontroller.audio_state.update(
        {
            "stream": None,
            "audio": None,
            "is_streaming": False,
            "thread": None,
            "is_recording": False,
            "record_frames": [],
        }
    )
    # start_streaming()/stop_streaming() build their JSON responses with
    # jsonify(), which needs a Flask application context. Push one for the whole
    # test so the functions run as they do under a request.
    with micwebcontroller.app.app_context():
        yield


def _one_chunk():
    """A single loud int16 input chunk (drives the jaw open in talk())."""
    t = np.arange(micwebcontroller.CHUNK)
    tone = 12000.0 * np.sin(2 * np.pi * 220.0 * t / micwebcontroller.RATE)
    return tone.astype(np.int16).tobytes()


def _fire_callback(stream):
    """Invoke the captured PortAudio callback once with one input chunk.

    Stands in for a single PortAudio callback firing on its own internal thread
    (which runs micwebcontroller's audio_callback -> talk() -> apply_effects()).
    """
    assert stream is not None and stream.callback is not None, (
        "fake stream did not capture a stream_callback"
    )
    in_data = _one_chunk()
    stream.callback(in_data, micwebcontroller.CHUNK, {}, 0)


# ---------------------------------------------------------------------------
# Property 1: Bug Condition — Routine Claims GPIO6/GPIO15 After a Mic Session
# ---------------------------------------------------------------------------


def test_bug_condition_mic_process_releases_gpio_after_stop():
    """Bug Condition / Expected Behavior: mic process frees GPIO6/GPIO15 on stop.

    Simulate the exact ``isBugCondition`` hand-off (bugfix.md):
      1. a live mic session runs (start_streaming -> worker opens the callback
         stream -> a callback fires during the stream, legitimately claiming
         GPIO6/GPIO15 via talk());
      2. the session is stopped (stop_streaming -> _release_gpio frees the pins);
      3. ONE straggler PortAudio callback fires AFTER stop_streaming() returns
         (a callback still in flight on PortAudio's own thread); and
      4. a separate Routine subprocess (its AudioPlayer.__init__) then claims
         the same GPIO6/GPIO15.

    Assertion A (bugfix.md 1.1 -> 2.1): after the straggler callback the module
    globals jaw_motor / led_eye_light are None and were off()+close()d during
    _release_gpio(), so the mic process holds neither pin.

    Assertion B (bugfix.md 1.2 / 1.3 -> 2.2): the fresh Routine-subprocess
    LED(EYE_LIGHT_PIN) / DigitalOutputDevice(MOUTH_MOTOR_PIN) construction
    succeeds with no simulated 'GPIO busy'.

    EXPECTED ON UNFIXED CODE: FAILS. Unfixed talk() consults neither is_streaming
    nor any stop flag, so the straggler callback re-creates the devices
    (globals non-None, pins re-claimed), and the Routine claim raises the
    simulated GPIO busy. The documented counterexamples (file header) confirm the
    "PortAudio callback re-creating devices after stop" mechanism in design.md.

    Validates: Requirements 1.1, 1.2, 1.3, 1.4 (Bug Condition);
               asserts 2.1, 2.2, 2.3, 2.4 (Expected Behavior)
    """
    # --- (1) Live mic session: open the callback stream on the worker thread.
    micwebcontroller.start_streaming()
    thread = micwebcontroller.audio_state.get("thread")
    if thread is not None:
        thread.join(timeout=5)

    pa = micwebcontroller.audio_state.get("audio")
    stream = micwebcontroller.audio_state.get("stream")
    assert pa is not None, "PyAudio instance should have been created"
    assert stream is not None, "stream handle should have been captured"

    # A callback fires DURING the live stream: talk() legitimately claims
    # GPIO6/GPIO15 while is_streaming is True (preservation 3.1 — this part is
    # correct and must keep working after the fix).
    _fire_callback(stream)
    jaw_during = micwebcontroller.jaw_motor
    led_during = micwebcontroller.led_eye_light
    assert jaw_during is not None and led_during is not None, (
        "talk() should claim GPIO6/GPIO15 while the stream is live"
    )

    # --- (2) Stop the mic session. _release_gpio() frees the pins in-process.
    micwebcontroller.stop_streaming()

    # The devices created DURING the stream must have been off()+close()d by
    # _release_gpio() (pin ownership relinquished for the Routine subprocess).
    assert jaw_during.off_called and jaw_during.closed, (
        "jaw_motor should be off()+close()d by _release_gpio() on stop"
    )
    assert led_during.off_called and led_during.closed, (
        "led_eye_light should be off()+close()d by _release_gpio() on stop"
    )

    # --- (3) A straggler PortAudio callback fires AFTER stop_streaming().
    # On the unfixed code talk() has no guard, so it re-creates fresh
    # DigitalOutputDevice(15) / LED(6) because the globals are None again,
    # re-claiming the pins the mic process just released.
    _fire_callback(stream)

    # ---- Assertion A (1.1 -> 2.1): the mic process holds NEITHER pin. --------
    # COUNTEREXAMPLE (unfixed): the straggler callback re-created the devices,
    # so these globals are non-None again and GPIO6/GPIO15 are re-claimed.
    assert micwebcontroller.jaw_motor is None, (
        "COUNTEREXAMPLE: a straggler post-stop callback re-created jaw_motor "
        f"(= {micwebcontroller.jaw_motor!r}), re-claiming MOUTH_MOTOR_PIN "
        f"(GPIO{MOUTH_MOTOR_PIN}) after _release_gpio() freed it — confirms the "
        "'callback re-creating devices after stop' mechanism (design.md #1)."
    )
    assert micwebcontroller.led_eye_light is None, (
        "COUNTEREXAMPLE: a straggler post-stop callback re-created led_eye_light "
        f"(= {micwebcontroller.led_eye_light!r}), re-claiming EYE_LIGHT_PIN "
        f"(GPIO{EYE_LIGHT_PIN}) after _release_gpio() freed it — confirms the "
        "'callback re-creating devices after stop' mechanism (design.md #1)."
    )

    # ---- Assertion B (1.2 / 1.3 -> 2.2): Routine subprocess can claim pins. --
    # Stand in for animatronic.py's AudioPlayer.__init__ claiming GPIO6/GPIO15
    # up front. On the unfixed code the mic process still owns them, so this
    # raises the simulated 'GPIO busy'.
    gpiozero = sys.modules["gpiozero"]
    try:
        routine_led = gpiozero.LED(EYE_LIGHT_PIN)
        routine_jaw = gpiozero.DigitalOutputDevice(MOUTH_MOTOR_PIN)
    except GPIOBusyError as exc:  # pragma: no cover - the unfixed-code path
        pytest.fail(
            "COUNTEREXAMPLE: the Routine subprocess's AudioPlayer.__init__ could "
            f"not claim its GPIO pin — {exc}. The mic process still owns "
            f"GPIO{EYE_LIGHT_PIN}/GPIO{MOUTH_MOTOR_PIN} after the mic session, so "
            "lgpio 'GPIO busy' kills the performance audio thread: no Routine "
            "audio and the jaw/eyes never move (confirms bugfix.md 1.2/1.3)."
        )

    # On the fixed code we reach here: clean up the Routine-owned pins so the
    # per-test registry is left empty.
    routine_led.close()
    routine_jaw.close()


# ===========================================================================
# Task 2 — Property 2: Preservation
#   Streaming Drive, First-Run, and Safety Paths Unchanged
# ===========================================================================
#
# These tests pin the behaviour that must stay IDENTICAL after the fix (the
# baseline to preserve). They follow the observation-first methodology: each
# was authored by running the UNFIXED code for non-bug-condition inputs
# (isBugCondition(X) == False) and asserting the OBSERVED outputs. All of them
# MUST PASS on the unfixed code. They reuse the sys.modules fakes installed at
# the top of this file (fake pyaudio capturing the stream_callback; fake
# gpiozero modelling GPIO6/GPIO15 pin ownership) plus the _reset_module_state
# fixture, _one_chunk(), and _fire_callback() helpers above, and add the Flask
# test-client / temp-ConfigStore pattern from tests/test_micwebcontroller.py for
# the /effects endpoints.
#
# OBSERVED BASELINE OUTPUTS (recorded this run against the UNFIXED code):
#   * Streaming-drive (3.1): with is_streaming True, talk() creates the devices
#     on the FIRST frame and its per-frame jaw_open decision sequence equals an
#     independent re-implementation of the same adaptive-envelope algorithm (the
#     differential oracle `_reference_jaw_decisions` below). Both jaw_motor and
#     led_eye_light receive on()/off() in lockstep with jaw_open. Note the
#     envelope opens the jaw only when the level RISES above open_ratio*avg; a
#     steady tone keeps it closed (avg tracks the level), which the oracle
#     reproduces exactly.
#   * Teardown ordering (3.3): stop_streaming() records, in order,
#     stop_stream -> close (on the live stream) -> terminate (on the PyAudio
#     instance), then runs _release_gpio(), then _reset_audio_dsp_state().
#   * GPIO release (3.3): after a normal start/stop (no straggler callback) both
#     fake devices are off()+close()d and jaw_motor / led_eye_light are None.
#   * First-run (3.2): with no prior mic session, constructing LED(EYE_LIGHT_PIN)
#     / DigitalOutputDevice(MOUTH_MOTOR_PIN) succeeds (no simulated GPIO busy).
#   * Effects (3.4): POST /effects {"style": s} loads the saved override else
#     the factory preset; /effects/save persists the live chain under the style
#     via ConfigStore; /effects/revert restores the prior saved snapshot. The
#     observed resolve-order is saved-override-else-factory, and effects_config /
#     saved_voice_styles / current_style reflect it.
#   * Mic preempt (3.5): POST /handler {"action": "stop"} dispatches to
#     stop_streaming() and leaves is_streaming False (the preempt trigger the
#     webapp relies on). The stop-path change does not alter this dispatch.
#   * Servo safety (3.6): verified by INSPECTION ONLY (see the final test's
#     docstring) — this spec touches no servo code; SERVO_SIM is NOT run.
#
# Run quietly:
#     pytest tests/test_voice_fx_dropout.py -q

import tempfile  # noqa: E402

from hypothesis import given, settings, strategies as st  # noqa: E402

from config_store import ConfigStore, PROFILE_MIC  # noqa: E402


# ---------------------------------------------------------------------------
# Differential oracle for the streaming-drive property (3.1)
# ---------------------------------------------------------------------------


def _reference_jaw_decisions(chunks, profile):
    """Independent re-implementation of talk()'s adaptive-envelope decision.

    Mirrors the exact algorithm in micwebcontroller.talk() (silence floor, EMA
    running average, open/close hysteresis ratios, and the close-hold debounce)
    but as a pure function with NO GPIO / module-global side effects. Used as a
    differential oracle: the real guarded talk() must reproduce this per-frame
    ``jaw_open`` sequence byte-for-byte while ``is_streaming`` is true, proving
    the ``is_streaming`` guard is transparent during a live stream (3.1).

    Args:
        chunks: An ordered iterable of float32 input chunks (int16 range).
        profile: The mic jaw profile dict (silence_floor, open_ratio,
            close_ratio, ema_alpha, close_hold_frames).

    Returns:
        A list of booleans, the ``jaw_open`` state after each chunk.
    """
    silence_floor = profile["silence_floor"]
    open_ratio = profile["open_ratio"]
    close_ratio = profile["close_ratio"]
    ema_alpha = profile["ema_alpha"]
    close_hold_frames = profile["close_hold_frames"]

    jaw_open = False
    below_count = 0
    avg_level = 0.0
    decisions = []
    for data in chunks:
        data = np.asarray(data, dtype=np.float32)
        level = float(np.sqrt(np.mean(data * data))) if data.size else 0.0

        if level >= silence_floor:
            if avg_level <= 0.0:
                avg_level = level
            else:
                avg_level += ema_alpha * (level - avg_level)

        open_thresh = open_ratio * avg_level
        close_thresh = close_ratio * avg_level

        if not jaw_open:
            if level >= silence_floor and level >= open_thresh:
                jaw_open = True
                below_count = 0
        else:
            if level < silence_floor or level < close_thresh:
                below_count += 1
                if below_count >= close_hold_frames:
                    jaw_open = False
                    below_count = 0
            else:
                below_count = 0
        decisions.append(jaw_open)
    return decisions


def _tone_chunk(amplitude):
    """Build one float32 input chunk: a 220 Hz tone at the given amplitude.

    Effects/envelope math is deterministic numpy (no ``random`` module), so a
    fixed tone shape with a hypothesis-chosen amplitude is reproducible.

    Args:
        amplitude: Peak amplitude in int16 range (0..32767).

    Returns:
        A float32 ndarray of length ``micwebcontroller.CHUNK``.
    """
    t = np.arange(micwebcontroller.CHUNK)
    tone = float(amplitude) * np.sin(2 * np.pi * 220.0 * t / micwebcontroller.RATE)
    return tone.astype(np.int16).astype(np.float32)


# Amplitudes span silence (< silence_floor=600) through loud, so generated
# sequences exercise jaw open, close, the close-hold debounce, and the silence
# gate — not just one branch.
_amplitude = st.integers(min_value=0, max_value=20000)


@settings(max_examples=120, deadline=None)
@given(amplitudes=st.lists(_amplitude, min_size=1, max_size=40))
def test_preservation_streaming_drive_matches_envelope_oracle(amplitudes):
    """Preservation 3.1: in-stream talk() jaw/eye drive is unchanged.

    With ``is_streaming`` true, feeding an arbitrary in-stream chunk sequence
    through ``talk()`` lazily creates the GPIO devices on the FIRST frame and
    produces exactly the jaw_open decision sequence of the independent envelope
    oracle, and drives jaw_motor (GPIO15) and led_eye_light (GPIO6) on/off in
    lockstep with that decision. The ``is_streaming`` guard added by the fix is
    transparent while streaming, so this baseline must hold identically on the
    unfixed and fixed code.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.1 (Preservation)
    """
    # Fresh per-example state (hypothesis reuses the process across examples).
    _PIN_OWNERS.clear()
    micwebcontroller.jaw_motor = None
    micwebcontroller.led_eye_light = None
    micwebcontroller.jaw_open = False
    micwebcontroller.below_count = 0
    micwebcontroller.avg_level = 0.0
    micwebcontroller.audio_state["is_streaming"] = True

    profile = micwebcontroller.jaw_profiles[PROFILE_MIC]
    chunks = [_tone_chunk(a) for a in amplitudes]
    expected = _reference_jaw_decisions(chunks, profile)

    observed = []
    created_on_first_frame = None
    for i, chunk in enumerate(chunks):
        micwebcontroller.talk(chunk, start_time=0.0)
        if created_on_first_frame is None:
            # Lazy creation happens on the first frame.
            created_on_first_frame = (
                micwebcontroller.jaw_motor is not None
                and micwebcontroller.led_eye_light is not None
            )
        observed.append(micwebcontroller.jaw_open)

    # Devices were lazily created on the first in-stream frame (3.1).
    assert created_on_first_frame is True, (
        "talk() should lazily create jaw_motor/led_eye_light on the first "
        "in-stream frame"
    )
    # The real guarded talk() reproduces the oracle decision sequence exactly.
    assert observed == expected, (
        "in-stream talk() jaw decisions diverged from the envelope oracle; "
        f"amplitudes={amplitudes} observed={observed} expected={expected}"
    )

    # Both devices are driven on the same pins talk() owns while streaming, and
    # received at least one on()/off() consistent with the decision sequence.
    assert micwebcontroller.jaw_motor.pin == MOUTH_MOTOR_PIN
    assert micwebcontroller.led_eye_light.pin == EYE_LIGHT_PIN
    if any(expected):
        assert micwebcontroller.jaw_motor.on_called
        assert micwebcontroller.led_eye_light.on_called
    if not all(expected):
        assert micwebcontroller.jaw_motor.off_called
        assert micwebcontroller.led_eye_light.off_called


def test_preservation_first_run_routine_claim_succeeds():
    """Preservation 3.2: a first-run Routine claim (no prior mic session) works.

    Standing in for a freshly started service with no prior mic session,
    constructing the fake ``LED(EYE_LIGHT_PIN)`` / ``DigitalOutputDevice(
    MOUTH_MOTOR_PIN)`` (as ``AudioPlayer.__init__`` would, up front) with no
    live mic owner succeeds — no simulated 'GPIO busy'. The known-good first-run
    behaviour is unaffected by the fix.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.2 (Preservation)
    """
    # No mic session has run: the module globals are None and no pin is owned
    # (guaranteed by the _reset_module_state fixture clearing _PIN_OWNERS).
    assert micwebcontroller.jaw_motor is None
    assert micwebcontroller.led_eye_light is None
    assert _PIN_OWNERS == {}

    gpiozero = sys.modules["gpiozero"]
    # AudioPlayer.__init__ claims the eye LED then the jaw motor up front.
    led = gpiozero.LED(EYE_LIGHT_PIN)
    jaw = gpiozero.DigitalOutputDevice(MOUTH_MOTOR_PIN)

    assert led.pin == EYE_LIGHT_PIN
    assert jaw.pin == MOUTH_MOTOR_PIN
    # Both pins are now owned by the (stand-in) Routine process.
    assert _PIN_OWNERS.get(EYE_LIGHT_PIN) is led
    assert _PIN_OWNERS.get(MOUTH_MOTOR_PIN) is jaw

    # Clean up so the per-test registry is left empty.
    led.close()
    jaw.close()


def test_preservation_orderly_teardown_ordering():
    """Preservation 3.3: stop_streaming() keeps the orderly teardown order.

    ``start_streaming()`` then ``stop_streaming()`` still records, in order,
    ``stop_stream()`` -> ``close()`` on the LIVE stream BEFORE ``terminate()``
    on the PyAudio instance, and still calls ``_reset_audio_dsp_state()`` on the
    way out. The only change the fix makes is that ``_release_gpio()`` runs after
    the stream is closed — the teardown calls themselves are unchanged.

    Observed order on unfixed code:
        stop_stream -> close -> terminate -> _release_gpio -> _reset_audio_dsp_state

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.3 (Preservation)
    """
    micwebcontroller.start_streaming()
    thread = micwebcontroller.audio_state.get("thread")
    if thread is not None:
        thread.join(timeout=5)

    stream = micwebcontroller.audio_state["stream"]
    pa = micwebcontroller.audio_state["audio"]
    assert stream is not None and pa is not None

    events = []

    # Instrument the live stream + PyAudio + the DSP reset with a shared log.
    orig_stop_stream = stream.stop_stream
    orig_close = stream.close
    orig_terminate = pa.terminate
    orig_reset = micwebcontroller._reset_audio_dsp_state

    def _log_stop_stream():
        events.append("stop_stream")
        return orig_stop_stream()

    def _log_close():
        events.append("close")
        return orig_close()

    def _log_terminate():
        events.append("terminate")
        return orig_terminate()

    def _log_reset():
        events.append("_reset_audio_dsp_state")
        return orig_reset()

    stream.stop_stream = _log_stop_stream
    stream.close = _log_close
    pa.terminate = _log_terminate
    micwebcontroller._reset_audio_dsp_state = _log_reset
    try:
        micwebcontroller.stop_streaming()
    finally:
        micwebcontroller._reset_audio_dsp_state = orig_reset

    # Orderly stream teardown before terminate (the already-applied, preserved
    # behaviour), and _reset_audio_dsp_state() still called last.
    assert events[:3] == ["stop_stream", "close", "terminate"], (
        f"teardown order changed: {events}"
    )
    assert "_reset_audio_dsp_state" in events, (
        "stop_streaming() must still call _reset_audio_dsp_state()"
    )
    # terminate precedes the DSP reset (reset is on the way out).
    assert events.index("terminate") < events.index("_reset_audio_dsp_state")


def test_preservation_gpio_release_on_normal_stop():
    """Preservation 3.3: _release_gpio() still off()+close()es and nulls both.

    After a NORMAL ``start_streaming()`` / ``stop_streaming()`` (no straggler
    post-stop callback), ``_release_gpio()`` still turns off and closes both
    fake devices created during the stream and nulls ``jaw_motor`` /
    ``led_eye_light``. This is the in-process pin release, unchanged by the fix.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.3 (Preservation)
    """
    micwebcontroller.start_streaming()
    thread = micwebcontroller.audio_state.get("thread")
    if thread is not None:
        thread.join(timeout=5)

    stream = micwebcontroller.audio_state["stream"]
    assert stream is not None

    # Fire one in-stream callback so talk() creates the devices (is_streaming is
    # still True here — this is the normal, legitimate claim).
    _fire_callback(stream)
    jaw = micwebcontroller.jaw_motor
    led = micwebcontroller.led_eye_light
    assert jaw is not None and led is not None

    micwebcontroller.stop_streaming()

    # _release_gpio() turned both off, closed them (freeing GPIO15/GPIO6), and
    # nulled the module globals.
    assert jaw.off_called and jaw.closed
    assert led.off_called and led.closed
    assert micwebcontroller.jaw_motor is None
    assert micwebcontroller.led_eye_light is None
    # The pins are free again in-process.
    assert MOUTH_MOTOR_PIN not in _PIN_OWNERS
    assert EYE_LIGHT_PIN not in _PIN_OWNERS


# ---------------------------------------------------------------------------
# Effect-chain load / persist / revert (3.4) — Flask test client + temp store
# ---------------------------------------------------------------------------


@pytest.fixture
def effects_client(tmp_path):
    """Flask test client with a fresh temp ConfigStore for the /effects routes.

    Mirrors the temp-store fixture pattern in tests/test_micwebcontroller.py:
    point micwebcontroller.config_store at a unique temp tuning.json and reset
    the in-memory voice-style caches so each test starts from a clean,
    isolated persistence state. Yields app.test_client().
    """
    config_path = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_path))
    micwebcontroller.config_store = store
    micwebcontroller.saved_voice_styles = store.load_voice_styles()
    micwebcontroller.current_style = {"value": None}
    # Reset the live chain to the module default so saves start from a known base.
    micwebcontroller.effects_config = {
        name: dict(params)
        for name, params in micwebcontroller.STYLE_PRESETS["ghost"].items()
    }

    micwebcontroller.app.config["TESTING"] = True
    with micwebcontroller.app.test_client() as client:
        client._tuning_path = str(config_path)
        yield client


def test_preservation_effects_select_resolves_factory_then_saved_override(effects_client):
    """Preservation 3.4: selecting a style resolves saved-override-else-factory.

    Selecting a style with no saved override loads the factory preset into the
    live ``effects_config`` and sets ``current_style``. After saving a tuned
    version, re-selecting that style loads the SAVED override instead — the
    observed saved-override-else-factory resolution, persisted via ConfigStore.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.4 (Preservation)
    """
    # Select 'ghost' with nothing saved yet -> factory preset.
    resp = effects_client.post("/effects", json={"style": "ghost"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == "ghost"
    assert payload["tuned"] is False
    assert micwebcontroller.current_style["value"] == "ghost"
    factory_ghost = {
        name: dict(params)
        for name, params in micwebcontroller.STYLE_PRESETS["ghost"].items()
    }
    assert micwebcontroller.effects_config == factory_ghost

    # Tune the live chain and SAVE it under 'ghost'.
    micwebcontroller.effects_config["echo"]["amount"] = 0.99
    resp = effects_client.post("/effects/save", json={"style": "ghost"})
    assert resp.status_code == 200
    assert "ghost" in micwebcontroller.saved_voice_styles
    assert micwebcontroller.saved_voice_styles["ghost"]["echo"]["amount"] == 0.99
    # Persisted to disk via ConfigStore.
    assert ConfigStore(config_path=effects_client._tuning_path).load_voice_styles()[
        "ghost"
    ]["echo"]["amount"] == 0.99

    # Select away, then back to 'ghost' -> now resolves the SAVED override.
    effects_client.post("/effects", json={"style": "natural"})
    resp = effects_client.post("/effects", json={"style": "ghost"})
    assert resp.status_code == 200
    assert resp.get_json()["tuned"] is True
    assert micwebcontroller.effects_config["echo"]["amount"] == 0.99


def test_preservation_effects_revert_restores_previous(effects_client):
    """Preservation 3.4: reverting restores the prior saved snapshot.

    Saving a style snapshots its prior value; a subsequent revert restores that
    prior value (the factory preset when the save first CREATED the override),
    applies it to the live chain, and re-persists it — unchanged by the fix.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.4 (Preservation)
    """
    # Create a saved override for 'ghost' (prior snapshot = factory, since none).
    effects_client.post("/effects", json={"style": "ghost"})
    micwebcontroller.effects_config["echo"]["amount"] = 0.99
    effects_client.post("/effects/save", json={"style": "ghost"})

    # Revert -> back to the factory 'ghost' echo amount (0.65), applied + saved.
    resp = effects_client.post("/effects/revert", json={})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == "ghost"
    factory_echo = micwebcontroller.STYLE_PRESETS["ghost"]["echo"]["amount"]
    assert payload["effects"]["echo"]["amount"] == factory_echo
    # Applied to the live chain.
    assert micwebcontroller.effects_config["echo"]["amount"] == factory_echo
    # Re-persisted via ConfigStore.
    assert ConfigStore(config_path=effects_client._tuning_path).load_voice_styles()[
        "ghost"
    ]["echo"]["amount"] == factory_echo


def test_preservation_mic_preempt_handler_stop_calls_stop_streaming():
    """Preservation 3.5: the preempt trigger still routes to stop_streaming().

    Requesting a Routine/Mode while the mic streams auto-preempts the mic: the
    webapp's preempt path posts ``/handler {"action": "stop"}`` to this
    controller, which dispatches to ``stop_streaming()``. After a live start,
    that POST stops the stream (``is_streaming`` False) — the trigger the stop
    path relies on is unchanged by the fix.

    EXPECTED: PASSES on unfixed code.

    Validates: Requirements 3.5 (Preservation)
    """
    micwebcontroller.start_streaming()
    thread = micwebcontroller.audio_state.get("thread")
    if thread is not None:
        thread.join(timeout=5)
    assert micwebcontroller.audio_state["is_streaming"] is True

    with micwebcontroller.app.test_client() as client:
        resp = client.post("/handler", json={"action": "stop"})

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    # The preempt trigger reached stop_streaming(): the mic is no longer live.
    assert micwebcontroller.audio_state["is_streaming"] is False
    # And the pins were released in-process on the preempt-driven stop.
    assert micwebcontroller.jaw_motor is None
    assert micwebcontroller.led_eye_light is None


def test_preservation_servo_safety_unchanged_by_inspection():
    """Preservation 3.6: the servo safety path is untouched by this spec.

    VERIFIED BY INSPECTION ONLY — this test is a documented assertion, not a
    simulation. Per the workspace hardware-movement rule, SERVO_SIM
    collision/limit verification is NOT run here.

    This bugfix is confined to the mic-stop / callback path in
    src/micwebcontroller.py (guarding talk()/audio_callback on is_streaming and
    reordering stop_streaming() so the stream is closed before _release_gpio()).
    It modifies NO servo code: it does not touch TrunkController.set_angle /
    move_to, the SAFE_LIMITS clamp, return_to_rest, FORBIDDEN_COMBINATIONS, or
    any verified-pose override. micwebcontroller.py imports none of the servo
    modules (trunkcontroller / movements / performance / constants' servo
    tables) — it only drives the jaw motor (GPIO15) and eye LED (GPIO6) via
    gpiozero. Therefore every servo angle in a Routine is still written through
    the clamped primitives and returns to rest exactly as before (3.6).

    EXPECTED: PASSES (trivially — it documents the inspection result).

    Validates: Requirements 3.6 (Preservation)
    """
    # micwebcontroller does not import the servo stack — a structural check that
    # backs up the inspection claim that no servo code is reachable from here.
    import inspect

    src = inspect.getsource(micwebcontroller)
    for servo_symbol in ("set_angle", "move_to", "SAFE_LIMITS",
                         "FORBIDDEN_COMBINATIONS", "return_to_rest",
                         "TrunkController", "ServoKit"):
        assert servo_symbol not in src, (
            f"unexpected servo symbol '{servo_symbol}' in micwebcontroller.py — "
            "this spec must not touch the servo safety path (3.6)"
        )
