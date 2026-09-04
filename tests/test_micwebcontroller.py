"""Tests for micwebcontroller.py's profile-aware Flask endpoints.

Covers the web layer of the separate-jaw-tuning-profiles feature:
  - POST /config out-of-bounds rejection without mutation   (Property 4)
  - POST /config unknown-profile rejection without mutation  (Property 5)
  - POST /config valid updates echo + GET /status both-profile echo (unit)

Property tests use hypothesis (minimum 100 iterations each). Example-based
unit tests use pytest and the Flask test client. Run with:

    pytest tests/test_micwebcontroller.py -q --maxfail=1

HARDWARE / IMPORT STUBBING
--------------------------
micwebcontroller.py imports `pyaudio` and `gpiozero` at module top and pulls in
`utils.audio_utils` and `model.constants`. It also constructs a module-level
`config_store = ConfigStore()` and `jaw_profiles = config_store.load_profiles()`
at import time. On a dev/CI box with no sound card or GPIO these imports would
fail, so BEFORE importing micwebcontroller we install lightweight stub modules
into sys.modules for the hardware-facing dependencies. The stubs are plain
MagicMock-backed objects — the endpoints under test (/config, /status) never
touch PyAudio or GPIO, so the mocks are never exercised during these tests.

To keep every test hermetic and deterministic, a fixture points the store at a
fresh temp file (via ANIMATRONIC_JAW_CONFIG, set before import) and, per test,
rebuilds `micwebcontroller.config_store` / `micwebcontroller.jaw_profiles`
against a unique temp path. Because the /config handler mutates the module
global `jaw_profiles` (`global jaw_profiles; jaw_profiles = ...`), reassigning
the module attribute in the fixture is what the handler reads and writes.
"""

import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest
from hypothesis import assume, given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# ---------------------------------------------------------------------------
# Install hardware/dependency stubs BEFORE importing micwebcontroller.
# These modules would otherwise fail to import (no sound card / no GPIO) or
# pull hardware in transitively. The endpoints under test never call into them.
# ---------------------------------------------------------------------------

# Point the store at a temp path from the very first import so the module-level
# ConfigStore() built at import time never touches a real home-directory file.
os.environ["ANIMATRONIC_JAW_CONFIG"] = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    ".pytest_import_jaw_config.json",
)


def _install_stub(name, module):
    """Register a stub module under `name` only if not already importable."""
    if name not in sys.modules:
        sys.modules[name] = module


# --- pyaudio stub: paInt16 / paContinue constants + PyAudio() factory. -------
_pyaudio_stub = types.ModuleType("pyaudio")
_pyaudio_stub.paInt16 = 8          # real value; only needs to be a stable int
_pyaudio_stub.paContinue = 0
_pyaudio_stub.PyAudio = MagicMock(name="PyAudio")
_install_stub("pyaudio", _pyaudio_stub)

# --- gpiozero stub: PWMLED / LED / DigitalOutputDevice as callables. ---------
_gpiozero_stub = types.ModuleType("gpiozero")
_gpiozero_stub.PWMLED = MagicMock(name="PWMLED")
_gpiozero_stub.LED = MagicMock(name="LED")
_gpiozero_stub.DigitalOutputDevice = MagicMock(name="DigitalOutputDevice")
_install_stub("gpiozero", _gpiozero_stub)

# --- utils.audio_utils stub: AudioUtils may pull hardware transitively. ------
# Only stub if the real one cannot be imported cleanly; try real first so we
# stay faithful when the environment happens to support it.
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

# Now it is safe to import the module under test and the store contract.
import micwebcontroller  # noqa: E402
from config_store import (  # noqa: E402
    ConfigStore,
    ALLOWED_PROFILES,
    PROFILE_FILE,
    PROFILE_MIC,
    DEFAULT_SILENCE_FLOOR,
    DEFAULT_OPEN_RATIO,
    DEFAULT_CLOSE_RATIO,
    DEFAULT_EMA_ALPHA,
    DEFAULT_CLOSE_HOLD_FRAMES,
)


def _default_profile():
    """Return the expected default adaptive profile dict (five fields)."""
    return {
        "silence_floor": DEFAULT_SILENCE_FLOOR,
        "open_ratio": DEFAULT_OPEN_RATIO,
        "close_ratio": DEFAULT_CLOSE_RATIO,
        "ema_alpha": DEFAULT_EMA_ALPHA,
        "close_hold_frames": DEFAULT_CLOSE_HOLD_FRAMES,
    }


def _default_pair():
    """Return the expected default {file, mic} profile pair."""
    return {PROFILE_FILE: _default_profile(), PROFILE_MIC: _default_profile()}


@pytest.fixture
def client(tmp_path):
    """Flask test client with the store pointed at a fresh temp Config_File.

    Rebuilds micwebcontroller.config_store against a unique temp path and
    resets micwebcontroller.jaw_profiles to a fresh default pair, so each test
    starts from a known, isolated state. Yields app.test_client().
    """
    config_path = tmp_path / "jaw_config.json"
    store = ConfigStore(config_path=str(config_path))

    # Point the module at the temp store and reset its in-memory profiles.
    micwebcontroller.config_store = store
    micwebcontroller.jaw_profiles = store.load_profiles()  # default pair

    micwebcontroller.app.config["TESTING"] = True
    with micwebcontroller.app.test_client() as test_client:
        # Expose the temp path so tests can reload with a fresh store.
        test_client._jaw_config_path = str(config_path)
        yield test_client


def _reload_stored_pair(config_path):
    """Reload both profiles from disk via a fresh store on the same path.

    Returns the default pair when the file was never written (no successful
    update has persisted anything yet), which is exactly the pre-request state.
    """
    return ConfigStore(config_path=str(config_path)).load_profiles()


# ---------------------------------------------------------------------------
# Task 7.4 — Property test: out-of-bounds rejection without mutation
# ---------------------------------------------------------------------------

# One out-of-bounds value per field, plus optional in-bounds values so the
# request is realistic (mixing valid and invalid fields).
_oob_silence_floor = st.floats(min_value=-1000.0, max_value=-0.0001)  # < 0 invalid
_oob_open_ratio = st.floats(min_value=-1000.0, max_value=0.0)  # <= 0 invalid
_oob_close_ratio = st.floats(min_value=-1000.0, max_value=0.0)  # <= 0 invalid
_oob_ema_low = st.floats(min_value=-1000.0, max_value=0.0)  # <= 0 invalid
_oob_ema_high = st.floats(min_value=1.0001, max_value=1000.0)  # > 1.0 invalid
_oob_ema = st.one_of(_oob_ema_low, _oob_ema_high)
_oob_close_hold = st.integers(min_value=-1000, max_value=0)  # < 1 invalid

_valid_silence_floor = st.floats(min_value=0.0, max_value=5000.0)
_valid_ema = st.floats(min_value=0.01, max_value=1.0)
_valid_close_hold = st.integers(min_value=1, max_value=50)


@st.composite
def _oob_update(draw):
    """Build an update dict containing AT LEAST ONE out-of-bounds field.

    Each of the five fields may be present; when present it is either the
    out-of-bounds generator or an in-bounds one. We force at least one field
    to be present and out-of-bounds so the request must be rejected.

    Note: open_ratio/close_ratio use out-of-bounds generators (<= 0) that the
    single-field validators reject before the cross-field check runs, so this
    strategy exercises the per-field bounds cleanly. The close_ratio >=
    open_ratio cross-check is covered separately below.
    """
    # Decide which fields are out-of-bounds (at least one must be True).
    oob_flags = draw(
        st.lists(st.booleans(), min_size=5, max_size=5).filter(lambda f: any(f))
    )
    update = {}
    # silence_floor
    if oob_flags[0]:
        update["silence_floor"] = draw(_oob_silence_floor)
    elif draw(st.booleans()):
        update["silence_floor"] = draw(_valid_silence_floor)
    # open_ratio
    if oob_flags[1]:
        update["open_ratio"] = draw(_oob_open_ratio)
    # close_ratio
    if oob_flags[2]:
        update["close_ratio"] = draw(_oob_close_ratio)
    # ema_alpha
    if oob_flags[3]:
        update["ema_alpha"] = draw(_oob_ema)
    elif draw(st.booleans()):
        update["ema_alpha"] = draw(_valid_ema)
    # close_hold_frames
    if oob_flags[4]:
        update["close_hold_frames"] = draw(_oob_close_hold)
    elif draw(st.booleans()):
        update["close_hold_frames"] = draw(_valid_close_hold)
    return update


@settings(max_examples=150, deadline=None)
@given(profile=st.sampled_from(list(ALLOWED_PROFILES)), update=_oob_update())
def test_property4_out_of_bounds_rejected_without_mutation(profile, update):
    """Feature: separate-jaw-tuning-profiles, Property 4: Out-of-bounds values are rejected without mutation.

    For any update request in which silence_floor < 0, open_ratio <= 0,
    close_ratio <= 0, ema_alpha falls outside (0, 1], or close_hold_frames < 1,
    the Web_Controller returns an error response and leaves both stored
    profiles unchanged.

    Validates: Requirements 5.1, 5.2, 5.3, 8.1
    """
    # Fresh, isolated store + reset in-memory profiles for each example so
    # state is deterministic (hypothesis reuses the process across examples).
    config_path = os.path.join(tempfile.mkdtemp(), "jaw_p4.json")
    store = ConfigStore(config_path=str(config_path))
    micwebcontroller.config_store = store
    micwebcontroller.jaw_profiles = store.load_profiles()

    before_memory = _default_pair()
    assert micwebcontroller.jaw_profiles == before_memory

    body = {"profile": profile, **update}
    with micwebcontroller.app.test_client() as c:
        resp = c.post("/config", json=body)

    # Rejected with an error (400).
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["status"] == "error"

    # In-memory profiles untouched.
    assert micwebcontroller.jaw_profiles == before_memory
    # On-disk state untouched: file was never written, so a fresh load returns
    # the default pair (the pre-request state).
    assert _reload_stored_pair(config_path) == before_memory


@settings(max_examples=100, deadline=None)
@given(
    profile=st.sampled_from(list(ALLOWED_PROFILES)),
    open_ratio=st.floats(min_value=0.5, max_value=2.0),
    # close is >= open, so the cross-field hysteresis check must reject it.
    extra=st.floats(min_value=0.0, max_value=2.0),
)
def test_property4_close_ge_open_rejected_without_mutation(profile, open_ratio, extra):
    """Property 4 (cross-field): close_ratio >= open_ratio is rejected.

    Both ratios are individually in-bounds (> 0), but the hysteresis invariant
    close_ratio < open_ratio is violated, so the Web_Controller must return an
    error and leave both stored profiles unchanged.

    Validates: Requirements 5.1, 5.2, 5.3, 8.1
    """
    close_ratio = open_ratio + extra  # guaranteed >= open_ratio

    config_path = os.path.join(tempfile.mkdtemp(), "jaw_p4x.json")
    store = ConfigStore(config_path=str(config_path))
    micwebcontroller.config_store = store
    micwebcontroller.jaw_profiles = store.load_profiles()

    before_memory = _default_pair()
    assert micwebcontroller.jaw_profiles == before_memory

    body = {"profile": profile, "open_ratio": open_ratio, "close_ratio": close_ratio}
    with micwebcontroller.app.test_client() as c:
        resp = c.post("/config", json=body)

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["status"] == "error"

    assert micwebcontroller.jaw_profiles == before_memory
    assert _reload_stored_pair(config_path) == before_memory


# ---------------------------------------------------------------------------
# Task 7.5 — Property test: unknown-profile rejection without mutation
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(
    profile=st.text(max_size=30),
    silence_floor=_valid_silence_floor,
    ema_alpha=_valid_ema,
)
def test_property5_unknown_profile_rejected_without_mutation(
    profile, silence_floor, ema_alpha
):
    """Feature: separate-jaw-tuning-profiles, Property 5: Unknown profile names are rejected without mutation.

    For any profile identifier that is not in the allowlist {file, mic}, the
    Web_Controller returns an error response and leaves both stored profiles
    unchanged.

    Validates: Requirements 5.4, 8.2
    """
    # Only names outside the allowlist qualify for this property.
    assume(profile not in ALLOWED_PROFILES)

    config_path = os.path.join(tempfile.mkdtemp(), "jaw_p5.json")
    store = ConfigStore(config_path=str(config_path))
    micwebcontroller.config_store = store
    micwebcontroller.jaw_profiles = store.load_profiles()

    before_memory = _default_pair()
    assert micwebcontroller.jaw_profiles == before_memory

    # Otherwise-valid fields — only the profile name is bad.
    body = {
        "profile": profile,
        "silence_floor": silence_floor,
        "ema_alpha": ema_alpha,
    }
    with micwebcontroller.app.test_client() as c:
        resp = c.post("/config", json=body)

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["status"] == "error"

    # Neither in-memory nor on-disk state changed.
    assert micwebcontroller.jaw_profiles == before_memory
    assert _reload_stored_pair(config_path) == before_memory


# ---------------------------------------------------------------------------
# Task 7.6 — Unit tests for valid updates and status echo
# ---------------------------------------------------------------------------


def test_valid_file_profile_update_returns_200_and_echoes(client):
    """Requirement 4.1: valid File_Profile update -> 200 with echoed fields.

    The response echoes the profile name and the updated fields, and reports
    both profiles with the file profile carrying the new values.
    """
    body = {"profile": PROFILE_FILE, "silence_floor": 350, "open_ratio": 1.25}
    resp = client.post("/config", json=body)

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["profile"] == PROFILE_FILE
    # Updated fields echoed back (values coerced to float by the handler).
    assert payload["updated"]["silence_floor"] == 350
    assert payload["updated"]["open_ratio"] == 1.25
    # Both profiles present; file profile reflects the update.
    assert set(payload["profiles"]) == {PROFILE_FILE, PROFILE_MIC}
    assert payload["profiles"][PROFILE_FILE]["silence_floor"] == 350
    assert payload["profiles"][PROFILE_FILE]["open_ratio"] == 1.25
    # close_ratio was not supplied, so it stays at the default.
    assert payload["profiles"][PROFILE_FILE]["close_ratio"] == DEFAULT_CLOSE_RATIO
    # Mic profile left at defaults (isolation).
    assert payload["profiles"][PROFILE_MIC] == _default_profile()


def test_valid_mic_profile_update_returns_200_and_echoes(client):
    """Requirement 4.2: valid Mic_Profile update -> 200 with echoed fields."""
    body = {"profile": PROFILE_MIC, "silence_floor": 800, "close_hold_frames": 4}
    resp = client.post("/config", json=body)

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    assert payload["profile"] == PROFILE_MIC
    assert payload["updated"]["silence_floor"] == 800
    assert payload["updated"]["close_hold_frames"] == 4
    assert payload["profiles"][PROFILE_MIC]["silence_floor"] == 800
    assert payload["profiles"][PROFILE_MIC]["close_hold_frames"] == 4
    # ema_alpha not supplied -> default retained.
    assert payload["profiles"][PROFILE_MIC]["ema_alpha"] == DEFAULT_EMA_ALPHA
    # File profile left at defaults (isolation).
    assert payload["profiles"][PROFILE_FILE] == _default_profile()


def test_status_returns_both_profiles_reflecting_stored_values(client):
    """Requirement 4.4: GET /status returns both profiles with current values.

    After updating each profile, /status echoes the currently stored file and
    mic profiles under the "profiles" key.
    """
    # Start: both profiles at defaults.
    resp = client.get("/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "profiles" in payload
    assert set(payload["profiles"]) == {PROFILE_FILE, PROFILE_MIC}
    assert payload["profiles"][PROFILE_FILE] == _default_profile()
    assert payload["profiles"][PROFILE_MIC] == _default_profile()

    # Apply one update per profile.
    client.post("/config", json={"profile": PROFILE_FILE, "silence_floor": 420})
    client.post("/config", json={"profile": PROFILE_MIC, "ema_alpha": 0.05})

    # /status now reflects the stored values.
    resp = client.get("/status")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["profiles"][PROFILE_FILE]["silence_floor"] == 420
    assert payload["profiles"][PROFILE_MIC]["ema_alpha"] == 0.05
    # Untouched fields remain at defaults.
    assert payload["profiles"][PROFILE_FILE]["open_ratio"] == DEFAULT_OPEN_RATIO
    assert payload["profiles"][PROFILE_MIC]["silence_floor"] == DEFAULT_SILENCE_FLOOR
