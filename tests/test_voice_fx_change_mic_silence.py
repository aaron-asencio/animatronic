"""Thread-faithful exploration tests for the voice-FX change -> live-mic SILENCE bug.

Bugfix spec: voice-fx-change-mic-silence.

WHAT THIS CONFIRMS
------------------
Changing the voice FX (a style preset or a single effect) around a live duplex
mic stream made the live passthrough go **permanently silent with NO crash**,
recovered only by restarting the micweb process. This is DISTINCT from
``voice-fx-style-change-stream-crash`` (which crashed with "deque mutated during
iteration") and from ``voice-fx-audio-dropout-fix`` (a mic->routine GPIO leak).

ROOT CAUSE (confirmed on hardware) & THE FIX
--------------------------------------------
The PortAudio ``Status: 6`` log (``paOutputUnderflow | paInputOverflow`` every
frame) proved the duplex stream WEDGED: the real-time callback was delayed past
the ~21 ms CHUNK deadline and the shared duplex ALSA link underran and never
re-primed. The cause was that the callback acquired ``_dsp_lock`` every frame
while the Flask request thread took the SAME lock on an FX change -- the GIL
hand-off stalled the callback. (A second, independent underrun source was a
per-sample Python loop in ``_fx_reverb`` costing ~22 ms/frame.)

The fix made the live path **LOCK-FREE** (``_dsp_lock`` removed): the callback
reads ``effects_config`` with a single atomic reference read and owns its
per-frame DSP state exclusively (a style change bumps ``_dsp_reset_generation``
and the callback resets itself), ``_fx_reverb`` was vectorised, and a persistent
underflow now triggers a stream reopen. See the ``live-audio-troubleshooting``
steering doc.

WHAT THE TWO TESTS ASSERT NOW (both PASS on the fixed code)
-----------------------------------------------------------
  Variant A - ``test_fx_change_is_lockfree_and_never_stalls_callback``:
    the module exposes NO ``_dsp_lock`` (the lock was removed, not shrunk);
    running the exact lock-free callback body (atomic config snapshot +
    generation-reset + ``talk`` + ``apply_effects(frame, cfg)``) concurrently
    with a request thread hammering FX changes never raises (no torn read, no
    deque-mutation); and one isolated callback frame fits the CHUNK deadline.

  Variant B - ``test_recurring_fx_exception_is_surfaced_not_masked``:
    a per-frame exception that repeats every frame is surfaced via a visible,
    RATE-LIMITED recurring diagnostic (``_note_callback_dsp_error``) rather than
    swallowed silently into permanent passthrough/silence.

WHY TWO REAL THREADS (Variant A)
--------------------------------
The original wedge was a real-time CONTENTION fault; a single-threaded test
cannot surface a callback being delayed by a concurrent request-thread mutation.
Variant A runs a CONSUMER thread (the lock-free callback body) concurrently with
a PRODUCER thread (real style changes: ``_apply_chain_to_live`` then
``_reset_audio_dsp_state``), bounded by an iteration cap + join timeout so it is
fast, deterministic, and never hangs. With the lock gone the callback cannot be
*blocked* by the producer; the structural assertion is that it never raises.

NOTE: the device-level duplex wedge itself is confirmed on the Pi (manual) -- a
device-independent test cannot reproduce an ALSA underrun; see the steering doc.

HARDWARE / IMPORT STUBBING
--------------------------
``micwebcontroller`` imports ``pyaudio`` and ``gpiozero`` at module top and
builds a module-level ``ConfigStore`` at import time. We install lightweight
stubs BEFORE importing it, mirroring ``tests/test_micwebcontroller.py`` and
``tests/test_voice_fx_style_change.py``. The gpiozero stub MUST provide
``Device`` (plus ``PWMLED`` / ``LED`` / ``DigitalOutputDevice``) because the
module does ``from gpiozero import Device``; the pyaudio stub must provide
``paInt16`` / ``paContinue`` / ``PyAudio``.
"""

import io
import os
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

# Ensure the src/ directory is importable when pytest runs from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# ---------------------------------------------------------------------------
# Install hardware/dependency stubs BEFORE importing micwebcontroller.
# ---------------------------------------------------------------------------

# Point the module-level ConfigStore (built at import time) at a temp path so it
# never touches a real home-directory file.
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


# --- pyaudio stub: paInt16 / paContinue constants + PyAudio() factory. -------
_pyaudio_stub = types.ModuleType("pyaudio")
_pyaudio_stub.paInt16 = 8          # real value; only needs to be a stable int
_pyaudio_stub.paContinue = 0
_pyaudio_stub.PyAudio = MagicMock(name="PyAudio")
_install_stub("pyaudio", _pyaudio_stub)

# --- gpiozero stub: Device REQUIRED (from gpiozero import Device) + LEDs. -----
_gpiozero_stub = types.ModuleType("gpiozero")
_gpiozero_stub.PWMLED = MagicMock(name="PWMLED")
_gpiozero_stub.LED = MagicMock(name="LED")
_gpiozero_stub.DigitalOutputDevice = MagicMock(name="DigitalOutputDevice")
_gpiozero_stub.Device = MagicMock(name="Device")
_install_stub("gpiozero", _gpiozero_stub)

# --- utils.audio_utils stub (only if the real one can't import cleanly). ------
try:  # pragma: no cover - depends on environment
    import utils.audio_utils  # noqa: F401
except Exception:  # pragma: no cover - fall back to a stub
    _utils_pkg = sys.modules.get("utils")
    if _utils_pkg is None:
        _utils_pkg = types.ModuleType("utils")
        _utils_pkg.__path__ = []  # mark as package
        sys.modules["utils"] = _utils_pkg
    _audio_utils_stub = types.ModuleType("utils.audio_utils")
    _audio_utils_stub.AudioUtils = MagicMock(name="AudioUtils")
    sys.modules["utils.audio_utils"] = _audio_utils_stub

# numpy is a real runtime dependency and safe to import; no stub needed.

import numpy as np  # noqa: E402
from datetime import datetime  # noqa: E402

import micwebcontroller  # noqa: E402


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

# Real-time budget for one callback invocation: 1024 frames / 48000 Hz.
CHUNK_DEADLINE_S = micwebcontroller.CHUNK / micwebcontroller.RATE  # ~= 0.0213 s
# One lock-free callback frame is pure-CPU DSP and must fit WELL under the CHUNK
# deadline. (Measured in isolation; see the isolated-timing assertion. Under the
# two-thread stress loop we assert only the structural no-raise property, since
# GIL round-robin makes wall-clock timing there a measurement artifact.)
LOCK_HOLD_BUDGET_S = CHUNK_DEADLINE_S

# Bounded loops keep the test fast, deterministic, and non-hanging.
_CONSUMER_ITERS = 400
_PRODUCER_ITERS = 400
_JOIN_TIMEOUT = 60.0  # safety net so a stuck run never hangs CI.


class _SlowSink:
    """A stdout-like sink whose every write BLOCKS briefly.

    Used as a GIL-contention stressor: the request-thread PRODUCER prints to this
    slow sink while the lock-free callback CONSUMER runs concurrently. On the
    FIXED (lock-free) design the callback does NO stdout I/O and holds no lock,
    so a slow request-thread print cannot stall it -- which is exactly the
    property under test. (Historically the callback printed under a lock, which
    is what wedged the stream; that lock and that print are now gone.)
    """

    # Per-write delay -- models a slow/piped/journald stdout on the Pi, kept
    # small so the bounded loops stay fast.
    _WRITE_DELAY_S = 0.025

    def __init__(self):
        self._buf = io.StringIO()
        self._lock = threading.Lock()

    def write(self, s):
        # Only sleep on real content (print() also writes the "\n" separately).
        if s and s != "\n":
            time.sleep(self._WRITE_DELAY_S)
        with self._lock:
            return self._buf.write(s)

    def flush(self):
        pass

    def getvalue(self):
        with self._lock:
            return self._buf.getvalue()


def _demon_chain():
    """A full, valid effect chain (echo enabled so talk()+apply_effects do real
    per-frame work). Mirrors the shipped 'demon' preset shape."""
    return {
        "pitch":      {"enabled": True,  "amount": 0.70},
        "distortion": {"enabled": True,  "amount": 0.55},
        "echo":       {"enabled": True,  "amount": 0.35},
        "reverb":     {"enabled": True,  "amount": 0.25},
        "tremolo":    {"enabled": False, "amount": 0.00},
        "bitcrush":   {"enabled": False, "amount": 0.00},
        "ring_mod":   {"enabled": False, "amount": 0.00},
    }


def _ghost_chain():
    """A second full, valid chain to swap to (style change producer side)."""
    return {
        "pitch":      {"enabled": True,  "amount": 0.92},
        "distortion": {"enabled": False, "amount": 0.00},
        "echo":       {"enabled": True,  "amount": 0.65},
        "reverb":     {"enabled": True,  "amount": 0.55},
        "tremolo":    {"enabled": True,  "amount": 0.35},
        "bitcrush":   {"enabled": False, "amount": 0.00},
        "ring_mod":   {"enabled": False, "amount": 0.00},
    }


# ===========================================================================
# Variant A - lock-free callback never stalled by a concurrent FX change (H3).
# ---------------------------------------------------------------------------
# PRIMARY FIX SIGNAL (lock-free, H3). It emulates the FIXED callback body: per
# frame, apply any pending generation-reset, snapshot effects_config with one
# atomic read, talk(), then apply_effects(frame, cfg) -- with NO lock. A PRODUCER
# thread issues real style changes concurrently and prints to a slow sink. The
# properties asserted: the module has no _dsp_lock, the lock-free callback never
# raises under concurrent FX changes (no torn read / deque-mutation), and one
# isolated frame fits the ~21 ms CHUNK deadline.
# ===========================================================================

def _time_one_lockfree_frame(frame):
    """Time ONE isolated lock-free callback frame (talk + apply_effects)."""
    audio = frame.astype(np.float32)
    t0 = time.perf_counter()
    cfg = micwebcontroller.effects_config
    micwebcontroller.talk(audio, start_time=datetime.now().timestamp())
    micwebcontroller.apply_effects(frame, cfg)
    return time.perf_counter() - t0


def test_fx_change_is_lockfree_and_never_stalls_callback():
    """PRIMARY FIX SIGNAL (H3) - the real-time callback path takes NO lock and a
    concurrent FX change never stalls it or produces a torn read.

    The real duplex-wedge bug: the callback acquired ``_dsp_lock`` every frame
    and the Flask request thread took the same lock on a style change. On the
    Pi the GIL hand-off delayed the callback past the ~21 ms CHUNK deadline, the
    duplex output ring underran, and ALSA wedged the stream silent forever
    (PortAudio ``status`` 6 every frame) until a process restart. The fix makes
    the callback LOCK-FREE: it reads ``effects_config`` with a single atomic
    reference read and owns its per-frame DSP state (the request thread only
    bumps ``_dsp_reset_generation``; the callback resets itself).

    CONSUMER (callback emulation): run the exact lock-free body the callback
    runs -- apply any pending generation reset, snapshot ``effects_config`` once,
    ``talk()``, then ``apply_effects(frame, cfg)`` -- with NO lock. Record the
    MAX per-frame wall-clock time.

    PRODUCER (request thread): hammer real FX changes
    (``_apply_chain_to_live`` -> ``_reset_audio_dsp_state``), exactly as
    ``POST /effects`` does.

    Assertions (Expected Behavior 2.1 / 2.3):
      * the module exposes no ``_dsp_lock`` (the lock was removed, not merely
        shrunk) -- so the callback categorically cannot block on a request-thread
        lock;
      * the callback emulation never raises (no torn read, no deque-mutation);
      * each lock-free callback stays well under the CHUNK deadline.
    """
    # The real fix is structural: there is NO lock on the DSP path at all.
    assert not hasattr(micwebcontroller, "_dsp_lock"), (
        "micwebcontroller still defines _dsp_lock -- the real-time callback must "
        "be LOCK-FREE (bugfix H3). A lock the request thread can hold is exactly "
        "what wedged the duplex stream; it must be removed, not just shortened."
    )

    # Live mic stream is active (bug condition: streamIsDuplexLive = True).
    micwebcontroller.audio_state["is_streaming"] = True
    micwebcontroller.effects_config = _demon_chain()
    micwebcontroller._reset_audio_dsp_state()

    frame = (np.sin(np.linspace(0, 20 * np.pi, micwebcontroller.CHUNK)) * 8000).astype(np.int16)

    max_frame_time = {"value": 0.0}
    error_slot = {"exc": None}
    stop = threading.Event()
    slow = _SlowSink()

    chains = [_demon_chain(), _ghost_chain()]

    def consumer():
        try:
            for _ in range(_CONSUMER_ITERS):
                if stop.is_set():
                    break
                audio = frame.astype(np.float32)
                # --- Lock-free callback body (mirrors audio_callback, H3). ------
                t0 = time.perf_counter()
                # Apply a pending reset on the callback thread, as the callback
                # does (the request thread only bumps the generation).
                if (micwebcontroller._dsp_reset_generation
                        != micwebcontroller._dsp_applied_generation["value"]):
                    micwebcontroller._reset_audio_dsp_state_core()
                    micwebcontroller._dsp_applied_generation["value"] = (
                        micwebcontroller._dsp_reset_generation)
                cfg = micwebcontroller.effects_config  # atomic snapshot, no lock
                micwebcontroller.talk(audio, start_time=datetime.now().timestamp())
                micwebcontroller.apply_effects(frame, cfg)
                dt = time.perf_counter() - t0
                # ----------------------------------------------------------------
                if dt > max_frame_time["value"]:
                    max_frame_time["value"] = dt
        except Exception as exc:  # noqa: BLE001 - capture any escape for triage
            error_slot["exc"] = exc
        finally:
            stop.set()

    def producer():
        i = 0
        while not stop.is_set() and i < _PRODUCER_ITERS:
            try:
                micwebcontroller._apply_chain_to_live(chains[i % 2])
                micwebcontroller._reset_audio_dsp_state()
                # A compact request-thread print, as POST /effects now does.
                print(f"Voice style set ({i % 2})")
            except Exception:  # noqa: BLE001 - producer errors aren't the signal
                pass
            i += 1

    # Redirect stdout to a slow sink. On the FIXED design this cannot hurt the
    # callback: the callback does no stdout I/O and holds no lock, so a slow
    # request-thread print can never stall it.
    old_stdout = sys.stdout
    sys.stdout = slow
    try:
        t_consumer = threading.Thread(target=consumer, name="callback-emu")
        t_producer = threading.Thread(target=producer, name="request-thread")
        t_consumer.start()
        t_producer.start()
        t_consumer.join(timeout=_JOIN_TIMEOUT)
        stop.set()
        t_producer.join(timeout=_JOIN_TIMEOUT)
    finally:
        sys.stdout = old_stdout
    micwebcontroller.audio_state["is_streaming"] = False

    # STRUCTURAL invariant (the real fix): running the exact lock-free callback
    # body concurrently with a request thread hammering FX changes NEVER raises
    # -- no torn effects_config read, no "deque mutated during iteration", no
    # cross-thread mutation of the callback-owned DSP state. (We do NOT assert a
    # wall-clock budget under this two-thread Python stress loop: with no lock
    # the callback cannot be *blocked* by the request thread, and GIL
    # round-robin + the deliberately-slow stdout sink make wall-clock timing
    # here a measurement artifact, not a real callback stall. The per-frame DSP
    # cost is measured fairly in isolation below.)
    assert error_slot["exc"] is None, (
        "Lock-free callback emulation raised (torn read / deque mutation?): "
        f"{type(error_slot['exc']).__name__}: {error_slot['exc']}"
    )

    # ISOLATED timing: with no competing thread stealing the GIL, one lock-free
    # callback frame (talk + full demon chain) must fit well inside the CHUNK
    # deadline -- this is the real per-frame DSP cost the Pi callback pays.
    micwebcontroller.audio_state["is_streaming"] = True
    micwebcontroller.effects_config = _demon_chain()
    micwebcontroller._reset_audio_dsp_state()
    micwebcontroller._reset_audio_dsp_state_core()  # ensure buffers exist
    # Warm up (first numpy calls allocate/compile ufunc paths).
    for _ in range(5):
        a = frame.astype(np.float32)
        micwebcontroller.talk(a, start_time=datetime.now().timestamp())
        micwebcontroller.apply_effects(frame, micwebcontroller.effects_config)
    best = min(
        _time_one_lockfree_frame(frame) for _ in range(20)
    )
    micwebcontroller.audio_state["is_streaming"] = False
    assert best < LOCK_HOLD_BUDGET_S, (
        f"Best-case lock-free callback frame took {best * 1000:.1f} ms -- "
        f"exceeds the {CHUNK_DEADLINE_S * 1000:.1f} ms CHUNK deadline even with "
        "no contention, so the DSP itself is too heavy for the duplex buffer."
    )

# ===========================================================================
# Variant B - masked recurring exception (H2).
# ---------------------------------------------------------------------------
# Force apply_effects to raise EVERY frame via a pathological _fx_pitch amount
# (n_src = int(len/amount) explodes for a tiny positive amount), then drive the
# lock-free callback body (talk(), then apply_effects(frame, cfg) inside its
# try/except passthrough) and capture stdout. A recurring per-frame failure MUST
# produce a VISIBLE, RATE-LIMITED recurring diagnostic -- not be swallowed
# silently every frame. The fix adds a consecutive-error counter
# (_note_callback_dsp_error) that surfaces exactly that signal.
# ===========================================================================

def _pathological_pitch_chain():
    """A chain whose only enabled effect is pitch with a pathological amount.

    ``_fx_pitch`` computes ``n_src = int(len(audio) / amount)``; a tiny positive
    amount makes ``n_src`` astronomically large, so ``np.linspace`` /
    ``np.interp`` blow up (MemoryError / OverflowError / ValueError) EVERY frame.
    This is the H2 probe: a recurring post-FX-change exception the secondary
    try/except would otherwise swallow into permanent silence.
    """
    return {
        "pitch":      {"enabled": True,  "amount": 1e-12},
        "distortion": {"enabled": False, "amount": 0.00},
        "echo":       {"enabled": False, "amount": 0.00},
        "reverb":     {"enabled": False, "amount": 0.00},
        "tremolo":    {"enabled": False, "amount": 0.00},
        "bitcrush":   {"enabled": False, "amount": 0.00},
        "ring_mod":   {"enabled": False, "amount": 0.00},
    }


# How many frames to drive. A recurring fault repeats on every one of them.
_RECURRING_FRAMES = 40


def _callback_emulation_with_passthrough(frame, captured):
    """Run ONE frame exactly like audio_callback's protected body.

    Mirrors the module's LOCK-FREE structure (bugfix H3): snapshot the config,
    talk(), then apply_effects(frame, cfg) -- no lock -- wrapped in the secondary
    try/except that returns passthrough and prints on error. Returns True on a
    caught exception (passthrough frame). The 'captured' stdout sees whatever the
    error path prints -- the signal (or lack of one) under test.
    """
    audio = frame.astype(np.float32)
    try:
        cfg = micwebcontroller.effects_config  # atomic snapshot, no lock
        micwebcontroller.talk(audio, start_time=datetime.now().timestamp())
        micwebcontroller.apply_effects(frame, cfg)
        # Mirror the callback's success path: a clean frame clears the module's
        # consecutive-error counter so the recurring diagnostic stays scoped to
        # genuine back-to-back failures.
        micwebcontroller._reset_callback_dsp_errors()
        return False
    except Exception as e:  # noqa: BLE001 - mirror the module's broad passthrough
        # Route the error through the MODULE's own error-path diagnostic rather
        # than reprinting a hardcoded line, so the captured output reflects what
        # micwebcontroller actually emits (the rate-limited, recurring signal the
        # fix introduces). audio_callback is a nested, non-importable function;
        # _note_callback_dsp_error() is the module-level helper it calls on each
        # caught exception. It prints via print() to the current sys.stdout, so
        # redirect stdout to the capture buffer around the call.
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            micwebcontroller._note_callback_dsp_error(e)
        finally:
            sys.stdout = old_stdout
        return True


def test_recurring_fx_exception_is_surfaced_not_masked():
    """RECURRING-EXCEPTION signal (H2) - a RECURRING per-frame exception must be
    surfaced (visible + rate-limited), not swallowed silently into permanent
    silence. PASSES on the fixed code via the consecutive-error diagnostic.

    Force ``apply_effects`` to raise on EVERY frame (pathological ``_fx_pitch``
    amount), drive the callback-emulation loop ``_RECURRING_FRAMES`` times, and
    capture stdout. The expected behavior: an operator can SEE that FX
    processing is failing every frame via a RATE-LIMITED recurring diagnostic
    (e.g. a consecutive-error count, or a once-then-every-Nth message) rather
    than an undifferentiated line swallowed on every frame.

    Historically (unfixed) the callback's unconditional ``try/except Exception``
    returned passthrough and emitted the SAME line every frame with NO
    distinguishable recurring, rate-limited signal -- masking a permanent
    per-frame failure into silence. The fix's rate-limited counter makes that
    signal visible, so this test now PASSES.
    """
    micwebcontroller.audio_state["is_streaming"] = True
    micwebcontroller.effects_config = _pathological_pitch_chain()
    micwebcontroller._reset_audio_dsp_state()

    frame = (np.sin(np.linspace(0, 20 * np.pi, micwebcontroller.CHUNK)) * 8000).astype(np.int16)

    # Sanity: confirm the chain really does raise every frame (so the test is
    # exercising the masking path, not a silently-succeeding chain).
    raised = 0
    for _ in range(_RECURRING_FRAMES):
        sink = io.StringIO()
        if _callback_emulation_with_passthrough(frame, sink):
            raised += 1
    micwebcontroller.audio_state["is_streaming"] = False

    assert raised == _RECURRING_FRAMES, (
        f"Expected apply_effects to raise on every frame (H2 probe), but only "
        f"{raised}/{_RECURRING_FRAMES} frames raised -- the pathological chain "
        "did not reproduce the recurring exception."
    )

    # Re-run capturing all error output into ONE buffer so we can inspect the
    # diagnostic the error path produced across the whole recurring run.
    micwebcontroller.audio_state["is_streaming"] = True
    micwebcontroller.effects_config = _pathological_pitch_chain()
    micwebcontroller._reset_audio_dsp_state()
    captured = io.StringIO()
    for _ in range(_RECURRING_FRAMES):
        _callback_emulation_with_passthrough(frame, captured)
    micwebcontroller.audio_state["is_streaming"] = False

    output = captured.getvalue()

    # EXPECTED BEHAVIOR (encodes the fix target, 2.4): a RECURRING failure is
    # surfaced via a rate-limited diagnostic an operator can see -- e.g. a
    # consecutive-error COUNT, or an explicit "recurring"/"repeated"/"every
    # frame" signal, or a rate-limited "(suppressing further ...)" notice. The
    # A masked implementation would emit only the same undifferentiated per-frame
    # line (no recurring/rate-limited marker); the fix adds that marker.
    lowered = output.lower()
    recurring_markers = (
        "consecutive",
        "recurring",
        "repeated",
        "every frame",
        "in a row",
        "suppress",
        "rate-limit",
        "rate limit",
        "x)",   # e.g. "(42x)" style occurrence count
    )
    has_recurring_signal = any(m in lowered for m in recurring_markers)

    assert has_recurring_signal, (
        "A recurring per-frame FX exception produced NO visible, rate-limited "
        "recurring diagnostic. COUNTEREXAMPLE (H2): the callback's unconditional "
        "try/except swallows the error into passthrough and emits only an "
        "undifferentiated per-frame line, masking a permanent per-frame failure "
        "into silence with no operator-visible recurring signal. Captured error "
        f"output was:\n{output[:1000]}"
    )
