"""Non-safety no-neck-write tests for the Scan arm-only responder motions.

Scan Mode runs the neck tracker concurrently with an arm-only responder, so the
four motions the Scan builder dispatches MUST never write the neck channels
(0/1) and must leave the arm channels at their documented rest. These tests run
each motion directly under SERVO_SIM=1 (with random.seed(0) so the random
cycle/curl counts are deterministic) and read the fake kit's last-written angle:
a channel never addressed by the motion still reads None.

They verify channel OWNERSHIP / return-to-rest only. They are NOT
collision/limit checks: nothing here asserts SAFE_LIMITS ranges, CLAMPED
warnings, or FORBIDDEN_COMBINATIONS. The operator validates travel limits and
collisions on the physical robot.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_adapter_channels.py -q --maxfail=1
"""

import asyncio
import os
import random
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


def _run(controller, coro_factory):
    """Reset the shared fake kit, run the motion, return the final kit state.

    Args:
        controller: The shared TrunkController whose fake kit to inspect.
        coro_factory: Zero-arg callable returning the coroutine to run.

    Returns:
        Dict channel -> last-written angle (None if never addressed), for all
        channels on the kit.
    """
    _reset_fake_kit(controller)
    random.seed(0)
    asyncio.run(coro_factory())
    return {
        ch: controller.kit.servo[ch].angle
        for ch in range(len(controller.kit.servo))
    }


def _assert_no_neck(final):
    """Assert neither neck channel was written (both still read None)."""
    assert final[constants.NECK_PAN] is None, "NECK_PAN must not be written"
    assert final[constants.NECK_TILT] is None, "NECK_TILT must not be written"


def test_scan_awaken_writes_no_neck():
    """awaken_arm_only: no neck write; arm channels end at REST_POSITIONS."""
    mv = Movements("scan-awaken")
    final = _run(mv.trunkController, lambda: mv.awaken_arm_only())

    _assert_no_neck(final)
    # The arm stir lowers back to the documented REST_POSITIONS.
    for ch in (constants.RT_SHOULDER_ROTATOR, constants.RT_SHOULDER_TILT,
               constants.RT_ELBOW_TILT, constants.RT_ELBOW_ROTATOR):
        assert final[ch] == constants.REST_POSITIONS[ch], (
            f"ch{ch} ended at {final[ch]}, expected rest {constants.REST_POSITIONS[ch]}")


def test_scan_come_get_candy_writes_no_neck():
    """Both come-get-candy branches (beckon, come_here): no neck write; arm rests.

    Each gesture lowers the arm back to its documented rest pose. The elbow
    tilt rests at 0 (arm extended) in both beckon and come_here, matching their
    ELBOW_REST, which the Scan arm-only responder uses as-is.
    """
    mv = Movements("scan-come-get-candy")

    # Final rest pose shared by beckon and come_here (channels {3,4,5,6,7}).
    expected = {
        constants.RT_WRIST_TILT: 90,
        constants.RT_ELBOW_ROTATOR: 150,
        constants.RT_ELBOW_TILT: 0,
        constants.RT_SHOULDER_TILT: 55,
        constants.RT_SHOULDER_ROTATOR: 0,
    }

    for name, factory in (("beckon", lambda: mv.beckon()),
                          ("come_here", lambda: mv.come_here())):
        final = _run(mv.trunkController, factory)
        _assert_no_neck(final)
        for ch, want in expected.items():
            assert final[ch] == want, (
                f"{name}: ch{ch} ended at {final[ch]}, expected {want}")


def test_scan_yawn_writes_no_neck():
    """yawn_cover_arm_only(152): no neck write; arm rests; elbow reached 152."""
    mv = Movements("scan-yawn")

    # Record the commanded elbow angles to confirm the 152 cover was reached,
    # since the motion returns the elbow to rest by the end.
    _reset_fake_kit(mv.trunkController)
    random.seed(0)
    elbow_cmds = []
    original = mv.trunkController.set_angle

    def spy(servo_num, angle):
        if servo_num == constants.RT_ELBOW_TILT:
            elbow_cmds.append(angle)
        return original(servo_num, angle)

    mv.trunkController.set_angle = spy
    try:
        asyncio.run(mv.yawn_cover_arm_only(elbow_cover=152))
    finally:
        mv.trunkController.set_angle = original

    final = {
        ch: mv.trunkController.kit.servo[ch].angle
        for ch in range(len(mv.trunkController.kit.servo))
    }

    _assert_no_neck(final)
    assert 152 in elbow_cmds, f"elbow cover 152 should be commanded, got {elbow_cmds}"
    # Arm lands at the yawn's documented rest (elbow at 0 = extended).
    assert final[constants.RT_SHOULDER_ROTATOR] == 0
    assert final[constants.RT_SHOULDER_TILT] == 55
    assert final[constants.RT_ELBOW_TILT] == 0
    assert final[constants.RT_ELBOW_ROTATOR] == 150


def test_scan_nice_day_writes_no_neck():
    """_wave_arm(include_neck=False): no neck write; arm channels end at rest."""
    mv = Movements("scan-nice-day")
    final = _run(mv.trunkController, lambda: mv._wave_arm(include_neck=False))

    _assert_no_neck(final)
    # The wave lowers every driven joint back to REST_POSITIONS.
    for ch in (constants.RT_WRIST_TILT, constants.RT_SHOULDER_ROTATOR,
               constants.RT_SHOULDER_TILT, constants.RT_ELBOW_TILT,
               constants.RT_ELBOW_ROTATOR):
        assert final[ch] == constants.REST_POSITIONS[ch], (
            f"ch{ch} ended at {final[ch]}, expected rest {constants.REST_POSITIONS[ch]}")
