"""eyetest.py

Flash the animatronic's LED eyes to verify the EYE_LIGHT_PIN wiring.

Drives the eye LED on ``EYE_LIGHT_PIN`` (via gpiozero) on and off a configurable
number of times at a configurable interval, then leaves the LED off. Useful as a
quick hardware smoke test after wiring or reseating the eye lights.

Runs on the Raspberry Pi with GPIO access. Like the other hardware scripts it
may need root:

    sudo ../.venv/bin/python3 eyetest.py
    sudo ../.venv/bin/python3 eyetest.py --count 10 --on-time 0.25 --off-time 0.25
"""

import argparse
import sys
import time

from gpiozero import LED

from constants import EYE_LIGHT_PIN


def flash_eyes(count, on_time, off_time):
    """Flash the eye LED on EYE_LIGHT_PIN a fixed number of times.

    The LED is always turned off before returning, so the eyes are never left
    energized after the test.

    Args:
        count:    Number of on/off blink cycles to perform.
        on_time:  Seconds to hold the LED on during each blink.
        off_time: Seconds to hold the LED off between blinks.
    """
    led = LED(EYE_LIGHT_PIN)
    print(f"Flashing eye LED on pin {EYE_LIGHT_PIN}: {count} cycles "
          f"(on={on_time}s, off={off_time}s)")
    try:
        for i in range(1, count + 1):
            led.on()
            print(f"Blink {i}/{count}: ON")
            time.sleep(on_time)
            led.off()
            print(f"Blink {i}/{count}: OFF")
            time.sleep(off_time)
    finally:
        # Never leave the eyes lit, even on Ctrl+C or an error mid-loop.
        led.off()
        print("Eye LED test complete; LED off.")


def main(args):
    """Run the eye-LED flash test from parsed CLI arguments.

    Args:
        args: Parsed argparse Namespace with count, on_time, and off_time.
    """
    if args.count <= 0:
        print("count must be greater than 0")
        sys.exit(1)
    if args.on_time < 0 or args.off_time < 0:
        print("on-time and off-time must be >= 0")
        sys.exit(1)
    flash_eyes(args.count, args.on_time, args.off_time)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Flash the animatronic's LED eyes (EYE_LIGHT_PIN) as a hardware test."
    )
    parser.add_argument('--count', type=int, default=5,
                        help='Number of on/off blink cycles (default: 5).')
    parser.add_argument('--on-time', dest='on_time', type=float, default=0.5,
                        help='Seconds the LED stays on per blink (default: 0.5).')
    parser.add_argument('--off-time', dest='off_time', type=float, default=0.5,
                        help='Seconds the LED stays off between blinks (default: 0.5).')
    args = parser.parse_args()
    main(args)
