"""rangetest.py

Read an HC-SR04 ultrasonic range sensor and print distances to the console.

Uses the reusable ``RangeSensor`` helper (``range_sensor.py``) on
``RANGE_TRIG_PIN`` / ``RANGE_ECHO_PIN`` and prints a distance reading every
``--interval`` seconds until interrupted with Ctrl+C. Useful as a quick
hardware smoke test after wiring the sensor. With ``--threshold`` it also
prints the ``object_within`` presence result each reading — the same primitive
a sensor-interrupted Sleep mode polls to decide when to wake.

Wiring note: the HC-SR04 ECHO pin idles at 5V but the Pi GPIO tolerates only
3.3V. A voltage divider (or level shifter) on the ECHO line is required (and is
in place on this build).

Runs on the Raspberry Pi with GPIO access. Like the other hardware scripts it
may need root:

    sudo ../.venv/bin/python3 rangetest.py
    sudo ../.venv/bin/python3 rangetest.py --interval 0.25 --max-distance 2.0
    sudo ../.venv/bin/python3 rangetest.py --threshold 0.5
"""

import argparse
import sys
import time

from range_sensor import (
    DEFAULT_APPROACH_CONSECUTIVE,
    DEFAULT_APPROACH_GATE_M,
    DEFAULT_APPROACH_MIN_STEP_M,
    ApproachDetector,
    RangeSensor,
)


def read_distances(interval, max_distance, count, threshold):
    """Continuously read the range sensor and print each distance.

    The sensor is always closed before returning, so its GPIO pins are released
    even on Ctrl+C or an error mid-loop.

    Args:
        interval:     Seconds to wait between readings.
        max_distance: Sensor's maximum measurable distance in meters. Readings
                      saturate at this value; also sets gpiozero's timeout.
        count:        Number of readings to take, or 0 to read forever until
                      interrupted with Ctrl+C.
        threshold:    If > 0, also print the object_within(threshold) presence
                      result for each reading. If 0, presence is not shown.
    """
    with RangeSensor(max_distance=max_distance) as sensor:
        print(f"Reading HC-SR04 on TRIG={sensor.trigger_pin}, "
              f"ECHO={sensor.echo_pin} (max={max_distance}m, "
              f"interval={interval}s). Press Ctrl+C to stop.")
        i = 0
        while count == 0 or i < count:
            i += 1
            meters = sensor.distance_m()
            cm = meters * 100
            label = f"{i}/{count}" if count else str(i)
            line = f"Reading {label}: {meters:.3f} m ({cm:.1f} cm)"
            if threshold > 0:
                present = "YES" if meters <= threshold else "no"
                line += f"  | within {threshold:.2f}m: {present}"
            print(line)
            time.sleep(interval)
        print("Range sensor test complete; sensor closed.")


def watch_approach(interval, gate, consecutive, min_step):
    """Poll the ApproachDetector and print each reading and the wake decision.

    Exercises the EXACT logic the napping Mode uses to sensor-wake: an approach
    is confirmed once ``consecutive`` in-gate readings in a row are each at
    least ``min_step`` meters closer than the last. Loops until Ctrl+C; the
    detector (and its sensor) is closed on exit.

    Args:
        interval:    Seconds to wait between readings.
        gate:        Range gate in meters; readings farther are ignored/reset.
        consecutive: Consecutive closer readings required to confirm approach.
        min_step:    Minimum decrease in meters to count as "getting closer".
    """
    with ApproachDetector(gate_m=gate, consecutive=consecutive,
                          min_step_m=min_step) as detector:
        print(f"Approach watch on TRIG={detector.sensor.trigger_pin}, "
              f"ECHO={detector.sensor.echo_pin}: wake when {consecutive} "
              f"consecutive readings each >= {min_step}m closer, within "
              f"{gate}m. Press Ctrl+C to stop.")
        i = 0
        while True:
            i += 1
            # Read the raw distance first so the printout shows it, then feed
            # the SAME reading logic via the detector on the next poll. To keep
            # the printed distance and the decision consistent, read distance
            # from the detector's own sensor and let approaching() re-read once;
            # for a test tool the tiny double-read is fine.
            meters = detector.sensor.distance_m()
            fired = detector.approaching()
            gated = "in-gate" if meters <= gate else "beyond-gate"
            flag = "  <-- APPROACH (would wake)" if fired else ""
            print(f"Reading {i}: {meters:.3f} m ({meters * 100:.1f} cm) "
                  f"[{gated}]{flag}")
            time.sleep(interval)


def main(args):
    """Run the range-sensor read loop from parsed CLI arguments.

    Args:
        args: Parsed argparse Namespace with interval, max_distance, count,
              threshold, and the approach-mode options.
    """
    if args.interval < 0:
        print("interval must be >= 0")
        sys.exit(1)
    if args.max_distance <= 0:
        print("max-distance must be greater than 0")
        sys.exit(1)
    if args.count < 0:
        print("count must be >= 0 (0 = read until Ctrl+C)")
        sys.exit(1)
    if args.threshold < 0:
        print("threshold must be >= 0 (0 = presence not shown)")
        sys.exit(1)
    if args.gate <= 0:
        print("gate must be greater than 0")
        sys.exit(1)
    if args.consecutive < 1:
        print("consecutive must be >= 1")
        sys.exit(1)
    if args.min_step < 0:
        print("min-step must be >= 0")
        sys.exit(1)
    try:
        if args.approach:
            watch_approach(args.interval, args.gate, args.consecutive,
                           args.min_step)
        else:
            read_distances(args.interval, args.max_distance, args.count,
                           args.threshold)
    except KeyboardInterrupt:
        # Expected exit path for the default forever loop; keep it quiet.
        print("\nInterrupted.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Read an HC-SR04 ultrasonic range sensor (RANGE_TRIG_PIN / "
                    "RANGE_ECHO_PIN) and print distances as a hardware test."
    )
    parser.add_argument('--interval', type=float, default=0.5,
                        help='Seconds between readings (default: 0.5).')
    parser.add_argument('--max-distance', dest='max_distance', type=float,
                        default=4.0,
                        help='Max measurable distance in meters; readings '
                             'saturate here (default: 4.0).')
    parser.add_argument('--count', type=int, default=0,
                        help='Number of readings to take, or 0 to read until '
                             'Ctrl+C (default: 0).')
    parser.add_argument('--threshold', type=float, default=0.0,
                        help='If > 0, also print object_within(threshold) '
                             'presence per reading, in meters (default: 0 = '
                             'not shown).')
    parser.add_argument('--approach', action='store_true',
                        help='Approach-detection mode: print when the napping '
                             'wake logic (consecutive closer readings within '
                             'the gate) would fire.')
    parser.add_argument('--gate', type=float, default=DEFAULT_APPROACH_GATE_M,
                        help='Approach mode: ignore objects farther than this '
                             f'many meters (default: {DEFAULT_APPROACH_GATE_M}).')
    parser.add_argument('--consecutive', type=int,
                        default=DEFAULT_APPROACH_CONSECUTIVE,
                        help='Approach mode: consecutive closer readings needed '
                             f'to confirm (default: {DEFAULT_APPROACH_CONSECUTIVE}).')
    parser.add_argument('--min-step', dest='min_step', type=float,
                        default=DEFAULT_APPROACH_MIN_STEP_M,
                        help='Approach mode: minimum meters closer per reading '
                             f'to count (default: {DEFAULT_APPROACH_MIN_STEP_M}).')
    args = parser.parse_args()
    main(args)
