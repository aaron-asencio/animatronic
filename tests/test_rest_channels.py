"""Non-safety channel-set test for TrunkController.return_to_rest(channels=...).

FEAT-001 adds an optional ``channels`` restriction to
``TrunkController.return_to_rest`` so an arm-only response's cleanup can rest
only the arm channels and never move the neck. This test verifies the
channel-SET addressing behaviour only (which channels get written vs. left
untouched) — it is NOT a collision/limit simulation check: it asserts nothing
about SAFE_LIMITS ranges, CLAMPED warnings, or FORBIDDEN_COMBINATIONS. The
operator validates travel limits and collisions on the physical robot.

The fake ServoKit (SERVO_SIM=1) starts every channel's ``.angle`` at None and
records the last-written value, so a channel that was never addressed by the
rest sweep still reads ``None`` while an addressed channel reads a number.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_rest_channels.py -q --maxfail=1
"""

import asyncio
import os
import sys

# Hardware-free servo path: set BEFORE importing anything from src that reaches
# the trunkcontroller/hardware stack.
os.environ["SERVO_SIM"] = "1"

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import constants  # noqa: E402
from trunkcontroller import TrunkController  # noqa: E402

NECK_CHANNELS = {constants.NECK_PAN, constants.NECK_TILT}


def _written_channels(controller):
    """Return the set of channels the fake kit has a commanded angle for.

    Args:
        controller: The TrunkController whose shared fake kit to inspect.

    Returns:
        A set of channel indices whose fake servo ``.angle`` is not None.
    """
    return {
        ch for ch in constants.REST_POSITIONS
        if controller.kit.servo[ch].angle is not None
    }


def _reset_fake_kit(controller):
    """Clear every fake servo's commanded angle back to None.

    The fake kit is shared at class level, so reset it between the two sweeps
    to measure each one independently.

    Args:
        controller: The TrunkController whose shared fake kit to reset.
    """
    for ch in range(len(controller.kit.servo)):
        controller.kit.servo[ch]._angle = None


def test_restricted_rest_skips_neck_channels():
    """return_to_rest(channels={arm}) writes no neck channel; default rests 0/1."""
    controller = TrunkController("rest-channel-test")

    arm_channels = frozenset({3, 4, 5, 6, 7})

    # Restricted sweep: only arm channels present in REST_POSITIONS are rested.
    _reset_fake_kit(controller)
    asyncio.run(controller.return_to_rest(channels=arm_channels))
    written_restricted = _written_channels(controller)

    # No neck channel (0/1) was addressed.
    assert written_restricted.isdisjoint(NECK_CHANNELS)
    # Only arm channels that actually exist in REST_POSITIONS were written.
    expected_arm = {ch for ch in arm_channels if ch in constants.REST_POSITIONS}
    assert written_restricted == expected_arm
    # Sanity: at least one arm channel was addressed.
    assert written_restricted

    # Default sweep rests the whole robot, including both neck channels.
    _reset_fake_kit(controller)
    asyncio.run(controller.return_to_rest())
    written_default = _written_channels(controller)

    assert NECK_CHANNELS <= written_default
    assert written_default == set(constants.REST_POSITIONS)
