"""Non-safety tests for TrunkController's per-task arm-only write guard.

FEAT-001 adds a fail-closed channel guard so Scan Mode's arm-only responder
cannot write the neck channels (0/1) owned by the concurrent neck tracker. The
guard is a ``contextvars.ContextVar`` consulted at the single ``set_angle``
chokepoint, entered via the ``TrunkController.restrict_channels`` context
manager. Because a ContextVar is COPIED when ``asyncio.create_task`` spawns a
task, the restriction applies only to the task that entered it (and its awaited
descendants), never to a concurrently running tracker task.

These tests verify that ownership/isolation behaviour ONLY. They are NOT
collision/limit simulation checks: nothing here asserts SAFE_LIMITS ranges,
CLAMPED warnings, or FORBIDDEN_COMBINATIONS. The operator validates travel
limits and collisions on the physical robot.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_channel_guard.py -q --maxfail=1
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
from trunkcontroller import TrunkController, ChannelGuardError  # noqa: E402

# Arm-only channel set the Scan responder is allowed to drive (the neck
# channels 0/1 are deliberately excluded). Matches detection_routine_map's
# ARM_ONLY_CHANNELS = {3,4,5,6,7} without importing the vision stack.
ARM_ONLY_CHANNELS = frozenset({3, 4, 5, 6, 7})


def test_restrict_channels_blocks_neck():
    """A neck write inside restrict_channels raises; arm write succeeds; no leak."""
    controller = TrunkController("guard-blocks-neck")

    with controller.restrict_channels(ARM_ONLY_CHANNELS):
        # A neck write (channel 0) is outside the allowed set -> fail closed.
        try:
            controller.set_angle(constants.NECK_PAN, 90)
            raised = False
        except ChannelGuardError:
            raised = True
        assert raised, "neck write inside restrict_channels should raise ChannelGuardError"

        # An arm write (channel 5) is allowed and goes through normally.
        controller.set_angle(constants.RT_ELBOW_TILT, 100)
        assert controller.kit.servo[constants.RT_ELBOW_TILT].angle is not None

    # After the block the guard is released -> the same neck write succeeds.
    controller.set_angle(constants.NECK_PAN, 90)
    assert controller.kit.servo[constants.NECK_PAN].angle is not None


def test_restrict_channels_is_per_task():
    """The guard is per-task: a concurrent tracker's neck writes never raise.

    Spawns a background 'tracker' task that loops neck writes WITHOUT any
    restrict_channels in its own context, concurrently with an 'adapter' task
    that enters restrict_channels(ARM_ONLY_CHANNELS) and awaits several times so
    the two interleave. Because create_task copies the contextvar, the adapter's
    restriction must not leak into the tracker: the tracker's neck writes never
    raise, while a neck write issued INSIDE the adapter task does raise.
    """
    controller = TrunkController("guard-per-task")

    async def scenario():
        tracker_error = {"raised": False}
        tracker_writes = {"count": 0}
        stop = asyncio.Event()

        async def tracker():
            # No restrict_channels here: the tracker owns the neck and must be
            # free to write channels 0/1 the whole time.
            try:
                while not stop.is_set():
                    controller.set_angle(constants.NECK_PAN, 90)
                    controller.set_angle(constants.NECK_TILT, 90)
                    tracker_writes["count"] += 1
                    await asyncio.sleep(0)
            except ChannelGuardError:
                tracker_error["raised"] = True

        async def adapter():
            adapter_neck_raised = {"raised": False}
            with controller.restrict_channels(ARM_ONLY_CHANNELS):
                # Interleave with the tracker a few times.
                for _ in range(5):
                    controller.set_angle(constants.RT_ELBOW_TILT, 100)
                    await asyncio.sleep(0)
                # A neck write INSIDE the guarded task must fail closed.
                try:
                    controller.set_angle(constants.NECK_PAN, 90)
                except ChannelGuardError:
                    adapter_neck_raised["raised"] = True
            return adapter_neck_raised["raised"]

        tracker_task = asyncio.create_task(tracker())
        adapter_neck_raised = await asyncio.create_task(adapter())
        stop.set()
        await tracker_task

        return tracker_error["raised"], tracker_writes["count"], adapter_neck_raised

    tracker_raised, tracker_count, adapter_neck_raised = asyncio.run(scenario())

    # The concurrent tracker's neck writes NEVER raised...
    assert tracker_raised is False
    assert tracker_count > 0, "tracker should have issued neck writes"
    # ...but a neck write inside the guarded adapter task DID raise.
    assert adapter_neck_raised is True
