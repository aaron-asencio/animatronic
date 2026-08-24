"""
concurrentMovements.py

Synchronous / thread-based movement choreography using ThreadPoolExecutor.

This module provides an alternative to the async-based Movements class for
situations where true thread-level parallelism is needed (e.g. running
multiple servos simultaneously without an event loop).  It uses the blocking
`time.sleep` and the `concurrent.futures.ThreadPoolExecutor` rather than
asyncio tasks.

Typical entry point: run `python concurrentMovements.py` directly to execute
the facePalm demo.

Dependency chain:
    ConcurrentMovements
        ├── TrunkController  (trunkcontroller.py) — owns the shared ServoKit instance
        └── Movements        (movements.py)       — async gestures
"""

from concurrent.futures import ThreadPoolExecutor
from trunkcontroller import TrunkController, SERVO_MAX_ANGLE
from movements import Movements
from time import sleep, perf_counter

import constants


class ConcurrentMovements:
    """Thread-based movement orchestration for concurrent servo control."""

    def __init__(self, name):
        self.name = name

    # Shared instances (class-level so hardware is initialised once).
    # TrunkController owns the sole ServoKit instance; use its kit directly
    # to avoid creating a second ServoKit object for the same I2C board.
    trunk = TrunkController("Servo TrunkController")
    mv    = Movements("Servo Movements")

    # Convenience reference to the shared ServoKit — do NOT instantiate a new one.
    kit = trunk.kit

    # ------------------------------------------------------------------ #
    # Synchronous head gestures                                            #
    # ------------------------------------------------------------------ #

    def shake_no(self, revert=True):
        """Shake the head "no" — wide arc from center to left then to right.

        Moves the neck pan from center (90°) out to NECK_LEFT (140°),
        then sweeps across to NECK_RIGHT (40°), pauses, and returns to center.

        Args:
            revert: Reserved for future use; not currently applied.
        """
        print("shake head no")
        NECK_LEFT  = 140
        NECK_RIGHT = 40

        increase = True
        self.move_by_dir(constants.NECK_PAN, constants.NECK_CENTER, NECK_LEFT, 0.05, increase)

        increase = False
        self.move_by_dir(constants.NECK_PAN, constants.NECK_CENTER, NECK_RIGHT, 0.05, increase)

        sleep(1)
        self.return_to_start(constants.NECK_PAN, constants.NECK_CENTER, delay=0.04)

    def shake_head(self, revert=True):
        """Shake the head with a small rapid oscillation (70–80°).

        Centres the neck first, repeats the tight pan 4 times, then returns
        to center.  Faster and more subtle than shake_no.

        Args:
            revert: Passed through to move(); if True each sweep reverts.
        """
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 80
        self.return_to_start(constants.NECK_PAN, constants.NECK_CENTER, delay=0.04)
        for _ in range(4):
            self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.02, revert, .1)
        self.return_to_start(constants.NECK_PAN, constants.NECK_CENTER, delay=0.04)

    # ------------------------------------------------------------------ #
    # Core synchronous movement primitives                                 #
    # ------------------------------------------------------------------ #

    def move(self, servo_num=0, start=0, stop=180, delay=0.1, revert=True, revert_delay=0.5):
        """Sweep a servo from start to stop, then optionally back (blocking).

        Mirrors TrunkController.move but uses time.sleep instead of
        asyncio.sleep, making it safe to call from threads.

        Args:
            servo_num:    Channel index of the target servo.
            start:        Starting angle in degrees.
            stop:         Destination angle in degrees (clamped to SERVO_MAX_ANGLE).
            delay:        Seconds to wait between each 1-degree step.
            revert:       If True, sweep back from stop to start after pausing.
            revert_delay: Seconds to hold at stop before reverting.
        """
        stop = min(stop, SERVO_MAX_ANGLE)
        start = max(start, 0)
        print(f"moving {constants.servos[servo_num]}")
        servo = self.kit.servo[servo_num]
        for i in range(start, stop, 1):
            servo.angle = i
            current_position = round(servo.angle)
            print(f"Servo angle set {i}; angle returned: {current_position}")
            sleep(delay)

        if revert:
            sleep(revert_delay)
            for i in range(stop, start, -1):
                servo.angle = i
                sleep(delay)

    def return_to_start(self, servo_num, start=0, delay=0.1):
        """Gently move a servo back to its resting position (blocking).

        Reads the current angle, then steps toward start one degree at a time.
        Handles None angle (servo not yet positioned) by snapping to start.

        Args:
            servo_num: Channel index of the target servo.
            start:     Target resting angle in degrees.
            delay:     Seconds between each 1-degree step.
        """
        print(f"returning servo num: {servo_num} to start: {start}")
        print(f"number of servo channels: {self.kit._channels}")
        print(self.kit.servo[servo_num])
        print(f"angle: {self.kit.servo[servo_num].angle}")

        if self.kit.servo[servo_num].angle is None:
            self.kit.servo[servo_num].angle = start

        current_position = round(self.kit.servo[servo_num].angle)

        print(f"return to start {constants.servos[servo_num]} which is at {current_position}")
        if current_position != start and current_position <= SERVO_MAX_ANGLE:
            if current_position > start:
                for i in range(current_position, start, -1):
                    print(f"return to start now {i}")
                    self.kit.servo[servo_num].angle = i
                    sleep(delay)
            else:
                for i in range(current_position, start, 1):
                    self.kit.servo[servo_num].angle = i
                    sleep(delay)

    def move_by_dir(self, servo_num, start, stop, delay=0.1, increasing=True):
        """Move a servo in one direction without auto-returning (blocking).

        Args:
            servo_num:  Channel index of the target servo.
            start:      Origin angle in degrees.
            stop:       Destination angle in degrees (clamped to SERVO_MAX_ANGLE).
            delay:      Seconds between each 1-degree step.
            increasing: True sweeps start→stop; False sweeps start→stop in
                        reverse (i.e. stop is lower than start).
        """
        stop = min(stop, SERVO_MAX_ANGLE)
        start = max(start, 0)
        print(f"moving {constants.servos[servo_num]}; increasing: {increasing}")

        if increasing:
            print(f"increasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                print(i)
                sleep(delay)
        else:
            print(f"decreasing {constants.servos[servo_num]}; start {start}; stop: {stop}")
            for i in range(start, stop, -1):
                self.kit.servo[servo_num].angle = i
                print(i)
                sleep(delay)

    # ------------------------------------------------------------------ #
    # Compound gestures                                                    #
    # ------------------------------------------------------------------ #

    def face_palm(self):
        """Raise the arm to cover the face while shaking the head.

        Uses a ThreadPoolExecutor to run three arm servos concurrently:
        - RT_SHOULDER_ROTATOR: raises/lowers the whole arm.
        - RT_ELBOW_ROTATOR:    rotates the forearm toward the face.
        - RT_ELBOW_TILT:       bends the elbow to reach the face.

        After a 1.25-second delay the head-shake starts in a fourth thread.
        All servos are returned to their resting positions after the gesture.

        Note: each thread targets a distinct servo channel, so concurrent
        writes do not contend on the same channel.

        Approximate face-coverage angles:
            Shoulder rotator : 200° = mouth, 230° = eyes, 260° = top of head
            Elbow rotator    : 140° = mouth level
            Elbow tilt       : 140° = head height
        """
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 260
        RT_SHOULDER_TILT_MIN    = 0
        RT_SHOULDER_TILT_MAX    = 90
        RT_ELBOW_ROTATE_MIN     = 0
        RT_ELBOW_ROTATE_MAX     = 140
        RT_ELBOW_TILT_MIN       = 0
        RT_ELBOW_TILT_MAX       = 140
        increasing              = True

        start = perf_counter()

        with ThreadPoolExecutor(max_workers=5) as exe:
            # Raise arm and position elbow simultaneously.
            # Each future targets a distinct servo channel — no write contention.
            future1 = exe.submit(self.move_by_dir, constants.RT_SHOULDER_ROTATOR,
                                 RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
            future3 = exe.submit(self.move_by_dir, constants.RT_ELBOW_ROTATOR,
                                 RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.005, increasing)
            future4 = exe.submit(self.move_by_dir, constants.RT_ELBOW_TILT,
                                 RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, increasing)
            # Delay slightly so arm is in position before head shakes.
            sleep(1.25)
            future2 = exe.submit(self.shake_head)

        # Hold the face-palm pose briefly before resetting.
        sleep(3)

        # Return all joints to their resting positions.
        self.return_to_start(constants.RT_ELBOW_TILT,       RT_ELBOW_TILT_MIN,       delay=0.005)
        self.return_to_start(constants.RT_ELBOW_ROTATOR,    RT_ELBOW_ROTATE_MIN,     delay=0.005)
        self.return_to_start(constants.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN, delay=0.005)
        self.return_to_start(constants.NECK_PAN,            constants.NECK_CENTER,   delay=0.04)
        self.return_to_start(constants.RT_SHOULDER_TILT,    RT_SHOULDER_TILT_MIN,    delay=0.005)

        finish = perf_counter()
        print(f"It took {finish - start} second(s) to finish.")


def main():
    mv = ConcurrentMovements("ConcurrentMovements")
    mv.face_palm()


if __name__ == '__main__':
    main()
