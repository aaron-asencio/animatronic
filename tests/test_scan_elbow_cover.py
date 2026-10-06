"""Non-safety tests for the scan -10 elbow-cover parameterization.

FEAT-001 lets Scan Mode pull the cover-mouth elbow back 10 degrees (162 -> 152)
so the hand clears the OFF-center head the neck tracker holds. The default
cover stays at 162 so every standalone / existing caller is byte-for-byte
unchanged. Two paths carry the parameter:

* the self-contained ``yawn_cover_arm_only`` (used directly by the Scan yawn
  responder), and
* the cover-mouth family primitive ``_yawn_cover_fold_up`` (used by the gated
  lead-in variants).

These tests assert the COMMANDED elbow angle (162 vs. 152) and that the arm-only
yawn writes NO neck channel while standalone yawn_cover still writes 162 AND the
neck. They are NOT collision/limit checks: nothing here asserts SAFE_LIMITS
ranges, CLAMPED warnings, or FORBIDDEN_COMBINATIONS. The operator validates
travel limits and collisions on the physical robot.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_elbow_cover.py -q --maxfail=1
"""

import asyncio
import os
import sys

# Hardware-free servo path: set BEFORE importing anything from src.
os.environ["SERVO_SIM"] = "1"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import constants  # noqa: E402
from movements import Movements  # noqa: E402

NECK_CHANNELS = {constants.NECK_PAN, constants.NECK_TILT}


def _reset_fake_kit(controller):
    """Clear every fake servo's commanded angle back to None (shared kit)."""
    for ch in range(len(controller.kit.servo)):
        controller.kit.servo[ch]._angle = None


def _run_recording(controller, coro_factory):
    """Run a coroutine while recording every angle commanded per channel.

    Wraps ``controller.set_angle`` so each write is captured BEFORE the normal
    clamp/write happens, then restores the original. Lets a test read the peak
    commanded angle for a channel (e.g. the elbow cover) even though the motion
    returns the joint to rest by the time it finishes.

    Args:
        controller: The shared TrunkController whose set_angle to spy on.
        coro_factory: Zero-arg callable returning the coroutine to run.

    Returns:
        Dict channel -> list of commanded angles, in write order.
    """
    _reset_fake_kit(controller)
    commanded = {}
    original = controller.set_angle

    def spy(servo_num, angle):
        commanded.setdefault(servo_num, []).append(angle)
        return original(servo_num, angle)

    controller.set_angle = spy
    try:
        asyncio.run(coro_factory())
    finally:
        controller.set_angle = original
    return commanded


def test_yawn_cover_arm_only_commands_152_no_neck():
    """yawn_cover_arm_only(152) commands elbow 152 and writes no neck channel."""
    mv = Movements("yawn-arm-only-152")
    commanded = _run_recording(
        mv.trunkController, lambda: mv.yawn_cover_arm_only(elbow_cover=152))

    elbow_cmds = commanded.get(constants.RT_ELBOW_TILT, [])
    assert 152 in elbow_cmds, f"expected elbow cover 152 to be commanded, got {elbow_cmds}"
    # The arm-only variant must never touch the neck channels.
    assert constants.NECK_PAN not in commanded
    assert constants.NECK_TILT not in commanded


def test_standalone_yawn_cover_commands_162_and_neck():
    """Standalone yawn_cover still commands elbow 162 AND writes the neck."""
    mv = Movements("yawn-standalone-162")
    commanded = _run_recording(mv.trunkController, lambda: mv.yawn_cover())

    elbow_cmds = commanded.get(constants.RT_ELBOW_TILT, [])
    assert 162 in elbow_cmds, f"expected standalone elbow cover 162, got {elbow_cmds}"
    # Standalone centers the head, so BOTH neck channels are commanded.
    assert constants.NECK_PAN in commanded
    assert constants.NECK_TILT in commanded


def test_fold_up_elbow_cover_parameter():
    """_yawn_cover_fold_up(152) commands 152; default commands 162."""
    mv = Movements("fold-up-param")

    scan = _run_recording(
        mv.trunkController, lambda: mv._yawn_cover_fold_up(elbow_cover=152))
    assert 152 in scan.get(constants.RT_ELBOW_TILT, [])

    default = _run_recording(
        mv.trunkController, lambda: mv._yawn_cover_fold_up())
    assert 162 in default.get(constants.RT_ELBOW_TILT, [])
