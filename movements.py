"""
movements.py

High-level gesture choreography built on top of TrunkController primitives.

Each method in Movements composes one or more TrunkController calls into a
recognizable gesture (wave, nod, head-shake, etc.).  All methods are async
coroutines and are intended to be driven by animatronic.py or controller.py.

Dependency chain:
    animatronic.py / controller.py
        └── Movements   (this file)
                └── TrunkController  (trunkcontroller.py)
                        └── adafruit_servokit / PCA9685 hardware
"""

import asyncio
from trunkcontroller import TrunkController
import constants


class Movements:
    """Orchestrates multi-joint gestures for the animatronic."""

    # Inter-step pause used as a default revert delay in several gestures.
    DEFAULT_DELAY = 0.05

    def __init__(self, name):
        self.name = name
        self.DEFAULT_DELAY = 0.05

    # Shared controller instance (class-level).
    trunkController = TrunkController("Servo TrunkController")

    # ------------------------------------------------------------------ #
    # Arm gestures                                                         #
    # ------------------------------------------------------------------ #

    async def come(self):
        """Beckon gesture: raise arm, rotate palm up, curl elbow 3×, then lower.

        Sequence:
        1. Raise the shoulder to a horizontal-ish position.
        2. Rotate the forearm so the palm faces up.
        3. Repeatedly curl (bend) the elbow to signal "come here."
        4. Lower the arm and rotate palm back down.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 110

        RT_ELBOW_ROTATE_MIN = 10
        RT_ELBOW_ROTATE_MAX = 260

        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 70

        revert = True
        increasing = True

        # Raise arm to gesture position.
        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        # Rotate palm up.
        await self.trunkController.move_by_direction(constants.RT_ELBOW_ROTATOR,
                                                     RT_ELBOW_ROTATE_MIN,
                                                     RT_ELBOW_ROTATE_MAX, 0.0025, increasing)
        # Curl elbow 3 times.
        for x in range(3):
            await self.trunkController.move(constants.RT_ELBOW_TILT,
                                            RT_ELBOW_TILT_MIN,
                                            RT_ELBOW_TILT_MAX, 0.005, revert, self.DEFAULT_DELAY)

        await asyncio.sleep(.2)

        # Lower arm back to resting position.
        increasing = False
        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
        # Rotate palm back down.
        await self.trunkController.move_by_direction(constants.RT_ELBOW_ROTATOR,
                                                     RT_ELBOW_ROTATE_MIN,
                                                     RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

    async def comein(self):
        """Variant beckon gesture with a tighter elbow rotation arc.

        Similar to come() but rotates the elbow only to 130° (vs 260°),
        giving a more compact "come inside" motion.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 40

        RT_ELBOW_ROTATE_MIN = 10
        RT_ELBOW_ROTATE_MAX = 130

        RT_ELBOW_TILT_MIN = 25
        RT_ELBOW_TILT_MAX = 160

        revert = True
        increasing = True

        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        await self.trunkController.move_by_direction(constants.RT_ELBOW_ROTATOR,
                                                     RT_ELBOW_ROTATE_MIN,
                                                     RT_ELBOW_ROTATE_MAX, 0.0025, increasing)
        for x in range(3):
            await self.trunkController.move(constants.RT_ELBOW_TILT,
                                            RT_ELBOW_TILT_MIN,
                                            RT_ELBOW_TILT_MAX, 0.005, revert, self.DEFAULT_DELAY)

        increasing = False
        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
        await self.trunkController.move_by_direction(constants.RT_ELBOW_ROTATOR,
                                                     RT_ELBOW_TILT_MIN,
                                                     RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

    async def wave(self):
        """Wave gesture: raise arm, bend elbow, oscillate forearm 3×, then lower.

        Sequence:
        1. Raise the shoulder to vertical.
        2. Bend the elbow up.
        3. Rotate the forearm back and forth 3 times (waving motion).
        4. Lower shoulder and elbow back to rest.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 270
        RT_ELBOW_ROTATE_MIN = 0
        RT_ELBOW_ROTATE_MAX = 45
        # elbow is reversed here but works for come() so we can't flip
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 0

        increasing = True

        # Raise arm fully.
        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.001, increasing)
        # Bend elbow to wave position.
        await self.trunkController.move_by_direction(constants.RT_ELBOW_TILT,
                                                     RT_ELBOW_TILT_MIN,
                                                     RT_ELBOW_TILT_MAX, 0.002, increasing)

        # Oscillate forearm: alternate revert each pass for a natural wave.
        for i in range(0, 3, 1):
            revert = i % 2 == 0
            await self.trunkController.move(constants.RT_ELBOW_ROTATOR,
                                            RT_ELBOW_ROTATE_MIN,
                                            RT_ELBOW_ROTATE_MAX, 0.002, revert, 0.04)

        # Lower arm back to rest.
        increasing = False
        await self.trunkController.move_by_direction(constants.RT_SHOULDER_ROTATOR,
                                                     RT_SHOULDER_ROTATOR_MIN,
                                                     RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        await self.trunkController.move_by_direction(constants.RT_ELBOW_TILT,
                                                     RT_ELBOW_TILT_MIN,
                                                     RT_ELBOW_TILT_MAX, 0.002, False)

    # ------------------------------------------------------------------ #
    # Head / neck gestures                                                 #
    # ------------------------------------------------------------------ #

    async def look_around(self):
        """Look around gesture: simultaneously pan and tilt the neck.

        Centers the neck first, then runs neck_tilt and neck_pan concurrently
        using asyncio tasks, then returns to center.
        """
        await self.trunkController.neck_center()
        neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(10, 50))
        neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
        await asyncio.gather(neck_tilt, neck_pan)
        await self.trunkController.neck_center()

    async def look_around_small(self):
        """Smaller look-around gesture repeated twice with short pauses.

        Uses a tighter tilt range (10–30°) vs look_around for a more subtle
        curiosity expression.
        """
        await self.trunkController.neck_center()
        await asyncio.sleep(.5)
        for x in range(2):
            neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(10, 30))
            neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
            await asyncio.gather(neck_tilt, neck_pan)
            await asyncio.sleep(.5)
        await self.trunkController.neck_center()

    async def neck_ellipse(self):
        """Trace an elliptical arc with the head (pan + tilt simultaneously).

        Moves tilt from 0–45° while panning the full neck range, creating a
        smooth oval head motion. Returns to center afterward.
        """
        await self.trunkController.neck_center()
        neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(0, 45))
        neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
        await asyncio.gather(neck_tilt, neck_pan)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def swivel_head(self):
        """Swivel the head in two consecutive ellipse arcs, then center."""
        await self.trunkController.neck_center()
        await self.neck_ellipse()
        await self.neck_ellipse()
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def scan(self):
        """Pan the head side-to-side twice, then return to center."""
        await self.trunkController.neck_center()
        for _ in range(2):
            await self.trunkController.neck_pan()
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def slow_scan(self):
        """Perform a slow deliberate head scan (delegates to TrunkController)."""
        await self.trunkController.neck_center()
        await self.trunkController.slow_scan()

    async def nod_yes(self, revert=True):
        """Nod the head up and down twice to indicate "yes."

        Args:
            revert: If True, each tilt returns to its start position.
        """
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 30
        for _ in range(2):
            await self.trunkController.move(constants.NECK_TILT,
                                            NECK_TILT_MIN, NECK_TILT_MAX,
                                            0.015, revert, .05)

    async def shake_head(self, revert=True):
        """Shake the head side-to-side three times (full pan arc).

        Args:
            revert: If True, each pan returns to its start position.
        """
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        await self.trunkController.neck_center()
        for _ in range(3):
            await self.trunkController.move(constants.NECK_PAN,
                                            NECK_PAN_MIN, NECK_PAN_MAX,
                                            0.01, revert, 1)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def shake_no(self, revert=True):
        """Shake the head "no" twice with a wide arc (30–150°).

        Args:
            revert: If True, each pan returns to its start position.
        """
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.trunkController.neck_center()
        for _ in range(2):
            await self.trunkController.move(constants.NECK_PAN,
                                            NECK_PAN_MIN, NECK_PAN_MAX,
                                            0.005, revert, 0.01)
        await asyncio.sleep(.5)
        await self.trunkController.neck_center()

    async def small_shake_no(self, revert=True):
        """Subtle head-shake "no" with a narrow arc (70–110°).

        Use when a less emphatic denial expression is needed.

        Args:
            revert: If True, each pan returns to its start position.
        """
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 110
        await self.trunkController.neck_center()
        for _ in range(2):
            await self.trunkController.move(constants.NECK_PAN,
                                            NECK_PAN_MIN, NECK_PAN_MAX,
                                            0.005, revert, 0.01)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()


if __name__ == '__main__':
    # Module-level instance for quick interactive testing.
    # Uncomment one of the asyncio.run lines below to exercise a gesture directly.
    mv = Movements("Servo Movements")
    # asyncio.run(mv.wave())
    # asyncio.run(mv.shake_head())
    # asyncio.run(mv.shake_no())
