"""Tests for config_store mode auto-chaining accessors.

Covers load_chain_max_transitions / save_chain_max_transitions:
  - default on a missing/empty/corrupt file (never raises)
  - round-trip for an in-range value
  - clamp on save (99 -> 20, -1 -> 0) and on load (out-of-range stored value)
  - ValueError on a non-int save value
  - saving `chain` preserves pre-existing scan/voice_styles/profiles sections
  - module-level wrappers delegate to the default store

Run with:  pytest tests/test_config_store_chain.py -q --maxfail=1
"""

import json
import os
import sys

import pytest

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import config_store  # noqa: E402
from config_store import (  # noqa: E402
    ConfigStore,
    CONFIG_PATH_OVERRIDE_ENV_PRIMARY,
    CHAIN_KEY,
    CHAIN_MAX_TRANSITIONS_KEY,
    CHAIN_MAX_TRANSITIONS_DEFAULT,
    CHAIN_MAX_TRANSITIONS_MIN,
    CHAIN_MAX_TRANSITIONS_MAX,
    SCAN_KEY,
    SCAN_TIMEOUT_KEY,
    VOICE_STYLES_KEY,
    PROFILE_FILE,
    PROFILE_MIC,
)


def test_chain_default_when_missing(tmp_path):
    """Missing file/section yields the default (4) without raising."""
    store = ConfigStore(config_path=str(tmp_path / "absent.json"))
    assert store.load_chain_max_transitions() == CHAIN_MAX_TRANSITIONS_DEFAULT == 4


def test_chain_round_trip(tmp_path):
    """A saved in-range value loads back unchanged, incl. a fresh store."""
    path = str(tmp_path / "tuning.json")
    store = ConfigStore(config_path=path)
    assert store.save_chain_max_transitions(6) == 6
    assert store.load_chain_max_transitions() == 6
    # A fresh store on the same path reads the same value.
    assert ConfigStore(config_path=path).load_chain_max_transitions() == 6


def test_chain_zero_round_trip(tmp_path):
    """The disable sentinel 0 persists and loads back as 0 (not clamped away)."""
    path = str(tmp_path / "tuning.json")
    store = ConfigStore(config_path=path)
    assert store.save_chain_max_transitions(0) == 0
    assert store.load_chain_max_transitions() == 0


@pytest.mark.parametrize(
    "given_value, expected",
    [
        (99, CHAIN_MAX_TRANSITIONS_MAX),   # above max -> 20
        (21, CHAIN_MAX_TRANSITIONS_MAX),   # just above -> 20
        (-1, CHAIN_MAX_TRANSITIONS_MIN),   # below min -> 0
        (20, 20),                          # exactly max
        (0, 0),                            # exactly min
        ("7", 7),                          # coercible string
    ],
)
def test_chain_save_clamps(tmp_path, given_value, expected):
    """save_chain_max_transitions coerces and clamps to [0, 20]."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    assert store.save_chain_max_transitions(given_value) == expected
    assert store.load_chain_max_transitions() == expected


def test_chain_save_rejects_non_int(tmp_path):
    """A value that cannot be coerced to int raises ValueError."""
    store = ConfigStore(config_path=str(tmp_path / "tuning.json"))
    with pytest.raises(ValueError):
        store.save_chain_max_transitions("not-a-number")


def test_chain_load_defaults_on_corrupt_or_out_of_range(tmp_path):
    """A non-dict/non-int or out-of-range stored value loads as the default."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    # Non-dict chain section -> default.
    config_file.write_text(json.dumps({CHAIN_KEY: "oops"}))
    assert store.load_chain_max_transitions() == CHAIN_MAX_TRANSITIONS_DEFAULT

    # Non-int value -> default.
    config_file.write_text(json.dumps({CHAIN_KEY: {CHAIN_MAX_TRANSITIONS_KEY: "x"}}))
    assert store.load_chain_max_transitions() == CHAIN_MAX_TRANSITIONS_DEFAULT

    # Out-of-range stored value is clamped on load (never raises).
    config_file.write_text(json.dumps({CHAIN_KEY: {CHAIN_MAX_TRANSITIONS_KEY: 999}}))
    assert store.load_chain_max_transitions() == CHAIN_MAX_TRANSITIONS_MAX
    config_file.write_text(json.dumps({CHAIN_KEY: {CHAIN_MAX_TRANSITIONS_KEY: -5}}))
    assert store.load_chain_max_transitions() == CHAIN_MAX_TRANSITIONS_MIN


def test_save_chain_preserves_other_sections(tmp_path):
    """Saving the chain budget preserves scan/voice_styles/profiles sections."""
    config_file = tmp_path / "tuning.json"
    store = ConfigStore(config_path=str(config_file))

    # Seed a profile pair, a scan timeout, and a voice style first.
    pair = {PROFILE_FILE: store.load_profile(PROFILE_FILE),
            PROFILE_MIC: store.load_profile(PROFILE_MIC)}
    store.save_profiles(pair)
    store.save_scan_timeout(42)
    store.save_voice_style("ghost", {})

    # Now save a chain value.
    store.save_chain_max_transitions(8)

    with open(config_file, "r") as f:
        raw = json.load(f)

    # The chain value landed.
    assert raw[CHAIN_KEY][CHAIN_MAX_TRANSITIONS_KEY] == 8
    # And the other sections survived.
    assert raw[SCAN_KEY][SCAN_TIMEOUT_KEY] == 42
    assert VOICE_STYLES_KEY in raw and "ghost" in raw[VOICE_STYLES_KEY]
    assert "profiles" in raw and PROFILE_FILE in raw["profiles"]


def test_module_level_chain_wrappers(tmp_path, monkeypatch):
    """The thin module-level wrappers delegate to the default store."""
    config_file = tmp_path / "tuning.json"
    monkeypatch.setenv(CONFIG_PATH_OVERRIDE_ENV_PRIMARY, str(config_file))
    # Rebind the default store so it picks up the overridden path.
    monkeypatch.setattr(config_store, "_default_store", ConfigStore())

    assert config_store.save_chain_max_transitions(11) == 11
    assert config_store.load_chain_max_transitions() == 11
