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

    # NOTE: the five signature gestures below are authored in CALIBRATED servo
    # angles (the same 0-270 values used by SAFE_LIMITS and the calibration
    # store) and every keyframe was validated collision-free with the kinematic
    # collision model (python -m kinematics.cli). Landmarks used:
    #   RT_SHOULDER_TILT (6): 55=arm at side, ~120=up-and-out, 150=high, 170=out
    #   RT_SHOULDER_ROTATOR (7): 0=down/at side, raises the arm forward as it
    #     increases; keep < 210 when the elbow is flexed (hand-to-face guard).
    #   RT_ELBOW_TILT (5): 5=straight, 90=right-angle-ish, 145=right angle
    #   RT_ELBOW_ROTATOR (4): 150=neutral, 270=palm up, 0=palm down

    async def wave(self):
        """Wave hello: raise the arm up and out, then rock the forearm.

        Channels: RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Raises the arm out to the side and up (tilt 55 -> 180), bends the elbow
        to a right angle so the forearm points up, rocks the forearm side to
        side 3x (the wave), then lowers. Shoulder rotator stays at 0, so this is
        a clean out-to-the-side wave that never approaches the head.
        """
        TILT_DOWN, TILT_UP = 55, 180
        ELBOW_STRAIGHT, ELBOW_BENT = 5, 90
        FOREARM_LEFT, FOREARM_RIGHT = 90, 210

        # Raise the arm out to the side, then bend the elbow up.
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_UP, 0.003, True)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT, ELBOW_STRAIGHT, ELBOW_BENT, 0.004, True)

        # Wave: rock the forearm between the two rotator angles 3x.
        for i in range(3):
            revert = i % 2 == 0
            await self.trunkController.move(
                constants.RT_ELBOW_ROTATOR,
                FOREARM_LEFT, FOREARM_RIGHT, 0.003, revert, 0.04)

        # Lower: straighten the elbow, then bring the arm back down.
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT, ELBOW_STRAIGHT, ELBOW_BENT, 0.004, False)
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_UP, 0.004, False)

    async def yawn_cover(self):
        """Yawn cover: bring the hand up in front of the mouth, hold, then lower.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Target pose (HARDWARE-MEASURED, hand directly in front of the mouth):
            tilt=35, rotator=200, elbow=170, forearm=185.

        NOTE: this pose sits in the coupled-shoulder region where the decoupled
        collision model is unreliable (it mislocates the folded-up hand), so the
        keyframes here were confirmed SAFE on the physical robot rather than by
        the model. The motion is staged to fold the arm up the same way a person
        would -- raise/rotate the shoulder first, THEN flex the elbow and turn
        the forearm to the mouth -- so the hand approaches the face from the
        front rather than sweeping through the body.
        """
        # Resting starts (from REST_POSITIONS) and measured yawn targets.
        TILT_REST, TILT_YAWN = 55, 35
        ROT_REST, ROT_YAWN = 0, 200
        ELBOW_REST, ELBOW_YAWN = 5, 170
        FOREARM_REST, FOREARM_YAWN = 150, 185

        # tilt=35 and elbow=170 fall BELOW/ABOVE the conservative global
        # SAFE_LIMITS (which stay tight because those angles collide in OTHER
        # shoulder positions, e.g. arm rotated to the side/head). This exact
        # folded-to-the-mouth pose was operator-verified safe on the physical
        # robot, so widen the clamp for JUST these two channels for the duration
        # of the gesture. All other channels keep the global limits, and the
        # override is still bounded by the servo electrical range.
        override = {
            constants.RT_SHOULDER_TILT: (35, 270),
            constants.RT_ELBOW_TILT: (0, 170),
        }
        with TrunkController.verified_pose_override(override):
            # UP: all four joints move together for a natural, non-robotic fold.
            # The shoulder (tilt + rotator) leads; the elbow and forearm hold
            # until the motion is ~1/3 done, then catch up and arrive with
            # everything else -- they are only collision-safe once the shoulder
            # has rotated part-way (operator-observed), and this also reads far
            # more lifelike than one-joint-at-a-time.
            await self.trunkController.move_to(
                {
                    constants.RT_SHOULDER_TILT: TILT_YAWN,
                    constants.RT_SHOULDER_ROTATOR: ROT_YAWN,
                    constants.RT_ELBOW_TILT: ELBOW_YAWN,
                    constants.RT_ELBOW_ROTATOR: FOREARM_YAWN,
                },
                steps=90, delay=0.02,
                start_fractions={
                    constants.RT_ELBOW_TILT: 0.33,
                    constants.RT_ELBOW_ROTATOR: 0.33,
                },
            )

            await asyncio.sleep(1.5)  # hold the yawn

            # DOWN: reverse. Open the elbow/forearm first (finish by 2/3), while
            # the shoulder lowers over the whole move, so the arm unfolds before
            # it drops -- the mirror of the safe ordering going up.
            await self.trunkController.move_to(
                {
                    constants.RT_ELBOW_TILT: ELBOW_REST,
                    constants.RT_ELBOW_ROTATOR: FOREARM_REST,
                    constants.RT_SHOULDER_ROTATOR: ROT_REST,
                    constants.RT_SHOULDER_TILT: TILT_REST,
                },
                steps=90, delay=0.02,
                start_fractions={
                    constants.RT_SHOULDER_ROTATOR: 0.33,
                    constants.RT_SHOULDER_TILT: 0.33,
                },
            )

    async def face_palm(self):
        """Face palm: bring the hand up toward the face in exasperation, then drop.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Raises and rotates the arm, then flexes the elbow to bring the palm up to
        the face -- stopping at the model-validated "approach" pose (elbow ~140,
        rotator ~190) that reaches the face WITHOUT entering the guarded
        hand-to-head zone. Holds the palm-to-face beat, then lowers.
        """
        TILT_DOWN, TILT_HOLD = 55, 60
        ROT_DOWN, ROT_UP = 0, 190
        ELBOW_STRAIGHT, ELBOW_FLEX = 5, 140

        # Tilt slightly + rotate the arm up in front of the face.
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_HOLD, 0.005, True)
        await self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_UP, 0.004, True)

        # Flex the elbow to bring the palm to the face (the "ugh" moment).
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT, ELBOW_STRAIGHT, ELBOW_FLEX, 0.004, True)

        await asyncio.sleep(1.2)  # hold the face-palm

        # Drop the hand and lower the arm.
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_TILT, ELBOW_STRAIGHT, ELBOW_FLEX, 0.005, False)
        lower_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_UP, 0.004, False))
        untilt_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_HOLD, 0.005, False))
        await asyncio.gather(lower_task, untilt_task)

    async def menacing_reach(self):
        """Menacing reach: slowly extend the arm out toward the audience, claw, retract.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5)

        Raises the arm out to roughly shoulder height and rotates it forward
        toward the audience (a slow, deliberate reach), then curls the elbow
        slightly into a grasping "claw", holds, and retracts. Stays out in front
        -- never near the head/body.
        """
        TILT_DOWN, TILT_OUT = 55, 130
        ROT_DOWN, ROT_FWD = 0, 100
        ELBOW_STRAIGHT, ELBOW_CLAW = 5, 40

        # Slow, deliberate raise + forward reach (simultaneous for a smooth reach).
        raise_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_OUT, 0.006, True))
        reach_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_FWD, 0.006, True))
        await asyncio.gather(raise_task, reach_task)

        # Claw: curl the elbow slightly, a couple of grasping motions.
        for _ in range(2):
            await self.trunkController.move(
                constants.RT_ELBOW_TILT, ELBOW_STRAIGHT, ELBOW_CLAW, 0.005, True, 0.2)

        await asyncio.sleep(0.6)  # hold the reach

        # Retract slowly.
        lower_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_OUT, 0.006, False))
        unreach_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_FWD, 0.006, False))
        await asyncio.gather(lower_task, unreach_task)

    async def beckon(self):
        """Beckon "come here": raise the arm, palm up, curl the forearm inward 3x.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Raises the arm up-and-forward, turns the palm up, then curls the elbow in
        and out three times (the "come here" summon), and lowers. Rotator stays
        modest so the curling forearm never enters the hand-to-face zone.
        """
        TILT_DOWN, TILT_UP = 55, 150
        ROT_DOWN, ROT_UP = 0, 90
        FOREARM_NEUTRAL, FOREARM_PALM_UP = 150, 270
        ELBOW_OPEN, ELBOW_CURL = 5, 100

        # Raise the arm up and forward, palm turning up.
        raise_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_UP, 0.004, True))
        rotate_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_UP, 0.004, True))
        await asyncio.gather(raise_task, rotate_task)
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR, FOREARM_NEUTRAL, FOREARM_PALM_UP, 0.003, True)

        # Beckon: curl the elbow in and out 3x.
        for _ in range(3):
            await self.trunkController.move(
                constants.RT_ELBOW_TILT, ELBOW_OPEN, ELBOW_CURL, 0.004, True, self.DEFAULT_DELAY)

        # Lower everything back to rest.
        await self.trunkController.move_by_direction(
            constants.RT_ELBOW_ROTATOR, FOREARM_NEUTRAL, FOREARM_PALM_UP, 0.003, False)
        lower_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_TILT, TILT_DOWN, TILT_UP, 0.004, False))
        unrotate_task = asyncio.create_task(self.trunkController.move_by_direction(
            constants.RT_SHOULDER_ROTATOR, ROT_DOWN, ROT_UP, 0.004, False))
        await asyncio.gather(lower_task, unrotate_task)

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
