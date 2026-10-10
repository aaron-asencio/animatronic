"""Tests for config_store path resolution and profile read logic.

Covers the already-implemented pieces of ConfigStore:
  - resolve_config_path() precedence (primary override -> legacy override ->
    app config/ dir)
  - load_profiles() / load_profile() defaulting on missing/corrupt files

Property tests use hypothesis (minimum 100 iterations each). Example-based
unit tests use pytest. Run with:

    pytest tests/test_config_store.py -q --maxfail=1
"""

import json
import os
import sys
from contextlib import contextmanager

import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import config_store  # noqa: E402
from config_store import (  # noqa: E402
    ConfigStore,
    CONFIG_FILENAME,
    CONFIG_DIRNAME,
    CONFIG_PATH_OVERRIDE_ENV,
    CONFIG_PATH_OVERRIDE_ENV_PRIMARY,
    DEFAULT_SILENCE_FLOOR,
    DEFAULT_OPEN_RATIO,
    DEFAULT_CLOSE_RATIO,
    DEFAULT_EMA_ALPHA,
    DEFAULT_CLOSE_HOLD_FRAMES,
    PROFILE_FILE,
    PROFILE_MIC,
    SCAN_KEY,
    SCAN_TIMEOUT_KEY,
    SCAN_TIMEOUT_DEFAULT_MIN,
    SCAN_TIMEOUT_MAX,
    SCAN_ROUTINE_POOL_KEY,
    SCAN_GESTURE_POOL_KEY,
    SCAN_POOL_WEIGHT_MIN,
    SCAN_POOL_WEIGHT_MAX,
    AWAKE_KEY,
    AWAKE_ROUTINE_POOL_KEY,
    AWAKE_GESTURE_POOL_KEY,
    MODE_TIMEOUT_KEY,
    VOICE_STYLES_KEY,
    sanitize_scan_pool,
)

# The environment variables that influence path resolution.
ENV_VARS = (CONFIG_PATH_OVERRIDE_ENV_PRIMARY, CONFIG_PATH_OVERRIDE_ENV, "SUDO_USER", "HOME")


def _expected_default_profile():
    """Return the expected default adaptive profile dict (five fields)."""
    return {
        "silence_floor": DEFAULT_SILENCE_FLOOR,
        "open_ratio": DEFAULT_OPEN_RATIO,
        "close_ratio": DEFAULT_CLOSE_RATIO,
        "ema_alpha": DEFAULT_EMA_ALPHA,
        "close_hold_frames": DEFAULT_CLOSE_HOLD_FRAMES,
    }


# ---------------------------------------------------------------------------
# Task 1.1 — Property test for deterministic path resolution
# ---------------------------------------------------------------------------

# Text values that make plausible env contents: paths, usernames, empty, unset.
# Values are constrained to ASCII (max_codepoint=127): env vars must be encodable
# by the platform's filesystem/env encoding, which can be latin-1 and would raise
# UnicodeEncodeError on characters above U+00FF. Realistic paths/usernames are ASCII.
_env_value = st.one_of(
    st.none(),  # unset the variable
    st.just(""),  # set-but-empty (falsy, treated as unset by the code)
    st.text(
        alphabet=st.characters(
            max_codepoint=127,
            whitelist_categories=("Lu", "Ll", "Nd"),
            whitelist_characters="/._-",
        ),
        min_size=1,
        max_size=40,
    ),
)


_ENV_UNSET = object()


@contextmanager
def _patched_env(**values):
    """Temporarily set/unset environment variables, restoring prior state.

    Snapshots the current value of each named variable (recording a sentinel
    when the variable is unset), applies the requested changes, yields, and
    then restores every variable to exactly its prior state in a finally block.
    Self-contained so it is safe to use inside a hypothesis @given test, where
    function-scoped fixtures like monkeypatch are disallowed.

    Args:
        **values: Mapping of environment variable name to desired value. A
            value of None removes the variable for the duration of the block;
            any other value is set as-is.
    """
    saved = {name: os.environ.get(name, _ENV_UNSET) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, prior in saved.items():
            if prior is _ENV_UNSET:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior


@settings(max_examples=150)
@given(override=_env_value, sudo_user=_env_value, home=_env_value)
def test_property7_path_resolution_is_deterministic(override, sudo_user, home):
    """Feature: separate-jaw-tuning-profiles, Property 7: Path resolution is deterministic.

    For any fixed environment (Config_Path_Override, SUDO_USER, and home held
    constant), resolving the Config_File path repeatedly yields the identical
    string.

    Validates: Requirements 7.1, 7.2, 7.3, 7.4
    """
    with _patched_env(
        **{CONFIG_PATH_OVERRIDE_ENV: override, "SUDO_USER": sudo_user, "HOME": home}
    ):
        first = ConfigStore.resolve_config_path()
        for _ in range(5):
            assert ConfigStore.resolve_config_path() == first
        assert isinstance(first, str)


# ---------------------------------------------------------------------------
# Task 1.2 — Unit tests for path-resolution branches
# ---------------------------------------------------------------------------


def test_legacy_override_returned_verbatim(monkeypatch):
    """Requirement 7.1: legacy override env value is used verbatim as the path.

    ANIMATRONIC_JAW_CONFIG is honored as a legacy fallback when the primary
    override is unset. SUDO_USER/HOME no longer affect resolution.
    """
    override_path = "/tmp/custom/jaw_config.json"
    monkeypatch.delenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, raising=False)
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV, override_path)
    monkeypatch.setenv("SUDO_USER", "someone")  # must be ignored
    assert ConfigStore.resolve_config_path() == override_path


def test_primary_override_precedence(monkeypatch):
    """Requirement 7.1: ANIMATRONIC_TUNING_CONFIG takes precedence.

    When both the primary (ANIMATRONIC_TUNING_CONFIG) and legacy
    (ANIMATRONIC_JAW_CONFIG) overrides are set, the primary wins and is used
    verbatim.
    """
    primary_path = "/tmp/primary/tuning.json"
    legacy_path = "/tmp/legacy/jaw_config.json"
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, primary_path)
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV, legacy_path)
    monkeypatch.setenv("SUDO_USER", "someone")  # must be ignored
    assert ConfigStore.resolve_config_path() == primary_path


def test_no_override_uses_app_config_dir(monkeypatch):
    """Requirement 7.3: no override -> <app_root>/config/tuning.json.

    Resolution is independent of the executing user's home directory. It is
    fine to leave SUDO_USER/HOME set — they no longer affect the result.
    """
    monkeypatch.delenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, raising=False)
    monkeypatch.delenv(CONFIG_PATH_OVERRIDE_ENV, raising=False)
    expected = os.path.join(
        os.path.dirname(os.path.abspath(config_store.__file__)),
        CONFIG_DIRNAME,
        CONFIG_FILENAME,
    )
    assert ConfigStore.resolve_config_path() == expected


# ---------------------------------------------------------------------------
# Task 2.2 — Property test: corrupt or missing config yields defaults
# ---------------------------------------------------------------------------

# Arbitrary text that is either malformed JSON or valid-JSON-but-wrong-shape.
_arbitrary_content = st.one_of(
    st.text(max_size=200),  # arbitrary text, mostly malformed JSON
    st.builds(json.dumps, st.integers()),  # valid JSON, wrong shape (int)
    st.builds(json.dumps, st.text(max_size=50)),  # valid JSON string, wrong shape
    st.builds(json.dumps, st.lists(st.integers(), max_size=5)),  # JSON list
    st.builds(  # dict but no "profiles" key / wrong nested shape
        json.dumps,
        st.dictionaries(st.text(max_size=10), st.integers(), max_size=5),
    ),
)


@settings(max_examples=150)
@given(content=_arbitrary_content)
def test_property6_corrupt_config_yields_defaults(tmp_path_factory, content):
    """Feature: separate-jaw-tuning-profiles, Property 6: Corrupt or missing config yields defaults.

    For any Config_File content that is absent or cannot be parsed as valid
    config JSON, loading profiles returns the default profile pair without
    raising.

    Validates: Requirements 6.4, 6.5
    """
    config_file = tmp_path_factory.mktemp("cfg") / "jaw.json"
    config_file.write_text(content, encoding="utf-8")

    store = ConfigStore(config_path=str(config_file))
    profiles = store.load_profiles()  # must not raise

    assert profiles[PROFILE_FILE] == _expected_default_profile()
    assert profiles[PROFILE_MIC] == _expected_default_profile()


def test_property6_missing_file_yields_defaults(tmp_path):
    """Property 6 (missing-file case): absent Config_File yields defaults.

    Validates: Requirements 6.4
    """
    missing = tmp_path / "does_not_exist.json"
    store = ConfigStore(config_path=str(missing))
    profiles = store.load_profiles()  # must not raise

    assert profiles[PROFILE_FILE] == _expected_default_profile()
    assert profiles[PROFILE_MIC] == _expected_default_profile()


# ---------------------------------------------------------------------------
# Task 2.3 — Unit tests for loaded config shape and missing-file defaults
# ---------------------------------------------------------------------------


def test_loaded_config_exposes_both_profiles_with_five_fields(tmp_path):
    """Requirements 1.1, 1.2, 1.3: loaded config exposes file and mic profiles.

    Each profile carries the five adaptive fields with the persisted values.
    """
    payload = {
        "version": 2,
        "profiles": {
            PROFILE_FILE: {
                "silence_floor": 400,
                "open_ratio": 1.20,
                "close_ratio": 0.80,
                "ema_alpha": 0.10,
                "close_hold_frames": 3,
            },
            PROFILE_MIC: {
                "silence_floor": 700,
                "open_ratio": 1.30,
                "close_ratio": 0.90,
                "ema_alpha": 0.25,
                "close_hold_frames": 4,
            },
        },
    }
    config_file = tmp_path / "jaw.json"
    config_file.write_text(json.dumps(payload))

    store = ConfigStore(config_path=str(config_file))
    profiles = store.load_profiles()

    assert set(profiles) == {PROFILE_FILE, PROFILE_MIC}
    for name in (PROFILE_FILE, PROFILE_MIC):
        assert set(profiles[name]) == {
            "silence_floor",
            "open_ratio",
            "close_ratio",
            "ema_alpha",
            "close_hold_frames",
        }

    assert profiles[PROFILE_FILE] == payload["profiles"][PROFILE_FILE]
    assert profiles[PROFILE_MIC] == payload["profiles"][PROFILE_MIC]


def test_missing_file_both_profiles_equal_defaults(tmp_path):
    """Requirement 6.4: missing file -> both profiles equal defaults."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))
    profiles = store.load_profiles()

    assert profiles[PROFILE_FILE] == _expected_default_profile()
    assert profiles[PROFILE_MIC] == _expected_default_profile()


def test_load_profile_returns_single_profile_and_validates_name(tmp_path):
    """Requirements 1.1, 6.4: load_profile returns one profile; bad name raises."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))

    assert store.load_profile(PROFILE_FILE) == _expected_default_profile()
    assert store.load_profile(PROFILE_MIC) == _expected_default_profile()

    with pytest.raises(ValueError):
        store.load_profile("bogus")

# ---------------------------------------------------------------------------
# Shared strategies / helpers for save_profiles / update_profile tests
# ---------------------------------------------------------------------------

# Finite numeric strategies matching the profile field bounds. NaN and inf are
# excluded because JSON round-trips floats via repr and those values either
# break equality (NaN != NaN) or are not standard JSON.
_silence_floor_values = st.one_of(
    st.integers(min_value=0, max_value=100_000),
    st.floats(
        min_value=0.0,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
    ),
)
# open_ratio in a realistic 0.5..3.0 band; close_ratio derived as a fraction of
# open_ratio so it is always strictly less than open_ratio (hysteresis).
_open_ratio_values = st.floats(
    min_value=0.5,
    max_value=3.0,
    allow_nan=False,
    allow_infinity=False,
)
_close_fraction_values = st.floats(
    min_value=0.05,
    max_value=0.95,
    allow_nan=False,
    allow_infinity=False,
)
_ema_alpha_values = st.floats(
    min_value=1e-6,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)
_close_hold_frames_values = st.integers(min_value=1, max_value=100)


@st.composite
def _valid_profile(draw):
    """Build a valid adaptive jaw-tuning profile dict via hypothesis.

    Fields respect the documented bounds: silence_floor >= 0, open_ratio > 0,
    close_ratio > 0 and strictly < open_ratio (derived as fraction * open_ratio),
    ema_alpha in (0, 1], and close_hold_frames an int >= 1. Values are finite
    ints or floats so they survive a JSON round-trip with exact equality.
    """
    open_ratio = draw(_open_ratio_values)
    close_ratio = open_ratio * draw(_close_fraction_values)
    return {
        "silence_floor": draw(_silence_floor_values),
        "open_ratio": open_ratio,
        "close_ratio": close_ratio,
        "ema_alpha": draw(_ema_alpha_values),
        "close_hold_frames": draw(_close_hold_frames_values),
    }


@st.composite
def _profile_pair(draw):
    """Build a valid {file, mic} profile pair via hypothesis."""
    return {
        PROFILE_FILE: draw(_valid_profile()),
        PROFILE_MIC: draw(_valid_profile()),
    }


@st.composite
def _profile_update(draw):
    """Build a non-empty subset update of valid profile fields.

    open_ratio and close_ratio are updated together (close derived from open) so
    the hysteresis invariant close_ratio < open_ratio always holds; the other
    three fields are independently included or omitted. At least one field is
    always present so the update actually changes something.

    Note: config_store.update_profile does not itself enforce cross-field
    bounds (that is the web layer's job); these updates simply stay valid so
    the isolation property is exercised with realistic data.
    """
    include_silence = draw(st.booleans())
    include_ratios = draw(st.booleans())
    include_ema = draw(st.booleans())
    include_hold = draw(st.booleans())
    # Guarantee a non-empty update.
    if not (include_silence or include_ratios or include_ema or include_hold):
        include_silence = True

    updates = {}
    if include_silence:
        updates["silence_floor"] = draw(_silence_floor_values)
    if include_ratios:
        open_ratio = draw(_open_ratio_values)
        updates["open_ratio"] = open_ratio
        updates["close_ratio"] = open_ratio * draw(_close_fraction_values)
    if include_ema:
        updates["ema_alpha"] = draw(_ema_alpha_values)
    if include_hold:
        updates["close_hold_frames"] = draw(_close_hold_frames_values)
    return updates


# ---------------------------------------------------------------------------
# Task 3.2 — Property test for profile update isolation
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    pair=_profile_pair(),
    updates=_profile_update(),
    target=st.sampled_from([PROFILE_FILE, PROFILE_MIC]),
)
def test_property2_profile_updates_are_isolated(
    tmp_path_factory, pair, updates, target
):
    """Feature: separate-jaw-tuning-profiles, Property 2: Profile updates are isolated.

    For any pair of stored profiles and any valid update applied to one
    profile, the other profile remains byte-for-byte unchanged after the
    update, and the named profile carries the merged updates.

    Validates: Requirements 1.4, 1.5, 4.3
    """
    config_file = tmp_path_factory.mktemp("iso") / "jaw.json"
    store = ConfigStore(config_path=str(config_file))
    store.save_profiles(pair)

    other = PROFILE_MIC if target == PROFILE_FILE else PROFILE_FILE
    # Snapshot the untouched profile as it was before the update.
    other_before = dict(pair[other])

    result = store.update_profile(target, updates)

    # The other profile is unchanged (equal dict) both in the returned pair...
    assert result[other] == other_before
    # ...and on disk after a fresh load.
    reloaded = ConfigStore(config_path=str(config_file)).load_profiles()
    assert reloaded[other] == other_before

    # The named profile has the merged updates applied over its prior values.
    expected_target = dict(pair[target])
    expected_target.update(updates)
    assert result[target] == expected_target
    assert reloaded[target] == expected_target


# ---------------------------------------------------------------------------
# Task 3.3 — Property test for persistence round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(pair=_profile_pair())
def test_property3_persistence_round_trip(tmp_path_factory, pair):
    """Feature: separate-jaw-tuning-profiles, Property 3: Persistence round-trip.

    For any valid pair of File_Profile and Mic_Profile values, saving them
    through the Config_Store and then loading from the same Config_File with a
    fresh store returns an equivalent pair. Finite ints/floats round-trip
    exactly through JSON (NaN/inf are excluded by the generators).

    Validates: Requirements 4.5, 6.2, 6.3
    """
    config_file = tmp_path_factory.mktemp("roundtrip") / "jaw.json"

    ConfigStore(config_path=str(config_file)).save_profiles(pair)

    # A fresh store on the same path must read back an equivalent pair.
    loaded = ConfigStore(config_path=str(config_file)).load_profiles()

    assert loaded == pair
    assert loaded[PROFILE_FILE] == pair[PROFILE_FILE]
    assert loaded[PROFILE_MIC] == pair[PROFILE_MIC]


# ---------------------------------------------------------------------------
# Task 3.4 — Unit test for single-file both-profile persistence
# ---------------------------------------------------------------------------


def test_single_file_contains_both_profiles_after_update(tmp_path):
    """Requirement 6.1: one JSON file on disk holds both profiles.

    After a save followed by an update, the single Config_File contains the
    documented schema: a top-level "profiles" object with both "file" and
    "mic" keys, each carrying the five adaptive tuning fields.
    """
    config_file = tmp_path / "jaw.json"
    store = ConfigStore(config_path=str(config_file))

    initial_pair = {
        PROFILE_FILE: {
            "silence_floor": 400,
            "open_ratio": 1.20,
            "close_ratio": 0.80,
            "ema_alpha": 0.10,
            "close_hold_frames": 3,
        },
        PROFILE_MIC: {
            "silence_floor": 700,
            "open_ratio": 1.30,
            "close_ratio": 0.90,
            "ema_alpha": 0.25,
            "close_hold_frames": 4,
        },
    }
    store.save_profiles(initial_pair)
    store.update_profile(PROFILE_MIC, {"silence_floor": 750})

    # Read the raw JSON straight off disk and assert the on-disk schema.
    with open(config_file, "r") as f:
        raw = json.load(f)

    assert raw["version"] == 2
    assert "profiles" in raw
    assert set(raw["profiles"]) == {PROFILE_FILE, PROFILE_MIC}
    for name in (PROFILE_FILE, PROFILE_MIC):
        assert set(raw["profiles"][name]) == {
            "silence_floor",
            "open_ratio",
            "close_ratio",
            "ema_alpha",
            "close_hold_frames",
        }

    # File_Profile untouched by the mic update; Mic_Profile carries the merge.
    assert raw["profiles"][PROFILE_FILE] == initial_pair[PROFILE_FILE]
    assert raw["profiles"][PROFILE_MIC] == {
        "silence_floor": 750,
        "open_ratio": 1.30,
        "close_ratio": 0.90,
        "ema_alpha": 0.25,
        "close_hold_frames": 4,
    }


# ---------------------------------------------------------------------------
# FEAT-001 — scan-mode timeout accessors (load_scan_timeout / save_scan_timeout)
# ---------------------------------------------------------------------------


def test_scan_timeout_round_trip(tmp_path):
    """A saved scan timeout loads back unchanged within the valid range."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))

    assert store.save_scan_timeout(45) == 45
    assert store.load_scan_timeout() == 45

    # A fresh store on the same path reads the same value.
    assert ConfigStore(config_path=str(tmp_path / "tuning.json")).load_scan_timeout() == 45


def test_scan_timeout_default_when_missing(tmp_path):
    """Missing file/section yields the default without raising."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))
    assert store.load_scan_timeout() == SCAN_TIMEOUT_DEFAULT_MIN


@pytest.mark.parametrize(
    "given_value, expected",
    [
        (0, 0),                      # exact floor (0 = no timeout) stays 0
        (-5, 0),                     # negative -> clamp up to 0 (the floor)
        (1, 1),                      # 1 is now inside range -> unchanged
        (120, SCAN_TIMEOUT_MAX),     # exact ceiling
        (999, SCAN_TIMEOUT_MAX),     # above ceiling -> clamp down to 120
        (60, 60),                    # mid-range unchanged
        ("30", 30),                  # numeric string coerced to int
    ],
)
def test_scan_timeout_save_clamps(tmp_path, given_value, expected):
    """save_scan_timeout coerces and clamps to [0, 120] and returns the result."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    assert store.save_scan_timeout(given_value) == expected
    assert store.load_scan_timeout() == expected


def test_scan_timeout_zero_round_trip(tmp_path):
    """The no-timeout sentinel 0 persists and loads back as 0 (not clamped away)."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    assert store.save_scan_timeout(0) == 0
    assert store.load_scan_timeout() == 0
    # A fresh store on the same path reads the same 0.
    assert ConfigStore(config_path=str(tmp_path / "tuning.json")).load_scan_timeout() == 0


def test_scan_timeout_save_rejects_non_numeric(tmp_path):
    """A value that cannot be coerced to int raises ValueError."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    with pytest.raises(ValueError):
        store.save_scan_timeout("not-a-number")


def test_load_scan_timeout_defaults_on_corrupt_section(tmp_path):
    """A non-int or non-dict scan section loads as the default (never raises)."""
    config_file = tmp_path / "tuning.json"

    # Non-int timeout value -> default.
    config_file.write_text(json.dumps({SCAN_KEY: {SCAN_TIMEOUT_KEY: "abc"}}))
    assert ConfigStore(config_path=str(config_file)).load_scan_timeout() == SCAN_TIMEOUT_DEFAULT_MIN

    # Scan section is not a dict -> default.
    config_file.write_text(json.dumps({SCAN_KEY: "oops"}))
    assert ConfigStore(config_path=str(config_file)).load_scan_timeout() == SCAN_TIMEOUT_DEFAULT_MIN

    # Out-of-range stored value is clamped on load.
    config_file.write_text(json.dumps({SCAN_KEY: {SCAN_TIMEOUT_KEY: 999}}))
    assert ConfigStore(config_path=str(config_file)).load_scan_timeout() == SCAN_TIMEOUT_MAX


def test_save_scan_timeout_preserves_other_sections(tmp_path):
    """Saving the scan timeout preserves existing profiles and voice_styles."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    # Seed a file with both a profiles section and a voice_styles section.
    pair = {
        PROFILE_FILE: _expected_default_profile(),
        PROFILE_MIC: _expected_default_profile(),
    }
    store.save_profiles(pair)
    store.save_voice_style("ghost", {})  # writes a voice_styles section

    store.save_scan_timeout(90)

    with open(config_file, "r") as f:
        raw = json.load(f)

    # The scan section is present and correct...
    assert raw[SCAN_KEY] == {SCAN_TIMEOUT_KEY: 90}
    # ...and the pre-existing sections are untouched.
    assert "profiles" in raw
    assert set(raw["profiles"]) == {PROFILE_FILE, PROFILE_MIC}
    assert raw["profiles"][PROFILE_FILE] == _expected_default_profile()
    assert VOICE_STYLES_KEY in raw
    assert "ghost" in raw[VOICE_STYLES_KEY]

    # And both accessors still read correctly.
    assert store.load_scan_timeout() == 90
    assert store.load_profiles() == pair


def test_module_level_scan_timeout_wrappers(tmp_path, monkeypatch):
    """The thin module-level wrappers delegate to the default store."""
    config_file = tmp_path / "tuning.json"
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, str(config_file))
    # Rebind the default store so it picks up the overridden path.
    monkeypatch.setattr(config_store, "_default_store", ConfigStore())

    assert config_store.save_scan_timeout(75) == 75
    assert config_store.load_scan_timeout() == 75


# ---------------------------------------------------------------------------
# FEAT-001 — scan responder pool (sanitize_scan_pool / load/save_scan_pools)
# ---------------------------------------------------------------------------

# A small fixed allowlist used as valid_names for the sanitizer tests.
_VALID_POOL_NAMES = frozenset({"wave", "beckon", "comeHere", "brains", "hypnotic"})

# Weight values: ints in/below/above range, negatives, zero, and non-ints.
_pool_weight = st.one_of(
    st.integers(min_value=-5, max_value=15),
    st.just(0),
    st.text(max_size=4),
    st.none(),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-5, max_value=15),
)


@settings(max_examples=200)
@given(
    pool=st.dictionaries(
        st.one_of(st.sampled_from(sorted(_VALID_POOL_NAMES)), st.text(max_size=6)),
        _pool_weight,
        max_size=10,
    )
)
def test_sanitize_scan_pool_keeps_only_valid_clamped(pool):
    """Output keys subset valid_names; all values int in [1,10]; bad entries dropped."""
    clean = sanitize_scan_pool(pool, _VALID_POOL_NAMES)

    assert set(clean) <= _VALID_POOL_NAMES
    for name, weight in clean.items():
        assert isinstance(weight, int)
        assert SCAN_POOL_WEIGHT_MIN <= weight <= SCAN_POOL_WEIGHT_MAX

    # Any name kept must have had an int-coercible weight >= 1 in the input.
    for name, weight in clean.items():
        raw = pool[name]
        coerced = int(raw)  # must not raise for a kept entry
        assert coerced >= SCAN_POOL_WEIGHT_MIN


def test_sanitize_scan_pool_drops_unknown_and_subone_coerces():
    """Unknown names dropped; <1/negative/non-int dropped; >10 clamped; strings coerced."""
    pool = {
        "wave": 3,            # kept as-is
        "beckon": 0,          # dropped (<1)
        "comeHere": -4,       # dropped (negative)
        "brains": 99,         # clamped to 10
        "hypnotic": "5",      # coerced to 5
        "bogus": 7,           # dropped (unknown name)
        "wave2": 2,           # dropped (unknown name)
    }
    clean = sanitize_scan_pool(pool, _VALID_POOL_NAMES)
    assert clean == {"wave": 3, "brains": SCAN_POOL_WEIGHT_MAX, "hypnotic": 5}


def test_sanitize_scan_pool_non_mapping_raises():
    """Only a non-mapping input raises ValueError."""
    for bad in ([], "nope", 5, None):
        with pytest.raises(ValueError):
            sanitize_scan_pool(bad, _VALID_POOL_NAMES)


def test_save_scan_pools_round_trip_preserves_timeout_profile_voice_style(tmp_path):
    """Saving pools preserves scan.timeout_min, a profile, and a voice_style."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    # Seed timeout + a profile + a voice style first.
    pair = {
        PROFILE_FILE: _expected_default_profile(),
        PROFILE_MIC: _expected_default_profile(),
    }
    store.save_profiles(pair)
    store.save_voice_style("ghost", {})
    store.save_scan_timeout(42)

    stored = store.save_scan_pools({"brains": 5}, {"wave": 8, "beckon": 3})
    assert stored == {
        SCAN_ROUTINE_POOL_KEY: {"brains": 5},
        SCAN_GESTURE_POOL_KEY: {"wave": 8, "beckon": 3},
    }

    with open(config_file, "r") as f:
        raw = json.load(f)

    # Pools stored inside the scan section alongside the preserved timeout.
    assert raw[SCAN_KEY][SCAN_TIMEOUT_KEY] == 42
    assert raw[SCAN_KEY][SCAN_ROUTINE_POOL_KEY] == {"brains": 5}
    assert raw[SCAN_KEY][SCAN_GESTURE_POOL_KEY] == {"wave": 8, "beckon": 3}

    # Pre-existing sections untouched.
    assert raw["profiles"][PROFILE_FILE] == _expected_default_profile()
    assert VOICE_STYLES_KEY in raw and "ghost" in raw[VOICE_STYLES_KEY]

    # Accessors agree on reload.
    fresh = ConfigStore(config_path=str(config_file))
    assert fresh.load_scan_timeout() == 42
    assert fresh.load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {"brains": 5},
        SCAN_GESTURE_POOL_KEY: {"wave": 8, "beckon": 3},
    }
    assert fresh.load_profiles() == pair


def test_save_scan_timeout_after_pools_preserves_pools(tmp_path):
    """A later save_scan_timeout merges and keeps the previously saved pools."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    store.save_scan_pools({"hypnotic": 2}, {"comeHere": 4})
    store.save_scan_timeout(90)

    with open(config_file, "r") as f:
        raw = json.load(f)
    assert raw[SCAN_KEY][SCAN_TIMEOUT_KEY] == 90
    assert raw[SCAN_KEY][SCAN_ROUTINE_POOL_KEY] == {"hypnotic": 2}
    assert raw[SCAN_KEY][SCAN_GESTURE_POOL_KEY] == {"comeHere": 4}


def test_save_scan_pools_clamps_defensively(tmp_path):
    """save_scan_pools coerces/clamps even if the caller passes raw values."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    stored = store.save_scan_pools({"brains": 99, "hypnotic": 0}, {"wave": "6"})
    assert stored == {
        SCAN_ROUTINE_POOL_KEY: {"brains": SCAN_POOL_WEIGHT_MAX},  # 99 -> 10
        SCAN_GESTURE_POOL_KEY: {"wave": 6},                       # "6" -> 6
    }
    # hypnotic weight 0 dropped (excluded).
    assert "hypnotic" not in stored[SCAN_ROUTINE_POOL_KEY]


def test_load_scan_pools_empty_on_missing(tmp_path):
    """Missing file yields two empty pools without raising."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))
    assert store.load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {},
        SCAN_GESTURE_POOL_KEY: {},
    }


def test_load_scan_pools_empty_on_corrupt_or_missing_section(tmp_path):
    """Corrupt JSON, missing scan section, or non-dict pools yield empty maps."""
    config_file = tmp_path / "tuning.json"

    # Corrupt JSON.
    config_file.write_text("{not valid json")
    assert ConfigStore(config_path=str(config_file)).load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {},
        SCAN_GESTURE_POOL_KEY: {},
    }

    # Valid JSON, no scan section.
    config_file.write_text(json.dumps({"profiles": {}}))
    assert ConfigStore(config_path=str(config_file)).load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {},
        SCAN_GESTURE_POOL_KEY: {},
    }

    # Scan section with non-dict pools -> both empty.
    config_file.write_text(
        json.dumps({SCAN_KEY: {SCAN_ROUTINE_POOL_KEY: "oops", SCAN_GESTURE_POOL_KEY: 5}})
    )
    assert ConfigStore(config_path=str(config_file)).load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {},
        SCAN_GESTURE_POOL_KEY: {},
    }


def test_load_scan_pools_coerces_and_clamps(tmp_path):
    """Stored pool weights are coerced to int, dropped if <1, clamped to [1,10]."""
    config_file = tmp_path / "tuning.json"
    config_file.write_text(
        json.dumps(
            {
                SCAN_KEY: {
                    SCAN_ROUTINE_POOL_KEY: {"brains": 99, "hypnotic": 0, "x": "3"},
                    SCAN_GESTURE_POOL_KEY: {"wave": -1, "beckon": 7},
                }
            }
        )
    )
    pools = ConfigStore(config_path=str(config_file)).load_scan_pools()
    assert pools[SCAN_ROUTINE_POOL_KEY] == {"brains": SCAN_POOL_WEIGHT_MAX, "x": 3}
    assert pools[SCAN_GESTURE_POOL_KEY] == {"beckon": 7}


def test_module_level_scan_pool_wrappers(tmp_path, monkeypatch):
    """The thin module-level pool wrappers delegate to the default store."""
    config_file = tmp_path / "tuning.json"
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, str(config_file))
    monkeypatch.setattr(config_store, "_default_store", ConfigStore())

    assert config_store.save_scan_pools({"brains": 4}, {"wave": 9}) == {
        SCAN_ROUTINE_POOL_KEY: {"brains": 4},
        SCAN_GESTURE_POOL_KEY: {"wave": 9},
    }
    assert config_store.load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {"brains": 4},
        SCAN_GESTURE_POOL_KEY: {"wave": 9},
    }


# ---------------------------------------------------------------------------
# Awake responder pool (load/save_awake_pools) — mirrors the scan-pool block
# above but targets the ``awake`` section. The one deliberate difference from
# Scan is seeding (FULL lists), which is enforced by the webapp, not this leaf
# store; the store itself is name-agnostic, so these tests mirror the Scan
# round-trip / clamp / empty-on-missing / coerce behavior for the awake section.
# ---------------------------------------------------------------------------


def test_save_awake_pools_round_trip_preserves_timeout_profile_voice_style(tmp_path):
    """Saving awake pools preserves awake.timeout_min, a profile, and a voice_style."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    pair = {
        PROFILE_FILE: _expected_default_profile(),
        PROFILE_MIC: _expected_default_profile(),
    }
    store.save_profiles(pair)
    store.save_voice_style("ghost", {})
    store.save_awake_timeout(30)

    # A HEAD gesture (wave) and a non-arm-only routine (startParty) are legal
    # here — the Awake store is name-agnostic (FULL-list seeding lives in the
    # webapp), unlike the arm-only-safe Scan pool.
    stored = store.save_awake_pools({"startParty": 5}, {"wave": 8, "lookAroundRandom": 3})
    assert stored == {
        AWAKE_ROUTINE_POOL_KEY: {"startParty": 5},
        AWAKE_GESTURE_POOL_KEY: {"wave": 8, "lookAroundRandom": 3},
    }

    with open(config_file, "r") as f:
        raw = json.load(f)

    assert raw[AWAKE_KEY][MODE_TIMEOUT_KEY] == 30
    assert raw[AWAKE_KEY][AWAKE_ROUTINE_POOL_KEY] == {"startParty": 5}
    assert raw[AWAKE_KEY][AWAKE_GESTURE_POOL_KEY] == {"wave": 8, "lookAroundRandom": 3}

    assert raw["profiles"][PROFILE_FILE] == _expected_default_profile()
    assert VOICE_STYLES_KEY in raw and "ghost" in raw[VOICE_STYLES_KEY]

    fresh = ConfigStore(config_path=str(config_file))
    assert fresh.load_awake_timeout() == 30
    assert fresh.load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {"startParty": 5},
        AWAKE_GESTURE_POOL_KEY: {"wave": 8, "lookAroundRandom": 3},
    }
    assert fresh.load_profiles() == pair


def test_save_awake_timeout_after_pools_preserves_pools(tmp_path):
    """A later save_awake_timeout merges and keeps the previously saved pools."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    store.save_awake_pools({"yawn": 2}, {"handVisor": 4})
    store.save_awake_timeout(60)

    with open(config_file, "r") as f:
        raw = json.load(f)
    assert raw[AWAKE_KEY][MODE_TIMEOUT_KEY] == 60
    assert raw[AWAKE_KEY][AWAKE_ROUTINE_POOL_KEY] == {"yawn": 2}
    assert raw[AWAKE_KEY][AWAKE_GESTURE_POOL_KEY] == {"handVisor": 4}


def test_save_awake_pools_clamps_defensively(tmp_path):
    """save_awake_pools coerces/clamps even if the caller passes raw values."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    stored = store.save_awake_pools({"brains": 99, "yawn": 0}, {"wave": "6"})
    assert stored == {
        AWAKE_ROUTINE_POOL_KEY: {"brains": SCAN_POOL_WEIGHT_MAX},  # 99 -> 10
        AWAKE_GESTURE_POOL_KEY: {"wave": 6},                       # "6" -> 6
    }
    assert "yawn" not in stored[AWAKE_ROUTINE_POOL_KEY]


def test_load_awake_pools_empty_on_missing(tmp_path):
    """Missing file yields two empty pools without raising."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))
    assert store.load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {},
        AWAKE_GESTURE_POOL_KEY: {},
    }


def test_load_awake_pools_empty_on_corrupt_or_missing_section(tmp_path):
    """Corrupt JSON, missing awake section, or non-dict pools yield empty maps."""
    config_file = tmp_path / "tuning.json"

    config_file.write_text("{not valid json")
    assert ConfigStore(config_path=str(config_file)).load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {},
        AWAKE_GESTURE_POOL_KEY: {},
    }

    config_file.write_text(json.dumps({"profiles": {}}))
    assert ConfigStore(config_path=str(config_file)).load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {},
        AWAKE_GESTURE_POOL_KEY: {},
    }

    config_file.write_text(
        json.dumps({AWAKE_KEY: {AWAKE_ROUTINE_POOL_KEY: "oops", AWAKE_GESTURE_POOL_KEY: 5}})
    )
    assert ConfigStore(config_path=str(config_file)).load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {},
        AWAKE_GESTURE_POOL_KEY: {},
    }


def test_load_awake_pools_coerces_and_clamps(tmp_path):
    """Stored awake pool weights are coerced to int, dropped if <1, clamped to [1,10]."""
    config_file = tmp_path / "tuning.json"
    config_file.write_text(
        json.dumps(
            {
                AWAKE_KEY: {
                    AWAKE_ROUTINE_POOL_KEY: {"brains": 99, "yawn": 0, "x": "3"},
                    AWAKE_GESTURE_POOL_KEY: {"wave": -1, "handVisor": 7},
                }
            }
        )
    )
    pools = ConfigStore(config_path=str(config_file)).load_awake_pools()
    assert pools[AWAKE_ROUTINE_POOL_KEY] == {"brains": SCAN_POOL_WEIGHT_MAX, "x": 3}
    assert pools[AWAKE_GESTURE_POOL_KEY] == {"handVisor": 7}


def test_awake_and_scan_pools_are_independent(tmp_path):
    """Awake and Scan pools persist in separate sections and don't collide."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    store.save_scan_pools({"brains": 3}, {"wave": 2})
    store.save_awake_pools({"startParty": 7}, {"menacingReach": 9})

    assert store.load_scan_pools() == {
        SCAN_ROUTINE_POOL_KEY: {"brains": 3},
        SCAN_GESTURE_POOL_KEY: {"wave": 2},
    }
    assert store.load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {"startParty": 7},
        AWAKE_GESTURE_POOL_KEY: {"menacingReach": 9},
    }


def test_module_level_awake_pool_wrappers(tmp_path, monkeypatch):
    """The thin module-level awake-pool wrappers delegate to the default store."""
    config_file = tmp_path / "tuning.json"
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, str(config_file))
    monkeypatch.setattr(config_store, "_default_store", ConfigStore())

    assert config_store.save_awake_pools({"brains": 4}, {"wave": 9}) == {
        AWAKE_ROUTINE_POOL_KEY: {"brains": 4},
        AWAKE_GESTURE_POOL_KEY: {"wave": 9},
    }
    assert config_store.load_awake_pools() == {
        AWAKE_ROUTINE_POOL_KEY: {"brains": 4},
        AWAKE_GESTURE_POOL_KEY: {"wave": 9},
    }
