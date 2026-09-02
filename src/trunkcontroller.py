"""
trunkcontroller.py

Low-level servo primitives for the animatronic.

TrunkController wraps the Adafruit ServoKit and exposes individual joint
movements (neck pan, neck tilt, shoulder, elbow) as async coroutines.
Higher-level gesture choreography lives in movements.py.

Hardware assumptions
--------------------
- 16-channel PCA9685 PWM board connected via I2C.
- All servo channels configured for 270-degree actuation range.
- Channel assignments defined in constants.py.
"""

import asyncio
import os
import constants

# Maximum safe angle for all servos on this hardware.
SERVO_MAX_ANGLE = 270

# Expected PWM frequency for hobby servos (Hz). ServoKit sets ~50 Hz on
# construction. If the PCA9685 browns out (e.g. a loose VCC/logic wire) it can
# reset and lose this setting, which stops clean PWM output — servos then
# ignore commands or move erratically. The startup health check reads this back
# and warns / restores it. Tolerance accounts for the chip's prescaler rounding.
SERVO_PWM_FREQ_HZ = 50
SERVO_PWM_FREQ_TOLERANCE = 5  # Hz

# Simulation mode: set SERVO_SIM=1 to run WITHOUT touching real hardware. Every
# servo write is logged instead of sent over I2C. Lets us verify program flow
# and exactly what angles WOULD be commanded, with zero risk of a servo moving.
SERVO_SIM = os.environ.get('SERVO_SIM') == '1'


class _FakeServo:
    """Stand-in for a single servo channel in simulation mode."""
    def __init__(self, channel):
        self._channel = channel
        self._angle = None  # unknown until commanded, mirrors real hardware

    @property
    def angle(self):
        return self._angle

    @angle.setter
    def angle(self, value):
        self._angle = value
        print(f"[SIM] servo[{self._channel}].angle = {value}")

    @property
    def actuation_range(self):
        return SERVO_MAX_ANGLE

    @actuation_range.setter
    def actuation_range(self, value):
        # Deliberately does nothing but log — proves configuration never
        # commands motion in sim.
        print(f"[SIM] servo[{self._channel}].actuation_range = {value} (no motion)")


class _FakePCA:
    """Stand-in for the underlying PCA9685 in simulation mode."""
    def __init__(self):
        self.frequency = SERVO_PWM_FREQ_HZ


class _FakeKit:
    """Stand-in for ServoKit in simulation mode."""
    def __init__(self, channels=16):
        self._channels = channels
        self.servo = {i: _FakeServo(i) for i in range(channels)}
        self._pca = _FakePCA()


def _make_kit():
    """Construct the real ServoKit, or a simulated one if SERVO_SIM=1."""
    if SERVO_SIM:
        print("SERVO_SIM=1 -> using simulated servo kit (NO hardware writes)")
        return _FakeKit(channels=16)
    # Imported lazily so sim mode doesn't require the hardware libs/board.
    from adafruit_servokit import ServoKit
    return ServoKit(channels=16)


class TrunkController:
    """Controls individual servo joints on the animatronic body."""

    # Shared kit instance (class-level so all instances share one board).
    # NOTE: constructing the kit does NOT command any servo to move.
    kit = _make_kit()

    # Track which channels we've configured so we only do it once.
    _configured = False

    def __init__(self, name):
        self.name = name
        # Configure actuation range lazily on first construction, and ONLY for
        # the channels we actually drive. Doing this at import time / for all
        # 16 channels was making unused and attached servos twitch on startup
        # (constructing a Servo sets its pulse-width range, which can emit a
        # pulse and jerk the horn — this is what slammed the neck down).
        if not TrunkController._configured:
            self._configure_channels()
            self.health_check()
            TrunkController._configured = True

    @classmethod
    def health_check(cls, fix=True):
        """Verify the PCA9685 is initialised and warn on a brownout signature.

        Reads back the PWM frequency. If it's missing/zero or far from the
        expected servo frequency, the chip likely reset (commonly a loose VCC /
        logic-power wire) — a state where servos accept commands over I2C but
        never actually move, or move erratically. When fix=True we re-set the
        frequency to recover without a manual power-cycle.

        Returns:
            True if the frequency looks healthy (after any fix), False otherwise.
        """
        try:
            pca = cls.kit._pca
            freq = pca.frequency
        except Exception as e:
            print(f"HEALTH CHECK: could not read PWM frequency from PCA9685: {e}. "
                  f"Check the board's VCC/logic power and I2C wiring.")
            return False

        low = SERVO_PWM_FREQ_HZ - SERVO_PWM_FREQ_TOLERANCE
        high = SERVO_PWM_FREQ_HZ + SERVO_PWM_FREQ_TOLERANCE
        if freq is None or not (low <= freq <= high):
            print(f"HEALTH CHECK WARNING: PWM frequency is {freq} Hz, expected "
                  f"~{SERVO_PWM_FREQ_HZ} Hz. The PCA9685 likely browned out / "
                  f"reset (check the VCC/logic-power wire — a loose VCC causes "
                  f"servos to ignore commands or move erratically).")
            if fix:
                try:
                    pca.frequency = SERVO_PWM_FREQ_HZ
                    print(f"HEALTH CHECK: reset PWM frequency to "
                          f"{SERVO_PWM_FREQ_HZ} Hz. If servos still don't move, "
                          f"the VCC connection is likely still intermittent.")
                    return low <= pca.frequency <= high
                except Exception as e:
                    print(f"HEALTH CHECK: failed to reset frequency: {e}")
                    return False
            return False

        print(f"HEALTH CHECK OK: PWM frequency {freq} Hz.")
        return True

    @classmethod
    def _configure_channels(cls):
        """Set actuation_range on the channels we use, without commanding motion.

        We do NOT write .angle here — only .actuation_range — so no servo is
        told to move. Any per-channel error is caught so one bad channel can't
        crash startup.
        """
        for channel in constants.servos:  # only our real joints, not all 16
            try:
                cls.kit.servo[channel].actuation_range = SERVO_MAX_ANGLE
            except Exception as e:
                name = constants.servos.get(channel, f"ch{channel}")
                print(f"WARN: could not configure {name} (ch{channel}): {e}")
        print("servo setup (configured channels: "
              f"{sorted(constants.servos)})")

    # Human-readable servo name map (mirrors constants.servos).
    servos = {}
    servos[constants.NECK_TILT]           = "NECK_TILT"
    servos[constants.NECK_PAN]            = "NECK_PAN"
    servos[constants.RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
    servos[constants.RT_SHOULDER_TILT]    = "RT_SHOULDER_TILT"
    servos[constants.RT_ELBOW_TILT]       = "RT_ELBOW_TILT"
    servos[constants.RT_ELBOW_ROTATOR]    = "RT_ELBOW_ROTATOR"

    # Neutral/center angle for the neck pan servo (degrees).
    NECK_CENTER = 90

    # ------------------------------------------------------------------ #
    # Safety: clamp every write to the mechanism's SAFE_LIMITS             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def clamp_angle(servo_num, angle):
        """Clamp a commanded angle to the safe range for this channel.

        Falls back to the servo electrical range [0, SERVO_MAX_ANGLE] for any
        channel without an explicit SAFE_LIMITS entry. This is the single point
        that guarantees no gesture can drive a servo into a mechanical jam.

        Args:
            servo_num: Channel index.
            angle:     Requested angle in degrees.

        Returns:
            The clamped angle (float/int) guaranteed within the safe range.
        """
        lo, hi = constants.SAFE_LIMITS.get(servo_num, (0, SERVO_MAX_ANGLE))
        if angle < lo:
            return lo
        if angle > hi:
            return hi
        return angle

    def set_angle(self, servo_num, angle):
        """Write an angle to a servo AFTER clamping to its safe range.

        Every servo write in this class MUST go through here. Logs when a
        requested angle is clamped so out-of-range gestures are visible.

        Args:
            servo_num: Channel index.
            angle:     Requested angle in degrees.

        Returns:
            The angle actually written (post-clamp).
        """
        safe = self.clamp_angle(servo_num, angle)
        if safe != angle:
            name = constants.servos.get(servo_num, f"ch{servo_num}")
            print(f"CLAMPED {name}: requested {angle} -> {safe} "
                  f"(safe range {constants.SAFE_LIMITS.get(servo_num)})")
        self.kit.servo[servo_num].angle = safe
        return safe

    # ------------------------------------------------------------------ #
    # Neck movements                                                       #
    # ------------------------------------------------------------------ #

    async def neck_pan(self, revert=True):
        """Pan the neck left then right across a moderate arc.

        Args:
            revert: If True, sweep back to the start position after reaching
                    the far end.
        """
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)

    async def neck_full_pan(self, revert=True):
        """Pan the neck through its full range (0–180 degrees).

        Args:
            revert: If True, sweep back to the start position after reaching
                    the far end.
        """
        NECK_PAN_MIN = 0
        NECK_PAN_MAX = 180
        await self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)

    async def neck_tilt(self, min=30, max=95, revert=True):
        """Tilt the neck up/down between min and max angles.

        Args:
            min:    Starting angle in degrees.
            max:    Ending angle in degrees.
            revert: If True, return to min after reaching max.
        """
        await self.move(constants.NECK_TILT, min, max, 0.025, revert, .1)

    async def neck_center(self):
        """Return the neck pan servo to the neutral center position (90°)."""
        await self.return_to_start(constants.NECK_PAN, self.NECK_CENTER, delay=0.04)

    async def neck_tilt_center(self):
        """Nudge the neck tilt servo to its mechanical center (~20°)."""
        NECK_TILT_MIN = 19
        NECK_TILT_MAX = 21
        await self.move(constants.NECK_TILT, NECK_TILT_MIN, NECK_TILT_MAX, 0.1, False, 1)

    # ------------------------------------------------------------------ #
    # Arm movements                                                        #
    # ------------------------------------------------------------------ #

    async def shoulder_tilt(self, revert=True):
        """Tilt the right shoulder forward/backward.

        Args:
            revert: If True, return to start after reaching max.
        """
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 230
        await self.move(constants.RT_SHOULDER_TILT, RT_SHOULDER_TILT_MIN,
                        RT_SHOULDER_TILT_MAX, 0.01, revert, 1)

    async def elbow_tilt(self, revert=True):
        """Bend the right elbow up/down.

        Args:
            revert: If True, return to start after reaching max.
        """
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 120
        await self.move(constants.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN,
                        RT_ELBOW_TILT_MAX, 0.01, revert, 1)

    async def elbow_rotate(self, revert=True):
        """Rotate the right forearm (wrist/palm orientation).

        Args:
            revert: If True, return to start after reaching max.
        """
        RT_ELBOW_ROTATE_MIN = 30
        RT_ELBOW_ROTATE_MAX = 150
        await self.move(constants.RT_ELBOW_ROTATOR, RT_ELBOW_ROTATE_MIN,
                        RT_ELBOW_ROTATE_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)

    async def shoulder_rotate(self, revert=True):
        """Rotate (raise/lower) the right shoulder.

        Args:
            revert: If True, return to start after reaching max.
        """
        RT_SHOULDER_ROTATOR_MIN = 60
        RT_SHOULDER_ROTATOR_MAX = 270
        await self.move(constants.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN,
                        RT_SHOULDER_ROTATOR_MAX, 0.01, revert, 1)

    # ------------------------------------------------------------------ #
    # Core movement primitives                                             #
    # ------------------------------------------------------------------ #

    async def move(self, servo_num=0, start=0, stop=180, delay=0.1, revert=True, revert_delay=0.5):
        """Sweep a single servo from start to stop, then optionally back.

        Args:
            servo_num:   Channel index of the target servo.
            start:       Starting angle in degrees.
            stop:        Target angle in degrees (clamped to SERVO_MAX_ANGLE).
            delay:       Seconds to wait between each 1-degree step.
            revert:      If True, sweep back from stop to start after pausing.
            revert_delay: Seconds to pause at the stop position before reverting.
        """
        # Clamp the sweep endpoints to the mechanism's safe range so the loop
        # never even iterates into a jam. set_angle re-clamps each write too.
        start = self.clamp_angle(servo_num, max(start, 0))
        stop = self.clamp_angle(servo_num, min(stop, SERVO_MAX_ANGLE))
        print(f"moving {constants.servos[servo_num]}")
        step = 1 if stop >= start else -1
        for i in range(start, stop, step):
            self.set_angle(servo_num, i)
            await asyncio.sleep(delay)

        if revert:
            await asyncio.sleep(revert_delay)
            for i in range(stop, start, -step):
                self.set_angle(servo_num, i)
                await asyncio.sleep(delay)

    async def slow_scan(self, revert=True):
        """Slowly pan the neck left then right from center.

        Moves to NECK_LEFT (110°) then to NECK_RIGHT (70°) relative to
        NECK_CENTER, giving a deliberate surveillance-style head sweep.
        """
        NECK_LEFT  = 110
        NECK_RIGHT = 70
        print("slow scan")
        increase = True
        await self.move_by_dir(constants.NECK_PAN, self.NECK_CENTER, NECK_LEFT, 0.05, increase)
        increase = False
        await self.move_by_dir(constants.NECK_PAN, self.NECK_CENTER, NECK_RIGHT, 0.05, increase)

    async def return_to_start(self, servo_num, start, delay=0.1):
        """Gently move a servo back to its start/neutral position.

        Reads the current angle and increments/decrements step-by-step to avoid
        sudden snapping. Handles the case where the angle is None (servo not yet
        positioned) by snapping directly to start.

        Args:
            servo_num: Channel index of the target servo.
            start:     Target resting angle in degrees.
            delay:     Seconds between each 1-degree step.
        """
        print(f"testing servo num: {servo_num}")
        print(f"number of servo channels: {self.kit._channels}")
        print(self.kit.servo[servo_num])
        print(f"angle: {self.kit.servo[servo_num].angle}")

        # Clamp the target so a caller can't ask us to rest into a jam.
        start = self.clamp_angle(servo_num, start)

        # If the servo has never been commanded, snap it to start (clamped).
        if self.kit.servo[servo_num].angle is None:
            self.set_angle(servo_num, start)

        current_position = round(self.kit.servo[servo_num].angle)

        print(f"return to start {constants.servos[servo_num]} which is at {current_position}")
        if current_position != start and current_position <= SERVO_MAX_ANGLE:
            if current_position > start:
                for i in range(current_position, start, -1):
                    self.set_angle(servo_num, i)
                    await asyncio.sleep(delay)
            else:
                for i in range(current_position, start, 1):
                    self.set_angle(servo_num, i)
                    await asyncio.sleep(delay)

    async def move_by_dir(self, servo_num, start, stop, delay=0.1, increasing=True):
        """Move a servo in one direction, returning to start afterward.

        Unlike move_by_direction, this method calls return_to_start both before
        moving (to ensure a known starting position) and after (to reset).

        Args:
            servo_num:  Channel index of the target servo.
            start:      Origin angle in degrees.
            stop:       Destination angle in degrees (clamped to SERVO_MAX_ANGLE).
            delay:      Seconds between each 1-degree step.
            increasing: True to sweep from start→stop; False for stop→start.
        """
        start = self.clamp_angle(servo_num, max(start, 0))
        stop = self.clamp_angle(servo_num, min(stop, SERVO_MAX_ANGLE))
        print(f"moving {constants.servos[servo_num]}; increasing: {increasing}")
        await self.return_to_start(servo_num, start, delay=0.1)

        if increasing:
            print(f"increasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, 1):
                self.set_angle(servo_num, i)
                await asyncio.sleep(delay)
        else:
            print(f"decreasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, -1):
                self.set_angle(servo_num, i)
                await asyncio.sleep(delay)

        await self.return_to_start(servo_num, start, delay=0.1)

    async def move_by_direction(self, servo_num, start, stop, delay=0.1, increasing=True):
        """Move a servo in one direction without auto-returning to start.

        Simpler than move_by_dir — no pre/post return_to_start calls. Used
        when the caller controls the full movement sequence.

        Args:
            servo_num:  Channel index of the target servo.
            start:      Origin angle in degrees (used when decreasing).
            stop:       Destination angle in degrees (used when increasing).
                        Clamped to SERVO_MAX_ANGLE.
            delay:      Seconds between each 1-degree step.
            increasing: True sweeps start→stop; False sweeps stop→start.
        """
        start = self.clamp_angle(servo_num, max(start, 0))
        stop = self.clamp_angle(servo_num, min(stop, SERVO_MAX_ANGLE))
        print(f"moving {constants.servos[servo_num]}; increasing: {increasing}")

        if increasing:
            for i in range(start, stop, 1):
                self.set_angle(servo_num, i)
                await asyncio.sleep(delay)

        if not increasing:
            print(f"not increasing {constants.servos[servo_num]}")
            print(f"start {start}; stop: {stop}")
            for i in range(stop, start, -1):
                self.set_angle(servo_num, i)
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # Diagnostics & composite tests                                        #
    # ------------------------------------------------------------------ #

    async def return_to_rest(self):
        """Drive every configured servo to its safe REST_POSITION.

        Called between routines and — critically — after any error, so servos
        are never left energized against a jam. Moves gently (step-by-step via
        return_to_start) and never raises: a failure here must not mask the
        original error, and we still want to attempt every other servo.
        """
        print("return_to_rest: moving all servos to safe resting positions")
        for servo_num, rest_angle in constants.REST_POSITIONS.items():
            try:
                await self.return_to_start(servo_num, rest_angle, delay=0.03)
            except Exception as e:
                name = constants.servos.get(servo_num, f"ch{servo_num}")
                print(f"return_to_rest: failed to rest {name}: {e}")

    def display_position(self):
        """Print the current angle of every configured servo channel."""
        for servo_num in range(0, len(constants.servos), 1):
            print(str(self.kit.servo[servo_num].angle))

    async def arm(self):
        """Run a full arm test sequence: shoulder up, shoulder tilt, elbow tilt, elbow rotate."""
        await self.shoulder_rotate()
        await self.shoulder_tilt()
        await self.elbow_tilt()
        await self.elbow_rotate()

    async def neck(self):
        """Run a neck test sequence: center then tilt."""
        await self.neck_center()
        await self.neck_tilt()

    async def test(self):
        """Run the full hardware test (arm + neck)."""
        await self.arm()
        await self.neck()


if __name__ == '__main__':
    # Instantiate and run the neck test directly for manual hardware verification.
    trunk_controller = TrunkController("Servo TrunkController")
    # asyncio.run(trunk_controller.neck())
