"""
movements.py

Gesture choreography built on top of TrunkController primitives.

## Design rules

Servo channel ownership
-----------------------
Every gesture below is tagged with the servo channels it owns.  When two
gestures are gathered concurrently (e.g. arm + head), their channel sets
MUST be disjoint — this is the only guarantee against mechanical interference.

    ARM  channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                   RT_ELBOW_TILT (5),       RT_ELBOW_ROTATOR (4)
    HEAD channels: NECK_PAN (0), NECK_TILT (1)

Concurrency model
-----------------
All methods are async coroutines.  Within a single gesture, concurrent joint
motion is expressed with asyncio.create_task() + asyncio.gather().  The event
loop is single-threaded, so interleaving happens at every await — one joint
moves one degree, yields, the other joint moves one degree, yields, etc.
This gives smooth, simultaneous-looking motion with no hardware contention as
long as each task owns distinct channels.

Safe pairs for asyncio.gather()
--------------------------------
Any ARM gesture + any HEAD gesture may be gathered — their channels never
overlap.  Never gather two arm gestures or two head gestures together.

Dependency chain
----------------
    animatronic.py / controller.py
        └── Movements (this file)
                └── TrunkController (trunkcontroller.py)
                        └── adafruit_servokit / PCA9685 hardware
"""

import asyncio
from trunkcontroller import TrunkController
import constants


class Movements:
    """Orchestrates multi-joint gestures for the animatronic.

    Gestures are grouped into ARM gestures (channels 4–7) and HEAD gestures
    (channels 0–1).  Any arm gesture can safely be gathered with any head
    gesture.  See module docstring for the full concurrency contract.
    """

    # Default revert-delay used when a gesture bounces back to start.
    DEFAULT_DELAY = 0.05

    def __init__(self, name):
        self.name = name

    # Shared controller — class-level so all Movements instances share the board.
    trunkController = TrunkController("Servo TrunkController")

    # ================================================================== #
    # ARM gestures  (channels: RT_SHOULDER_ROTATOR, RT_SHOULDER_TILT,    #
    #                           RT_ELBOW_TILT, RT_ELBOW_ROTATOR)          #
    # Safe to gather with any HEAD gesture.                               #
    # ================================================================== #

    async def wave(self):
        """Wave: raise arm fully, bend elbow, oscillate forearm 3×, lower.

        Channels: RT_SHOULDER_ROTATOR (7), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Sequence:
        1. Raise the shoulder to vertical (0 → 270°).
        2. Bend the elbow up to wave height (0 → 50°).
        3. Rotate the forearm back and forth 3× for the wave motion.
        4. Lower shoulder and elbow back to rest.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 270
        RT_ELBOW_TILT_MIN       = 0
        RT_ELBOW_TILT_MAX       = 50
        RT_ELBOW_ROTATE_MIN     = 0
        RT_ELBOW_ROTATE_MAX     = 80

        increasing = True

        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.001, increasing)

        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.002, increasing)

        # Alternate revert on each pass so the forearm rocks naturally.
        for i in range(3):
            revert = i % 2 == 0
            await self.trunkController.move(
                constants.RT_ELBOW_ROTATOR,
                RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.002, revert, 0.04)

        increasing = False
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.002, False)

    async def come(self):
        """Beckon: raise arm, rotate palm up, curl elbow 3×, lower.

        Channels: RT_SHOULDER_ROTATOR (7), RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Sequence:
        1. Raise shoulder to horizontal-ish position (0 → 40°).
        2. Rotate forearm so palm faces upward (10 → 260°).
        3. Curl the elbow 3 times to signal "come here" (0 → 70°, reverting).
        4. Lower arm and rotate palm back down.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 40
        RT_ELBOW_ROTATE_MIN     = 10
        RT_ELBOW_ROTATE_MAX     = 260
        RT_ELBOW_TILT_MIN       = 0
        RT_ELBOW_TILT_MAX       = 70

        increasing = True

        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)

        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

        for _ in range(3):
            await self.trunkController.move(
                constants.RT_ELBOW_TILT,
                RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, True, self.DEFAULT_DELAY)

        await asyncio.sleep(.2)

        increasing = False
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

    async def comein(self):
        """Compact beckon: tighter elbow rotation arc than come().

        Channels: RT_SHOULDER_ROTATOR (7), RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Like come() but rotates the elbow to 130° (vs 260°), giving a more
        restrained "come inside" motion with a wider elbow curl (25 → 160°).
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 40
        RT_ELBOW_ROTATE_MIN     = 10
        RT_ELBOW_ROTATE_MAX     = 130
        RT_ELBOW_TILT_MIN       = 25
        RT_ELBOW_TILT_MAX       = 160

        increasing = True

        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

        for _ in range(3):
            await self.trunkController.move(
                constants.RT_ELBOW_TILT,
                RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, True, self.DEFAULT_DELAY)

        increasing = False
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_TILT_MIN, RT_ELBOW_ROTATE_MAX, 0.0025, increasing)

    async def reach_out(self):
        """Reach: extend arm forward at shoulder height, then retract.

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5)

        Raises the shoulder, tilts it forward (extending toward the audience),
        then slowly retracts — a reaching or pointing-out gesture.
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 60
        RT_SHOULDER_TILT_MIN    = 0
        RT_SHOULDER_TILT_MAX    = 80
        RT_ELBOW_TILT_MIN       = 0
        RT_ELBOW_TILT_MAX       = 40

        # Raise shoulder and extend elbow simultaneously.
        raise_task   = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.003, True))
        tilt_task    = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT,
            RT_SHOULDER_TILT_MIN, RT_SHOULDER_TILT_MAX, 0.003, True))
        await asyncio.gather(raise_task, tilt_task)

        # Extend elbow slightly.
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, True)

        await asyncio.sleep(0.5)

        # Retract: reverse all joints.
        elbow_back   = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, False))
        await asyncio.gather(elbow_back)

        lower_task   = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.004, False))
        untilt_task  = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT,
            RT_SHOULDER_TILT_MIN, RT_SHOULDER_TILT_MAX, 0.004, False))
        await asyncio.gather(lower_task, untilt_task)

    async def yawn_cover(self):
        """Yawn: raise arm to cover mouth, hold, then lower slowly.

        Channels: RT_SHOULDER_ROTATOR (7), RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Raises the arm and bends the elbow so the forearm sweeps across the
        mouth area — the classic polite yawn cover.  Held for a beat before
        the arm lowers back to rest.

        Approximate face-coverage angles (tune per physical build):
            Shoulder rotator : ~200° = mouth level
            Elbow rotator    : ~90°  = forearm across face
            Elbow tilt       : ~60°  = forearm raised to face height
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 200
        RT_ELBOW_ROTATE_MIN     = 0
        RT_ELBOW_ROTATE_MAX     = 90
        RT_ELBOW_TILT_MIN       = 0
        RT_ELBOW_TILT_MAX       = 60

        # Raise shoulder and begin bending elbow simultaneously.
        raise_task  = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.003, True))
        rotate_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.004, True))
        await asyncio.gather(raise_task, rotate_task)

        # Finish positioning the forearm across the mouth.
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, True)

        # Hold pose — yawn duration.
        await asyncio.sleep(1.5)

        # Lower: reverse all joints simultaneously.
        lower_task   = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR,
            RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.004, False))
        unrotate_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR,
            RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.005, False))
        untilt_task  = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT,
            RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, False))
        await asyncio.gather(lower_task, unrotate_task, untilt_task)

    # ================================================================== #
    # HEAD gestures  (channels: NECK_PAN (0), NECK_TILT (1))             #
    # Safe to gather with any ARM gesture.                                #
    # ================================================================== #

    async def nod(self, reps=2):
        """Nod: tilt head down and back up, repeat reps times.

        Channels: NECK_TILT (1)

        Args:
            reps: Number of nod cycles (default 2).
        """
        NECK_TILT_MIN = 20
        NECK_TILT_MAX = 60
        for _ in range(reps):
            await self.trunkController.move(
                constants.NECK_TILT,
                NECK_TILT_MIN, NECK_TILT_MAX, 0.015, True, 0.05)

    async def nod_yes(self, reps=2):
        """Emphatic yes-nod: wider tilt arc than nod(), same repeat pattern.

        Channels: NECK_TILT (1)

        Args:
            reps: Number of nod cycles (default 2).
        """
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 30
        for _ in range(reps):
            await self.trunkController.move(
                constants.NECK_TILT,
                NECK_TILT_MIN, NECK_TILT_MAX, 0.015, True, 0.05)

    async def look_up(self):
        """Look up: tilt head back, hold briefly, return to level.

        Channels: NECK_TILT (1)
        """
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 90
        await self.trunkController.move(
            constants.NECK_TILT,
            NECK_TILT_MIN, NECK_TILT_MAX, 0.02, False)
        await asyncio.sleep(0.8)
        await self.trunkController.move(
            constants.NECK_TILT,
            NECK_TILT_MAX, NECK_TILT_MIN, 0.02, False)

    async def look_around(self):
        """Pan and tilt the neck simultaneously, then return to center.

        Channels: NECK_PAN (0), NECK_TILT (1)
        """
        await self.trunkController.neck_center()
        neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(10, 50))
        neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
        await asyncio.gather(neck_tilt, neck_pan)
        await self.trunkController.neck_center()

    async def look_around_small(self):
        """Subtle look-around: tighter tilt range, repeated twice.

        Channels: NECK_PAN (0), NECK_TILT (1)
        """
        await self.trunkController.neck_center()
        await asyncio.sleep(.5)
        for _ in range(2):
            neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(10, 30))
            neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
            await asyncio.gather(neck_tilt, neck_pan)
            await asyncio.sleep(.5)
        await self.trunkController.neck_center()

    async def neck_ellipse(self):
        """Trace an oval arc: pan + tilt simultaneously, return to center.

        Channels: NECK_PAN (0), NECK_TILT (1)
        """
        await self.trunkController.neck_center()
        neck_tilt = asyncio.create_task(self.trunkController.neck_tilt(0, 45))
        neck_pan  = asyncio.create_task(self.trunkController.neck_pan())
        await asyncio.gather(neck_tilt, neck_pan)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def swivel_head(self):
        """Two consecutive neck ellipse arcs, then center.

        Channels: NECK_PAN (0), NECK_TILT (1)
        """
        await self.trunkController.neck_center()
        await self.neck_ellipse()
        await self.neck_ellipse()
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def scan(self):
        """Pan the head side-to-side twice, return to center.

        Channels: NECK_PAN (0)
        """
        await self.trunkController.neck_center()
        for _ in range(2):
            await self.trunkController.neck_pan()
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def slow_scan(self):
        """Deliberate surveillance sweep: center → left → right → center.

        Channels: NECK_PAN (0)
        """
        await self.trunkController.neck_center()
        await self.trunkController.slow_scan()

    async def shake_head(self, reps=3):
        """Side-to-side head shake (full pan arc).

        Channels: NECK_PAN (0)

        Args:
            reps: Number of pan sweeps (default 3).
        """
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        await self.trunkController.neck_center()
        for _ in range(reps):
            await self.trunkController.move(
                constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, True, 1)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    async def shake_no(self, reps=2):
        """Emphatic "no" shake: wide pan arc (30–150°).

        Channels: NECK_PAN (0)

        Args:
            reps: Number of pan sweeps (default 2).
        """
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.trunkController.neck_center()
        for _ in range(reps):
            await self.trunkController.move(
                constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.005, True, 0.01)
        await asyncio.sleep(.5)
        await self.trunkController.neck_center()

    async def small_shake_no(self, reps=2):
        """Subtle "no" shake: narrow pan arc (70–110°).

        Channels: NECK_PAN (0)

        Args:
            reps: Number of pan sweeps (default 2).
        """
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 110
        await self.trunkController.neck_center()
        for _ in range(reps):
            await self.trunkController.move(
                constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.005, True, 0.01)
        await asyncio.sleep(1)
        await self.trunkController.neck_center()

    # ================================================================== #
    # COMPOSITE gestures — arm + head gathered simultaneously             #
    # Each method documents which arm and head gesture it combines.       #
    # ================================================================== #

    async def wave_and_nod(self):
        """Wave the arm while nodding yes.

        ARM: wave()  ·  HEAD: nod_yes()
        """
        await asyncio.gather(
            asyncio.create_task(self.wave()),
            asyncio.create_task(self.nod_yes()),
        )

    async def wave_and_look_around(self):
        """Wave the arm while scanning the environment.

        ARM: wave()  ·  HEAD: look_around()
        """
        await asyncio.gather(
            asyncio.create_task(self.wave()),
            asyncio.create_task(self.look_around()),
        )

    async def wave_and_swivel(self):
        """Wave the arm while doing a double neck-ellipse swivel.

        ARM: wave()  ·  HEAD: swivel_head()
        """
        await asyncio.gather(
            asyncio.create_task(self.wave()),
            asyncio.create_task(self.swivel_head()),
        )

    async def come_and_look(self):
        """Beckon while scanning the environment.

        ARM: come()  ·  HEAD: look_around()
        """
        await asyncio.gather(
            asyncio.create_task(self.come()),
            asyncio.create_task(self.look_around()),
        )

    async def come_and_swivel(self):
        """Beckon while doing a double neck-ellipse swivel.

        ARM: come()  ·  HEAD: swivel_head()
        """
        await asyncio.gather(
            asyncio.create_task(self.come()),
            asyncio.create_task(self.swivel_head()),
        )

    async def reach_and_look(self):
        """Reach toward audience while looking around.

        ARM: reach_out()  ·  HEAD: look_around()
        """
        await asyncio.gather(
            asyncio.create_task(self.reach_out()),
            asyncio.create_task(self.look_around()),
        )

    async def yawn_and_look_up(self):
        """Cover mouth for a yawn while tilting head back.

        ARM: yawn_cover()  ·  HEAD: look_up()
        """
        await asyncio.gather(
            asyncio.create_task(self.yawn_cover()),
            asyncio.create_task(self.look_up()),
        )

    async def patrol(self):
        """Idle patrol: neck ellipse followed by a small look-around.

        HEAD only — no arm movement.  Used for low-key ambient animation.
        """
        await self.neck_ellipse()
        await asyncio.sleep(1)
        await self.look_around_small()


if __name__ == '__main__':
    # Quick interactive testing — uncomment the gesture you want to run.
    mv = Movements("Servo Movements")
    # asyncio.run(mv.wave())
    # asyncio.run(mv.come())
    # asyncio.run(mv.wave_and_nod())
    # asyncio.run(mv.yawn_and_look_up())
