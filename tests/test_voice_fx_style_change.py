"""Thread-faithful exploration tests for the voice-FX style-change stream crash.

Bugfix spec: voice-fx-style-change-stream-crash (Task 1 — bug-condition exploration).

WHAT THIS CONFIRMS
------------------
The live mic path in ``src/micwebcontroller.py`` shares DSP state between two
threads with **no lock**:

  - the PortAudio callback thread, which runs ``apply_effects`` (and inside it
    ``_fx_echo``) on every frame, and
  - the Flask request thread, which on a style change calls
    ``_reset_audio_dsp_state()`` — rebinding ``audio_state['echo_buffer']`` to a
    brand-new ``deque``.

``_fx_echo`` iterates that deque (``for i, old in enumerate(...)``) and then
appends to it. Because the shared ``audio_state['echo_buffer']`` is touched by
two threads with no lock, the request thread's style-change work lands inside
the callback's live iteration of that same deque, and Python raises::

    RuntimeError: deque mutated during iteration

inside the callback. PortAudio treats an unhandled callback exception as fatal
and stops the stream, so the live mic goes silent for the rest of the session
(bugfix 1.1, 1.2, 1.3).

COUNTEREXAMPLE (documented)
---------------------------
    _fx_echo iterating audio_state['echo_buffer'] while the style-change path
    concurrently touches that SAME shared deque (append / rebind) with no lock
    serialising the two threads  ->  RuntimeError: deque mutated during iteration

Note on the mechanism: the RuntimeError fires specifically when the deque the
callback is iterating is mutated (appended) by another thread during that
iteration. ``_reset_audio_dsp_state()`` alone *rebinds* the slot, which widens
the unsynchronised-access window; the stream-killing fault is that the shared
echo buffer is read/iterated and mutated across threads with no lock at all.
This harness drives that exact unsynchronised shared-deque access.

WHY TWO REAL THREADS
--------------------
This defect is a *data race*. A single-threaded test can never surface it:
with one thread, ``_fx_echo``'s iterate+append and ``_reset_audio_dsp_state``'s
rebind can never interleave. The harness below therefore runs a CONSUMER thread
(``apply_effects`` loop) and a PRODUCER thread (``_reset_audio_dsp_state`` loop)
concurrently, both bounded by an iteration cap and a join timeout so the test is
fast, deterministic, and never hangs.

EXPECTED OUTCOME ON UNFIXED CODE: these tests FAIL (the RuntimeError escapes the
consumer). The failure confirms the bug. After the synchronization fix (Task 3)
the SAME tests PASS unchanged (re-run in Task 3.6).

HARDWARE / IMPORT STUBBING
--------------------------
``micwebcontroller`` imports ``pyaudio`` and ``gpiozero`` at module top and
builds a module-level ``ConfigStore`` at import time. We install lightweight
stubs BEFORE importing it, mirroring ``tests/test_micwebcontroller.py``. The
gpiozero stub MUST provide ``Device`` (plus ``PWMLED`` / ``LED`` /
``DigitalOutputDevice``) because the module does ``from gpiozero import Device``.
"""

import copy
import os
import sys
import threading
import types
from unittest.mock import MagicMock

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

import micwebcontroller  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Iterations per thread. The race is a GIL-scheduling interleaving, so a larger
# budget makes reproduction reliable (not flaky) while a numpy-light chain keeps
# each iteration cheap enough that the whole run stays fast and CI-friendly.
_ITERATIONS = 60000
_JOIN_TIMEOUT = 60.0  # seconds; a safety net so a stuck run never hangs CI.


def _echo_only_config():
    """A minimal chain with ONLY echo enabled, so _fx_echo's iterate+append
    loop over echo_buffer is the only work the consumer does — keeping the race
    window tight and the reproduction reliable."""
    return {
        "pitch":      {"enabled": False, "amount": 0.85},
        "distortion": {"enabled": False, "amount": 0.40},
        "echo":       {"enabled": True,  "amount": 0.50},
        "reverb":     {"enabled": False, "amount": 0.30},
        "tremolo":    {"enabled": False, "amount": 0.50},
        "bitcrush":   {"enabled": False, "amount": 0.40},
        "ring_mod":   {"enabled": False, "amount": 0.50},
    }


# ---------------------------------------------------------------------------
# Primary variant — the deque race (the FAIL signal)
# ---------------------------------------------------------------------------

def test_style_change_never_crashes_live_callback():
    """PRIMARY FAIL SIGNAL — deque mutated during iteration.

    This models the REAL production concurrency under the lock-free design
    (bugfix H3): the per-frame DSP state (the ``echo_buffer`` deque) is owned
    EXCLUSIVELY by the single PortAudio callback thread, and the Flask request
    thread's style change never touches it — on a live stream
    ``_reset_audio_dsp_state()`` only bumps ``_dsp_reset_generation`` and the
    callback performs the reset itself at the top of a frame.

    CONSUMER thread = the sole callback thread: a tight bounded loop that applies
    any pending generation-reset (as the callback does), snapshots
    ``effects_config`` with a single atomic read, and runs ``apply_effects`` (the
    same ``_fx_echo`` iterate+append over ``echo_buffer``). It is the ONLY thread
    that touches that deque. Any exception is captured into a shared slot.

    PRODUCER thread = the Flask request thread doing style changes via the REAL
    endpoint path (``_apply_chain_to_live`` → ``_reset_audio_dsp_state``) while
    ``is_streaming`` is True, so the reset defers to the callback rather than
    rebinding the deque under the consumer. No lock is taken on either side.

    Assertion (Expected Behavior 2.1 / 2.2 / 2.3): no ``RuntimeError`` escapes
    the consumer AND it keeps returning ``int16`` of length ``CHUNK``. Because
    the deque has a single owner, there is no cross-thread mutation to race, so
    "deque mutated during iteration" cannot recur — without any lock on the
    real-time path. A single-threaded test can never surface the interleaving.
    """
    # Enable echo so every frame iterates+appends the shared echo_buffer.
    micwebcontroller.effects_config = _echo_only_config()
    # Live stream: _reset_audio_dsp_state() defers the buffer reset to the
    # callback (the consumer), matching production.
    micwebcontroller.audio_state["is_streaming"] = True
    # Start from a clean, known DSP state (no stream yet live when this runs
    # inline, so it resets the buffers directly here before we flip streaming).
    micwebcontroller._reset_audio_dsp_state_core()

    frame = np.zeros(micwebcontroller.CHUNK, dtype=np.int16)

    # Shared state between the two threads.
    error_slot = {"exc": None}
    bad_output = {"value": None}
    stop = threading.Event()

    def consumer():
        try:
            for _ in range(_ITERATIONS):
                if stop.is_set():
                    break
                # Apply a pending reset on THIS (callback-owner) thread, as
                # audio_callback does — the only thread that touches the deque.
                if (micwebcontroller._dsp_reset_generation
                        != micwebcontroller._dsp_applied_generation["value"]):
                    micwebcontroller._reset_audio_dsp_state_core()
                    micwebcontroller._dsp_applied_generation["value"] = (
                        micwebcontroller._dsp_reset_generation)
                cfg = micwebcontroller.effects_config  # atomic snapshot, no lock
                out = micwebcontroller.apply_effects(frame, cfg)
                # Must keep producing int16[CHUNK] — a stalled/short frame also fails.
                if out.dtype != np.int16 or len(out) != micwebcontroller.CHUNK:
                    bad_output["value"] = (str(out.dtype), len(out))
                    break
        except Exception as exc:  # noqa: BLE001 - capture ANY escape for triage
            error_slot["exc"] = exc
        finally:
            stop.set()

    def producer():
        # The Flask request thread doing style changes via the REAL endpoint
        # path for the WHOLE consumer run. It rebinds effects_config atomically
        # (_apply_chain_to_live) and asks for a DSP reset (_reset_audio_dsp_state
        # — which, with is_streaming True, only bumps the generation and does NOT
        # touch the consumer-owned deque). It never calls apply_effects itself
        # (no production request thread does). The producer's own errors are not
        # the signal — only the consumer's escape is.
        chains = [_echo_only_config(),
                  {**_echo_only_config(), "reverb": {"enabled": True, "amount": 0.3}}]
        i = 0
        while not stop.is_set():
            try:
                micwebcontroller._apply_chain_to_live(chains[i % 2])
                micwebcontroller._reset_audio_dsp_state()
            except Exception:  # noqa: BLE001 - producer errors aren't the signal
                pass
            i += 1

    t_consumer = threading.Thread(target=consumer, name="consumer")
    t_producer = threading.Thread(target=producer, name="producer")
    t_consumer.start()
    t_producer.start()
    t_consumer.join(timeout=_JOIN_TIMEOUT)
    stop.set()
    t_producer.join(timeout=_JOIN_TIMEOUT)
    micwebcontroller.audio_state["is_streaming"] = False

    # With the deque owned by a single thread (the consumer) and the request
    # thread only bumping the reset generation, there is no cross-thread deque
    # mutation to race — so "deque mutated during iteration" cannot recur, with
    # NO lock on the real-time path (bugfix H3). (A single-threaded test can
    # never surface this interleaving.)
    assert error_slot["exc"] is None, (
        "Style change crashed the live callback: "
        f"{type(error_slot['exc']).__name__}: {error_slot['exc']}"
    )
    assert bad_output["value"] is None, (
        f"Callback stopped producing int16[CHUNK]: got dtype/len {bad_output['value']}"
    )


# ---------------------------------------------------------------------------
# Second variant — torn-config read (softer check)
# ---------------------------------------------------------------------------

def test_style_change_never_shows_torn_config_chain():
    """SECONDARY / softer check — torn effects_config read (bugfix 1.5 / 2.2).

    PRODUCER alternates ``_apply_chain_to_live`` between two complete presets.
    CONSUMER snapshots ``effects_config`` and asserts the chain it observes is
    always a COMPLETE preset (exactly one of the two), never a half-old/half-new
    mix.

    On the UNFIXED module ``_apply_chain_to_live`` overwrites keys in place one
    at a time, so a torn mix CAN appear and this check may FAIL. The deque
    ``RuntimeError`` above remains the primary FAIL signal; this documents the
    secondary non-crash hazard. (A single-threaded test cannot surface it.)
    """
    preset_a = _echo_only_config()
    preset_b = copy.deepcopy(preset_a)
    # Make B differ from A on MULTIPLE fields so an in-place per-key overwrite
    # has a visible half-updated window.
    preset_b["pitch"] = {"enabled": True, "amount": 0.70}
    preset_b["distortion"] = {"enabled": True, "amount": 0.55}
    preset_b["reverb"] = {"enabled": True, "amount": 0.25}
    preset_b["echo"] = {"enabled": False, "amount": 0.00}

    complete_presets = [preset_a, preset_b]

    micwebcontroller.effects_config = copy.deepcopy(preset_a)

    # Cheap, order-stable fingerprints of each COMPLETE preset for a fast
    # per-iteration comparison (deepcopy is too slow to catch the narrow torn
    # window). A torn read fingerprints to neither.
    def _fingerprint(cfg):
        return tuple(
            (name, cfg[name]["enabled"], cfg[name]["amount"])
            for name in sorted(cfg)
        )

    complete_fingerprints = {_fingerprint(p) for p in complete_presets}

    torn = {"value": None}
    stop = threading.Event()

    def consumer():
        for _ in range(_ITERATIONS):
            if stop.is_set():
                break
            # Snapshot the shared reference and fingerprint it. A torn read
            # (keys half-overwritten in place) matches neither complete preset.
            observed = micwebcontroller.effects_config
            fp = _fingerprint(observed)
            if fp not in complete_fingerprints:
                torn["value"] = copy.deepcopy(observed)
                break
        stop.set()

    def producer():
        # Alternate complete presets for the WHOLE consumer run so the in-place
        # per-key overwrite window overlaps the consumer's reads throughout.
        i = 0
        while not stop.is_set():
            micwebcontroller._apply_chain_to_live(complete_presets[i % 2])
            i += 1

    t_consumer = threading.Thread(target=consumer, name="torn-consumer")
    t_producer = threading.Thread(target=producer, name="torn-producer")
    t_consumer.start()
    t_producer.start()
    t_consumer.join(timeout=_JOIN_TIMEOUT)
    stop.set()
    t_producer.join(timeout=_JOIN_TIMEOUT)

    assert torn["value"] is None, (
        "Observed a torn (half-updated) effects_config chain — neither preset: "
        f"{torn['value']}"
    )

# ===========================================================================
# Task 2.1 — PRESERVATION tests (Property 2): DSP output equality
# ---------------------------------------------------------------------------
# These are OBSERVATION-FIRST preservation tests. They capture the DSP output
# produced by the UNFIXED code for a fixed, fully-applied chain starting from a
# clean reset state, then assert a second identical run (same clean state)
# reproduces it byte-for-byte. This proves the chain is DETERMINISTIC for a
# fixed seeded state — the baseline Task 3's synchronization fix must preserve.
#
# Why determinism holds for a fixed seeded state: all DSP carry-over lives in
# module globals that _reset_audio_dsp_state() restores to known zeros — the
# echo deque (audio_state['echo_buffer']), the reverb buffer / position
# (audio_state['reverb_buffer'] / ['reverb_pos']), and the tremolo/ring phases
# (effect_state). With the same effects_config, the same clean reset state, and
# the same input sequence, apply_effects is a pure function of those inputs.
#
# These tests MUST PASS on the unfixed code (no source change here). They do NOT
# use two threads — a single-threaded, non-racing path is exactly the
# "NOT isBugCondition" scope that preservation covers.
#
# _Requirements: 3.1_
# ===========================================================================

from hypothesis import given, settings, strategies as st  # noqa: E402


def _fully_applied_config():
    """A fixed chain with EVERY effect enabled, so the baseline exercises the
    whole signal chain (pitch -> ring_mod -> bitcrush -> distortion -> tremolo
    -> echo -> reverb) rather than just one effect."""
    return {
        'pitch':      {'enabled': True, 'amount': 0.80},
        'distortion': {'enabled': True, 'amount': 0.45},
        'echo':       {'enabled': True, 'amount': 0.50},
        'reverb':     {'enabled': True, 'amount': 0.30},
        'tremolo':    {'enabled': True, 'amount': 0.55},
        'bitcrush':   {'enabled': True, 'amount': 0.40},
        'ring_mod':   {'enabled': True, 'amount': 0.50},
    }


def _fixed_input_sequence(num_frames=6, seed=12345):
    """A fixed, reproducible sequence of int16 frames of length CHUNK."""
    rng = np.random.RandomState(seed)
    return [
        rng.randint(-20000, 20000, size=micwebcontroller.CHUNK, dtype=np.int16)
        for _ in range(num_frames)
    ]


def _run_chain_from_clean_state(config, frames):
    """Reset DSP state to a clean slate, install the given chain, and run the
    input sequence through apply_effects. Returns the list of EXACT output
    bytes (one bytes object per frame)."""
    micwebcontroller.effects_config = {
        name: dict(params) for name, params in config.items()
    }
    micwebcontroller._reset_audio_dsp_state()
    outputs = []
    for frame in frames:
        out = micwebcontroller.apply_effects(frame)
        outputs.append(out.tobytes())
    return outputs


def test_dsp_output_equality_single_threaded():
    """PRESERVATION 3.1 — DSP output is deterministic for a fixed seeded state.

    OBSERVE: with a fixed fully-applied chain and a clean DSP state, run a fixed
    input sequence through apply_effects and record the EXACT output bytes.
    ASSERT: a second identical run (same clean reset state, same chain, same
    input) reproduces the recorded baseline byte-for-byte.

    This passes on the UNFIXED code: apply_effects is a pure function of
    (effects_config, clean reset state, input sequence), so two runs from the
    same clean slate are identical. Task 3's lock must not change this.
    """
    config = _fully_applied_config()
    frames = _fixed_input_sequence()

    # Observe the baseline on the unfixed code.
    baseline = _run_chain_from_clean_state(config, frames)

    # Each measured output must be int16[CHUNK] (sanity on the recorded bytes:
    # CHUNK int16 samples == CHUNK * 2 bytes).
    for out_bytes in baseline:
        assert len(out_bytes) == micwebcontroller.CHUNK * 2

    # Assert a second identical run from a clean state reproduces it exactly.
    repeat = _run_chain_from_clean_state(config, frames)

    assert repeat == baseline, (
        "DSP output not reproducible from a clean seeded state — the chain "
        "should be deterministic for a fixed effects_config + reset state + "
        "input sequence."
    )


# --- Property-based variant (hypothesis, >= 100 examples) ------------------

_AMOUNT = st.floats(
    min_value=0.0, max_value=1.0,
    allow_nan=False, allow_infinity=False,
)
_ENABLED = st.booleans()
_EFFECT_NAMES = ('distortion', 'echo', 'reverb',
                 'tremolo', 'bitcrush', 'ring_mod')

# Pitch 'amount' is a resample RATIO (n_src = len/amount), so a realistic valid
# chain uses amounts near 1.0 (presets span 0.60-1.00). A near-zero positive
# amount makes n_src astronomically large and blows up np.linspace in _fx_pitch
# — a pre-existing DSP-math edge case that is OUT OF SCOPE for this
# synchronization bugfix (the fix must not touch _fx_* math). We therefore
# constrain pitch to its realistic valid band, matching the operator-tuned
# range the live chain actually uses.
_PITCH_AMOUNT = st.floats(
    min_value=0.5, max_value=2.0,
    allow_nan=False, allow_infinity=False,
)


@st.composite
def _valid_chains(draw):
    """A random but VALID effects_config: every known effect present with an
    enabled flag and an amount — the same shape the live chain uses. Pitch
    amount is kept to its realistic resample-ratio band (see _PITCH_AMOUNT)."""
    chain = {
        name: {'enabled': draw(_ENABLED), 'amount': draw(_AMOUNT)}
        for name in _EFFECT_NAMES
    }
    chain['pitch'] = {'enabled': draw(_ENABLED), 'amount': draw(_PITCH_AMOUNT)}
    return chain


@st.composite
def _int16_frames(draw):
    """A random sequence of 1-4 int16 frames of length CHUNK.

    Each frame is built from a drawn seed via numpy rather than a 1024-element
    hypothesis list: drawing CHUNK integers per frame directly is prohibitively
    slow (and dwarfs the chain under test), while a per-frame seed still ranges
    the full int16 input space reproducibly."""
    num_frames = draw(st.integers(min_value=1, max_value=4))
    seeds = draw(
        st.lists(
            st.integers(min_value=0, max_value=2**32 - 1),
            min_size=num_frames,
            max_size=num_frames,
        )
    )
    frames = []
    for seed in seeds:
        rng = np.random.RandomState(seed)
        frames.append(
            rng.randint(-32768, 32768, size=micwebcontroller.CHUNK, dtype=np.int16)
        )
    return frames


@settings(max_examples=100, deadline=None)
@given(config=_valid_chains(), frames=_int16_frames())
def test_dsp_output_int16_chunk_and_deterministic(config, frames):
    """PRESERVATION 3.1 (PBT) — for any valid chain + int16 input, apply_effects
    returns int16[CHUNK] and is deterministic for a fixed seeded state.

    Validates: Requirements 3.1

    Resets the DSP state before EACH measured run so the run is reproducible,
    then asserts two clean-state runs over the same input produce identical
    bytes, and that every output frame is int16 of length CHUNK.

    Passes on the UNFIXED code — this is the non-racing (single-threaded)
    behaviour preservation must keep intact.
    """
    # First measured run from a clean slate.
    micwebcontroller.effects_config = {
        name: dict(params) for name, params in config.items()
    }
    micwebcontroller._reset_audio_dsp_state()
    first = []
    for frame in frames:
        out = micwebcontroller.apply_effects(frame)
        assert out.dtype == np.int16
        assert len(out) == micwebcontroller.CHUNK
        first.append(out.tobytes())

    # Second measured run from an identical clean slate — must match exactly.
    micwebcontroller.effects_config = {
        name: dict(params) for name, params in config.items()
    }
    micwebcontroller._reset_audio_dsp_state()
    second = []
    for frame in frames:
        out = micwebcontroller.apply_effects(frame)
        assert out.dtype == np.int16
        assert len(out) == micwebcontroller.CHUNK
        second.append(out.tobytes())

    assert first == second, (
        "apply_effects not deterministic for a fixed seeded state and chain"
    )


# ===========================================================================
# Task 2.2 — PRESERVATION tests (Property 2): Endpoint behavior
# ---------------------------------------------------------------------------
# OBSERVATION-FIRST preservation tests for the voice-FX HTTP surface. They
# exercise each endpoint on the UNFIXED module, observe the real status code /
# JSON payload / persisted ConfigStore state, and assert exactly those
# observations. They are the non-racing (single request thread) baseline Task
# 3's synchronization fix must preserve. All MUST PASS on unfixed code.
#
# Endpoints covered:
#   - POST /effects   (style branch + single-effect branch)
#   - POST /effects/save
#   - POST /effects/revert
#   - POST /config
# plus a PBT over valid effect names/amounts and valid style names, and an
# invalid-body rejection-without-mutation property.
#
# Isolation: micwebcontroller keeps the live chain (effects_config), the
# ConfigStore, the saved voice-style cache (saved_voice_styles), and the
# selected-style marker (current_style) as module globals that the handlers
# read/write. Each test rebuilds a fresh temp-backed ConfigStore and resets
# those globals so examples never bleed into one another (hypothesis reuses the
# process across examples).
#
# _Requirements: 3.2
# ===========================================================================

import json as _json  # noqa: E402
import tempfile  # noqa: E402

from config_store import ConfigStore as _ConfigStore  # noqa: E402
from config_store import VOICE_STYLES_KEY, VOICE_STYLES_PREVIOUS_KEY  # noqa: E402


def _factory_effects_config():
    """A fresh copy of the module's shipped default live effects_config.

    Matches micwebcontroller.effects_config at import time, so each test starts
    the live chain from the same known baseline regardless of order.
    """
    return {
        'pitch':      {'enabled': True,  'amount': 0.85},
        'distortion': {'enabled': False, 'amount': 0.40},
        'echo':       {'enabled': True,  'amount': 0.50},
        'reverb':     {'enabled': False, 'amount': 0.30},
        'tremolo':    {'enabled': False, 'amount': 0.50},
        'bitcrush':   {'enabled': False, 'amount': 0.40},
        'ring_mod':   {'enabled': False, 'amount': 0.50},
    }


def _fresh_store_env():
    """Point micwebcontroller at a brand-new temp-backed ConfigStore and reset
    the voice-FX module globals the endpoints read/write.

    Returns:
        (store, config_path) — the installed ConfigStore and its temp path, so
        a test can re-load persisted state from disk via a fresh store.
    """
    config_path = os.path.join(tempfile.mkdtemp(), "tuning_fx.json")
    store = _ConfigStore(config_path=config_path)
    micwebcontroller.config_store = store
    micwebcontroller.effects_config = _factory_effects_config()
    micwebcontroller.saved_voice_styles = store.load_voice_styles()  # {} (no file yet)
    micwebcontroller.current_style = {'value': None}
    micwebcontroller.app.config["TESTING"] = True
    return store, config_path


def _stored_voice_section(config_path):
    """Return the raw persisted voice-style sections from disk as a dict.

    Reads the tuning file directly rather than through the store, so the test
    asserts on exactly what was persisted. Returns the voice_styles and
    voice_styles_previous slots (each defaulting to a sentinel when absent).
    """
    if not os.path.exists(config_path):
        return {"voice_styles": "<<no file>>", "voice_styles_previous": "<<no file>>"}
    with open(config_path, "r", encoding="utf-8") as f:
        raw = _json.load(f)
    return {
        "voice_styles": raw.get(VOICE_STYLES_KEY, "<<absent>>"),
        "voice_styles_previous": raw.get(VOICE_STYLES_PREVIOUS_KEY, "<<absent>>"),
    }


# ---------------------------------------------------------------------------
# POST /effects — style branch
# ---------------------------------------------------------------------------

def test_effects_style_branch_status_payload_and_state():
    """PRESERVATION 3.2 — POST /effects {"style": ...} observed behavior.

    OBSERVE on unfixed code: selecting a valid style returns 200 with
    status/style/tuned/effects, overwrites the live effects_config with that
    style's factory chain, and sets current_style. No file is written (a style
    *select* persists nothing — only /effects/save does). ASSERT exactly that.
    """
    _fresh_store_env()
    expected_chain = micwebcontroller._factory_preset('ghost')

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects", json={"style": "ghost"})

    # Observed status + payload.
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == "ghost"
    assert payload["tuned"] is False          # no saved override exists yet
    assert payload["effects"] == expected_chain

    # Observed live-state mutation: the whole chain was swapped to ghost.
    assert micwebcontroller.effects_config == expected_chain
    assert micwebcontroller.current_style["value"] == "ghost"


# ---------------------------------------------------------------------------
# POST /effects — single-effect branch (toggle enabled / set amount)
# ---------------------------------------------------------------------------

def test_effects_single_effect_branch_status_payload_and_state():
    """PRESERVATION 3.2 — POST /effects {"effect": ...} observed behavior.

    OBSERVE on unfixed code: toggling enabled + setting amount for one effect
    returns 200 with status/effect/config echoing that single effect's new
    state, mutates ONLY that effect in the live chain, and leaves the rest of
    the chain untouched. No file write. ASSERT exactly that.
    """
    _fresh_store_env()
    before = _factory_effects_config()

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects", json={"effect": "reverb", "enabled": True, "amount": 0.7})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["effect"] == "reverb"
    assert payload["config"] == {"enabled": True, "amount": 0.7}

    # Only reverb changed; every other effect is byte-for-byte the prior value.
    assert micwebcontroller.effects_config["reverb"] == {"enabled": True, "amount": 0.7}
    for name in before:
        if name != "reverb":
            assert micwebcontroller.effects_config[name] == before[name]


# ---------------------------------------------------------------------------
# POST /effects/save
# ---------------------------------------------------------------------------

def test_effects_save_status_payload_and_persisted_state():
    """PRESERVATION 3.2 — POST /effects/save observed behavior + persistence.

    OBSERVE on unfixed code: saving the live chain under a style returns 200
    with status/style/effects/can_revert, and persists the sanitised chain into
    the ConfigStore's voice_styles section (readable back via a fresh store).
    ASSERT exactly that.
    """
    _, config_path = _fresh_store_env()

    # Load a style and tweak the live chain, then save under that style.
    with micwebcontroller.app.test_client() as c:
        c.post("/effects", json={"style": "ghost"})
        c.post("/effects", json={"effect": "echo", "amount": 0.42})
        resp = c.post("/effects/save", json={"style": "ghost"})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == "ghost"
    # Payload echoes the sanitised live chain that was saved.
    assert payload["effects"]["echo"]["amount"] == 0.42
    assert payload["effects"]["echo"]["enabled"] is True
    assert payload["can_revert"] is True   # a prior-snapshot slot was written

    # Persisted state: a fresh store reads back the saved override for ghost.
    reloaded = _ConfigStore(config_path=config_path).load_voice_styles()
    assert "ghost" in reloaded
    assert reloaded["ghost"]["echo"]["amount"] == 0.42
    # The persisted chain equals the payload chain (same sanitised result).
    assert reloaded["ghost"] == payload["effects"]


# ---------------------------------------------------------------------------
# POST /effects/revert
# ---------------------------------------------------------------------------

def test_effects_revert_status_payload_and_persisted_state():
    """PRESERVATION 3.2 — POST /effects/revert observed behavior + persistence.

    OBSERVE on unfixed code: after a save creates a ghost override (prior slot =
    None → factory), reverting restores the factory preset, returns 200 with
    status/style/effects/can_revert, applies it to the live chain, and persists
    it as the saved override. ASSERT exactly that.
    """
    _, config_path = _fresh_store_env()
    factory_ghost = micwebcontroller._factory_preset('ghost')

    with micwebcontroller.app.test_client() as c:
        # Select ghost, tweak, and save → prior snapshot is None (first save),
        # so revert should restore the factory preset for ghost.
        c.post("/effects", json={"style": "ghost"})
        c.post("/effects", json={"effect": "echo", "amount": 0.42})
        c.post("/effects/save", json={"style": "ghost"})
        resp = c.post("/effects/revert", json={})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == "ghost"
    # Reverted to the factory ghost preset (prior override was None).
    assert payload["effects"] == factory_ghost

    # Applied to the live chain.
    assert micwebcontroller.effects_config == factory_ghost
    assert micwebcontroller.current_style["value"] == "ghost"

    # Persisted: the saved override now matches the reverted (factory) chain.
    reloaded = _ConfigStore(config_path=config_path).load_voice_styles()
    assert reloaded["ghost"] == factory_ghost


def test_effects_revert_nothing_to_revert_returns_409():
    """PRESERVATION 3.2 — POST /effects/revert with nothing to revert.

    OBSERVE on unfixed code: with no prior save, revert returns 409 with an
    error status and persists nothing. ASSERT exactly that.
    """
    _, config_path = _fresh_store_env()

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects/revert", json={})

    assert resp.status_code == 409
    payload = resp.get_json()
    assert payload["status"] == "error"
    # No voice-style file was ever written.
    assert not os.path.exists(config_path)


# ---------------------------------------------------------------------------
# POST /config
# ---------------------------------------------------------------------------

def test_config_endpoint_status_payload_and_persisted_state():
    """PRESERVATION 3.2 — POST /config observed behavior + persistence.

    OBSERVE on unfixed code: a valid jaw-profile update returns 200 with
    status/profile/updated/profiles, and persists both profiles to the
    ConfigStore (readable back via a fresh store). ASSERT exactly that. This
    mirrors the existing coverage in tests/test_micwebcontroller.py but is kept
    here so Task 2.2's endpoint-preservation suite is self-contained.
    """
    from config_store import PROFILE_MIC as _PROFILE_MIC  # noqa: E402

    store, config_path = _fresh_store_env()
    # /config reads/writes micwebcontroller.jaw_profiles; reset it against the
    # fresh temp store too so this test is isolated.
    micwebcontroller.jaw_profiles = store.load_profiles()

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/config", json={"profile": _PROFILE_MIC, "silence_floor": 700})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["profile"] == _PROFILE_MIC
    assert payload["updated"]["silence_floor"] == 700
    assert payload["profiles"][_PROFILE_MIC]["silence_floor"] == 700

    # Persisted: a fresh store reads back the updated mic profile.
    reloaded = _ConfigStore(config_path=config_path).load_profiles()
    assert reloaded[_PROFILE_MIC]["silence_floor"] == 700


# ---------------------------------------------------------------------------
# PBT — valid effect names/amounts + valid style names accepted with the
# expected effect; invalid-body rejection WITHOUT mutation.
# ---------------------------------------------------------------------------

_EFFECT_NAME = st.sampled_from(
    ('pitch', 'distortion', 'echo', 'reverb', 'tremolo', 'bitcrush', 'ring_mod')
)
_STYLE_NAME = st.sampled_from(('natural', 'demon', 'ghost', 'robot', 'possessed'))
_VALID_AMOUNT = st.floats(
    min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False
)


@settings(max_examples=100, deadline=None)
@given(effect=_EFFECT_NAME, enabled=st.booleans(), amount=_VALID_AMOUNT)
def test_pbt_effects_single_effect_valid_accepted(effect, enabled, amount):
    """PRESERVATION 3.2 (PBT) — any valid single-effect update is accepted and
    changes ONLY the named effect.

    Validates: Requirements 3.2

    For any known effect name, boolean enabled, and amount >= 0, POST /effects
    returns 200 echoing that effect's new state, mutates only that effect in the
    live chain, and leaves every other effect at its prior value.
    """
    _fresh_store_env()
    before = _factory_effects_config()

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects", json={"effect": effect, "enabled": enabled, "amount": amount})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["effect"] == effect
    assert payload["config"] == {"enabled": enabled, "amount": amount}

    # Only the named effect changed.
    assert micwebcontroller.effects_config[effect] == {"enabled": enabled, "amount": amount}
    for name in before:
        if name != effect:
            assert micwebcontroller.effects_config[name] == before[name]


@settings(max_examples=100, deadline=None)
@given(style=_STYLE_NAME)
def test_pbt_effects_style_valid_accepted(style):
    """PRESERVATION 3.2 (PBT) — any valid style name loads its factory chain.

    Validates: Requirements 3.2

    For any known style, POST /effects {"style": style} returns 200, swaps the
    live chain to that style's factory preset (no saved override exists in a
    fresh store), and marks it current.
    """
    _fresh_store_env()
    expected = micwebcontroller._factory_preset(style)

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects", json={"style": style})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["style"] == style
    assert payload["tuned"] is False
    assert payload["effects"] == expected
    assert micwebcontroller.effects_config == expected
    assert micwebcontroller.current_style["value"] == style


@settings(max_examples=100, deadline=None)
@given(
    bad=st.one_of(
        # Unknown effect name.
        st.fixed_dictionaries({"effect": st.text(max_size=12).filter(
            lambda s: s.lower() not in micwebcontroller.effects_config)}),
        # Unknown style name.
        st.fixed_dictionaries({"style": st.text(max_size=12).filter(
            lambda s: s.lower() not in micwebcontroller.STYLE_PRESETS)}),
        # Negative amount on a known effect.
        st.fixed_dictionaries({
            "effect": _EFFECT_NAME,
            "amount": st.floats(min_value=-1000.0, max_value=-0.0001,
                                allow_nan=False, allow_infinity=False),
        }),
        # Neither style nor effect provided.
        st.just({"nonsense": "value"}),
    )
)
def test_pbt_effects_invalid_body_rejected_without_mutation(bad):
    """PRESERVATION 3.2 (PBT) — invalid /effects bodies are rejected WITHOUT
    mutating the live effects_config.

    Validates: Requirements 3.2

    For an unknown effect, an unknown style, a negative amount, or a body with
    neither selector, POST /effects returns 400 with an error status and leaves
    the live effects_config byte-for-byte unchanged.
    """
    _fresh_store_env()
    before = _factory_effects_config()

    with micwebcontroller.app.test_client() as c:
        resp = c.post("/effects", json=bad)

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["status"] == "error"
    # The live chain is untouched by any rejected request.
    assert micwebcontroller.effects_config == before


# ===========================================================================
# Task 2.3 — PRESERVATION tests (Property 2): Lifecycle unaffected
# ---------------------------------------------------------------------------
# OBSERVATION-FIRST preservation tests for the stream lifecycle at the STUB
# boundary. They exercise start_streaming / stop_streaming / _release_gpio and
# the audio_callback's is_streaming short-circuit on the UNFIXED module, observe
# the real stub-level behavior (is_streaming transitions, thread start, PyAudio
# open/stop/close/terminate calls, GPIO device + pin-factory release), and
# assert exactly those observations. This is the non-racing (NOT isBugCondition)
# baseline Task 3's synchronization fix must preserve. All MUST PASS on unfixed
# code — the fix adds a lock only inside the DSP work, leaving these lifecycle
# paths observably identical.
#
# How the stub boundary is used:
#   - pyaudio.PyAudio is the module-level MagicMock installed above. Each test
#     installs a FRESH per-test MagicMock as the PyAudio() instance so the
#     audio.open(...) stream handle, stop_stream/close, and terminate() calls
#     are observable and isolated.
#   - stream_mic() calls audio_state['audio'].open(..., stream_callback=cb) and
#     returns the stub stream; capturing the cb kwarg gives us the real
#     audio_callback closure to exercise the is_streaming short-circuit directly.
#   - gpiozero devices / Device.pin_factory are MagicMocks whose .off()/.close()
#     calls are observable, so _release_gpio's release behavior is asserted at
#     the stub level.
#
# _Requirements: 3.4
# ===========================================================================

import pytest  # noqa: E402


def _reset_lifecycle_state():
    """Reset the audio_state lifecycle slots to the not-streaming baseline and
    null the GPIO device globals, so each lifecycle test starts clean regardless
    of order (hypothesis / other tests reuse the process)."""
    micwebcontroller.audio_state['is_streaming'] = False
    micwebcontroller.audio_state['is_recording'] = False
    micwebcontroller.audio_state['stream'] = None
    micwebcontroller.audio_state['audio'] = None
    micwebcontroller.audio_state['thread'] = None
    micwebcontroller.audio_state['record_frames'] = []
    micwebcontroller.jaw_motor = None
    micwebcontroller.led_eye_light = None


def _install_fresh_pyaudio_instance():
    """Point the pyaudio stub's PyAudio() factory at a fresh MagicMock instance
    and return (instance, stream, captured) where:

      - instance is what PyAudio() returns (so .terminate() is observable),
      - stream is what instance.open(...) returns (so stop_stream/close are
        observable),
      - captured['callback'] receives the stream_callback stream_mic() passes to
        instance.open(...), i.e. the real audio_callback closure.

    stream_mic() runs on the start_streaming() thread, so open(...) is captured
    there; the fake open blocks nothing (returns immediately)."""
    instance = MagicMock(name="PyAudioInstance")
    stream = MagicMock(name="PyAudioStream")
    captured = {}

    def _fake_open(*args, **kwargs):
        captured['callback'] = kwargs.get('stream_callback')
        captured['open_kwargs'] = kwargs
        return stream

    instance.open.side_effect = _fake_open
    micwebcontroller.pyaudio.PyAudio = MagicMock(name="PyAudio", return_value=instance)
    return instance, stream, captured


# ---------------------------------------------------------------------------
# start_streaming — observable stub-level behavior
# ---------------------------------------------------------------------------

def test_start_streaming_sets_flag_starts_thread_and_opens_stream():
    """PRESERVATION 3.4 — start_streaming observable behavior.

    OBSERVE on unfixed code: from a not-streaming baseline, start_streaming
    returns success, flips is_streaming True, constructs a PyAudio() instance,
    runs its stream thread (which calls audio.open with a stream_callback and
    stores the returned stream handle). ASSERT exactly that at the stub level.
    """
    _reset_lifecycle_state()
    instance, stream, captured = _install_fresh_pyaudio_instance()

    # start_streaming() calls jsonify, which needs a Flask application context.
    with micwebcontroller.app.app_context():
        micwebcontroller.start_streaming()
        # start_streaming spawns a thread that runs stream_mic(); wait for it so
        # the open(...) call and stream-handle store are observable.
        if micwebcontroller.audio_state['thread'] is not None:
            micwebcontroller.audio_state['thread'].join(timeout=5.0)

    assert micwebcontroller.audio_state['is_streaming'] is True
    # A PyAudio instance was constructed and a stream opened with a callback.
    assert micwebcontroller.pyaudio.PyAudio.called
    assert instance.open.called
    assert captured.get('callback') is not None
    # stream_mic()'s returned handle was stored for later teardown.
    assert micwebcontroller.audio_state['stream'] is stream
    # The returned handle had start_stream() invoked (stream_mic starts it).
    assert stream.start_stream.called

    # Clean up so we don't leave the module marked streaming for later tests.
    _reset_lifecycle_state()


def test_start_streaming_when_already_streaming_returns_error_400():
    """PRESERVATION 3.4 — start_streaming is a no-op error when already live.

    OBSERVE on unfixed code: calling start_streaming while is_streaming is True
    returns a (response, 400) tuple with an error status and does NOT construct
    a new PyAudio instance. ASSERT exactly that.
    """
    _reset_lifecycle_state()
    micwebcontroller.audio_state['is_streaming'] = True
    micwebcontroller.pyaudio.PyAudio = MagicMock(name="PyAudio")

    with micwebcontroller.app.app_context():
        result = micwebcontroller.start_streaming()
        assert isinstance(result, tuple)
        resp, status = result
        assert status == 400
        assert resp.get_json()["status"] == "error"

    # No new PyAudio instance was built for the rejected call.
    assert not micwebcontroller.pyaudio.PyAudio.called

    _reset_lifecycle_state()


# ---------------------------------------------------------------------------
# stop_streaming — observable stub-level behavior
# ---------------------------------------------------------------------------

def test_stop_streaming_clears_flag_tears_down_stream_and_audio():
    """PRESERVATION 3.4 — stop_streaming observable teardown.

    OBSERVE on unfixed code: from a streaming state (with a stub stream + audio
    and a joined thread), stop_streaming flips is_streaming False, stops+closes
    the live stream, terminates PyAudio, and nulls the stream/audio slots.
    ASSERT exactly that at the stub level.
    """
    _reset_lifecycle_state()
    instance = MagicMock(name="PyAudioInstance")
    stream = MagicMock(name="PyAudioStream")
    thread = MagicMock(name="StreamThread")

    micwebcontroller.audio_state['is_streaming'] = True
    micwebcontroller.audio_state['audio'] = instance
    micwebcontroller.audio_state['stream'] = stream
    micwebcontroller.audio_state['thread'] = thread

    # Give _release_gpio real stub devices + pin factory so stop runs end-to-end.
    jaw = MagicMock(name="jaw_motor")
    eye = MagicMock(name="led_eye_light")
    micwebcontroller.jaw_motor = jaw
    micwebcontroller.led_eye_light = eye
    micwebcontroller.Device.pin_factory = MagicMock(name="pin_factory")

    with micwebcontroller.app.app_context():
        micwebcontroller.stop_streaming()

    # Flag cleared and thread joined.
    assert micwebcontroller.audio_state['is_streaming'] is False
    assert thread.join.called
    # Stream stopped + closed BEFORE PyAudio terminate (orderly teardown).
    assert stream.stop_stream.called
    assert stream.close.called
    assert instance.terminate.called
    # Slots nulled for the next session.
    assert micwebcontroller.audio_state['stream'] is None
    assert micwebcontroller.audio_state['audio'] is None

    _reset_lifecycle_state()


def test_stop_streaming_when_not_streaming_returns_error_400():
    """PRESERVATION 3.4 — stop_streaming is an error no-op when idle.

    OBSERVE on unfixed code: calling stop_streaming while is_streaming is False
    returns a (response, 400) tuple with an error status. ASSERT exactly that.
    """
    _reset_lifecycle_state()

    with micwebcontroller.app.app_context():
        result = micwebcontroller.stop_streaming()
        assert isinstance(result, tuple)
        resp, status = result
        assert status == 400
        assert resp.get_json()["status"] == "error"

    _reset_lifecycle_state()


# ---------------------------------------------------------------------------
# audio_callback is_streaming short-circuit
# ---------------------------------------------------------------------------

def test_callback_short_circuits_passthrough_when_not_streaming():
    """PRESERVATION 3.4 — the callback's is_streaming short-circuit.

    OBSERVE on unfixed code: the audio_callback (captured from stream_mic's
    audio.open call) returns the UNPROCESSED input bytes paired with
    pyaudio.paContinue, and does NO DSP work, when is_streaming is False. We
    prove "no DSP work" by enabling echo and asserting the shared echo_buffer is
    not appended to (a processed frame would push a new entry). ASSERT exactly
    this current short-circuit behavior.
    """
    _reset_lifecycle_state()
    instance, stream, captured = _install_fresh_pyaudio_instance()

    # Bring up a stream to capture the real callback closure, then join.
    with micwebcontroller.app.app_context():
        micwebcontroller.start_streaming()
        if micwebcontroller.audio_state['thread'] is not None:
            micwebcontroller.audio_state['thread'].join(timeout=5.0)
    callback = captured.get('callback')
    assert callback is not None, "stream_mic did not pass a stream_callback"

    # Enable echo so a PROCESSED frame would append to the shared echo_buffer.
    micwebcontroller.effects_config = _echo_only_config()
    micwebcontroller._reset_audio_dsp_state()

    # Short-circuit path: is_streaming False → passthrough, no DSP.
    micwebcontroller.audio_state['is_streaming'] = False
    buf_before = list(micwebcontroller.audio_state['echo_buffer'])

    frame = np.zeros(micwebcontroller.CHUNK, dtype=np.int16)
    in_bytes = frame.tobytes()
    ret_data, ret_flag = callback(in_bytes, micwebcontroller.CHUNK, {}, 0)

    # Passthrough: exact input bytes returned, with the paContinue flag.
    assert ret_data == in_bytes
    assert ret_flag == micwebcontroller.pyaudio.paContinue
    # No DSP work happened: the echo buffer deque was not appended to.
    buf_after = list(micwebcontroller.audio_state['echo_buffer'])
    assert len(buf_after) == len(buf_before)
    assert all(np.array_equal(a, b) for a, b in zip(buf_after, buf_before))

    _reset_lifecycle_state()


def test_callback_processes_frame_when_streaming():
    """PRESERVATION 3.4 — while streaming the callback DOES process (contrast).

    OBSERVE on unfixed code: with is_streaming True and echo enabled, the
    captured callback returns a (processed_bytes, paContinue) tuple of the right
    size and DOES advance the shared echo_buffer (DSP ran). This is the
    transparent (non-short-circuit) side of the same guard, confirming the
    short-circuit is the ONLY behavioral branch the guard adds. ASSERT that.
    """
    _reset_lifecycle_state()
    instance, stream, captured = _install_fresh_pyaudio_instance()

    with micwebcontroller.app.app_context():
        micwebcontroller.start_streaming()
        if micwebcontroller.audio_state['thread'] is not None:
            micwebcontroller.audio_state['thread'].join(timeout=5.0)
    callback = captured.get('callback')
    assert callback is not None

    micwebcontroller.effects_config = _echo_only_config()
    micwebcontroller._reset_audio_dsp_state()
    # start_streaming() already set is_streaming True and the thread has exited;
    # the flag is still True (stop was never called), so the guard is transparent.
    assert micwebcontroller.audio_state['is_streaming'] is True

    frame = np.zeros(micwebcontroller.CHUNK, dtype=np.int16)
    ret_data, ret_flag = callback(frame.tobytes(), micwebcontroller.CHUNK, {}, 0)

    assert ret_flag == micwebcontroller.pyaudio.paContinue
    # Processed output is int16[CHUNK] worth of bytes.
    assert len(ret_data) == micwebcontroller.CHUNK * 2
    # DSP ran: _fx_echo appended this frame to the shared echo_buffer, so the
    # most-recent entry equals the input frame (echo buffer is maxlen=3).
    last = micwebcontroller.audio_state['echo_buffer'][-1]
    assert np.array_equal(last, frame)

    _reset_lifecycle_state()


# ---------------------------------------------------------------------------
# _release_gpio — observable stub-level device + pin-factory release
# ---------------------------------------------------------------------------

def test_release_gpio_closes_devices_and_pin_factory():
    """PRESERVATION 3.4 — _release_gpio device + pin-factory release.

    OBSERVE on unfixed code (the mic-gpio-line-release-leak fix): _release_gpio
    turns off and closes both GPIO device globals, nulls them, then closes the
    gpiozero pin factory (Device.pin_factory.close()) and nulls Device.pin_factory
    so the next session rebuilds it lazily. It also resets jaw_open/below_count.
    ASSERT exactly that at the stub level.
    """
    _reset_lifecycle_state()
    jaw = MagicMock(name="jaw_motor")
    eye = MagicMock(name="led_eye_light")
    factory = MagicMock(name="pin_factory")
    micwebcontroller.jaw_motor = jaw
    micwebcontroller.led_eye_light = eye
    micwebcontroller.Device.pin_factory = factory
    micwebcontroller.jaw_open = True
    micwebcontroller.below_count = 7

    micwebcontroller._release_gpio()

    # Each device turned off and closed (closing frees its underlying pin).
    assert jaw.off.called and jaw.close.called
    assert eye.off.called and eye.close.called
    # Device globals nulled so talk() re-creates them next stream.
    assert micwebcontroller.jaw_motor is None
    assert micwebcontroller.led_eye_light is None
    # Pin factory closed (drops the kernel line claims) and nulled.
    assert factory.close.called
    assert micwebcontroller.Device.pin_factory is None
    # Jaw envelope reset to closed/zero.
    assert micwebcontroller.jaw_open is False
    assert micwebcontroller.below_count == 0

    _reset_lifecycle_state()


def test_release_gpio_is_noop_when_devices_none():
    """PRESERVATION 3.4 — _release_gpio is safe when devices are already None.

    OBSERVE on unfixed code: with both device globals None, _release_gpio makes
    no device calls, leaves them None, still attempts the pin-factory release
    when a factory is present (nulling it), and resets the jaw envelope. ASSERT
    exactly that.
    """
    _reset_lifecycle_state()
    micwebcontroller.jaw_motor = None
    micwebcontroller.led_eye_light = None
    factory = MagicMock(name="pin_factory")
    micwebcontroller.Device.pin_factory = factory
    micwebcontroller.jaw_open = True
    micwebcontroller.below_count = 3

    micwebcontroller._release_gpio()

    assert micwebcontroller.jaw_motor is None
    assert micwebcontroller.led_eye_light is None
    # Factory still present → closed and nulled.
    assert factory.close.called
    assert micwebcontroller.Device.pin_factory is None
    assert micwebcontroller.jaw_open is False
    assert micwebcontroller.below_count == 0

    _reset_lifecycle_state()

# ===========================================================================
# Task 2.4 — PRESERVATION tests (Property 2): _reset_audio_dsp_state intent
# ---------------------------------------------------------------------------
# OBSERVATION-FIRST preservation tests for _reset_audio_dsp_state(). They call
# the reset on the UNFIXED module and assert the EXACT transient-DSP-state intent
# it produces today, so Task 3.4 (which only serialises the write under a lock)
# must reproduce it byte-for-byte:
#
#   - echo buffer   : a fresh deque, len 3, maxlen 3, every element a zero
#                     int16 array of length CHUNK (observed element type/shape).
#   - reverb buffer : a fresh zero float32 array of length 9600, reverb_pos 0.
#   - effect phases : effect_state tremolo_phase / ring_phase both 0.0
#                     (observed keys), and no extra keys introduced.
#   - jaw envelope  : jaw_open=False, below_count=0, avg_level=0.0 (the exact
#                     jaw-envelope globals the function resets).
#
# These are single-threaded (non-racing) — the "NOT isBugCondition" scope that
# preservation covers — and MUST PASS on the unfixed code (no source change).
#
# _Requirements: 2.4
# ===========================================================================

from collections import deque as _deque  # noqa: E402


def _dirty_dsp_state():
    """Scribble non-reset values over every piece of transient DSP state the
    reset is supposed to clear, so the assertions prove the reset actually
    overwrote them rather than finding them already-clean."""
    # Jaw envelope globals -> non-reset values.
    micwebcontroller.jaw_open = True
    micwebcontroller.below_count = 11
    micwebcontroller.avg_level = 123.456
    # Effect phases -> non-zero.
    micwebcontroller.effect_state['tremolo_phase'] = 1.2345
    micwebcontroller.effect_state['ring_phase'] = 2.6789
    # Echo buffer -> a differently-shaped deque with non-zero content.
    micwebcontroller.audio_state['echo_buffer'] = _deque(
        [np.full(micwebcontroller.CHUNK, 7, dtype=np.int16)], maxlen=5)
    # Reverb buffer/pos -> non-zero, wrong length.
    micwebcontroller.audio_state['reverb_buffer'] = np.ones(10, dtype=np.float32)
    micwebcontroller.audio_state['reverb_pos'] = 4242


def test_reset_audio_dsp_state_echo_deque_intent():
    """PRESERVATION 2.4 — fresh zero echo deque (len 3, maxlen 3, zero int16).

    OBSERVE on unfixed code: _reset_audio_dsp_state() rebinds
    audio_state['echo_buffer'] to a brand-new deque holding 3 entries, maxlen 3,
    each a zero-filled int16 array of length CHUNK. ASSERT exactly that shape,
    maxlen, dtype, and all-zero content.
    """
    _dirty_dsp_state()
    micwebcontroller._reset_audio_dsp_state()

    echo = micwebcontroller.audio_state['echo_buffer']
    # It is a fresh deque, bounded to 3.
    assert isinstance(echo, _deque)
    assert len(echo) == 3
    assert echo.maxlen == 3
    # Each element is a zero int16 array of length CHUNK (observed element form).
    for old in echo:
        assert isinstance(old, np.ndarray)
        assert old.dtype == np.int16
        assert old.shape == (micwebcontroller.CHUNK,)
        assert np.array_equal(old, np.zeros(micwebcontroller.CHUNK, dtype=np.int16))


def test_reset_audio_dsp_state_reverb_buffer_intent():
    """PRESERVATION 2.4 — fresh zero reverb buffer (len 9600) at pos 0.

    OBSERVE on unfixed code: the reset rebinds audio_state['reverb_buffer'] to a
    fresh zero float32 array of length 9600 and sets reverb_pos to 0. ASSERT
    exactly that dtype, length, zero content, and position.
    """
    _dirty_dsp_state()
    micwebcontroller._reset_audio_dsp_state()

    buf = micwebcontroller.audio_state['reverb_buffer']
    assert isinstance(buf, np.ndarray)
    assert buf.dtype == np.float32
    assert buf.shape == (9600,)
    assert len(buf) == 9600
    assert np.array_equal(buf, np.zeros(9600, dtype=np.float32))
    # Position rewound to the start of the fresh buffer.
    assert micwebcontroller.audio_state['reverb_pos'] == 0


def test_reset_audio_dsp_state_effect_phases_zeroed():
    """PRESERVATION 2.4 — tremolo/ring phases zeroed (observed effect_state keys).

    OBSERVE on unfixed code: the reset zeroes effect_state['tremolo_phase'] and
    effect_state['ring_phase']. These are the only keys the function manages, so
    assert both are 0.0 and that the reset introduces no extra keys.
    """
    _dirty_dsp_state()
    micwebcontroller._reset_audio_dsp_state()

    assert micwebcontroller.effect_state['tremolo_phase'] == 0.0
    assert micwebcontroller.effect_state['ring_phase'] == 0.0
    # The exact phase keys the reset manages — nothing more, nothing less.
    assert set(micwebcontroller.effect_state.keys()) == {'tremolo_phase', 'ring_phase'}


def test_reset_audio_dsp_state_jaw_envelope_reset():
    """PRESERVATION 2.4 — jaw envelope globals reset to their rest values.

    OBSERVE on unfixed code: the reset sets the jaw-envelope globals
    jaw_open=False, below_count=0, avg_level=0.0. ASSERT exactly those reset
    values (the three globals the function declares and clears).
    """
    _dirty_dsp_state()
    micwebcontroller._reset_audio_dsp_state()

    assert micwebcontroller.jaw_open is False
    assert micwebcontroller.below_count == 0
    assert micwebcontroller.avg_level == 0.0


def test_reset_audio_dsp_state_rebinds_fresh_objects():
    """PRESERVATION 2.4 — the reset REBINDS fresh echo/reverb objects.

    OBSERVE on unfixed code: _reset_audio_dsp_state() replaces the echo deque and
    reverb buffer with brand-new objects (it does not mutate the old ones in
    place). ASSERT the post-reset objects are not the same instances as the
    dirtied ones — this is the rebind behaviour whose unsynchronised timing is
    the race Task 3 serialises, and which the fix must preserve.
    """
    _dirty_dsp_state()
    old_echo = micwebcontroller.audio_state['echo_buffer']
    old_reverb = micwebcontroller.audio_state['reverb_buffer']

    micwebcontroller._reset_audio_dsp_state()

    assert micwebcontroller.audio_state['echo_buffer'] is not old_echo
    assert micwebcontroller.audio_state['reverb_buffer'] is not old_reverb
