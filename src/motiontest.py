"""motiontest.py

Read an HC-SR501 PIR motion sensor and print motion transitions to the console.

Uses the reusable ``PirMotionSensor`` helper (``motion_sensor.py``) on
``PIR_MOTION_PIN`` and polls every ``--interval`` seconds, printing a line each
time the motion state CHANGES (motion started / motion ended) until interrupted
with Ctrl+C. Useful as a quick hardware smoke test after wiring the sensor.

Wiring note: unlike the HC-SR04 ECHO line, the HC-SR501 OUT is a clean 3.3V
push-pull logic signal (HIGH = motion). It needs NO voltage divider or level
shifter — it wires straight to the Pi GPIO input.

Runs on the Raspberry Pi with GPIO access. Like the other hardware scripts it
may need root:

    sudo ../.venv/bin/python3 motiontest.py
    sudo ../.venv/bin/python3 motiontest.py --interval 0.1
    sudo ../.venv/bin/python3 motiontest.py --count 50
"""

import argparse
import sys
import time

from constants import PIR_MOTION_PIN
from motion_sensor import PirMotionSensor


def watch_motion(interval, count):
    """Continuously read the PIR sensor and print each motion transition.

    The sensor is always closed before returning, so its GPIO pin is released
    even on Ctrl+C or an error mid-loop.

    Args:
        interval: Seconds to wait between readings.
        count:    Number of readings to take, or 0 to read forever until
                  interrupted with Ctrl+C.
    """
    with PirMotionSensor() as sensor:
        print(f"Reading HC-SR501 PIR on pin {PIR_MOTION_PIN} "
              f"(interval={interval}s). Press Ctrl+C to stop.")
        last = None
        i = 0
        while count == 0 or i < count:
            i += 1
            motion = sensor.motion_detected()
            if motion != last:
                label = "MOTION started" if motion else "motion ended"
                print(f"Reading {i}: {label}")
                last = motion
            time.sleep(interval)
        print("Motion sensor test complete; sensor closed.")


def main(args):
    """Run the PIR motion read loop from parsed CLI arguments.

    Args:
        args: Parsed argparse Namespace with interval and count.
    """
    if args.interval < 0:
        print("interval must be >= 0")
        sys.exit(1)
    if args.count < 0:
        print("count must be >= 0 (0 = read until Ctrl+C)")
        sys.exit(1)
    try:
        watch_motion(args.interval, args.count)
    except KeyboardInterrupt:
        # Expected exit path for the default forever loop; keep it quiet.
        print("\nInterrupted.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Read an HC-SR501 PIR motion sensor (PIR_MOTION_PIN) and "
                    "print motion transitions as a hardware test."
    )
    parser.add_argument('--interval', type=float, default=0.2,
                        help='Seconds between readings (default: 0.2).')
    parser.add_argument('--count', type=int, default=0,
                        help='Number of readings to take, or 0 to read until '
                             'Ctrl+C (default: 0).')
    args = parser.parse_args()
    main(args)
