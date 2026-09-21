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
import contextlib
import random
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
        """Wave hello: raise the arm up, wave it while the head pans, then lower.

        Channels: NECK_PAN (0), RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Standalone signature wave: the head pans as part of the gesture. For use
        inside composites that pair the wave with a separate head gesture, call
        _wave_arm(include_neck=False) so the head gesture owns the neck channels.
        """
        await self._wave_arm(include_neck=True)

    async def _wave_arm(self, include_neck=True):
        """Core wave motion: raise the arm, oscillate it, then lower.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4), and optionally
                  NECK_PAN (0).

        Hardware-measured arm-up pose: shoulder rotator=270 (this is what lifts
        the arm), shoulder tilt=55, elbow tilt=0 (arm extended), forearm=30.
        With the arm up, the wave flaps the elbow rotator (22<->38) and swings
        the shoulder tilt (45<->65) TOGETHER a random 1-2 times, then returns to
        center and lowers. The elbow tilt stays flat at 0 throughout. All
        keyframes validated collision-free.

        Args:
            include_neck: When True, the head pans (84<->96) in sync with the
                wave. Set False when a caller gathers this with a head gesture,
                so the neck-pan channel isn't driven by two coroutines at once.
        """
        # Rest + arm-up values.
        ROT_REST, ROT_UP = 0, 270            # shoulder rotator lifts the arm
        TILT_REST, TILT_CENTER = 55, 55      # shoulder tilt stays ~55 (rest == wave center)
        ELBOW_REST, ELBOW_UP = 0, 0          # elbow tilt held flat (arm extended)
        FOREARM_REST, FOREARM_CENTER = 150, 30
        # Wave oscillation extremes (paired so the joints swing together).
        TILT_LO, TILT_HI = 45, 65            # shoulder tilt
        FOREARM_LO, FOREARM_HI = 22, 38      # elbow rotator (ch4) = the wave flap
        PAN_LO, PAN_HI = 84, 96
        PAN_CENTER = constants.NECK_CENTER   # 90
        NECK_TILT_LEVEL = 90                 # head level

        # RAISE: rotator up, elbow to the wave center, forearm to its up angle,
        # all together for a smooth lift. When we own the neck (standalone
        # wave), also level the head tilt to 90 so the wave starts head-level
        # regardless of where a prior gesture left it.
        raise_targets = {
            constants.RT_SHOULDER_ROTATOR: ROT_UP,
            constants.RT_SHOULDER_TILT: TILT_CENTER,
            constants.RT_ELBOW_TILT: ELBOW_UP,
            constants.RT_ELBOW_ROTATOR: FOREARM_CENTER,
        }
        if include_neck:
            raise_targets[constants.NECK_TILT] = NECK_TILT_LEVEL
        await self.trunkController.move_to(raise_targets, steps=45, delay=0.02)

        # WAVE: a random 1-2 cycles. Each cycle swings the tilt/elbow/pan to one
        # extreme then the other (that is one back-and-forth wave), moved
        # together via move_to for a natural synchronized wave. delay=0.04 runs
        # the wave at half the previous speed for a slower, smoother motion.
        cycles = random.randint(1, 2)
        print(f"[wave] waving {cycles} time(s)")
        for _ in range(cycles):
            hi = {
                constants.RT_SHOULDER_TILT: TILT_HI,
                constants.RT_ELBOW_ROTATOR: FOREARM_HI,
            }
            lo = {
                constants.RT_SHOULDER_TILT: TILT_LO,
                constants.RT_ELBOW_ROTATOR: FOREARM_LO,
            }
            if include_neck:
                hi[constants.NECK_PAN] = PAN_LO
                lo[constants.NECK_PAN] = PAN_HI
            await self.trunkController.move_to(hi, steps=16, delay=0.04)
            await self.trunkController.move_to(lo, steps=16, delay=0.04)

        # Return the waved joints (and head, if we drove it) to center.
        recenter = {
            constants.RT_SHOULDER_TILT: TILT_CENTER,
            constants.RT_ELBOW_ROTATOR: FOREARM_CENTER,
        }
        if include_neck:
            recenter[constants.NECK_PAN] = PAN_CENTER
        await self.trunkController.move_to(recenter, steps=16, delay=0.02)

        # LOWER: rotator back down, elbow/forearm back to rest, together.
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: ROT_REST,
                constants.RT_SHOULDER_TILT: TILT_REST,
                constants.RT_ELBOW_TILT: ELBOW_REST,
                constants.RT_ELBOW_ROTATOR: FOREARM_REST,
            },
            steps=56, delay=0.02,
        )

    async def yawn_cover(self):
        """Yawn cover: center the head, bring the hand to the mouth, hold, lower.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        The head is centered (pan=90, tilt=90) FIRST so the mouth faces forward
        for the yawn -- otherwise, if a prior gesture left the head turned or
        tilted, the hand would cover empty air instead of the mouth.

        Target arm pose (HARDWARE-MEASURED, hand directly in front of the mouth):
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
        ELBOW_REST, ELBOW_YAWN = 0, 165
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
            # Center the head first so the mouth faces forward for the yawn.
            # Neck channels (0,1) are disjoint from the arm channels and use the
            # normal global SAFE_LIMITS (the override only covers the arm).
            await self.trunkController.move_to(
                {
                    constants.NECK_PAN: constants.NECK_CENTER,   # 90 = forward
                    constants.NECK_TILT: 90,                     # 90 = level
                },
                steps=30, delay=0.02,
            )

            # UP (~0.9s, 2x faster than before so the hand covers the mouth
            # WHILE the ~2.5s yawn plays): the SHOULDER ROTATES FIRST -- rotator
            # and tilt start immediately -- and the ELBOW + forearm hold until
            # the move is 30% through, then bend the hand up to the mouth. This
            # gives a clear "arm swings up, THEN the hand folds to the face"
            # order (start_fractions are a portion of the whole move's timeline,
            # 0.0-1.0; steps*delay sets the total duration).
            await self.trunkController.move_to(
                {
                    constants.RT_SHOULDER_TILT: TILT_YAWN,
                    constants.RT_SHOULDER_ROTATOR: ROT_YAWN,
                    constants.RT_ELBOW_TILT: ELBOW_YAWN,
                    constants.RT_ELBOW_ROTATOR: FOREARM_YAWN,
                },
                steps=45, delay=0.02,
                start_fractions={
                    constants.RT_ELBOW_TILT: 0.30,
                    constants.RT_ELBOW_ROTATOR: 0.30,
                },
            )

            # Hold the hand over the mouth for the rest of the yawn, then lower
            # as the sound finishes (~2.5s clip - ~0.3s lead-in - ~0.9s fold).
            await asyncio.sleep(1.3)

            # DOWN (~1.1s): reverse. Open the elbow/forearm first, then the
            # shoulder lowers -- the arm unfolds before it drops. Lowered 20%
            # slower than the up-fold (steps 45 -> 56) for a relaxed settle.
            await self.trunkController.move_to(
                {
                    constants.RT_ELBOW_TILT: ELBOW_REST,
                    constants.RT_ELBOW_ROTATOR: FOREARM_REST,
                    constants.RT_SHOULDER_ROTATOR: ROT_REST,
                    constants.RT_SHOULDER_TILT: TILT_REST,
                },
                steps=56, delay=0.02,
                start_fractions={
                    constants.RT_SHOULDER_ROTATOR: 0.33,
                    constants.RT_SHOULDER_TILT: 0.33,
                },
            )

    async def face_palm(self):
        """Face palm: head drops into the hand, shakes 3x in dismay, then recovers.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Hardware-measured hand-at-face pose:
            NECK_PAN=90, NECK_TILT=140 (head down), elbow_rot=150, elbow_tilt=145,
            shoulder_tilt=40, shoulder_rotator=260.

        The arm folds up to the face while the head drops to meet it; then, with
        the hand covering the face, the head shakes side to side 3x (pan +/-15
        from center) in a "I can't believe it" dismay; then the arm lowers and
        the head returns to level.

        NOTE: shoulder_tilt=40 is just below the global SAFE_LIMITS floor (45),
        so this operator-verified pose widens that one channel via a verified-
        pose override for the duration of the gesture. All other channels use
        the normal global limits.
        """
        # Rest starts and measured face-palm targets.
        NECK_PAN_CENTER = constants.NECK_CENTER          # 90
        NECK_TILT_LEVEL, NECK_TILT_DOWN = 90, 140
        TILT_REST, TILT_FACE = 55, 40                    # shoulder tilt unchanged
        ROT_REST, ROT_FACE = 0, 200                      # ch7 = 200 (measured)
        ELBOW_REST, ELBOW_FACE = 0, 145                  # ch5 = 145
        FOREARM_REST, FOREARM_FACE = 150, 200            # ch4 = 200 (forearm turned)

        # Head-shake parameters (pan +/-7 from center, 3x).
        SHAKE_LEFT, SHAKE_RIGHT = NECK_PAN_CENTER + 7, NECK_PAN_CENTER - 7  # 97 / 83

        # shoulder_tilt=40 is below the global floor (45); operator-verified safe
        # in this folded-to-the-face pose only, so widen just that channel.
        override = {constants.RT_SHOULDER_TILT: (40, 270)}
        with TrunkController.verified_pose_override(override):
            # Make sure the head starts centered in pan before it drops.
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_CENTER}, steps=20, delay=0.02)

            # UP: shoulder rotates first, elbow folds the hand to the face, and
            # the head drops to meet it -- all together for a natural motion.
            await self.trunkController.move_to(
                {
                    constants.RT_SHOULDER_TILT: TILT_FACE,
                    constants.RT_SHOULDER_ROTATOR: ROT_FACE,
                    constants.RT_ELBOW_TILT: ELBOW_FACE,
                    constants.RT_ELBOW_ROTATOR: FOREARM_FACE,
                    constants.NECK_TILT: NECK_TILT_DOWN,
                },
                steps=45, delay=0.02,
                start_fractions={
                    constants.RT_ELBOW_TILT: 0.30,
                    constants.RT_ELBOW_ROTATOR: 0.30,
                },
            )

            # HOLD + head shake: with the hand over the face, shake the head
            # side to side 3x (+/-15 from center), then return to center.
            for _ in range(3):
                await self.trunkController.move(
                    constants.NECK_PAN, SHAKE_RIGHT, SHAKE_LEFT, 0.024, True, 0.05)
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_CENTER}, steps=20, delay=0.02)

            # DOWN (~1.1s, 20% slower): open the elbow first, then the shoulder
            # lowers and the head comes back up to level.
            await self.trunkController.move_to(
                {
                    constants.RT_ELBOW_TILT: ELBOW_REST,
                    constants.RT_ELBOW_ROTATOR: FOREARM_REST,
                    constants.RT_SHOULDER_ROTATOR: ROT_REST,
                    constants.RT_SHOULDER_TILT: TILT_REST,
                    constants.NECK_TILT: NECK_TILT_LEVEL,
                },
                steps=84, delay=0.02,
                start_fractions={
                    constants.RT_SHOULDER_ROTATOR: 0.33,
                    constants.RT_SHOULDER_TILT: 0.33,
                },
            )

    async def menacing_reach(self):
        """Menacing reach: extend the arm out, then slowly menace with the shoulder tilt.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Hardware-measured arm-out pose: shoulder rotator=209 (rotates the
        extended arm out toward the audience), shoulder tilt=40, elbow rotator=0,
        elbow tilt=0 (arm straight). With the arm out, the shoulder tilt swings
        slowly within [25, 60] eight times (the menacing reach). Each swing picks
        one of three randomized motion patterns -- a full crossing, an
        out-and-back via center, or a partial move -- relative to the arm's
        current tilt, with randomized endpoints/timing and a small smooth rotator
        jitter (see ``_menacing_reach_swing``), then the arm retracts (lowering
        ~40% faster than the reach-out).

        shoulder_tilt dips to 25, below the global floor (45); this is
        operator-verified safe in this arm-out pose only, so widen just that
        channel via verified_pose_override. All keyframes validated
        collision-free against the kinematic model.

        This standalone gesture delegates to the same phase primitives the
        Performance_Framework drives (reach lead-in, one menace swing, retract
        return), so composing ``menacing_reach_lead_in`` + eight
        ``menacing_reach_loop_body`` + ``menacing_reach_return`` reproduces the
        exact same servo command sequence WHEN THE RNG IS SEEDED IDENTICALLY
        before each run (the swings draw from the shared ``random`` module)
        (Requirement 9.3).
        """
        # tilt dips to 25, below the global floor of 45; operator-verified safe
        # in this arm-out pose only, so widen just that channel. The standalone
        # gesture holds the override across the whole reach/swing/retract span.
        with TrunkController.verified_pose_override(self._MENACING_REACH_OVERRIDE):
            await self._menacing_reach_reach()
            # MENACE: swing the shoulder tilt slowly within [25, 60], eight times.
            for _ in range(8):
                await self._menacing_reach_swing()
            await self._menacing_reach_retract()

    # --- menacing_reach shared primitives + phase adapters ---------------- #
    #
    # The standalone gesture above and the phase adapters below both call these
    # primitives, so a phased composition (lead_in + loop_body x3 + return)
    # issues the identical move_to command sequence as the standalone gesture
    # (Requirement 9.3). No audio logic lives in any of these methods
    # (Requirement 9.2).

    # Rest + arm-out pose values (shared by the standalone gesture and phases).
    _MR_ROT_REST, _MR_ROT_OUT = 0, 209        # shoulder rotator extends the arm out
    _MR_TILT_REST, _MR_TILT_CENTER = 55, 40   # shoulder tilt (40 = reach center)
    _MR_FOREARM_REST, _MR_FOREARM_OUT = 150, 0  # elbow rotator (arm extended)
    _MR_ELBOW_REST, _MR_ELBOW_OUT = 0, 0      # elbow tilt straight (arm extended)
    _MR_TILT_LO, _MR_TILT_HI = 25, 60         # the slow menacing swing band
    # shoulder_tilt dips to 25, below the global floor; operator-verified safe
    # in the arm-out pose only, so widen just that channel.
    _MENACING_REACH_OVERRIDE = {constants.RT_SHOULDER_TILT: (25, 270)}

    async def _menacing_reach_reach(self):
        """REACH: rotate the extended arm out and forward together.

        Also initializes the swing's tilt-tracking state to the reach center
        (``_MR_TILT_CENTER`` = 40) so both the standalone gesture and the phased
        composition start their menace swings from the same known tilt position.
        Because both paths call this primitive first, seeding ``random`` before
        each run makes their subsequent stateful swings produce identical command
        sequences (Requirement 9.3).

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5).
        """
        # Track where the shoulder tilt actually is between swings so each
        # _menacing_reach_swing can choose its next pattern relative to the
        # current position. Reset here (shared by both paths) to keep the
        # standalone and phased command streams in lock-step under a fixed seed.
        self._mr_tilt_pos = self._MR_TILT_CENTER
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._MR_ROT_OUT,
                constants.RT_SHOULDER_TILT: self._MR_TILT_CENTER,
                constants.RT_ELBOW_ROTATOR: self._MR_FOREARM_OUT,
                constants.RT_ELBOW_TILT: self._MR_ELBOW_OUT,
            },
            steps=50, delay=0.025,
        )

    async def _menace_tilt_move(self, tilt):
        """Drive one sub-move of the menace swing: tilt to ``tilt`` with a smooth
        rotator jitter layered into the SAME move_to.

        The shoulder-rotator jitter of ``_MR_ROT_OUT`` +/- up to 4 (in
        [205, 213]) is placed in the same targets dict as the tilt so it eases
        smoothly to its new jittered target over the same duration rather than
        snapping -- no tiny rapid separate writes. Timing varies per sub-move
        (steps ~[18, 30], delay ~0.03 +/- 0.006). Updates ``self._mr_tilt_pos``
        to the commanded tilt so the next swing chooses relative to where the arm
        actually is.

        Args:
            tilt: Target shoulder-tilt angle. Kept inside the safe swing band
                [25, 60] by the caller; move_to clamps as a final safeguard.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7).
        """
        rot = self._MR_ROT_OUT + random.randint(-4, 4)        # [205, 213]
        steps = random.randint(18, 30)
        delay = 0.03 + random.uniform(-0.006, 0.006)
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_TILT: tilt,
                constants.RT_SHOULDER_ROTATOR: rot,
            },
            steps=steps, delay=delay,
        )
        # Remember where the tilt ended so the next swing is chosen relatively.
        self._mr_tilt_pos = tilt

    async def _menacing_reach_swing(self):
        """MENACE: perform ONE randomly-chosen swing pattern of the shoulder tilt.

        The swing tracks the arm's current tilt across invocations via
        ``self._mr_tilt_pos`` (initialized to the reach center 40 in
        ``_menacing_reach_reach``) so the menace never looks metronomic. Each
        call randomly picks one of three motion patterns (via the shared
        ``random`` module):

        1. FULL CROSSING (most common): move from the current side to the
           OPPOSITE end -- LT->RT if currently low, RT->LT if currently high. If
           near center, a random end is picked. One sub-move.
        2. OUT-AND-BACK to the SAME side via center: e.g. at/near LT go
           LT -> center -> LT; at/near RT go RT -> center -> RT. Three sub-moves.
        3. PARTIAL move to a random intermediate target within the band, for
           finer variability. One sub-move.

        Endpoints are randomized WITHIN the band [25, 60]: low targets ~[25, 32],
        high targets ~[53, 60], center ~[38, 46]. A smooth shoulder-rotator
        jitter (209 +/- 4, in [205, 213]) is layered into the SAME move_to as
        each tilt sub-move (see ``_menace_tilt_move``). Timing varies per
        sub-move (steps ~[18, 30], delay ~0.03 +/- 0.006). So a single call may
        issue 1, 2, or 3 move_to sub-moves depending on the chosen pattern.

        Randomness is sourced from the shared ``random`` module and the tilt
        state is reset in the shared ``_menacing_reach_reach`` primitive, so
        seeding ``random.seed(x)`` before a run makes both the standalone gesture
        and the phased composition reproduce the identical command sequence
        (Requirement 9.3).

        SAFETY: elbow tilt stays at 0 (straight) throughout the swing, so with
        the rotator only in [205, 213] the hand-to-face FORBIDDEN_COMBINATIONS
        rule (elbow tilt 150-270 AND rotator 210-270 simultaneously) never
        triggers. All tilt targets are in-band [25, 60] by construction; move_to
        clamps too.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7).
        """
        # Helpers to draw randomized in-band targets for each region.
        def low_target():
            return random.randint(self._MR_TILT_LO, 32)          # ~[25, 32]

        def high_target():
            return random.randint(53, self._MR_TILT_HI)          # ~[53, 60]

        def center_target():
            return random.randint(38, 46)                        # ~[38, 46]

        # Classify where the arm currently is relative to the band center (40).
        pos = self._mr_tilt_pos
        near_center = 34 <= pos <= 46
        is_low = pos < 40

        # Pick a pattern. Bias toward full crossings (most menacing look) but
        # keep all three reachable: crossing ~50%, out-and-back ~30%, partial
        # ~20%.
        roll = random.random()
        if roll < 0.5:
            # Pattern 1: FULL CROSSING to the opposite end.
            if near_center:
                # From center, pick a random end to cross toward.
                target = high_target() if random.random() < 0.5 else low_target()
            elif is_low:
                target = high_target()
            else:
                target = low_target()
            await self._menace_tilt_move(target)
        elif roll < 0.8:
            # Pattern 2: OUT-AND-BACK to the SAME side via center.
            if near_center:
                # From center, drift out to a random side and back to center.
                if random.random() < 0.5:
                    await self._menace_tilt_move(high_target())
                else:
                    await self._menace_tilt_move(low_target())
                await self._menace_tilt_move(center_target())
            elif is_low:
                await self._menace_tilt_move(low_target())
                await self._menace_tilt_move(center_target())
                await self._menace_tilt_move(low_target())
            else:
                await self._menace_tilt_move(high_target())
                await self._menace_tilt_move(center_target())
                await self._menace_tilt_move(high_target())
        else:
            # Pattern 3: PARTIAL move to a random intermediate target in-band.
            await self._menace_tilt_move(
                random.randint(self._MR_TILT_LO, self._MR_TILT_HI))

    async def _menacing_reach_retract(self):
        """RETRACT: return the arm to rest, lowering ~40% faster.

        This is the phase the runner calls when playback ends, so once the
        audio/menace is done the arm drops quicker: the per-step delay is reduced
        from 0.025 to 0.015 (0.025 * 0.6), cutting the total sweep time ~40% while
        staying smooth (steps stay at 55 for smoothness).

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5).
        """
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._MR_ROT_REST,
                constants.RT_SHOULDER_TILT: self._MR_TILT_REST,
                constants.RT_ELBOW_ROTATOR: self._MR_FOREARM_REST,
                constants.RT_ELBOW_TILT: self._MR_ELBOW_REST,
            },
            steps=55, delay=0.015,
        )

    async def menacing_reach_lead_in(self):
        """Lead-in phase: reach the arm out; opens the audio gate.

        Owns arm channels 4-7. Opens the verified_pose_override for the sub-floor
        shoulder tilt and holds it across the lead-in -> loop -> return lifetime
        via an AsyncExitStack owned by this adapter; ``menacing_reach_return``
        closes it. Contains no audio logic -- reaching the arm out fully IS the
        gate condition, signalled by this coroutine simply completing
        (Requirements 9.1, 9.2, 9.3, 9.5).
        """
        # Open the override on a per-adapter AsyncExitStack so it stays active
        # across the loop-body swings and is released only in the return phase.
        self._menacing_reach_stack = contextlib.AsyncExitStack()
        self._menacing_reach_stack.enter_context(
            TrunkController.verified_pose_override(self._MENACING_REACH_OVERRIDE))
        await self._menacing_reach_reach()

    async def menacing_reach_loop_body(self):
        """Loop-body phase: one randomized menace swing of the shoulder tilt.

        Owns RT_SHOULDER_TILT (6) and RT_SHOULDER_ROTATOR (7) for the smooth
        rotator jitter. One invocation equals one swing; eight invocations
        reproduce the standalone gesture's eight swings (given the same RNG seed).
        Contains no audio logic (Requirements 9.1, 9.2, 9.3).
        """
        await self._menacing_reach_swing()

    async def menacing_reach_return(self):
        """Return phase: retract the arm to rest and release the pose override.

        Owns arm channels 4-7. Retracts via the shared primitive, then closes the
        AsyncExitStack opened in ``menacing_reach_lead_in`` so the
        verified_pose_override is scoped to exactly the lead-in -> loop -> return
        span (Requirements 8.2, 9.1, 9.3, 9.5).
        """
        try:
            await self._menacing_reach_retract()
        finally:
            stack = getattr(self, "_menacing_reach_stack", None)
            if stack is not None:
                await stack.aclose()
                self._menacing_reach_stack = None

    async def come_here(self):
        """Come here: wave someone toward you -- sweep the arm from rest to a
        pulled-in "come toward me" pose, twice.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Each rep runs from the resting position to the hardware-measured closed
        pose (arm closest to the body): shoulder rotator=129, shoulder tilt=14,
        elbow rotator=189, elbow tilt=140. All joints move concurrently. Runs
        twice with a 0.1s pause at the closed pose between reps.

        shoulder_tilt dips to 14, below the global floor (45); operator-verified
        safe in this pose only, so widen just that channel via
        verified_pose_override. All keyframes validated collision-free against
        the kinematic model.
        """
        # Rest + closed-pose values.
        ROT_REST, ROT_CLOSED = 0, 129        # shoulder rotator
        TILT_REST, TILT_CLOSED = 55, 14      # shoulder tilt (14 = pulled in)
        FOREARM_REST, FOREARM_CLOSED = 150, 189   # elbow rotator
        ELBOW_REST, ELBOW_CLOSED = 0, 140    # elbow tilt (deep flex = come here)

        closed = {
            constants.RT_SHOULDER_ROTATOR: ROT_CLOSED,
            constants.RT_SHOULDER_TILT: TILT_CLOSED,
            constants.RT_ELBOW_ROTATOR: FOREARM_CLOSED,
            constants.RT_ELBOW_TILT: ELBOW_CLOSED,
        }
        rest = {
            constants.RT_SHOULDER_ROTATOR: ROT_REST,
            constants.RT_SHOULDER_TILT: TILT_REST,
            constants.RT_ELBOW_ROTATOR: FOREARM_REST,
            constants.RT_ELBOW_TILT: ELBOW_REST,
        }

        # tilt dips to 14, below the global floor of 45; operator-verified safe
        # in this pulled-in pose only, so widen just that channel.
        override = {constants.RT_SHOULDER_TILT: (14, 270)}
        with TrunkController.verified_pose_override(override):
            for rep in range(2):
                # Sweep everything from rest to the closed pose, concurrently.
                await self.trunkController.move_to(closed, steps=40, delay=0.02)
                # Hold the "come here" pose briefly (0.1s).
                await asyncio.sleep(0.1)
                # Return to rest (concurrently) before the next rep / finish.
                await self.trunkController.move_to(rest, steps=40, delay=0.02)

    async def beckon(self):
        """Beckon "come here": raise the arm close to the body, curl the forearm 2-3x.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        Hardware-measured beckon pose (arm closest to the body): shoulder
        rotator=90 (lifts the arm), shoulder tilt=55, elbow rotator=270, elbow
        tilt=125. The "come here" curl swings the elbow tilt 105<->135, the
        shoulder rotation and elbow tilt moving concurrently as the arm lifts.
        Repeats a random 3-4 times, holds briefly, then lowers. All keyframes
        validated collision-free.
        """
        # Rest + beckon-pose values.
        ROT_REST, ROT_UP = 0, 90             # shoulder rotator lifts the arm
        TILT_REST, TILT_UP = 55, 55          # shoulder tilt stays ~55
        FOREARM_REST, FOREARM_UP = 150, 270  # elbow rotator (palm turned in)
        ELBOW_REST = 0                       # elbow tilt at rest (arm extended)
        ELBOW_LO, ELBOW_HI = 105, 135        # the "come here" curl arc

        # RAISE: rotator up, tilt, forearm and the elbow-tilt curl all move
        # together from t=0 -- the shoulder rotation and elbow tilt happen
        # concurrently so the arm curls in as it lifts.
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: ROT_UP,
                constants.RT_SHOULDER_TILT: TILT_UP,
                constants.RT_ELBOW_ROTATOR: FOREARM_UP,
                constants.RT_ELBOW_TILT: ELBOW_LO,
            },
            steps=45, delay=0.025,
        )

        # BECKON: curl the forearm in and out a random 3-4 times.
        curls = random.randint(3, 4)
        print(f"[beckon] beckoning {curls} time(s)")
        for _ in range(curls):
            await self.trunkController.move_to(
                {constants.RT_ELBOW_TILT: ELBOW_HI}, steps=14, delay=0.025)
            await self.trunkController.move_to(
                {constants.RT_ELBOW_TILT: ELBOW_LO}, steps=14, delay=0.025)

        # Hold the beckon pose briefly before lowering.
        await asyncio.sleep(0.25)

        # LOWER: extend the elbow, lower the arm, forearm back to rest, together.
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: ROT_REST,
                constants.RT_SHOULDER_TILT: TILT_REST,
                constants.RT_ELBOW_ROTATOR: FOREARM_REST,
                constants.RT_ELBOW_TILT: ELBOW_REST,
            },
            steps=50, delay=0.025,
        )

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

        Starts and ends level (tilt=90). Uses move_to so the smoothstep
        ease-in/out applies to each dip.

        Args:
            reps: Number of nod cycles (default 2).
        """
        NECK_TILT_LEVEL = constants.NECK_CENTER   # 90 = level
        NECK_TILT_DOWN = 120                       # chin down for the nod
        # Start level.
        await self.trunkController.move_to(
            {constants.NECK_TILT: NECK_TILT_LEVEL}, steps=15, delay=0.02)
        for _ in range(reps):
            await self.trunkController.move_to(
                {constants.NECK_TILT: NECK_TILT_DOWN}, steps=18, delay=0.02)
            await self.trunkController.move_to(
                {constants.NECK_TILT: NECK_TILT_LEVEL}, steps=18, delay=0.02)

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

    async def look_around_random(self, duration=15.0):
        """Idly scan the room: look to random spots for ~15s, then return to rest.

        Channels: NECK_PAN (0), NECK_TILT (1)

        Starts from center (pan=90, tilt=90) and, for ``duration`` seconds,
        repeatedly picks a random head direction and moves there:
          - pan (left/right): 40..140  (center 90 +/- 50)
          - tilt (up/down):   85..115  (85 = looking up, 115 = looking down)
        Both neck joints move together via move_to, so the smoothstep ease-in/
        ease-out applies and each glance accelerates and settles smoothly. A
        short random pause between glances makes the scanning feel natural.
        Returns to the resting center when the time is up.

        This standalone gesture delegates to the same phase primitives the
        Performance_Framework drives (center lead-in, one random glance + dwell,
        center return), so composing ``look_scan_lead_in`` + N x
        ``look_scan_loop_body`` + ``look_scan_return`` reuses the identical
        per-glance random bounds and dwell behavior (Requirement 9.4).

        Args:
            duration: How long to keep looking around, in seconds.
        """
        # Start centered so every scan begins from a known head-level pose.
        await self._look_scan_center()

        loop = asyncio.get_event_loop()
        start = loop.time()
        last_pan = constants.NECK_CENTER
        while (loop.time() - start) < duration:
            last_pan = await self._look_scan_glance(last_pan)

        # Return to the resting center when done.
        await self._look_scan_center()

    # --- look_around_random shared primitives + phase adapters ------------ #
    #
    # The standalone gesture above and the phase adapters below both call these
    # primitives, so the loop-body phase reuses the identical per-glance random
    # pan/tilt bounds and dwell behavior as the standalone scan (Requirement
    # 9.4). No audio logic lives in any of these methods (Requirement 9.2).

    # Scan bounds shared by the standalone gesture and the phase adapters.
    _LOOK_PAN_MIN, _LOOK_PAN_MAX = 40, 140       # 90 +/- 50
    _LOOK_TILT_MIN, _LOOK_TILT_MAX = 85, 115     # 85 = up, 115 = down
    _LOOK_MIN_PAN_DELTA = 8                        # reject glances that barely move

    async def _look_scan_center(self):
        """Center the head at the neutral pan/tilt pose (pan=90, tilt=90).

        Channels: NECK_PAN (0), NECK_TILT (1).
        """
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: constants.NECK_CENTER,
                constants.NECK_TILT: constants.NECK_CENTER,
            },
            steps=25, delay=0.04,
        )

    async def _look_scan_glance(self, last_pan):
        """Perform ONE random glance + settle dwell, then report the chosen pan.

        Channels: NECK_PAN (0), NECK_TILT (1).

        Picks a fresh random pan/tilt target each glance. Pan roams freely across
        its whole range, so successive looks vary naturally -- a short hop back
        toward center, a return near the same side, or a full left<->right
        crossing -- rather than always swinging side to side. A target so close
        to the current pan that it wouldn't visibly move is rejected. Every
        commanded angle stays within pan in [40, 140] and tilt in [85, 115].

        Args:
            last_pan: The pan angle of the previous glance, used to reject a new
                target that is too close to move visibly.

        Returns:
            The pan angle chosen for this glance (the caller's next ``last_pan``).
        """
        pan = random.randint(self._LOOK_PAN_MIN, self._LOOK_PAN_MAX)
        while abs(pan - last_pan) < self._LOOK_MIN_PAN_DELTA:
            pan = random.randint(self._LOOK_PAN_MIN, self._LOOK_PAN_MAX)
        tilt = random.randint(self._LOOK_TILT_MIN, self._LOOK_TILT_MAX)

        # Vary the travel time a little so the scanning looks organic.
        steps = random.randint(22, 34)
        await self.trunkController.move_to(
            {constants.NECK_PAN: pan, constants.NECK_TILT: tilt},
            steps=steps, delay=0.04,
        )
        # Random settle/gaze pause before the next glance.
        await asyncio.sleep(random.uniform(1.2, 3.6))
        return pan

    # Channels this movement's phase adapters own (disjoint from the arm set).
    look_scan_owned_channels = frozenset({constants.NECK_PAN, constants.NECK_TILT})

    async def look_scan_lead_in(self):
        """Lead-in phase: center the head before the scan begins.

        Owns NECK_PAN (0) and NECK_TILT (1). Establishes the known head-level
        start pose and seeds the per-glance ``last_pan`` state used by the loop
        body. Contains no audio logic (Requirements 9.1, 9.2, 9.4).
        """
        await self._look_scan_center()
        self._look_scan_last_pan = constants.NECK_CENTER

    async def look_scan_loop_body(self):
        """Loop-body phase: ONE random glance + dwell, reusing the scan bounds.

        Owns NECK_PAN (0) and NECK_TILT (1). One invocation equals one glance,
        using the same random pan/tilt bounds and dwell as the standalone scan;
        every commanded angle stays within pan in [40, 140], tilt in [85, 115]
        (Requirement 9.4). Contains no audio logic (Requirements 9.1, 9.2).
        """
        last_pan = getattr(self, "_look_scan_last_pan", constants.NECK_CENTER)
        self._look_scan_last_pan = await self._look_scan_glance(last_pan)

    async def look_scan_return(self):
        """Return phase: recenter the neck to the resting pose.

        Owns NECK_PAN (0) and NECK_TILT (1). Recenters via the shared primitive
        so the head ends at its neutral rest (Requirements 9.1, 9.4).
        """
        await self._look_scan_center()
        self._look_scan_last_pan = constants.NECK_CENTER

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

    async def scan(self, reps=2):
        """Pan the head side-to-side, return to center.

        Channels: NECK_PAN (0), NECK_TILT (1)

        Levels the tilt to 90 and centers pan first, sweeps left/right via
        move_to (smoothstep eased), then returns to center.

        Args:
            reps: Number of full side-to-side sweeps (default 2).
        """
        CENTER = constants.NECK_CENTER   # 90 pan + tilt neutral
        PAN_LEFT, PAN_RIGHT = 120, 60
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)
        for _ in range(reps):
            await self.trunkController.move_to(
                {constants.NECK_PAN: PAN_LEFT}, steps=22, delay=0.02)
            await self.trunkController.move_to(
                {constants.NECK_PAN: PAN_RIGHT}, steps=22, delay=0.02)
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)

    async def shake_head(self, reps=3):
        """Side-to-side head shake (full pan arc).

        Channels: NECK_PAN (0), NECK_TILT (1)

        Levels the tilt to 90 and centers pan first, shakes left/right via
        move_to (smoothstep eased), then returns to center.

        Args:
            reps: Number of pan sweeps (default 3).
        """
        CENTER = constants.NECK_CENTER   # 90
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)
        for _ in range(reps):
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MAX}, steps=20, delay=0.02)
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MIN}, steps=20, delay=0.02)
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)

    async def shake_no(self, reps=2):
        """Emphatic "no" shake: wide pan arc (30–150°).

        Channels: NECK_PAN (0)

        Args:
            reps: Number of pan sweeps (default 2).
        """
        CENTER = constants.NECK_CENTER   # 90
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)
        for _ in range(reps):
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MAX}, steps=18, delay=0.02)
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MIN}, steps=18, delay=0.02)
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=18, delay=0.02)

    async def small_shake_no(self, reps=2):
        """Subtle "no" shake: narrow pan arc (70–110°).

        Channels: NECK_PAN (0)

        Args:
            reps: Number of pan sweeps (default 2).
        """
        CENTER = constants.NECK_CENTER   # 90
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 110
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=15, delay=0.02)
        for _ in range(reps):
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MAX}, steps=15, delay=0.02)
            await self.trunkController.move_to(
                {constants.NECK_PAN: NECK_PAN_MIN}, steps=15, delay=0.02)
        await self.trunkController.move_to(
            {constants.NECK_PAN: CENTER, constants.NECK_TILT: CENTER},
            steps=15, delay=0.02)

    # ================================================================== #
    # COMPOSITE gestures — arm + head gathered simultaneously             #
    # Each method documents which arm and head gesture it combines.       #
    # ================================================================== #

    async def wave_and_swivel(self):
        """Wave the arm while doing a double neck-ellipse swivel.

        ARM: _wave_arm(no neck)  ·  HEAD: swivel_head()

        Uses the neck-free wave so swivel_head() owns the neck channels without
        the wave fighting it for the pan channel.
        """
        await asyncio.gather(
            asyncio.create_task(self._wave_arm(include_neck=False)),
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

    async def reach_and_look(self):
        """Reach toward audience while looking around.

        ARM: reach_out()  ·  HEAD: look_around()
        """
        await asyncio.gather(
            asyncio.create_task(self.reach_out()),
            asyncio.create_task(self.look_around()),
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
