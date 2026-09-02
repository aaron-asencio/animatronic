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

# Ensure the project root is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config_store  # noqa: E402
from config_store import (  # noqa: E402
    ConfigStore,
    CONFIG_FILENAME,
    CONFIG_DIRNAME,
    CONFIG_PATH_OVERRIDE_ENV,
    CONFIG_PATH_OVERRIDE_ENV_PRIMARY,
    DEFAULT_DROP_THRESHOLD,
    DEFAULT_NOISE_FLOOR,
    DEFAULT_SENSITIVITY,
    PROFILE_FILE,
    PROFILE_MIC,
)

# The environment variables that influence path resolution.
ENV_VARS = (CONFIG_PATH_OVERRIDE_ENV_PRIMARY, CONFIG_PATH_OVERRIDE_ENV, "SUDO_USER", "HOME")


def _expected_default_profile():
    """Return the expected default profile dict (sensitivity/noise/drop)."""
    return {
        "sensitivity": DEFAULT_SENSITIVITY,
        "noise_floor": DEFAULT_NOISE_FLOOR,
        "drop_threshold": DEFAULT_DROP_THRESHOLD,
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


def test_loaded_config_exposes_both_profiles_with_three_fields(tmp_path):
    """Requirements 1.1, 1.2, 1.3: loaded config exposes file and mic profiles.

    Each profile carries the three fields with the persisted values.
    """
    payload = {
        "version": 1,
        "profiles": {
            PROFILE_FILE: {
                "sensitivity": 400,
                "noise_floor": 300,
                "drop_threshold": 0.10,
            },
            PROFILE_MIC: {
                "sensitivity": 700,
                "noise_floor": 800,
                "drop_threshold": 0.30,
            },
        },
    }
    config_file = tmp_path / "jaw.json"
    config_file.write_text(json.dumps(payload))

    store = ConfigStore(config_path=str(config_file))
    profiles = store.load_profiles()

    assert set(profiles) == {PROFILE_FILE, PROFILE_MIC}
    for name in (PROFILE_FILE, PROFILE_MIC):
        assert set(profiles[name]) == {"sensitivity", "noise_floor", "drop_threshold"}

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
_sensitivity_values = st.one_of(
    st.integers(min_value=1, max_value=100_000),
    st.floats(
        min_value=1e-6,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
    ),
)
_noise_floor_values = st.one_of(
    st.integers(min_value=0, max_value=100_000),
    st.floats(
        min_value=0.0,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
    ),
)
_drop_threshold_values = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _valid_profile(draw):
    """Build a valid jaw-tuning profile dict via hypothesis.

    Fields respect the documented bounds: sensitivity > 0, noise_floor >= 0,
    and drop_threshold in [0.0, 1.0]. Values are finite ints or floats so they
    survive a JSON round-trip with exact equality.
    """
    return {
        "sensitivity": draw(_sensitivity_values),
        "noise_floor": draw(_noise_floor_values),
        "drop_threshold": draw(_drop_threshold_values),
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

    Each of the three fields is independently included or omitted, but at least
    one field is always present so the update actually changes something.
    """
    include_sensitivity = draw(st.booleans())
    include_noise_floor = draw(st.booleans())
    include_drop_threshold = draw(st.booleans())
    # Guarantee a non-empty update.
    if not (include_sensitivity or include_noise_floor or include_drop_threshold):
        include_sensitivity = True

    updates = {}
    if include_sensitivity:
        updates["sensitivity"] = draw(_sensitivity_values)
    if include_noise_floor:
        updates["noise_floor"] = draw(_noise_floor_values)
    if include_drop_threshold:
        updates["drop_threshold"] = draw(_drop_threshold_values)
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
    "mic" keys, each carrying the three tuning fields.
    """
    config_file = tmp_path / "jaw.json"
    store = ConfigStore(config_path=str(config_file))

    initial_pair = {
        PROFILE_FILE: {
            "sensitivity": 400,
            "noise_floor": 300,
            "drop_threshold": 0.10,
        },
        PROFILE_MIC: {
            "sensitivity": 700,
            "noise_floor": 800,
            "drop_threshold": 0.30,
        },
    }
    store.save_profiles(initial_pair)
    store.update_profile(PROFILE_MIC, {"sensitivity": 750})

    # Read the raw JSON straight off disk and assert the on-disk schema.
    with open(config_file, "r") as f:
        raw = json.load(f)

    assert raw["version"] == 1
    assert "profiles" in raw
    assert set(raw["profiles"]) == {PROFILE_FILE, PROFILE_MIC}
    for name in (PROFILE_FILE, PROFILE_MIC):
        assert set(raw["profiles"][name]) == {
            "sensitivity",
            "noise_floor",
            "drop_threshold",
        }

    # File_Profile untouched by the mic update; Mic_Profile carries the merge.
    assert raw["profiles"][PROFILE_FILE] == initial_pair[PROFILE_FILE]
    assert raw["profiles"][PROFILE_MIC] == {
        "sensitivity": 750,
        "noise_floor": 800,
        "drop_threshold": 0.30,
    }
