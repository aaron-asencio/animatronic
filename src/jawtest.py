"""jawtest.py

Trigger the animatronic jaw motor on and off to verify the MOUTH_MOTOR_PIN wiring.

Drives the jaw motor on ``MOUTH_MOTOR_PIN`` (via gpiozero's DigitalOutputDevice)
open and closed a configurable number of times at a configurable interval, then
leaves the jaw closed. Useful as a quick hardware smoke test after wiring or
reseating the jaw motor.

The jaw motor is a simple on/off device (not a variable servo): ``on()`` opens
the jaw, ``off()`` closes it — the same device AudioPlayer/AudioStreamer pulse
from audio amplitude.

Runs on the Raspberry Pi with GPIO access. Like the other hardware scripts it
may need root:

    sudo ../.venv/bin/python3 jawtest.py
    sudo ../.venv/bin/python3 jawtest.py --count 10 --on-time 0.15 --off-time 0.15
"""

import argparse
import sys
import time

from gpiozero import DigitalOutputDevice

from model.constants import MOUTH_MOTOR_PIN


def flash_jaw(count, on_time, off_time):
    """Open and close the jaw motor on MOUTH_MOTOR_PIN a fixed number of times.

    The jaw is always closed before returning, so the motor is never left
    energized (which would cause heating and wear).

    Args:
        count:    Number of open/close cycles to perform.
        on_time:  Seconds to hold the jaw open during each cycle.
        off_time: Seconds to hold the jaw closed between cycles.
    """
    jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
    print(f"Triggering jaw motor on pin {MOUTH_MOTOR_PIN}: {count} cycles "
          f"(open={on_time}s, closed={off_time}s)")
    try:
        for i in range(1, count + 1):
            jaw_motor.on()
            print(f"Cycle {i}/{count}: OPEN")
            time.sleep(on_time)
            jaw_motor.off()
            print(f"Cycle {i}/{count}: CLOSED")
            time.sleep(off_time)
    finally:
        # Never leave the jaw motor energized, even on Ctrl+C or an error.
        jaw_motor.off()
        print("Jaw motor test complete; jaw closed.")


def main(args):
    """Run the jaw-motor trigger test from parsed CLI arguments.

    Args:
        args: Parsed argparse Namespace with count, on_time, and off_time.
    """
    if args.count <= 0:
        print("count must be greater than 0")
        sys.exit(1)
    if args.on_time < 0 or args.off_time < 0:
        print("on-time and off-time must be >= 0")
        sys.exit(1)
    flash_jaw(args.count, args.on_time, args.off_time)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Trigger the animatronic jaw motor (MOUTH_MOTOR_PIN) on and off as a hardware test."
    )
    parser.add_argument('--count', type=int, default=5,
                        help='Number of open/close cycles (default: 5).')
    parser.add_argument('--on-time', dest='on_time', type=float, default=0.3,
                        help='Seconds the jaw stays open per cycle (default: 0.3).')
    parser.add_argument('--off-time', dest='off_time', type=float, default=0.3,
                        help='Seconds the jaw stays closed between cycles (default: 0.3).')
    args = parser.parse_args()
    main(args)
