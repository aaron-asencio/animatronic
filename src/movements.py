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
    # Reusable motion primitives                                          #
    # ================================================================== #

    async def randomized_centering_move(
        self, channel, center, half_range, jitter_pct, state_attr,
        *, steps_range=(18, 24), delay_base=0.02, delay_jitter=0.005,
        ease=True, companion=None,
    ):
        """Randomize a joint within a band, transitioning among LT/CENTER/RT.

        A single joint oscillates around ``center`` within the band
        ``[center - half_range, center + half_range]``. Three LOGICAL positions
        live in that band:

            LT     = center - half_range   (low)
            CENTER = center
            RT     = center + half_range   (high)

        Each call classifies the joint's CURRENT logical position (read from
        ``getattr(self, state_attr, center)``) by NEAREST of the three nominal
        angles -- ties resolve to CENTER -- then randomly picks a DESTINATION
        among the two OTHER positions, per the transition table:

            from RT     -> {LT, CENTER}
            from LT     -> {RT, CENTER}
            from CENTER -> {RT, LT}

        So a call never re-selects the logical position it is already at; the
        joint always transitions. The chosen destination's nominal angle then
        gets an ENDPOINT JITTER of ``+/- (jitter_pct * half_range)``::

            jitter = round(random.uniform(-1, 1) * jitter_pct * half_range)
            target = nominal + jitter

        ``target`` is CLAMPED back into the band ``[center - half_range,
        center + half_range]`` and rounded to an int. (``move_to`` additionally
        clamps to SAFE_LIMITS / any active verified-pose override as a final
        safeguard, so out-of-band physical damage cannot occur.) Example:
        ``center=100, half_range=50, jitter_pct=0.25`` gives a jitter magnitude
        of ``0.25 * 50 = 12.5``; an RT target of 150 lands in
        ``[137.5, 150]`` after band-clamping, and a CENTER target of 100 lands
        in ``[87.5, 112.5]``.

        ``center``, ``half_range`` and ``jitter_pct`` are ALL per-call
        parameters (not hardcoded), so each gesture tunes its own band and
        jitter. Per-gesture ``state_attr`` keeps two gestures from sharing
        transition state (e.g. ``"_mr_tilt_pos"`` vs ``"_pp_bob_pos"``); the
        ACTUAL commanded target is stored back into ``state_attr`` so the next
        call classifies relative to where the joint really ended.

        All randomness is drawn from the shared ``random`` module and there is
        no wall-clock or other nondeterminism, so ``random.seed(x)`` before a
        run makes the sequence DETERMINISTIC -- required for the phased-vs-
        standalone equivalence tests.

        Args:
            channel: The servo channel to drive (a ``constants.*`` channel).
            center: The band center angle in degrees.
            half_range: Half the band width; the band is
                ``[center - half_range, center + half_range]``.
            jitter_pct: Endpoint jitter as a fraction of ``half_range`` (0..1);
                jitter magnitude is ``jitter_pct * half_range`` degrees.
            state_attr: Name of the per-gesture attribute tracking this joint's
                last commanded logical position (read via ``getattr`` defaulting
                to ``center``, written back with the actual target).
            steps_range: ``(lo, hi)`` inclusive range for ``random.randint`` to
                pick the ``move_to`` step count.
            delay_base: Base per-step delay in seconds.
            delay_jitter: Symmetric random delay jitter; the delay is
                ``delay_base + random.uniform(-delay_jitter, delay_jitter)``.
            ease: Passed through to ``move_to`` (smoothstep ease when True).
            companion: Optional zero-arg callable returning a dict of
                ``{extra_channel: angle}`` merged into the SAME ``move_to`` so a
                companion joint eases smoothly alongside this one (used by
                menacing_reach's smooth rotator jitter).

        Returns:
            The commanded target angle (int).

        Channels: ``channel`` (plus any channels the ``companion`` supplies).
        """
        # Nominal logical angles for the three positions.
        lt = center - half_range
        rt = center + half_range
        nominals = {"LT": lt, "CENTER": center, "RT": rt}

        # Classify the current position by NEAREST nominal; ties -> CENTER by
        # ordering CENTER first in the min() key comparison.
        current = getattr(self, state_attr, center)
        order = ("CENTER", "LT", "RT")
        current_pos = min(order, key=lambda p: abs(current - nominals[p]))

        # Pick a destination among the two OTHER logical positions.
        transitions = {
            "RT": ("LT", "CENTER"),
            "LT": ("RT", "CENTER"),
            "CENTER": ("RT", "LT"),
        }
        dest_pos = random.choice(transitions[current_pos])
        nominal = nominals[dest_pos]

        # Endpoint jitter, then clamp back into the band and round to int.
        jitter = round(random.uniform(-1, 1) * jitter_pct * half_range)
        target = nominal + jitter
        target = int(round(max(lt, min(rt, target))))

        steps = random.randint(*steps_range)
        delay = delay_base + random.uniform(-delay_jitter, delay_jitter)

        targets = {channel: target}
        if companion is not None:
            targets.update(companion())

        print(
            f"[centering] ch={channel} {current_pos}->{dest_pos} "
            f"target={target} steps={steps}"
        )
        await self.trunkController.move_to(
            targets, steps=steps, delay=delay, ease=ease)

        # Store the ACTUAL commanded target so the next call classifies from
        # where the joint really ended.
        setattr(self, state_attr, target)
        return target

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
        ELBOW_REST, ELBOW_UP = 5, 0          # rest at 5 (REST_POSITIONS); 0 while arm extended
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

    # --- yawn_cover shared primitives + phase adapters -------------------- #
    #
    # The standalone ``yawn_cover`` above holds a FIXED 1.3s (tuned to the yawn
    # clip). The phased adapters below instead let the Performance_Framework own
    # the timing: the hand-to-mouth reach supplies the audio gate, the hold loops
    # for the audio duration, and the lower runs when playback ends. They reuse
    # yawn_cover's EXACT operator-verified poses and its verified_pose_override,
    # so no new pose is introduced. Used by the ``clearThroat`` routine to bring
    # the hand to the mouth, play ``clear_throat.wav`` once the hand arrives, hold
    # until it finishes, then lower. Contains no audio logic (the framework owns
    # audio); these only drive channels 0,1 (head) and 4-7 (arm).

    # Rest + hand-to-mouth pose values. The COVER pose matches yawn_cover's
    # operator-verified "hand in front of mouth" keyframes exactly; the REST
    # values match constants.REST_POSITIONS so the return lands each joint on
    # its documented rest -- notably RT_ELBOW_TILT rests at 5 (elbow straight),
    # NOT 0, which the standalone yawn_cover overshoots (the runner sweeps it
    # home there; here the adapter's own return lands it correctly).
    _YC_TILT_REST, _YC_TILT_COVER = 55, 35
    _YC_ROT_REST, _YC_ROT_COVER = 0, 200
    _YC_ELBOW_REST, _YC_ELBOW_COVER = 5, 165
    _YC_FOREARM_REST, _YC_FOREARM_COVER = 150, 185
    # Same two sub-limit channels yawn_cover widens, operator-verified safe in
    # THIS folded-to-the-mouth pose only. Held across lead-in -> loop -> return.
    _YAWN_COVER_OVERRIDE = {
        constants.RT_SHOULDER_TILT: (35, 270),
        constants.RT_ELBOW_TILT: (0, 170),
    }

    # Seconds to open the audio gate BEFORE the hand fully settles at the mouth,
    # so ``clear_throat.wav`` starts a touch early (the sound leads the final
    # settle rather than waiting for it). The fold's last ``_YC_GATE_LEAD``
    # seconds finish during the first hold. Must be < the fold duration
    # (45 * 0.02 = 0.9s) so the gate still opens partway INTO the fold.
    _YC_GATE_LEAD = 0.5

    async def _yawn_cover_center_head(self):
        """Center the head so the mouth faces forward for the throat-clear.

        Channels: NECK_PAN (0), NECK_TILT (1). Neck uses global SAFE_LIMITS;
        the arm override only covers the two arm channels.
        """
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: constants.NECK_CENTER,   # 90 = forward
                constants.NECK_TILT: 90,                     # 90 = level
            },
            steps=30, delay=0.02,
        )

    async def _yawn_cover_fold_up(self):
        """Fold the hand up in front of the mouth (yawn_cover's up-fold).

        Shoulder rotates first; elbow + forearm hold until 30% through, then
        bend the hand up to the mouth -- same staging/keyframes as yawn_cover.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).
        """
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_TILT: self._YC_TILT_COVER,
                constants.RT_SHOULDER_ROTATOR: self._YC_ROT_COVER,
                constants.RT_ELBOW_TILT: self._YC_ELBOW_COVER,
                constants.RT_ELBOW_ROTATOR: self._YC_FOREARM_COVER,
            },
            steps=45, delay=0.02,
            start_fractions={
                constants.RT_ELBOW_TILT: 0.30,
                constants.RT_ELBOW_ROTATOR: 0.30,
            },
        )

    async def _yawn_cover_raise(self):
        """RAISE: center the head, then fold the hand up in front of the mouth.

        Reproduces yawn_cover's centering + up-fold keyframes exactly (head to
        90/90 first so the mouth faces forward; shoulder rotates first, elbow +
        forearm fold in from 30% through). Used by the standalone-style full
        raise; the phased ``yawn_cover_lead_in`` instead splits this so it can
        open the audio gate ``_YC_GATE_LEAD`` seconds before the fold settles.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)
        """
        await self._yawn_cover_center_head()
        await self._yawn_cover_fold_up()

    async def _yawn_cover_lower(self):
        """LOWER: unfold the hand and settle the arm back to rest.

        Reverse of ``_yawn_cover_raise``'s up-fold: open the elbow/forearm first,
        then the shoulder lowers (same start_fractions/steps as yawn_cover's DOWN
        move), so the arm unfolds before it drops.

        Channels: RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4),
                  RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6).
        """
        await self.trunkController.move_to(
            {
                constants.RT_ELBOW_TILT: self._YC_ELBOW_REST,
                constants.RT_ELBOW_ROTATOR: self._YC_FOREARM_REST,
                constants.RT_SHOULDER_ROTATOR: self._YC_ROT_REST,
                constants.RT_SHOULDER_TILT: self._YC_TILT_REST,
            },
            steps=56, delay=0.02,
            start_fractions={
                constants.RT_SHOULDER_ROTATOR: 0.33,
                constants.RT_SHOULDER_TILT: 0.33,
            },
        )

    async def yawn_cover_lead_in(self):
        """Lead-in phase: raise the hand to the mouth; opens the audio gate.

        Owns head channels 0,1 and arm channels 4-7. Opens the
        verified_pose_override for the two sub-limit arm channels and holds it
        across the lead-in -> loop -> return lifetime via a per-adapter
        AsyncExitStack (``_yawn_cover_stack``); ``yawn_cover_return`` closes it.
        Contains no audio logic. To start ``clear_throat.wav`` ~0.5s SOONER, the
        gate is opened partway INTO the up-fold rather than after it: this
        coroutine centers the head, launches the fold as a background task, and
        returns once the fold is within ``_YC_GATE_LEAD`` (0.5s) of finishing.
        Because the framework opens the audio gate the instant this coroutine
        returns, the sound leads the hand's final settle by ~0.5s. The still-
        running fold task is stored on ``self._yawn_cover_fold_task`` and awaited
        at the top of the first hold (``yawn_cover_loop_body``) so the motion
        always completes fully before the hold begins.
        """
        # Open the override on a per-adapter AsyncExitStack so it stays active
        # across the hold loop and is released only in the return phase.
        self._yawn_cover_stack = contextlib.AsyncExitStack()
        self._yawn_cover_stack.enter_context(
            TrunkController.verified_pose_override(self._YAWN_COVER_OVERRIDE))

        # Center the head, then start the up-fold WITHOUT awaiting it so the gate
        # can open before the hand fully settles.
        await self._yawn_cover_center_head()
        self._yawn_cover_fold_task = asyncio.ensure_future(self._yawn_cover_fold_up())

        # Return ~0.5s before the fold finishes so audio starts that much sooner.
        # Fold duration is 45 * 0.02 = 0.9s; sleep the remainder (>= 0), letting
        # the fold's last _YC_GATE_LEAD seconds run during the first hold.
        fold_duration = 45 * 0.02
        await asyncio.sleep(max(0.0, fold_duration - self._YC_GATE_LEAD))

    async def yawn_cover_loop_body(self):
        """Loop-body phase: HOLD the hand at the mouth while audio plays.

        The framework repeats this while playback is active, checking
        ``is_active()`` only between whole iterations, so the hand simply stays
        folded at the mouth for the full duration of ``clear_throat.wav`` and no
        servo is re-commanded (the arm is already at the cover pose). A short
        sleep yields control so the runner can re-check playback between holds.
        Contains no audio logic.

        First, finish the up-fold started (un-awaited) by ``yawn_cover_lead_in``:
        the gate opened ~0.5s early, so the fold's final settle completes here,
        at the top of the hold, before the hand truly holds still.
        """
        await self._await_pending_fold()
        await asyncio.sleep(0.1)

    async def _await_pending_fold(self):
        """Await and clear the pending up-fold task, if one is outstanding.

        ``yawn_cover_lead_in`` launches the fold without awaiting it so the audio
        gate can open early; this completes that motion. Safe to call more than
        once and when no fold is pending (a no-op), so both the first hold and
        the return path can call it to guarantee the fold always finishes.
        """
        task = getattr(self, "_yawn_cover_fold_task", None)
        if task is not None:
            self._yawn_cover_fold_task = None
            await task

    async def yawn_cover_return(self):
        """Return phase: lower the hand to rest and release the pose override.

        Owns arm channels 4-7. Lowers via the shared ``_yawn_cover_lower``
        primitive, then closes the AsyncExitStack opened in
        ``yawn_cover_lead_in`` so the verified_pose_override is scoped to exactly
        the lead-in -> loop -> return span.

        First finish any still-pending up-fold (for a very short clip the loop
        may not have run), so the arm lowers from the fully-folded cover pose
        rather than mid-fold.
        """
        try:
            await self._await_pending_fold()
            await self._yawn_cover_lower()
        finally:
            stack = getattr(self, "_yawn_cover_stack", None)
            if stack is not None:
                await stack.aclose()
                self._yawn_cover_stack = None

    async def face_palm(self):
        """Face palm: head drops into the hand, shakes 3x in dismay, then recovers.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Hardware-measured hand-at-face pose:
            NECK_PAN=90, NECK_TILT=140 (head down), elbow_rot=200, elbow_tilt=145,
            shoulder_tilt=40, shoulder_rotator=200.

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
        extended arm out toward the audience), shoulder tilt=43, elbow rotator=0,
        elbow tilt=0 (arm straight). With the arm out, the shoulder tilt swings
        slowly within [26, 60] eight times (the menacing reach). Each swing is a
        centering transition: from the arm's current logical tilt position
        (LT=26 / CENTER=43 / RT=60) it moves to one of the two OTHER positions
        (jittered +/- ~4 deg, clamped to the band) with randomized timing and a
        small smooth rotator jitter layered into the same move_to (see
        ``randomized_centering_move`` and ``_menacing_reach_swing``), then the
        arm retracts (lowering ~40% faster than the reach-out).

        shoulder_tilt dips to 26, still above the (25, 270) override floor; this is
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
    _MR_TILT_REST, _MR_TILT_CENTER = 55, 43   # shoulder tilt (43 = reach + swing center)
    _MR_FOREARM_REST, _MR_FOREARM_OUT = 150, 0  # elbow rotator (arm extended)
    _MR_ELBOW_REST, _MR_ELBOW_OUT = 0, 0      # elbow tilt straight (arm extended)
    # Centering-transition swing band: center 43 +/- 17 = [26, 60], safely
    # inside the (25, 270) override floor. jitter_pct 0.25 => +/- ~4.25 deg.
    _MR_TILT_CENTER_ANGLE = 43
    _MR_TILT_HALF_RANGE = 17
    _MR_TILT_JITTER_PCT = 0.25
    # shoulder_tilt dips to 25, below the global floor; operator-verified safe
    # in the arm-out pose only, so widen just that channel.
    _MENACING_REACH_OVERRIDE = {constants.RT_SHOULDER_TILT: (25, 270)}

    async def _menacing_reach_reach(self):
        """REACH: rotate the extended arm out and forward together.

        Also initializes the swing's tilt-tracking state to the reach + swing
        center (``_MR_TILT_CENTER`` = 43) so both the standalone gesture and the
        phased composition start their menace swings from the same known tilt
        position.
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

    async def _menacing_reach_swing(self):
        """MENACE: perform ONE centering-transition swing of the shoulder tilt.

        Delegates to ``randomized_centering_move`` for RT_SHOULDER_TILT over the
        band ``center 43 +/- 17 = [26, 60]`` (``jitter_pct=0.25`` =>
        +/- ~4.25 deg endpoint jitter, clamped to the band). Each swing reads the
        arm's current logical tilt position from ``self._mr_tilt_pos``
        (initialized to the swing center 43 in ``_menacing_reach_reach``) and
        transitions to one of the two OTHER logical positions (LT / CENTER / RT)
        per the helper's transition table -- so the menace never looks
        metronomic and never re-picks the position it is already at. This
        REPLACES the previous three-pattern model with the centering-transition
        model (intended behavior change).

        A smooth shoulder-rotator jitter of ``_MR_ROT_OUT`` (209) +/- up to 4
        (in [205, 213]) is preserved via a ``companion`` callable, so the rotator
        eases to its new jittered target inside the SAME ``move_to`` as the tilt
        (no tiny separate snap writes). Timing matches the old feel:
        ``steps_range=(18, 30)``, ``delay_base=0.03``, ``delay_jitter=0.006``.

        Randomness is sourced from the shared ``random`` module (both the swing's
        transition/jitter and the rotator companion), and the tilt state is reset
        in the shared ``_menacing_reach_reach`` primitive, so seeding
        ``random.seed(x)`` before a run makes both the standalone gesture and the
        phased composition reproduce the identical command sequence
        (Requirement 9.3).

        SAFETY: elbow tilt stays at 0 (straight) throughout the swing, so with
        the rotator only in [205, 213] the hand-to-face FORBIDDEN_COMBINATIONS
        rule (elbow tilt 150-270 AND rotator 210-270 simultaneously) never
        triggers. All tilt targets are in-band [26, 60] by construction; move_to
        clamps too.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7).
        """
        def rotator_jitter():
            # Smooth rotator jitter eased in the SAME move_to as the tilt.
            return {
                constants.RT_SHOULDER_ROTATOR: self._MR_ROT_OUT
                + random.randint(-4, 4)  # [205, 213]
            }

        await self.randomized_centering_move(
            constants.RT_SHOULDER_TILT,
            center=self._MR_TILT_CENTER_ANGLE,
            half_range=self._MR_TILT_HALF_RANGE,
            jitter_pct=self._MR_TILT_JITTER_PCT,
            state_attr="_mr_tilt_pos",
            steps_range=(18, 30),
            delay_base=0.03,
            delay_jitter=0.006,
            companion=rotator_jitter,
        )

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

    async def hypnotic_arm(self):
        """Hypnotic arm: hold the arm in the operator-approved reach pose, then
        slowly sway the shoulder tilt, and finally retract to rest.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5)

        This is a PARAMETERIZED variant of ``menacing_reach``: it reuses the same
        reach + centering-swing + retract machinery but with hypnotic's own pose
        and band, and its OWN transition-state attribute (``_hyp_tilt_pos``) and
        pose override, so it shares NO state with ``menacing_reach`` and cannot
        alter its behavior.

        Operator-approved hypnotic hold pose: shoulder rotator=230 (arm rotated
        out toward the audience, a touch higher than menacing_reach's 209),
        shoulder tilt=0, elbow rotator=0, elbow tilt=0 (arm straight). With the
        arm out, the shoulder tilt sways slowly within the band ``center 15 +/-
        15 = [0, 30]`` (``jitter_pct=0.25`` => +/- ~3.75 deg endpoint jitter,
        clamped to the band). Each sway is a centering transition (LT=0 /
        CENTER=15 / RT=30) with a small smooth rotator jitter (230 +/- 4, in
        [226, 234]) layered into the SAME move_to. The arm then retracts to
        REST_POSITIONS, lowering ~40% faster (reusing ``_menacing_reach_retract``).

        shoulder_tilt drops to 0, below the global SAFE_LIMITS floor (45), so the
        hypnotic-specific verified-pose override ``_HYPNOTIC_ARM_OVERRIDE``
        ({RT_SHOULDER_TILT: (0, 270)}) is held across the whole reach/sway/retract
        span. This pose is USER-REQUESTED and OPERATOR bench-verified.

        SAFETY: elbow tilt stays at 0 (straight) throughout, and the rotator only
        ranges over [226, 234]. The hand-to-face FORBIDDEN_COMBINATION requires
        elbow tilt in [150, 270] AND rotator in [210, 270] SIMULTANEOUSLY. Here
        the rotator IS within [210, 270] (226-234), BUT the elbow tilt is 0 (far
        below the 150 floor), so the rule does NOT trigger. Every combined pose
        is validated collision-free by tests/test_hypnotic_collision.py.

        Like ``menacing_reach``, this standalone gesture delegates to the same
        phase primitives the Performance_Framework drives (reach lead-in, one
        sway loop body, retract return), so composing ``hypnotic_arm_lead_in`` +
        N x ``hypnotic_arm_loop_body`` + ``hypnotic_arm_return`` reproduces the
        exact same servo command sequence WHEN THE RNG IS SEEDED IDENTICALLY
        before each run (the sways draw from the shared ``random`` module).
        """
        with TrunkController.verified_pose_override(self._HYPNOTIC_ARM_OVERRIDE):
            await self._hypnotic_arm_reach()
            # SWAY: swing the shoulder tilt slowly within [0, 30], eight times.
            for _ in range(8):
                await self._hypnotic_arm_swing()
            await self._menacing_reach_retract()

    # --- hypnotic_arm shared primitives + phase adapters ------------------ #
    #
    # A PARAMETERIZED brains-style reach+swing+retract. The standalone gesture
    # above and the phase adapters below both call these primitives, so a phased
    # composition (lead_in + loop_body x8 + return) issues the identical move_to
    # command sequence as the standalone gesture (given the same RNG seed). No
    # audio logic lives in any of these methods. These reuse menacing_reach's
    # generic machinery (randomized_centering_move, _menacing_reach_retract) but
    # with hypnotic's own pose/band/state, so brains' behavior is UNCHANGED.

    # Rest + hypnotic arm-out pose values.
    _HYP_ROT_REST, _HYP_ROT_OUT = 0, 230       # shoulder rotator extends the arm out
    _HYP_TILT_REST = 55                         # shoulder tilt rest (from REST_POSITIONS)
    _HYP_FOREARM_REST, _HYP_FOREARM_OUT = 150, 0  # elbow rotator (arm extended)
    _HYP_ELBOW_REST, _HYP_ELBOW_OUT = 5, 0      # elbow tilt straight (arm extended)
    # Centering-transition sway band: center 15 +/- 15 = [0, 30]. jitter_pct 0.25
    # => +/- ~3.75 deg endpoint jitter, clamped to the band.
    _HYP_TILT_CENTER = 15
    _HYP_TILT_HALF_RANGE = 15
    _HYP_TILT_JITTER_PCT = 0.25
    # shoulder_tilt drops to 0, below the global floor (45); user-requested and
    # operator bench-verified safe in this arm-out pose only, so widen just that
    # channel. Held across the whole reach/sway/retract span.
    _HYPNOTIC_ARM_OVERRIDE = {constants.RT_SHOULDER_TILT: (0, 270)}

    async def _hypnotic_arm_reach(self):
        """REACH: rotate the extended arm out to the hypnotic hold pose.

        Also initializes the sway's tilt-tracking state to the sway center
        (``_HYP_TILT_CENTER`` = 15) so both the standalone gesture and the phased
        composition start their sways from the same known tilt position. Because
        both paths call this primitive first, seeding ``random`` before each run
        makes their subsequent stateful sways produce identical command sequences.

        Uses a DISTINCT state attribute (``_hyp_tilt_pos``) from
        ``menacing_reach``'s ``_mr_tilt_pos`` so the two gestures never collide.

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_ROTATOR (4), RT_ELBOW_TILT (5).
        """
        # Track where the shoulder tilt actually is between sways, reset here so
        # the standalone and phased command streams stay in lock-step under a
        # fixed seed. Distinct from menacing_reach's _mr_tilt_pos.
        self._hyp_tilt_pos = self._HYP_TILT_CENTER
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._HYP_ROT_OUT,
                constants.RT_SHOULDER_TILT: self._HYP_TILT_CENTER,
                constants.RT_ELBOW_ROTATOR: self._HYP_FOREARM_OUT,
                constants.RT_ELBOW_TILT: self._HYP_ELBOW_OUT,
            },
            steps=50, delay=0.025,
        )

    async def _hypnotic_arm_swing(self):
        """SWAY: perform ONE centering-transition sway of the shoulder tilt.

        Delegates to ``randomized_centering_move`` for RT_SHOULDER_TILT over the
        band ``center 15 +/- 15 = [0, 30]`` (``jitter_pct=0.25`` => +/- ~3.75 deg
        endpoint jitter, clamped to the band). Each sway reads the arm's current
        logical tilt position from ``self._hyp_tilt_pos`` (initialized to the
        sway center 15 in ``_hypnotic_arm_reach``) and transitions to one of the
        two OTHER logical positions (LT=0 / CENTER=15 / RT=30).

        A smooth shoulder-rotator jitter of ``_HYP_ROT_OUT`` (230) +/- up to 4
        (in [226, 234]) is layered via a ``companion`` callable so the rotator
        eases to its new jittered target inside the SAME ``move_to`` as the tilt.
        Timing matches brains' swing feel: ``steps_range=(18, 30)``,
        ``delay_base=0.03``, ``delay_jitter=0.006``.

        SAFETY: elbow tilt stays at 0 (straight) throughout, so with the rotator
        only in [226, 234] the hand-to-face FORBIDDEN_COMBINATION (elbow tilt
        150-270 AND rotator 210-270 simultaneously) never triggers -- the rotator
        is in-range but the elbow tilt is far below 150. All tilt targets are
        in-band [0, 30] by construction; move_to clamps too.

        Channels: RT_SHOULDER_TILT (6), RT_SHOULDER_ROTATOR (7).
        """
        def rotator_jitter():
            # Smooth rotator jitter eased in the SAME move_to as the tilt.
            return {
                constants.RT_SHOULDER_ROTATOR: self._HYP_ROT_OUT
                + random.randint(-4, 4)  # [226, 234]
            }

        await self.randomized_centering_move(
            constants.RT_SHOULDER_TILT,
            center=self._HYP_TILT_CENTER,
            half_range=self._HYP_TILT_HALF_RANGE,
            jitter_pct=self._HYP_TILT_JITTER_PCT,
            state_attr="_hyp_tilt_pos",
            steps_range=(18, 30),
            delay_base=0.03,
            delay_jitter=0.006,
            companion=rotator_jitter,
        )

    async def hypnotic_arm_lead_in(self):
        """Lead-in phase: reach the arm out to the hypnotic hold pose.

        Owns arm channels 4-7. Opens the hypnotic verified_pose_override for the
        sub-floor shoulder tilt (0) and holds it across the lead-in -> loop ->
        return lifetime via a per-adapter AsyncExitStack (``_hypnotic_arm_stack``,
        distinct from menacing_reach's ``_menacing_reach_stack``);
        ``hypnotic_arm_return`` closes it. Contains no audio logic -- reaching the
        arm out fully IS the (ungated) start condition, signalled by this
        coroutine simply completing.
        """
        # Open the override on a per-adapter AsyncExitStack so it stays active
        # across the loop-body sways and is released only in the return phase.
        self._hypnotic_arm_stack = contextlib.AsyncExitStack()
        self._hypnotic_arm_stack.enter_context(
            TrunkController.verified_pose_override(self._HYPNOTIC_ARM_OVERRIDE))
        await self._hypnotic_arm_reach()

    async def hypnotic_arm_loop_body(self):
        """Loop-body phase: one randomized hypnotic sway of the shoulder tilt.

        Owns RT_SHOULDER_TILT (6) and RT_SHOULDER_ROTATOR (7) for the smooth
        rotator jitter. One invocation equals one sway; eight invocations
        reproduce the standalone gesture's eight sways (given the same RNG seed).
        Contains no audio logic.
        """
        await self._hypnotic_arm_swing()

    async def hypnotic_arm_return(self):
        """Return phase: retract the arm to rest and release the pose override.

        Owns arm channels 4-7. Retracts via the shared ``_menacing_reach_retract``
        primitive (the ~40%-faster settle to REST_POSITIONS), then closes the
        AsyncExitStack opened in ``hypnotic_arm_lead_in`` so the
        verified_pose_override is scoped to exactly the lead-in -> loop -> return
        span.
        """
        try:
            await self._menacing_reach_retract()
        finally:
            stack = getattr(self, "_hypnotic_arm_stack", None)
            if stack is not None:
                await stack.aclose()
                self._hypnotic_arm_stack = None

    async def sleep_snore(self):
        """Sleep/snore: drop the head as if nodding off, then bob + rock while
        "asleep", then wake back up to rest.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_ELBOW_ROTATOR (4),
                  RT_ELBOW_TILT (5), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7).

        The gesture starts from the arm's REST pose (elbow rotator at its rest
        angle 150, shoulder tilt at rest 55, the rest of the arm at rest), then
        performs a JERKY, heavy-head drop -- the neck tilt sinks from level (90)
        down to fully dropped (180), descending in small eased chunks but pausing
        or jerking back UP a few degrees at random intervals to sell a tired,
        fighting-to-stay-awake nod. Once dropped, it repeats a gentle sleep
        cycle: the head BOBS within [170, 180] and the shoulder rotator ROCKS
        within [0, 10] (an occasional ~0.5s pause between rocks). Finally it
        wakes: the shoulder rotator eases to 0 and the neck returns to level with
        the arm settling back to REST_POSITIONS (the forearm stays at its rest
        angle 150 throughout).

        NECK_TILT reaches 180 (above the global SAFE_LIMITS ceiling of 160), so
        the sleep-specific verified-pose override ``_SLEEP_OVERRIDE`` widens JUST
        that one channel ({NECK_TILT: (30, 180)}) for the whole
        drop/bob/rock/return span. RT_SHOULDER_TILT holds 55 (within the global
        (45,270) limit) so it needs no override. The dropped-head pose is
        OPERATOR bench-verified safe. Every commanded angle stays inside the
        widened band and ``move_to`` clamps as a final safeguard.

        Like ``hypnotic_arm``, this standalone gesture delegates to the SAME
        phase primitives the Performance_Framework drives (drop lead-in, one
        bob+rock loop body, wake return), so composing ``sleep_snore_lead_in`` +
        N x ``sleep_snore_loop_body`` + ``sleep_snore_return`` reproduces the
        exact same servo command sequence WHEN THE RNG IS SEEDED IDENTICALLY
        before each run (the jerks, bobs, and rocks draw from the shared
        ``random`` module). No audio logic lives in any of these methods.
        """
        with TrunkController.verified_pose_override(self._SLEEP_OVERRIDE):
            await self._sleep_snore_drop()
            # SLEEP: gently bob the head + rock the arm a fixed number of cycles.
            for _ in range(6):
                await self._sleep_snore_bob_rock()
            await self._sleep_snore_wake()

    # --- sleep_snore shared primitives + phase adapters ------------------- #
    #
    # A heavy-head-drop + sleep-bob/arm-rock + wake gesture. The standalone
    # gesture above and the phase adapters below both call these primitives, so a
    # phased composition (lead_in + loop_body x N + return) issues the identical
    # move_to command sequence as the standalone gesture (given the same RNG
    # seed). All randomness is drawn from the shared ``random`` module; there is
    # no wall-clock nondeterminism, so ``random.seed(x)`` makes both paths
    # deterministic. No audio logic lives in any of these methods.

    # Neck tilt landmarks for the sleep drop/bob. Rest = level (90); down = fully
    # dropped chin-to-chest (180, above the global ceiling 160 -- override held).
    _SLEEP_TILT_REST = 90
    _SLEEP_TILT_DOWN = 180
    # Head-bob band while "asleep": dips up from 180 toward 170 and back.
    _SLEEP_BOB_MIN = 170
    _SLEEP_BOB_MAX = 180
    # Shoulder-rotator rock band while "asleep": gentle sway near the side.
    _SLEEP_ROCK_MIN = 0
    _SLEEP_ROCK_MAX = 10
    # Jerk-up magnitude during the tired head drop (degrees back toward level).
    _SLEEP_JERK_MIN = 5
    _SLEEP_JERK_MAX = 10
    # Elbow rotator: parks at its rest angle (150) during sleep and stays there
    # through wake -- the forearm never leaves rest in this routine.
    _SLEEP_ELBOW_ROT_PARK = 150
    # NECK_TILT hits 180 (above the global ceiling 160), operator bench-verified
    # safe in this sleep pose, so widen just that one channel. Held across the
    # whole span. RT_SHOULDER_TILT now holds 55 (within the global (45,270)
    # limit), so it no longer needs an override.
    _SLEEP_OVERRIDE = {
        constants.NECK_TILT: (30, 180),
    }

    async def _sleep_snore_drop(self):
        """DROP: settle into the start pose, then jerkily drop the head to sleep.

        Moves all six channels to the arm's REST start pose (NECK_PAN 90,
        NECK_TILT 90, RT_ELBOW_ROTATOR 150, RT_ELBOW_TILT 5, RT_SHOULDER_TILT 55,
        RT_SHOULDER_ROTATOR 0) with an eased ``move_to``, then lowers NECK_TILT
        from 90 to 180 in small eased chunks. To sell a heavy, tired head that
        keeps nodding off and catching itself, at random intervals it either
        PAUSES briefly (``asyncio.sleep`` ~0.2-0.5s) or JERKS the head back UP by
        ``random(5, 10)`` degrees before resuming the descent. The drop always
        ENDS at exactly 180.

        Also initializes the bob state (``_sleep_bob_center`` = 180) so both the
        standalone gesture and the phased composition start bobbing from the same
        known center, keeping their command streams in lock-step under a fixed
        seed. Every commanded neck-tilt angle stays within [90, 180]; the
        ``_SLEEP_OVERRIDE`` (and ``move_to``'s clamp) bound it.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_ELBOW_ROTATOR (4),
                  RT_ELBOW_TILT (5), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7).
        """
        # Settle into the start pose (all six channels, eased/simultaneous).
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: self._SLEEP_TILT_REST,          # 90 (centered)
                constants.NECK_TILT: self._SLEEP_TILT_REST,         # 90 (level)
                constants.RT_ELBOW_ROTATOR: self._SLEEP_ELBOW_ROT_PARK,  # 150 (rest)
                constants.RT_ELBOW_TILT: 5,
                constants.RT_SHOULDER_TILT: 55,
                constants.RT_SHOULDER_ROTATOR: 0,
            },
            steps=50, delay=0.025,
        )

        # JERKY HEAD DROP: descend NECK_TILT 90 -> 180 in small chunks, with
        # random pauses / jerk-ups mid-descent. Keep every angle in [90, 180].
        current = float(self._SLEEP_TILT_REST)
        step_deg = 6  # small eased chunk per advance
        while current < self._SLEEP_TILT_DOWN:
            # Occasionally fight the drop: either jerk the head back up or pause.
            if random.random() < 0.3:
                if random.random() < 0.5:
                    # Jerk UP a few degrees, then resume descending.
                    jerk = random.randint(self._SLEEP_JERK_MIN, self._SLEEP_JERK_MAX)
                    up = max(self._SLEEP_TILT_REST, current - jerk)
                    print(f"[sleep] head jerk up to {up:.0f}")
                    await self.trunkController.move_to(
                        {constants.NECK_TILT: int(up)}, steps=6, delay=0.02,
                    )
                    current = up
                else:
                    # Brief tired pause mid-nod.
                    await asyncio.sleep(random.uniform(0.2, 0.5))

            # Advance the descent by a small eased chunk (never past 180).
            current = min(self._SLEEP_TILT_DOWN, current + step_deg)
            await self.trunkController.move_to(
                {constants.NECK_TILT: int(current)}, steps=8, delay=0.02,
            )

        # Ensure we END exactly at the fully-dropped pose.
        await self.trunkController.move_to(
            {constants.NECK_TILT: self._SLEEP_TILT_DOWN}, steps=6, delay=0.02,
        )

        # Initialize loop state: the head bobs around the fully-dropped center.
        self._sleep_bob_center = self._SLEEP_TILT_DOWN

    async def _sleep_snore_bob_rock(self):
        """SLEEP CYCLE: one gentle head bob + one arm rock.

        HEAD BOB: NECK_TILT dips up from 180 to ``180 - random(3, 10)`` (staying
        within [170, 180]) and eases back toward 180 -- a slow, shallow nod. ARM
        ROCK: RT_SHOULDER_ROTATOR eases to a random target in [0, 10], with an
        occasional (~30%) ~0.5s pause first to vary the rhythm. Both moves are
        slow and eased. All targets are in-band by construction and ``move_to``
        clamps as a final safeguard. Randomness is from the shared ``random``.

        Channels: NECK_TILT (1), RT_SHOULDER_ROTATOR (7).
        """
        # HEAD BOB: dip up a few degrees within [170, 180], then ease back to 180.
        dip = random.randint(3, 10)
        bob_top = max(self._SLEEP_BOB_MIN, self._SLEEP_TILT_DOWN - dip)
        print(f"[sleep] head bob up to {bob_top:.0f} then back to {self._SLEEP_TILT_DOWN}")
        await self.trunkController.move_to(
            {constants.NECK_TILT: int(bob_top)}, steps=30, delay=0.03,
        )
        await self.trunkController.move_to(
            {constants.NECK_TILT: self._SLEEP_TILT_DOWN}, steps=30, delay=0.03,
        )

        # ARM ROCK: gentle shoulder-rotator sway within [0, 10], occasionally
        # preceded by a brief pause to vary the breathing rhythm.
        if random.random() < 0.3:
            await asyncio.sleep(0.5)
        rock = random.randint(self._SLEEP_ROCK_MIN, self._SLEEP_ROCK_MAX)
        print(f"[sleep] arm rock to {rock}")
        await self.trunkController.move_to(
            {constants.RT_SHOULDER_ROTATOR: rock}, steps=30, delay=0.03,
        )

    async def _sleep_snore_wake(self):
        """WAKE/RETURN: ease the arm and head back to a clean rest pose.

        Slowly eases RT_SHOULDER_ROTATOR (7) home to 0, then brings NECK_TILT
        back to level (90) with the remaining channels to their REST_POSITIONS
        so the figure ends clean and unloaded. The forearm already sits at its
        rest angle (RT_ELBOW_ROTATOR 150) throughout sleep, so there is no
        separate forearm-home move -- the final settle drives it to REST.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_ELBOW_ROTATOR (4),
                  RT_ELBOW_TILT (5), RT_SHOULDER_TILT (6),
                  RT_SHOULDER_ROTATOR (7).
        """
        # Ease the shoulder rotator home first.
        await self.trunkController.move_to(
            {constants.RT_SHOULDER_ROTATOR: 0}, steps=40, delay=0.03,
        )
        # Bring the head level and settle everything to REST_POSITIONS.
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                constants.NECK_TILT: self._SLEEP_TILT_REST,
                constants.RT_ELBOW_ROTATOR: constants.REST_POSITIONS[constants.RT_ELBOW_ROTATOR],
                constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
                constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
                constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
            },
            steps=40, delay=0.03,
        )

    async def sleep_snore_lead_in(self):
        """Lead-in phase: settle in and jerkily drop the head to sleep.

        Owns all six sleep channels. Opens the sleep verified_pose_override for
        the out-of-range neck tilt (180) and holds it across the lead-in -> loop
        -> return lifetime via a per-adapter
        AsyncExitStack (``_sleep_stack``, distinct from the other gestures'
        stacks); ``sleep_snore_return`` closes it. Contains no audio logic --
        completing the heavy head drop IS the (gating) start condition, signalled
        by this coroutine simply completing. This is the AUDIO GATE.
        """
        # Open the override on a per-adapter AsyncExitStack so it stays active
        # across the loop-body bobs/rocks and is released only in the return.
        self._sleep_stack = contextlib.AsyncExitStack()
        self._sleep_stack.enter_context(
            TrunkController.verified_pose_override(self._SLEEP_OVERRIDE))
        await self._sleep_snore_drop()

    async def sleep_snore_loop_body(self):
        """Loop-body phase: one sleep cycle (head bob + arm rock).

        Owns NECK_TILT (1) and RT_SHOULDER_ROTATOR (7). One invocation equals one
        bob+rock cycle; N invocations reproduce the standalone gesture's N cycles
        (given the same RNG seed). Contains no audio logic.
        """
        await self._sleep_snore_bob_rock()

    async def sleep_snore_return(self):
        """Return phase: wake to rest and release the pose override.

        Owns all six sleep channels. Wakes via the shared ``_sleep_snore_wake``
        primitive (forearm + shoulder home, head level, rest of arm to
        REST_POSITIONS), then closes the AsyncExitStack opened in
        ``sleep_snore_lead_in`` so the verified_pose_override is scoped to
        exactly the lead-in -> loop -> return span.
        """
        try:
            await self._sleep_snore_wake()
        finally:
            stack = getattr(self, "_sleep_stack", None)
            if stack is not None:
                await stack.aclose()
                self._sleep_stack = None

    async def present_palm(self):
        """Present palm: raise the forearm palm-up, gently bob it, then lower.

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)

        Operator-approved present pose: shoulder rotator=35 (arm up ~35deg),
        shoulder tilt=55 (rest), elbow tilt=120 (forearm raised ~120deg from
        straight), elbow rotator=235 (palm up, slightly inward -- short of the
        full 270). With the forearm up, it gently bobs a few times. Each bob is
        a centering transition of the elbow tilt within the reachable band
        ``center 120 +/- 35 = [85, 155]``: from its current logical position
        (LT=85 / CENTER=120 / RT=155) it moves to one of the two OTHER positions
        (jittered +/- ~10.5 deg, clamped to the band) with slight timing jitter
        so it isn't metronomic -- then the arm lowers back to rest.

        Every target is inside the global SAFE_LIMITS (elbow tilt [0,160],
        elbow rotator [0,270], shoulder rotator [0,270], shoulder tilt [45,270]),
        so no verified_pose_override is needed. The up-bob reaches up to ~155
        elbow tilt (within SAFE_LIMITS 0-160), which is above the 150 mark, but
        the hand-to-face FORBIDDEN_COMBINATION also requires shoulder rotator
        210-270 and the rotator here stays at 35, so it never triggers.

        This standalone gesture delegates to the same phase primitives the
        Performance_Framework drives (raise lead-in, one bob loop body, lower
        return), so composing ``present_palm_lead_in`` + N x
        ``present_palm_loop_body`` + ``present_palm_return`` reproduces the exact
        same servo command sequence WHEN THE RNG IS SEEDED IDENTICALLY before
        each run (the bob draws from the shared ``random`` module).
        """
        await self._present_palm_raise()
        # BOB: a fixed small number of gentle up/down bobs of the forearm.
        for _ in range(4):
            await self._present_palm_bob()
        await self._present_palm_lower()

    # --- present_palm shared primitives + phase adapters ------------------ #
    #
    # The standalone gesture above and the phase adapters below both call these
    # primitives, so a phased composition (lead_in + loop_body x N + return)
    # issues the identical move_to command sequence as the standalone gesture
    # (given the same RNG seed). No audio logic lives in any of these methods.

    # Present-pose values (shared by the standalone gesture and phases).
    _PP_ROT_REST, _PP_ROT_UP = 0, 35         # shoulder rotator (35 = arm up ~35deg)
    _PP_TILT_REST = 55                       # shoulder tilt stays at rest
    _PP_ELBOW_REST, _PP_ELBOW_UP = 5, 120    # elbow tilt (120 = forearm raised)
    _PP_FOREARM_REST, _PP_FOREARM_UP = 150, 235  # elbow rotator (235 = palm up, inward)
    # Centering-transition bob band: center 120 +/- 35 = [85, 155], the reachable
    # elbow-tilt span. jitter_pct 0.30 => +/- ~10.5 deg endpoint jitter.
    _PP_BOB_CENTER = 120
    _PP_BOB_HALF_RANGE = 35
    _PP_BOB_JITTER_PCT = 0.30
    _PP_BOB_LO, _PP_BOB_HI = 85, 155         # overall bob span around 120: [85, 155]

    async def _present_palm_raise(self):
        """RAISE: bring the arm to the palm-up present pose, all four channels
        together, eased.

        Also initializes the bob-tracking state (``_pp_bob_pos`` = the raised
        elbow-tilt center 120) so both the standalone gesture and the phased
        composition start bobbing from the same known position. Because both
        paths call this primitive first, seeding ``random`` before each run makes
        their subsequent stateful bobs produce identical command sequences.

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).
        """
        # Track where the elbow tilt actually is between bobs so each bob can
        # jitter relative to the raised center. Reset here (shared by both paths)
        # to keep the standalone and phased command streams in lock-step under a
        # fixed seed.
        self._pp_bob_pos = self._PP_ELBOW_UP
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._PP_ROT_UP,
                constants.RT_SHOULDER_TILT: self._PP_TILT_REST,
                constants.RT_ELBOW_TILT: self._PP_ELBOW_UP,
                constants.RT_ELBOW_ROTATOR: self._PP_FOREARM_UP,
            },
            steps=45, delay=0.02,
        )

    async def _present_palm_bob(self):
        """BOB: one centering-transition bob of the forearm (elbow tilt).

        Delegates to ``randomized_centering_move`` for RT_ELBOW_TILT over the
        reachable band ``center 120 +/- 35 = [85, 155]`` (``jitter_pct=0.30`` =>
        +/- ~10.5 deg endpoint jitter, clamped to the band, so RT lands ~[144,
        155] and LT ~[85, 96] -- a spread comparable to the old amplitude
        jitter). Each bob reads the forearm's current logical position from
        ``self._pp_bob_pos`` (initialized to the raised center 120 in
        ``_present_palm_raise``) and transitions to one of the two OTHER logical
        positions (LT=85 / CENTER=120 / RT=155) per the helper's transition
        table.

        This REPLACES the old target-then-settle-back-to-center bob (which was
        two sub-moves per call) with ONE move per call, and the motion character
        now includes center transitions rather than only up/down around center
        (intended behavior change). Over the loop of many bobs the forearm still
        rocks within [85, 155]. Timing preserves today's doubled bob speed:
        ``steps_range=(18, 24)``, ``delay_base=0.015``, ``delay_jitter=0.005``.

        Randomness is sourced from the shared ``random`` module and the bob state
        is reset in the shared ``_present_palm_raise`` primitive, so seeding
        ``random.seed(x)`` before a run makes both the standalone gesture and the
        phased composition reproduce the identical command sequence.

        Channels: RT_ELBOW_TILT (5).
        """
        await self.randomized_centering_move(
            constants.RT_ELBOW_TILT,
            center=self._PP_BOB_CENTER,
            half_range=self._PP_BOB_HALF_RANGE,
            jitter_pct=self._PP_BOB_JITTER_PCT,
            state_attr="_pp_bob_pos",
            steps_range=(18, 24),
            delay_base=0.015,
            delay_jitter=0.005,
        )

    async def _present_palm_lower(self):
        """LOWER: return the arm to its rest positions, eased.

        Channels: RT_SHOULDER_ROTATOR (7), RT_SHOULDER_TILT (6),
                  RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).
        """
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._PP_ROT_REST,
                constants.RT_SHOULDER_TILT: self._PP_TILT_REST,
                constants.RT_ELBOW_TILT: self._PP_ELBOW_REST,
                constants.RT_ELBOW_ROTATOR: self._PP_FOREARM_REST,
            },
            steps=45, delay=0.02,
        )

    async def present_palm_lead_in(self):
        """Lead-in phase: raise the arm to the palm-up present pose.

        Owns arm channels 4-7. Establishes the raised pose and initializes the
        bob-tracking state via the shared ``_present_palm_raise`` primitive.
        Contains no audio logic.
        """
        await self._present_palm_raise()

    async def present_palm_loop_body(self):
        """Loop-body phase: one gentle forearm bob.

        Owns RT_ELBOW_TILT (5). One invocation equals one bob; N invocations
        reproduce N standalone bobs (given the same RNG seed). Contains no audio
        logic.
        """
        await self._present_palm_bob()

    async def present_palm_return(self):
        """Return phase: lower the arm back to rest.

        Owns arm channels 4-7. Lowers via the shared primitive. Contains no
        audio logic.
        """
        await self._present_palm_lower()

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

    # --- hypnotic head sway: parameterized limited scan (+/-10 from center) - #
    #
    # A PARAMETERIZED variant of look_scan bounded to +/-10 from center on BOTH
    # axes for a gentle "hypnotic" sway. It uses its OWN glance primitive and
    # bounds constants (and its own last-pan state attr), leaving brains'
    # ``_LOOK_*`` constants and ``_look_scan_glance`` RNG draw order UNCHANGED.

    # Hypnotic scan bounds: +/-10 from center 90 on both axes (narrow sway).
    _HYP_PAN_MIN, _HYP_PAN_MAX = 80, 100         # 90 +/- 10
    _HYP_TILT_MIN, _HYP_TILT_MAX = 80, 100       # 90 +/- 10
    # Narrow range, so a small min-delta so glances still visibly move.
    _HYP_MIN_PAN_DELTA = 3
    # Gate delay: the hypnotic neck supplies the audio gate, so its lead-in
    # sleeps this long before completing, delaying audio start by 100ms.
    _HYP_GATE_DELAY = 0.1

    async def _hyp_scan_glance(self, last_pan):
        """Perform ONE gentle hypnotic glance + dwell, then report the chosen pan.

        Channels: NECK_PAN (0), NECK_TILT (1).

        A parameterized copy of ``_look_scan_glance`` bounded to the narrow
        hypnotic range: pan in [80, 100], tilt in [80, 100] (center 90 +/- 10). A
        target so close to the current pan that it wouldn't visibly move (within
        ``_HYP_MIN_PAN_DELTA`` = 3) is rejected. The existing look-scan dwell
        (``asyncio.sleep(random.uniform(1.2, 3.6))``) is kept -- a slow sway with
        a gaze pause suits the hypnotic feel. This uses its own bounds so brains'
        ``_look_scan_glance`` is untouched.

        Args:
            last_pan: The pan angle of the previous glance, used to reject a new
                target that is too close to move visibly.

        Returns:
            The pan angle chosen for this glance (the caller's next ``last_pan``).
        """
        pan = random.randint(self._HYP_PAN_MIN, self._HYP_PAN_MAX)
        while abs(pan - last_pan) < self._HYP_MIN_PAN_DELTA:
            pan = random.randint(self._HYP_PAN_MIN, self._HYP_PAN_MAX)
        tilt = random.randint(self._HYP_TILT_MIN, self._HYP_TILT_MAX)

        # Vary the travel time a little so the sway looks organic.
        steps = random.randint(22, 34)
        await self.trunkController.move_to(
            {constants.NECK_PAN: pan, constants.NECK_TILT: tilt},
            steps=steps, delay=0.04,
        )
        # Random settle/gaze pause before the next glance.
        await asyncio.sleep(random.uniform(1.2, 3.6))
        return pan

    async def hyp_scan_lead_in(self):
        """Lead-in phase: wait 100ms (audio gate), then center the head.

        Owns NECK_PAN (0) and NECK_TILT (1). This movement SUPPLIES the audio
        gate for the ``hypnotic`` performance: the framework starts audio the
        instant this coroutine completes, so the initial ``asyncio.sleep(0.1)``
        yields exactly a 100ms audio delay after the routine begins (mirrors
        blah's ``shake_no_lead_in`` 250ms gate). The standalone hypnotic scan
        never calls this adapter, so the 100ms gate lives ONLY here. Then centers
        the head and seeds the per-glance ``last_pan`` state. Contains no other
        audio logic.
        """
        # 100ms gate delay: audio starts when this lead-in completes.
        await asyncio.sleep(self._HYP_GATE_DELAY)
        await self._look_scan_center()
        self._hyp_scan_last_pan = constants.NECK_CENTER

    async def hyp_scan_loop_body(self):
        """Loop-body phase: ONE gentle hypnotic glance + dwell.

        Owns NECK_PAN (0) and NECK_TILT (1). One invocation equals one glance,
        using the narrow hypnotic bounds (pan in [80, 100], tilt in [80, 100]).
        Contains no audio logic.
        """
        last_pan = getattr(self, "_hyp_scan_last_pan", constants.NECK_CENTER)
        self._hyp_scan_last_pan = await self._hyp_scan_glance(last_pan)

    async def hyp_scan_return(self):
        """Return phase: recenter the neck to the resting pose.

        Owns NECK_PAN (0) and NECK_TILT (1). Recenters via the shared
        ``_look_scan_center`` primitive so the head ends at its neutral rest.
        Contains no audio logic.
        """
        await self._look_scan_center()
        self._hyp_scan_last_pan = constants.NECK_CENTER

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
        """Emphatic "no" shake: wide, smoothed, randomized pan arc (~25–155°).

        Channels: NECK_PAN (0), NECK_TILT (1)

        Centers the head (pan + tilt to 90), performs ``reps`` randomized pan
        sweeps, then recenters. Each sweep swings to a randomized "right" extreme
        (~[25, 35]) then a randomized "left" extreme (~[145, 155]) via eased
        move_to, with slight per-sweep timing jitter, so the shake keeps its wide
        emphatic feel without looking metronomic.

        This standalone gesture delegates to the same ``_shake_no_sweep``
        primitive the Performance_Framework loop body drives, so a phased
        composition reuses the identical randomized sweep behavior (given the
        same RNG seed). The standalone path does NOT include the 250ms audio-gate
        delay -- that lives only in ``shake_no_lead_in`` (used solely by the
        performance), which the standalone gesture never calls.

        Args:
            reps: Number of pan sweeps (default 2).
        """
        await self._shake_no_center()
        for _ in range(reps):
            await self._shake_no_sweep()
        await self._shake_no_center()

    # --- shake_no shared primitives + phase adapters ---------------------- #
    #
    # The standalone gesture above and the phase adapters below both call the
    # shared sweep/center primitives, so the loop-body phase reuses the identical
    # randomized pan sweep as the standalone shake (given the same RNG seed). No
    # audio logic lives in the shared primitives; the ONLY audio-coupled piece is
    # the 250ms gate delay at the top of ``shake_no_lead_in`` (performance-only).

    # Randomized sweep bounds (kept within NECK_PAN SAFE_LIMITS (5, 175)).
    _SN_RIGHT_LO, _SN_RIGHT_HI = 25, 35      # randomized "right" extreme
    _SN_LEFT_LO, _SN_LEFT_HI = 145, 155      # randomized "left" extreme
    # 250ms audio-gate delay: shake_no supplies the gate in the blah performance,
    # so its lead-in sleeps this long before completing, delaying audio start.
    _SN_GATE_DELAY = 0.25
    # NECK_TILT "randomize within range with centering" band for the blah
    # performance loop body: 82-98 (center 90, half_range 8). Used ONLY by the
    # phased blah head shake, NOT the standalone shake_no gesture, so the pan
    # sweep behavior of the standalone gesture is unchanged.
    _SN_TILT_CENTER = 90
    _SN_TILT_HALF = 8

    async def _shake_no_center(self):
        """Center the head at the neutral pan/tilt pose (pan=90, tilt=90).

        Channels: NECK_PAN (0), NECK_TILT (1).
        """
        await self.trunkController.move_to(
            {constants.NECK_PAN: constants.NECK_CENTER,
             constants.NECK_TILT: constants.NECK_CENTER},
            steps=18, delay=0.04)

    async def _shake_no_sweep(self):
        """Perform ONE randomized pan sweep: to a right extreme then a left one.

        Channels: NECK_PAN (0).

        Draws a randomized right extreme (~[25, 35]) and left extreme
        (~[145, 155]) plus slightly jittered timing (steps ~[16, 22], ~0.04 base
        delay jitter for half-speed motion) from the shared ``random`` module,
        and swings the pan there via eased move_to. Both extremes stay within
        the NECK_PAN SAFE_LIMITS (5, 175); move_to clamps as a final safeguard.
        """
        right = random.randint(self._SN_RIGHT_LO, self._SN_RIGHT_HI)
        left = random.randint(self._SN_LEFT_LO, self._SN_LEFT_HI)
        steps = random.randint(16, 22)
        delay = 0.04 + random.uniform(-0.004, 0.004)
        await self.trunkController.move_to(
            {constants.NECK_PAN: right}, steps=steps, delay=delay)
        await self.trunkController.move_to(
            {constants.NECK_PAN: left}, steps=steps, delay=delay)

    async def shake_no_lead_in(self):
        """Lead-in phase: wait 250ms (audio gate), then center the head.

        Owns NECK_PAN (0) and NECK_TILT (1). This movement supplies the audio
        gate for the ``blah`` performance: the framework starts audio the instant
        this coroutine completes, so the initial ``asyncio.sleep(0.25)`` yields
        exactly a 250ms audio delay after the routine begins. The standalone
        ``shake_no`` gesture does NOT call this adapter, so it never incurs the
        gate delay. Contains no other audio logic.
        """
        # 250ms gate delay: audio starts when this lead-in completes.
        await asyncio.sleep(self._SN_GATE_DELAY)
        await self._shake_no_center()

    async def shake_no_loop_body(self):
        """Loop-body phase: ONE randomized pan sweep + ONE tilt centering move.

        Owns NECK_PAN (0) and NECK_TILT (1). One invocation equals one pan
        sweep (reusing the same randomized right/left extremes and jittered
        timing as the standalone shake, given the same RNG seed) plus ONE
        "randomize within range with centering" transition on NECK_TILT within
        the 82-98 band (center 90, half_range 8). The tilt centering runs AFTER
        the pan sweep, so the head drifts up/down organically over the course of
        the blah performance instead of staying flat at 90. The two axes are
        disjoint channels both owned by this movement, so driving them here is
        safe. Contains no audio logic.

        This tilt motion lives ONLY in the phased blah loop body, so the
        standalone ``shake_no`` gesture (which calls ``_shake_no_sweep``
        directly) is unaffected.
        """
        await self._shake_no_sweep()
        await self.randomized_centering_move(
            constants.NECK_TILT,
            center=self._SN_TILT_CENTER,
            half_range=self._SN_TILT_HALF,
            jitter_pct=0.3,
            state_attr="_sn_tilt_pos",
        )

    async def shake_no_return(self):
        """Return phase: recenter the neck to the resting pose.

        Owns NECK_PAN (0) and NECK_TILT (1). Recenters via the shared primitive
        so the head ends at its neutral rest. Contains no audio logic.
        """
        await self._shake_no_center()

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
    # SNUCK UP — "you snuck up on me!" reaction (head jerk + arm recoil)   #
    # ================================================================== #

    # snuck_up pose landmarks (all within the global SAFE_LIMITS, so NO
    # verified_pose_override is needed):
    #   NECK_TILT 35  — chin JERKED UP (decrease raises the head; floor is 30)
    #   NECK_TILT 100 — small dip DOWN during the recover nod (increase lowers)
    #   NECK_TILT 90  — level rest
    #   RT_SHOULDER_ROTATOR 90 -> 20 — arm jolts UP from rest (0), settles to 20
    #   RT_SHOULDER_TILT 75    — arm pulls out from rest (55) and holds
    #   RT_ELBOW_TILT 75       — elbow flexes to a raised "on-guard" hold
    #   RT_ELBOW_ROTATOR 270   — forearm rotated palm-up, held
    #   NECK_PAN 85-95         — head does a small nervous swivel around center
    #                            (90) via the randomized-centering primitive
    # The final arm hold (rotator 20 / elbow 75) stays FAR below the hand-to-
    # face FORBIDDEN_COMBINATION (needs rotator>=210 AND elbow>=150), so the
    # flexed-and-raised pose is safe.
    _SU_TILT_UP = 35        # chin jerked up (fast)
    _SU_TILT_DOWN = 100     # small dip down on the recover nod
    _SU_ROT_JOLT = 90       # arm jolts up on the initial recoil
    _SU_ROT_FINAL = 20      # arm settles to this raised hold
    _SU_SHOULDER_TILT = 75  # arm pulled out, held
    _SU_ELBOW_TILT = 75     # elbow flexed to the on-guard hold
    _SU_ELBOW_ROT = 270     # forearm rotated palm-up, held
    # Neck-pan "randomize within range with centering" band: 85-95.
    _SU_NECK_PAN_CENTER = 90
    _SU_NECK_PAN_HALF = 5

    async def snuck_up(self):
        """Snuck up: a "you snuck up on me!" reaction — head jerk + arm recoil.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_ROTATOR (7),
                  RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).

        The reaction the napping Mode runs when the proximity sensor wakes it.
        It assumes the standard REST pose as its start (the nap wake leaves the
        head level at 90 and the arm at REST before this runs) and:

        1. JERK BACK (fast, concurrent): the head jerks chin-UP once
           (NECK_TILT 90 -> 35) while the arm recoils UP and out
           (RT_SHOULDER_ROTATOR 0 -> 90, RT_SHOULDER_TILT 55 -> 75,
           RT_ELBOW_TILT 5 -> 75, RT_ELBOW_ROTATOR 150 -> 270 palm-up). One
           sharp jolt (low steps + tiny delay), imitating the _sleep_snore_drop
           jerk idiom. The shoulder rotator then eases from the 90 jolt down to
           its 20 hold, and the arm STAYS in this raised on-guard pose.
        2. RECOVER NOD: the head lowers back at NORMAL speed to level, then does
           a single small nod DOWN to 100 and back UP to 90 (rest). No side-to-
           side shake.
        3. NERVOUS SWIVEL: while holding the on-guard arm, the head does a few
           "randomize within range with centering" pans in the 85-95 band
           (center 90) so it flicks side to side organically rather than
           freezing.
        4. LOWER TO REST: the on-guard arm relaxes back down and the head
           recenters, so the figure ends clean and unloaded at REST_POSITIONS.

        Every commanded angle is inside the global SAFE_LIMITS, so this gesture
        needs NO verified_pose_override. The transient on-guard hold (rotator 20,
        elbow 75) is far from the hand-to-face FORBIDDEN_COMBINATION
        (rotator>=210 AND elbow>=150), so the flexed/raised pose is safe.
        """
        # 1. JERK BACK: head chin-up + arm recoil together, fast (the jolt).
        print("[snuck_up] jerk back: head up + arm recoil")
        await self.trunkController.move_to(
            {
                constants.NECK_TILT: self._SU_TILT_UP,
                constants.RT_SHOULDER_ROTATOR: self._SU_ROT_JOLT,
                constants.RT_SHOULDER_TILT: self._SU_SHOULDER_TILT,
                constants.RT_ELBOW_TILT: self._SU_ELBOW_TILT,
                constants.RT_ELBOW_ROTATOR: self._SU_ELBOW_ROT,
            },
            steps=7, delay=0.012,
        )
        # Arm settles the shoulder rotator to its raised hold (90 -> 20).
        await self.trunkController.move_to(
            {constants.RT_SHOULDER_ROTATOR: self._SU_ROT_FINAL},
            steps=18, delay=0.02,
        )

        # 2. RECOVER NOD: head eases back to level at normal speed, then a single
        # small nod down to 100 and back up to 90 (rest). No side-to-side shake.
        print("[snuck_up] recover nod")
        await self.trunkController.move_to(
            {constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT]},
            steps=25, delay=0.02,
        )
        await self.trunkController.move_to(
            {constants.NECK_TILT: self._SU_TILT_DOWN}, steps=18, delay=0.02)
        await self.trunkController.move_to(
            {constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT]},
            steps=18, delay=0.02,
        )

        # 3. NERVOUS SWIVEL: hold the on-guard arm and flick the head pan within
        # the 85-95 band via the shared randomized-centering primitive.
        print("[snuck_up] nervous swivel (neck-pan centering 85-95)")
        for _ in range(3):
            await self.randomized_centering_move(
                constants.NECK_PAN,
                center=self._SU_NECK_PAN_CENTER,
                half_range=self._SU_NECK_PAN_HALF,
                jitter_pct=0.3,
                state_attr="_su_neck_pan_pos",
            )

        # 4. LOWER TO REST: the "on-guard" arm relaxes back down and the head
        # recenters, so the figure ends clean and unloaded at REST_POSITIONS.
        print("[snuck_up] lower arm to rest")
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
                constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
                constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
                constants.RT_ELBOW_ROTATOR: constants.REST_POSITIONS[constants.RT_ELBOW_ROTATOR],
            },
            steps=35, delay=0.03,
        )

    # ================================================================== #
    # AWAKEN — groggy "just woke up" reaction (gentle arm + lazy head bob) #
    # ================================================================== #

    # awaken reuses snuck_up's arm motion but with the shoulder motion HALVED
    # (a sleepy, half-hearted stir instead of a sharp startle). Halving is taken
    # as half the DISPLACEMENT FROM REST, so the values stay within SAFE_LIMITS:
    #   RT_SHOULDER_ROTATOR stir 90 -> 45 (rest is 0; halved)
    #   RT_SHOULDER_TILT 75 -> 65 (rest is 55; snuck_up's +20 becomes +10 — a
    #     literal 75/2=37.5 would fall below the SAFE_LIMITS floor of 45, so we
    #     halve the offset from rest instead).
    #   RT_ELBOW_TILT 75 and RT_ELBOW_ROTATOR 270 are unchanged (only the two
    #     SHOULDER channels were called out to halve).
    _AW_ROT_STIR = 45        # shoulder rotator stir (snuck_up 90, halved)
    _AW_SHOULDER_TILT = 65   # shoulder tilt (rest 55 + half of snuck_up's +20)
    _AW_ELBOW_TILT = 75      # elbow flex (same as snuck_up)
    _AW_ELBOW_ROT = 270      # forearm palm-up (same as snuck_up)
    # Lazy head-bob "randomize within range with centering" band: 75-105 on BOTH
    # neck axes (center 90, half_range 15).
    _AW_HEAD_CENTER = 90
    _AW_HEAD_HALF = 15

    # Arm move speed: snuck_up used delay=0.02 s/step. "1/3 faster" means the
    # same sweep in 3/4 of the time, i.e. delay = 0.02 / (4/3) = 0.015 s/step.
    _AW_ARM_DELAY = 0.015
    # How long the arm holds at its raised stir pose before lowering.
    _AW_ARM_HOLD_S = 0.5

    async def awaken(self, duration=7.0):
        """Awaken: a groggy "just woke up" reaction — quick stir + lazy head bob.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_ROTATOR (7),
                  RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).

        The reaction the napping Mode runs when it is interrupted. Reuses
        ``snuck_up``'s arm motion with the two SHOULDER channels halved (a sleepy
        stir, not a startle), and the head lazily bobs the whole time as if just
        waking up. The ARM and HEAD run CONCURRENTLY over disjoint channels
        (arm 4-7, head 0-1):

        - ARM: raises to the stir pose (RT_SHOULDER_ROTATOR 0 -> 45,
          RT_SHOULDER_TILT 55 -> 65, RT_ELBOW_TILT 5 -> 75, RT_ELBOW_ROTATOR
          150 -> 270), holds only ~0.5 s, then lowers back to REST. Arm moves
          run 1/3 faster than snuck_up (delay 0.02 -> 0.015 s/step). So the arm
          is up only briefly near the start, then rests for the remainder.
        - HEAD: for the full ``duration`` seconds, both NECK_PAN and NECK_TILT
          do "randomize within range with centering" moves in the 75-105 band
          (center 90) at a slow, sleepy pace, so the head lolls around until the
          awakened.wav audio finishes.

        Every commanded angle is inside the global SAFE_LIMITS, so this gesture
        needs NO verified_pose_override. The transient arm pose (rotator 45,
        elbow 75) is far from the hand-to-face FORBIDDEN_COMBINATION
        (rotator>=210 AND elbow>=150), so the flexed/raised pose is safe.

        Args:
            duration: Seconds to keep the lazy head bob going — set to the
                awakened.wav length so motion ends with the audio. Default 7.0.
        """
        loop = asyncio.get_event_loop()
        end = loop.time() + max(0.0, duration)

        async def arm_stir():
            """Raise the arm quickly, hold ~0.5 s, then lower back to REST."""
            print("[awaken] arm stir: raise (1/3 faster), hold 0.5s, lower")
            await self.trunkController.move_to(
                {
                    constants.RT_SHOULDER_ROTATOR: self._AW_ROT_STIR,
                    constants.RT_SHOULDER_TILT: self._AW_SHOULDER_TILT,
                    constants.RT_ELBOW_TILT: self._AW_ELBOW_TILT,
                    constants.RT_ELBOW_ROTATOR: self._AW_ELBOW_ROT,
                },
                steps=25, delay=self._AW_ARM_DELAY,
            )
            await asyncio.sleep(self._AW_ARM_HOLD_S)
            await self.trunkController.move_to(
                {
                    constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
                    constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
                    constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
                    constants.RT_ELBOW_ROTATOR: constants.REST_POSITIONS[constants.RT_ELBOW_ROTATOR],
                },
                steps=40, delay=self._AW_ARM_DELAY,
            )

        async def head_bob():
            """Loll the head (pan + tilt centering, 75-105) until the audio ends."""
            print(f"[awaken] lazy head bob for ~{duration:.1f}s")
            while loop.time() < end:
                await self.randomized_centering_move(
                    constants.NECK_PAN,
                    center=self._AW_HEAD_CENTER, half_range=self._AW_HEAD_HALF,
                    jitter_pct=0.3, state_attr="_aw_pan_pos",
                    steps_range=(28, 40), delay_base=0.03, delay_jitter=0.006,
                )
                if loop.time() >= end:
                    break
                await self.randomized_centering_move(
                    constants.NECK_TILT,
                    center=self._AW_HEAD_CENTER, half_range=self._AW_HEAD_HALF,
                    jitter_pct=0.3, state_attr="_aw_tilt_pos",
                    steps_range=(28, 40), delay_base=0.03, delay_jitter=0.006,
                )
            # Recenter the head to REST once the bob finishes.
            await self.trunkController.move_to(
                {
                    constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                    constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT],
                },
                steps=30, delay=0.03,
            )

        # Arm (4-7) and head (0-1) own disjoint channels, so run them together:
        # the arm does its brief raise/hold/lower near the start while the head
        # keeps bobbing for the whole duration.
        await asyncio.gather(
            asyncio.create_task(arm_stir()),
            asyncio.create_task(head_bob()),
        )

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

    # --- wave_and_swivel_smooth landmarks (used by startParty) ------------ #
    # Smooth head swivel band. NECK_PAN ("neck rotate") uses the current
    # neck_pan() range 30-150 => center 90, half_range 60, driven by
    # randomized_centering_move so the head rotate is "randomize within range
    # with centering" instead of a fixed metronomic sweep. NECK_TILT keeps the
    # neck_ellipse swing but HALVED: the ellipse tilted with amplitude 45, so
    # here it dips only ~22 deg below level (90 -> 112) at the ellipse low.
    _WSS_PAN_CENTER = constants.NECK_CENTER   # 90
    _WSS_PAN_HALF_RANGE = 60                  # band 30..150 (current neck_pan range)
    _WSS_PAN_JITTER_PCT = 0.20                # endpoint jitter += 0.20*60 = 12 deg
    _WSS_TILT_LEVEL = 90                      # head level (rest)
    _WSS_TILT_DIP = 112                       # ellipse low: 22 deg dip (half of 45)

    async def wave_and_swivel_smooth(self, arcs=2):
        """Smooth wave + swivel for startParty; arm and head return to rest.

        ARM: _wave_arm(no neck)  ·  HEAD: eased swivel over NECK_PAN + NECK_TILT.

        The arm and head run CONCURRENTLY over DISJOINT channels (arm 4-7, head
        0-1). Unlike ``wave_and_swivel`` (raw one-degree ``neck_ellipse``
        sweeps), the head here eases every move through ``move_to`` for smooth
        acceleration/settle:

        - NECK_PAN ("neck rotate"): each arc picks its pan target via
          ``randomized_centering_move`` over the CURRENT pan range
          (center 90, half_range 60 => band [30, 150]) -- "randomize within
          range with centering", so the head rotate transitions among LT/CENTER/
          RT with endpoint jitter instead of a fixed metronomic sweep.
        - NECK_TILT: dips toward ``_WSS_TILT_DIP`` (112) then back to level,
          HALVING the old ellipse tilt amplitude (45 -> ~22 deg below level).
          Eased alongside the pan in the SAME ``move_to`` (via the primitive's
          ``companion`` hook) so pan + tilt arrive together like an ellipse arc.

        Ends by easing the head back to level-center (pan 90, tilt 90); the wave
        arm returns itself to REST, so the figure finishes clean and unloaded.

        Channels: NECK_PAN (0), NECK_TILT (1) [head]; RT_SHOULDER_ROTATOR (7),
                  RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4)
                  [arm].

        Args:
            arcs: Number of eased swivel arcs the head traces (default 2, to
                match wave_and_swivel's double neck-ellipse).
        """
        async def head_swivel():
            # Start level and centered so the swivel begins from a known pose.
            await self.trunkController.move_to(
                {constants.NECK_PAN: self._WSS_PAN_CENTER,
                 constants.NECK_TILT: self._WSS_TILT_LEVEL},
                steps=20, delay=0.02)
            # Seed the centering primitive's pan state at center.
            self._wss_pan_pos = self._WSS_PAN_CENTER
            for i in range(arcs):
                # Ellipse LOW: pan transitions (randomize+center) while the tilt
                # dips ~22 deg (half the old amplitude), eased together.
                await self.randomized_centering_move(
                    constants.NECK_PAN,
                    center=self._WSS_PAN_CENTER,
                    half_range=self._WSS_PAN_HALF_RANGE,
                    jitter_pct=self._WSS_PAN_JITTER_PCT,
                    state_attr="_wss_pan_pos",
                    steps_range=(24, 32), delay_base=0.03,
                    companion=lambda: {constants.NECK_TILT: self._WSS_TILT_DIP},
                )
                # Ellipse HIGH: another pan transition while the tilt returns to
                # level, completing the eased oval arc.
                await self.randomized_centering_move(
                    constants.NECK_PAN,
                    center=self._WSS_PAN_CENTER,
                    half_range=self._WSS_PAN_HALF_RANGE,
                    jitter_pct=self._WSS_PAN_JITTER_PCT,
                    state_attr="_wss_pan_pos",
                    steps_range=(24, 32), delay_base=0.03,
                    companion=lambda: {constants.NECK_TILT: self._WSS_TILT_LEVEL},
                )
            # RETURN TO REST: ease the head back to level-center.
            await self.trunkController.move_to(
                {constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                 constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT]},
                steps=24, delay=0.02)

        await asyncio.gather(
            asyncio.create_task(self._wave_arm(include_neck=False)),
            asyncio.create_task(head_swivel()),
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

    # --- reach_and_look_smooth landmarks (used by vincentPrice) ----------- #
    # Reach pose (eased, arm arrives together). Same landmarks as reach_out's
    # extended pose, driven through move_to for smoothstep easing instead of the
    # older linear move_by_direction 1-deg sweeps.
    _RL_REACH = {
        constants.RT_SHOULDER_ROTATOR: 60,
        constants.RT_SHOULDER_TILT:    80,
        constants.RT_ELBOW_TILT:       40,
    }
    # Smooth head look-around waypoints (pan, tilt) — a slow, flowing sweep that
    # eases between points. Kept within a comfortable range around center.
    _RL_LOOK_POINTS = (
        (110, 80),   # look up-left
        (70, 100),   # look down-right
        (105, 100),  # look down-left
        (75, 80),    # look up-right
        (90, 90),    # back to center-ish
    )

    async def reach_and_look_smooth(self, duration=14.0):
        """Smooth reach + flowing head look-around, then return to rest.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_ROTATOR (7),
                  RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5).

        The vincentPrice routine's motion. Unlike ``reach_and_look`` (which uses
        the older linear ``move_by_direction`` sweeps), every move here goes
        through ``move_to`` with smoothstep easing so the arm and head
        accelerate and settle smoothly. The ARM and HEAD run CONCURRENTLY over
        disjoint channels:

        - ARM: REPEATS an eased reach — out to the reach pose (shoulder rotator
          60, shoulder tilt 80, elbow tilt 40 — arriving together), brief hold,
          eased back to REST, then a 1 s pause — looping for ``duration`` so the
          arm keeps reaching throughout the laugh.
        - HEAD: slowly sweeps between look-around waypoints (eased ``move_to``
          on pan+tilt together) for ``duration`` seconds, then eases back to
          REST (center 90/90). Sized to flow for the whole audio clip.

        Ends with every channel at REST_POSITIONS.

        Args:
            duration: Seconds to keep the head sweeping — set to the
                vincent-price-laugh.wav length so motion flows for the whole
                clip. Default 14.0.
        """
        loop = asyncio.get_event_loop()
        end = loop.time() + max(0.0, duration)

        async def arm_reach():
            """Repeat eased reach/hold/retract with a 1 s pause, until duration."""
            while loop.time() < end:
                # Ease out to the reach pose.
                await self.trunkController.move_to(
                    dict(self._RL_REACH), steps=45, delay=0.02)
                # Hold the reach a moment, then retract smoothly to rest.
                await asyncio.sleep(1.0)
                await self.trunkController.move_to(
                    {
                        constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
                        constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
                        constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
                    },
                    steps=50, delay=0.02,
                )
                # 1 s pause at rest before the next reach (skip if we're done).
                if loop.time() >= end:
                    break
                await asyncio.sleep(1.0)

        async def head_look():
            """Slowly sweep the head between waypoints until duration elapses."""
            i = 0
            while loop.time() < end:
                pan, tilt = self._RL_LOOK_POINTS[i % len(self._RL_LOOK_POINTS)]
                i += 1
                # Slow, eased sweep so the head flows rather than snapping.
                await self.trunkController.move_to(
                    {constants.NECK_PAN: pan, constants.NECK_TILT: tilt},
                    steps=45, delay=0.03,
                )
            # Ease back to a level, centered rest.
            await self.trunkController.move_to(
                {
                    constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                    constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT],
                },
                steps=40, delay=0.03,
            )

        # Arm (5,6,7) and head (0,1) own disjoint channels — run them together.
        await asyncio.gather(
            asyncio.create_task(arm_reach()),
            asyncio.create_task(head_look()),
        )

    async def patrol(self):
        """Idle patrol: neck ellipse followed by a small look-around.

        HEAD only — no arm movement.  Used for low-key ambient animation.
        """
        await self.neck_ellipse()
        await asyncio.sleep(1)
        await self.look_around_small()

    # ================================================================== #
    # more_candy — "too much candy" sugar-rush shakes                     #
    #                                                                     #
    # Four INDEPENDENT single-joint randomized-centering shakes run       #
    # concurrently at a QUICK tempo so the character looks jittery /      #
    # over-sugared: elbow tilt (5), shoulder rotator (7), neck tilt (1),  #
    # neck pan (0). Each owns disjoint channels and delegates to the      #
    # shared randomized_centering_move helper with a fast steps/delay.    #
    #                                                                     #
    # The elbow-tilt shake centers on 175 within [160, 190], ABOVE the    #
    # global RT_ELBOW_TILT SAFE_LIMITS ceiling (160). This band is        #
    # operator bench-verified safe in this upright-forearm pose only, so  #
    # the elbow movement widens JUST that channel via verified_pose_      #
    # override, held across its lead-in -> loop -> return span (mirroring #
    # menacing_reach). All other channels use the normal global limits.   #
    # These are HEAD (0,1) + ARM (5,7) shakes; move_to clamps every       #
    # target to the (possibly overridden) safe range as a final guard.    #
    # ================================================================== #

    # Start-pose values (the destination the routine holds/oscillates around).
    _MC_ELBOW_ROT_START = 270    # ch4 elbow rotator — static during the shake
    _MC_ELBOW_TILT_START = 190   # ch5 elbow tilt — start; oscillates in [160,190]
    _MC_SHOULDER_TILT_START = 55  # ch6 shoulder tilt — static during the shake
    _MC_SHOULDER_ROT_START = 45  # ch7 shoulder rotator — start; osc in [35,45]
    _MC_NECK_PAN_START = 90      # ch0 — start; oscillates in [85,95]
    _MC_NECK_TILT_START = 90     # ch1 — start; oscillates in [80,100]

    # Rest values the return phases drive back to (from constants.REST_POSITIONS).
    _MC_ELBOW_ROT_REST = 150
    _MC_ELBOW_TILT_REST = 5
    _MC_SHOULDER_TILT_REST = 55
    _MC_SHOULDER_ROT_REST = 0

    # Centering bands: center = peak, half_range spans the requested window.
    _MC_ELBOW_TILT_CENTER, _MC_ELBOW_TILT_HALF = 175, 15   # [160, 190]
    _MC_SHOULDER_ROT_CENTER, _MC_SHOULDER_ROT_HALF = 40, 5  # [35, 45]
    _MC_NECK_TILT_CENTER, _MC_NECK_TILT_HALF = 90, 10       # [80, 100]
    _MC_NECK_PAN_CENTER, _MC_NECK_PAN_HALF = 90, 5          # [85, 95]
    _MC_JITTER_PCT = 0.30    # endpoint jitter as a fraction of half_range

    # Quick, jittery tempo shared by every sugar-rush shake.
    _MC_STEPS_RANGE = (8, 14)
    _MC_DELAY_BASE = 0.012
    _MC_DELAY_JITTER = 0.004

    # Elbow tilt (160-190) exceeds the global ceiling (160); operator-verified
    # safe in this pose only, so widen just that channel for the routine.
    _MORE_CANDY_OVERRIDE = {constants.RT_ELBOW_TILT: (0, 190)}

    # 250ms audio-gate delay: the elbow movement supplies the gate, so its
    # lead-in sleeps this long before completing, delaying audio start.
    _MC_GATE_DELAY = 0.25

    # --- Elbow-tilt shake (ch4 + ch5); supplies the gate + owns the override -- #

    async def more_candy_elbow_lead_in(self):
        """Lead-in: open the elbow override, wait 250ms (audio gate), set start.

        Owns RT_ELBOW_ROTATOR (4) and RT_ELBOW_TILT (5). Opens the
        verified_pose_override for the sub-ceiling elbow-tilt band on a
        per-adapter AsyncExitStack held across the lead-in -> loop -> return
        span (``more_candy_elbow_return`` closes it). This movement supplies the
        audio gate: the framework starts audio the instant this coroutine
        completes, so the ``asyncio.sleep(0.25)`` yields a 250ms audio delay
        after the routine begins. Drives the forearm to its upright start pose
        (elbow rotator 270, elbow tilt at the shake center 175) and seeds the
        centering state so the first swing transitions from a known position.
        """
        self._more_candy_elbow_stack = contextlib.AsyncExitStack()
        self._more_candy_elbow_stack.enter_context(
            TrunkController.verified_pose_override(self._MORE_CANDY_OVERRIDE))
        # 250ms gate delay: audio starts when this lead-in completes.
        await asyncio.sleep(self._MC_GATE_DELAY)
        # Seed the centering tracker to the shake center so the first transition
        # is well-defined, then move to the upright start pose.
        self._mc_elbow_pos = self._MC_ELBOW_TILT_CENTER
        await self.trunkController.move_to(
            {
                constants.RT_ELBOW_ROTATOR: self._MC_ELBOW_ROT_START,
                constants.RT_ELBOW_TILT: self._MC_ELBOW_TILT_START,
            },
            steps=22, delay=0.02,
        )

    async def more_candy_elbow_loop_body(self):
        """Loop body: one quick centering shake of the elbow tilt in [160, 190].

        Owns RT_ELBOW_TILT (5). Delegates to ``randomized_centering_move`` at the
        quick sugar-rush tempo; move_to clamps to the active elbow override.
        """
        await self.randomized_centering_move(
            constants.RT_ELBOW_TILT,
            center=self._MC_ELBOW_TILT_CENTER,
            half_range=self._MC_ELBOW_TILT_HALF,
            jitter_pct=self._MC_JITTER_PCT,
            state_attr="_mc_elbow_pos",
            steps_range=self._MC_STEPS_RANGE,
            delay_base=self._MC_DELAY_BASE,
            delay_jitter=self._MC_DELAY_JITTER,
        )

    async def more_candy_elbow_return(self):
        """Return: lower the forearm to rest, then release the elbow override.

        Owns RT_ELBOW_ROTATOR (4) and RT_ELBOW_TILT (5). Drives both back to
        their rest positions, then closes the AsyncExitStack opened in
        ``more_candy_elbow_lead_in`` so the verified_pose_override is scoped to
        exactly the lead-in -> loop -> return span.
        """
        try:
            await self.trunkController.move_to(
                {
                    constants.RT_ELBOW_TILT: self._MC_ELBOW_TILT_REST,
                    constants.RT_ELBOW_ROTATOR: self._MC_ELBOW_ROT_REST,
                },
                steps=30, delay=0.02,
            )
        finally:
            stack = getattr(self, "_more_candy_elbow_stack", None)
            if stack is not None:
                await stack.aclose()
                self._more_candy_elbow_stack = None

    # --- Shoulder-rotator shake (ch6 + ch7) -------------------------------- #

    async def more_candy_shoulder_lead_in(self):
        """Lead-in (t=0): move the shoulder to its start pose.

        Owns RT_SHOULDER_TILT (6) and RT_SHOULDER_ROTATOR (7). No gate: begins at
        time zero. Seeds the rotator centering state and sets the static shoulder
        tilt (55) plus the rotator shake center (40).
        """
        self._mc_shoulder_pos = self._MC_SHOULDER_ROT_CENTER
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_TILT: self._MC_SHOULDER_TILT_START,
                constants.RT_SHOULDER_ROTATOR: self._MC_SHOULDER_ROT_START,
            },
            steps=22, delay=0.02,
        )

    async def more_candy_shoulder_loop_body(self):
        """Loop body: one quick centering shake of the shoulder rotator [35, 45].

        Owns RT_SHOULDER_ROTATOR (7).
        """
        await self.randomized_centering_move(
            constants.RT_SHOULDER_ROTATOR,
            center=self._MC_SHOULDER_ROT_CENTER,
            half_range=self._MC_SHOULDER_ROT_HALF,
            jitter_pct=self._MC_JITTER_PCT,
            state_attr="_mc_shoulder_pos",
            steps_range=self._MC_STEPS_RANGE,
            delay_base=self._MC_DELAY_BASE,
            delay_jitter=self._MC_DELAY_JITTER,
        )

    async def more_candy_shoulder_return(self):
        """Return: lower the shoulder to rest.

        Owns RT_SHOULDER_TILT (6) and RT_SHOULDER_ROTATOR (7).
        """
        await self.trunkController.move_to(
            {
                constants.RT_SHOULDER_ROTATOR: self._MC_SHOULDER_ROT_REST,
                constants.RT_SHOULDER_TILT: self._MC_SHOULDER_TILT_REST,
            },
            steps=30, delay=0.02,
        )

    # --- Neck-tilt shake (ch1) --------------------------------------------- #

    async def more_candy_neck_tilt_lead_in(self):
        """Lead-in (t=0): center the neck tilt at its start (90).

        Owns NECK_TILT (1). No gate.
        """
        self._mc_neck_tilt_pos = self._MC_NECK_TILT_CENTER
        await self.trunkController.move_to(
            {constants.NECK_TILT: self._MC_NECK_TILT_START}, steps=18, delay=0.02)

    async def more_candy_neck_tilt_loop_body(self):
        """Loop body: one quick centering shake of the neck tilt in [80, 100].

        Owns NECK_TILT (1).
        """
        await self.randomized_centering_move(
            constants.NECK_TILT,
            center=self._MC_NECK_TILT_CENTER,
            half_range=self._MC_NECK_TILT_HALF,
            jitter_pct=self._MC_JITTER_PCT,
            state_attr="_mc_neck_tilt_pos",
            steps_range=self._MC_STEPS_RANGE,
            delay_base=self._MC_DELAY_BASE,
            delay_jitter=self._MC_DELAY_JITTER,
        )

    async def more_candy_neck_tilt_return(self):
        """Return: neck tilt back to level rest (90).

        Owns NECK_TILT (1).
        """
        await self.trunkController.move_to(
            {constants.NECK_TILT: constants.NECK_CENTER}, steps=20, delay=0.02)

    # --- Neck-pan shake (ch0) ---------------------------------------------- #

    async def more_candy_neck_pan_lead_in(self):
        """Lead-in (t=0): center the neck pan at its start (90).

        Owns NECK_PAN (0). No gate.
        """
        self._mc_neck_pan_pos = self._MC_NECK_PAN_CENTER
        await self.trunkController.move_to(
            {constants.NECK_PAN: self._MC_NECK_PAN_START}, steps=18, delay=0.02)

    async def more_candy_neck_pan_loop_body(self):
        """Loop body: one quick centering shake of the neck pan in [85, 95].

        Owns NECK_PAN (0).
        """
        await self.randomized_centering_move(
            constants.NECK_PAN,
            center=self._MC_NECK_PAN_CENTER,
            half_range=self._MC_NECK_PAN_HALF,
            jitter_pct=self._MC_JITTER_PCT,
            state_attr="_mc_neck_pan_pos",
            steps_range=self._MC_STEPS_RANGE,
            delay_base=self._MC_DELAY_BASE,
            delay_jitter=self._MC_DELAY_JITTER,
        )

    async def more_candy_neck_pan_return(self):
        """Return: neck pan back to center rest (90).

        Owns NECK_PAN (0).
        """
        await self.trunkController.move_to(
            {constants.NECK_PAN: constants.NECK_CENTER}, steps=20, delay=0.02)

    # ================================================================== #
    # hand_visor — "block the sun and look around"                        #
    #                                                                     #
    # The arm raises to a fixed "hand-as-a-visor" pose, then the head     #
    # looks around for ~5s while the ARM FOLLOWS the neck pan (the visor  #
    # tracks with the head). Arm (4-7) and head (0-1) own disjoint         #
    # channels; each glance eases the neck pan/tilt, then eases the arm    #
    # (blended for the chosen pan) so the visor tracks the head.           #
    #                                                                     #
    # Every commanded angle stays inside the global SAFE_LIMITS           #
    # (ch4 [0,270], ch5 [0,160], ch6 [45,270] — 45 is exactly the floor,  #
    # ch7 [0,270]), so this gesture needs NO verified_pose_override.      #
    # ================================================================== #

    # Visor CENTER arm pose (reached first, before the head starts looking).
    #   ch4 RT_ELBOW_ROTATOR 190, ch5 RT_ELBOW_TILT 130,
    #   ch6 RT_SHOULDER_TILT 45,  ch7 RT_SHOULDER_ROTATOR 270.
    _HV_ARM_CENTER = {
        constants.RT_ELBOW_ROTATOR: 190,   # ch4
        constants.RT_ELBOW_TILT:    130,   # ch5
        constants.RT_SHOULDER_TILT:  45,   # ch6
        constants.RT_SHOULDER_ROTATOR: 270,  # ch7
    }
    # Arm pose when the neck pan is at its HIGH extreme (NECK_PAN 110).
    _HV_ARM_PAN_HIGH = {
        constants.RT_ELBOW_ROTATOR: 190,   # ch4
        constants.RT_ELBOW_TILT:    125,   # ch5
        constants.RT_SHOULDER_TILT:  45,   # ch6
        constants.RT_SHOULDER_ROTATOR: 270,  # ch7
    }
    # Arm pose when the neck pan is at its LOW extreme (NECK_PAN 70).
    _HV_ARM_PAN_LOW = {
        constants.RT_ELBOW_ROTATOR: 190,   # ch4
        constants.RT_ELBOW_TILT:    130,   # ch5
        constants.RT_SHOULDER_TILT:  60,   # ch6
        constants.RT_SHOULDER_ROTATOR: 270,  # ch7
    }
    # Neck "randomize within range with centering" bands for the look-around.
    #   NECK_PAN  ~70-110 => center 90, half_range 20.
    #   NECK_TILT ~85-95  => center 90, half_range 5 (too small to affect the arm).
    _HV_PAN_CENTER, _HV_PAN_HALF = 90, 20    # [70, 110]
    _HV_TILT_CENTER, _HV_TILT_HALF = 90, 5   # [85, 95]
    _HV_PAN_LOW, _HV_PAN_HIGH = 70, 110      # pan extremes the arm interpolates over

    def _hand_visor_arm_for_pan(self, pan):
        """Arm targets for a given neck-pan angle (linear blend of the extremes).

        The visor arm FOLLOWS the neck pan: at the LOW pan extreme (70) the arm
        holds ``_HV_ARM_PAN_LOW``, at the HIGH extreme (110) it holds
        ``_HV_ARM_PAN_HIGH``, and in between each channel is linearly
        interpolated by where ``pan`` sits in ``[70, 110]`` (clamped). Only
        RT_SHOULDER_TILT (60<->45) and RT_ELBOW_TILT (130<->125) actually change
        between the two poses; RT_ELBOW_ROTATOR (190) and RT_SHOULDER_ROTATOR
        (270) are constant. move_to clamps every result to SAFE_LIMITS.

        Args:
            pan: The neck-pan angle chosen for this glance.

        Returns:
            Dict of arm channel -> target angle for this pan.
        """
        span = self._HV_PAN_HIGH - self._HV_PAN_LOW
        frac = (pan - self._HV_PAN_LOW) / span if span else 0.0
        frac = min(1.0, max(0.0, frac))  # clamp: pan may sit outside [70,110]
        arm = {}
        for channel, low in self._HV_ARM_PAN_LOW.items():
            high = self._HV_ARM_PAN_HIGH[channel]
            arm[channel] = int(round(low + (high - low) * frac))
        return arm

    async def hand_visor(self, duration=10.0):
        """Hand visor: raise a hand to block the sun, then look around ~5s.

        Channels: NECK_PAN (0), NECK_TILT (1), RT_SHOULDER_ROTATOR (7),
                  RT_SHOULDER_TILT (6), RT_ELBOW_TILT (5), RT_ELBOW_ROTATOR (4).

        Starts from REST. Sequence:

        1. VISOR UP: the four arm channels move CONCURRENTLY (one eased
           ``move_to``) to the visor center pose — RT_ELBOW_ROTATOR 190,
           RT_ELBOW_TILT 130, RT_SHOULDER_TILT 45, RT_SHOULDER_ROTATOR 270 — so
           the hand comes up to shade the eyes.
        2. LOOK AROUND (~``duration`` s): the head does "randomize within range
           with centering" glances — NECK_PAN in [70, 110] (center 90) and
           NECK_TILT in [85, 95] (center 90). Because the arm and head own
           DISJOINT channels, each glance drives BOTH in the SAME ``move_to``:
           the neck pan/tilt AND the arm blended for that pan (see
           ``_hand_visor_arm_for_pan``), so the visor arm tracks the head as it
           turns. As the pan rises toward 110 the shoulder tilt eases to 45 and
           the elbow tilt to 125; as it falls toward 70 they ease to 60 and 130.
           NECK_TILT's band is intentionally tiny (±5), so it does not perturb
           the arm/hand position.
        3. RETURN TO REST: the head recenters (90/90) and the arm eases back to
           REST_POSITIONS, so the figure ends clean and unloaded.

        Every commanded angle is within the global SAFE_LIMITS (RT_SHOULDER_TILT
        bottoms at exactly its 45 floor), so this gesture needs NO
        verified_pose_override. The visor pose keeps RT_SHOULDER_ROTATOR at 270
        but RT_ELBOW_TILT at 125-130 (below the 150 hand-to-face floor), so the
        FORBIDDEN_COMBINATION (elbow tilt 150-270 AND rotator 210-270) never
        triggers.

        All randomness is drawn from the shared ``random`` module, so
        ``random.seed(x)`` before a run makes the glance sequence deterministic.

        Args:
            duration: How long to keep looking around, in seconds (default 10.0).
        """
        # 1. VISOR UP: raise the arm to the visor center pose, all four channels
        # together (eased). The hand comes up to shade the eyes.
        print("[hand_visor] visor up: raise hand to block the sun")
        await self.trunkController.move_to(
            dict(self._HV_ARM_CENTER), steps=45, delay=0.02)

        # Seed the neck centering trackers at center so the first glance
        # transitions from a known position.
        self._hv_pan_pos = self._HV_PAN_CENTER
        self._hv_tilt_pos = self._HV_TILT_CENTER

        # 2. LOOK AROUND: for ~duration seconds, each glance picks the next
        # NECK_PAN via "randomize within range with centering" [70,110] and
        # eases the neck AND the pan-tracked arm there TOGETHER in one move_to.
        # The arm blend is supplied through the primitive's ``companion`` hook,
        # so pan + arm arrive together (disjoint channels). NECK_TILT is a
        # separate small centering move ([85,95], too small to affect the arm).
        print(f"[hand_visor] look around for ~{duration:.1f}s (arm follows pan)")
        loop = asyncio.get_event_loop()
        end = loop.time() + max(0.0, duration)
        while loop.time() < end:
            # Pick + ease the next NECK_PAN via the shared centering primitive;
            # it returns the committed pan target. Run a small NECK_TILT
            # centering drift ([85,95]) CONCURRENTLY — both neck channels are
            # owned only here and are disjoint from each other and the arm.
            pan_task = asyncio.create_task(self.randomized_centering_move(
                constants.NECK_PAN,
                center=self._HV_PAN_CENTER, half_range=self._HV_PAN_HALF,
                jitter_pct=0.3, state_attr="_hv_pan_pos",
                steps_range=(22, 34), delay_base=0.04, delay_jitter=0.0,
            ))
            tilt_task = asyncio.create_task(self.randomized_centering_move(
                constants.NECK_TILT,
                center=self._HV_TILT_CENTER, half_range=self._HV_TILT_HALF,
                jitter_pct=0.3, state_attr="_hv_tilt_pos",
                steps_range=(22, 34), delay_base=0.04, delay_jitter=0.0,
            ))
            # The arm FOLLOWS the pan: blend the visor pose for the pan target
            # and ease it there CONCURRENTLY with the neck (disjoint arm
            # channels 4-7), so the visor tracks the head as it turns.
            pan = await pan_task
            arm_task = asyncio.create_task(self.trunkController.move_to(
                self._hand_visor_arm_for_pan(pan), steps=28, delay=0.04))
            await asyncio.gather(tilt_task, arm_task)

            # Random settle/gaze pause before the next glance.
            await asyncio.sleep(random.uniform(0.6, 1.4))

        # 3. RETURN TO REST: recenter the head and ease the arm back to REST.
        print("[hand_visor] return to rest")
        await self.trunkController.move_to(
            {
                constants.NECK_PAN: constants.REST_POSITIONS[constants.NECK_PAN],
                constants.NECK_TILT: constants.REST_POSITIONS[constants.NECK_TILT],
                constants.RT_SHOULDER_ROTATOR: constants.REST_POSITIONS[constants.RT_SHOULDER_ROTATOR],
                constants.RT_SHOULDER_TILT: constants.REST_POSITIONS[constants.RT_SHOULDER_TILT],
                constants.RT_ELBOW_TILT: constants.REST_POSITIONS[constants.RT_ELBOW_TILT],
                constants.RT_ELBOW_ROTATOR: constants.REST_POSITIONS[constants.RT_ELBOW_ROTATOR],
            },
            steps=45, delay=0.03,
        )


if __name__ == '__main__':
    # Quick interactive testing — uncomment the gesture you want to run.
    mv = Movements("Servo Movements")
    # asyncio.run(mv.wave())
    # asyncio.run(mv.come())
