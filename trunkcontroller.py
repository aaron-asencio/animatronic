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

from adafruit_servokit import ServoKit
from time import sleep
import asyncio
import constants

# Maximum safe angle for all servos on this hardware.
SERVO_MAX_ANGLE = 270


class TrunkController:
    """Controls individual servo joints on the animatronic body."""

    def __init__(self, name):
        self.name = name

    # Shared ServoKit instance (class-level so all instances share one board).
    kit = ServoKit(channels=16)
    for i in range(0, 16):
        kit.servo[i].actuation_range = SERVO_MAX_ANGLE

    print("servo setup")

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
        stop = min(stop, SERVO_MAX_ANGLE)
        start = max(start, 0)
        print(f"moving {constants.servos[servo_num]}")
        servo = self.kit.servo[servo_num]
        for i in range(start, stop, 1):
            servo.angle = i
            current_position = round(servo.angle)
            print(f"Servo angle set {i}; angle returned: {current_position}")
            await asyncio.sleep(delay)

        if revert:
            sleep(revert_delay)
            for i in range(stop, start, -1):
                servo.angle = i
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

        # If the servo has never been commanded, snap it to start.
        if self.kit.servo[servo_num].angle is None:
            self.kit.servo[servo_num].angle = start

        current_position = round(self.kit.servo[servo_num].angle)

        print(f"return to start {constants.servos[servo_num]} which is at {current_position}")
        if current_position != start and current_position <= SERVO_MAX_ANGLE:
            if current_position > start:
                for i in range(current_position, start, -1):
                    print(f"return to start now {i}")
                    self.kit.servo[servo_num].angle = i
                    await asyncio.sleep(delay)
            else:
                for i in range(current_position, start, 1):
                    self.kit.servo[servo_num].angle = i
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
        stop = min(stop, SERVO_MAX_ANGLE)
        start = max(start, 0)
        print(f"moving {constants.servos[servo_num]}; increasing: {increasing}")
        await self.return_to_start(servo_num, start, delay=0.1)

        if increasing:
            print(f"increasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                print(i)
                await asyncio.sleep(delay)
        else:
            print(f"decreasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, -1):
                self.kit.servo[servo_num].angle = i
                print(i)
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
        stop = min(stop, SERVO_MAX_ANGLE)
        start = max(start, 0)
        print(f"moving {constants.servos[servo_num]}; increasing: {increasing}")

        if increasing:
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)

        if not increasing:
            print(f"not increasing {constants.servos[servo_num]}")
            print(f"start {start}; stop: {stop}")
            for i in range(stop, start, -1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # Diagnostics & composite tests                                        #
    # ------------------------------------------------------------------ #

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
