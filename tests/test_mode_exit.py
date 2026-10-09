"""Tests for the mode_exit leaf module (end-reason <-> exit-code mapping).

The leaf is stdlib-only, so no SERVO_SIM / hardware setup is needed. Run with:

    pytest tests/test_mode_exit.py -q --maxfail=1
"""

import os
import sys

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import mode_exit  # noqa: E402
import servo_lock  # noqa: E402


def test_reason_to_exit_code_known_reasons():
    """The three known reasons map to their distinct exit codes."""
    assert mode_exit.reason_to_exit_code("timeout") == mode_exit.EXIT_TIMEOUT == 10
    assert mode_exit.reason_to_exit_code("sensor") == mode_exit.EXIT_SENSOR == 11
    assert mode_exit.reason_to_exit_code("stop") == mode_exit.EXIT_STOP == 12


def test_reason_to_exit_code_unknown_and_none_fail_safe():
    """An unknown string or None maps to EXIT_STOP (never chains — fail safe)."""
    assert mode_exit.reason_to_exit_code("bogus") == mode_exit.EXIT_STOP
    assert mode_exit.reason_to_exit_code(None) == mode_exit.EXIT_STOP


def test_exit_code_to_reason_round_trip():
    """exit_code_to_reason inverts reason_to_exit_code for the three codes."""
    for reason in ("timeout", "sensor", "stop"):
        code = mode_exit.reason_to_exit_code(reason)
        assert mode_exit.exit_code_to_reason(code) == reason


def test_exit_code_to_reason_unknown_fail_safe():
    """An unrecognised code is treated as a non-chaining stop."""
    assert mode_exit.exit_code_to_reason(0) == mode_exit.REASON_STOP
    assert mode_exit.exit_code_to_reason(1) == mode_exit.REASON_STOP
    assert mode_exit.exit_code_to_reason(99) == mode_exit.REASON_STOP


def test_chainable_exit_codes_contents():
    """CHAINABLE_EXIT_CODES is exactly {timeout, sensor} and excludes stop."""
    assert mode_exit.CHAINABLE_EXIT_CODES == {mode_exit.EXIT_TIMEOUT,
                                              mode_exit.EXIT_SENSOR}
    assert mode_exit.EXIT_TIMEOUT in mode_exit.CHAINABLE_EXIT_CODES
    assert mode_exit.EXIT_SENSOR in mode_exit.CHAINABLE_EXIT_CODES
    # Stop, busy, crash, and clean are all NON-chainable.
    assert mode_exit.EXIT_STOP not in mode_exit.CHAINABLE_EXIT_CODES
    assert servo_lock.BUSY_EXIT_CODE not in mode_exit.CHAINABLE_EXIT_CODES
    assert 1 not in mode_exit.CHAINABLE_EXIT_CODES   # crash
    assert 0 not in mode_exit.CHAINABLE_EXIT_CODES   # clean


def test_exit_codes_pairwise_distinct_from_reserved():
    """The three chain codes are distinct from each other and from 0/1/3 (busy)."""
    codes = {mode_exit.EXIT_TIMEOUT, mode_exit.EXIT_SENSOR, mode_exit.EXIT_STOP}
    assert len(codes) == 3                              # pairwise distinct
    reserved = {0, 1, servo_lock.BUSY_EXIT_CODE}        # clean / crash / busy
    assert codes.isdisjoint(reserved)


def test_reason_strings_match_animatronic_constants():
    """The leaf REASON_* strings equal Animatronic's NAP/AWAKE interrupt values.

    Importing them and comparing guards the code<->reason table against drift:
    if someone renames a reason string on one side, this fails. Importing
    animatronic needs the simulated servo kit, so set SERVO_SIM before import.
    """
    os.environ.setdefault("SERVO_SIM", "1")
    from animatronic import Animatronic  # noqa: E402

    assert mode_exit.REASON_TIMEOUT == Animatronic.NAP_INTERRUPT_TIMEOUT
    assert mode_exit.REASON_SENSOR == Animatronic.NAP_INTERRUPT_SENSOR
    assert mode_exit.REASON_STOP == Animatronic.NAP_INTERRUPT_STOP
    assert mode_exit.REASON_TIMEOUT == Animatronic.AWAKE_INTERRUPT_TIMEOUT
    assert mode_exit.REASON_SENSOR == Animatronic.AWAKE_INTERRUPT_SENSOR
    assert mode_exit.REASON_STOP == Animatronic.AWAKE_INTERRUPT_STOP
