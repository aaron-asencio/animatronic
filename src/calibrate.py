"""
calibrate.py

Interactive single-servo calibration tool for finding safe angle limits.

Moves ONE servo channel to ONE target angle, approaching gently one degree at a
time so you can watch the mechanism and cut power the instant it starts to bind.
Use it to find each joint's true physical min/max, then record those in
constants.SAFE_LIMITS.

SAFETY
------
- By default the target angle is CLAMPED to constants.SAFE_LIMITS, so you can
  never drive past the currently-known-safe range by accident.
- To probe BEYOND the current limits (to discover the real physical stop), pass
  --unsafe. Do this only with a hand on the power switch. Approach in small
  steps and STOP the moment you hear/see/ feel the servo strain.
- The servo lock is acquired so a routine can't move other joints while you
  calibrate.

USAGE
-----
  # Read the current angle of the neck-tilt channel without moving it:
  sudo .venv/bin/python3 calibrate.py --channel 1 --read

  # Gently move neck tilt to 45 degrees (clamped to SAFE_LIMITS):
  sudo .venv/bin/python3 calibrate.py --channel 1 --angle 45

  # Probe below the current safe min to find the real jam point (CAREFUL):
  sudo .venv/bin/python3 calibrate.py --channel 1 --angle 10 --unsafe --step-delay 0.15

  # Channels: 0=NECK_PAN 1=NECK_TILT 4=RT_ELBOW_ROTATOR 5=RT_ELBOW_TILT
  #           6=RT_SHOULDER_TILT 7=RT_SHOULDER_ROTATOR
"""

import argparse
import asyncio
import sys

import constants
from trunkcontroller import TrunkController, SERVO_MAX_ANGLE
from servo_lock import servo_lock, ServoBusyError, BUSY_EXIT_CODE


def channel_name(ch):
    return constants.servos.get(ch, f"ch{ch}")


async def gentle_move(trunk, channel, target, step_delay, allow_unsafe):
    """Step a servo from its current angle to target, one degree at a time.

    Args:
        trunk:        TrunkController instance.
        channel:      Servo channel index.
        target:       Desired angle in degrees.
        step_delay:   Seconds between each 1-degree step (higher = slower/safer).
        allow_unsafe: If True, bypass SAFE_LIMITS and write raw angles so we can
                      probe past the currently-known-safe range.
    """
    servo = trunk.kit.servo[channel]

    # Compute the effective (clamped) target first.
    if allow_unsafe:
        effective = max(0, min(SERVO_MAX_ANGLE, target))  # only the HW electrical bound
        if effective != target:
            print(f"target {target} clamped to servo electrical range -> {effective}")
        note = "UNSAFE (SAFE_LIMITS bypassed)"
    else:
        effective = trunk.clamp_angle(channel, target)
        if effective != target:
            lo, hi = constants.SAFE_LIMITS.get(channel, (0, SERVO_MAX_ANGLE))
            print(f"target {target} clamped to SAFE_LIMITS[{channel_name(channel)}]="
                  f"({lo},{hi}) -> {effective}. Pass --unsafe to probe past it.")
        note = "clamped to SAFE_LIMITS"

    # Where are we now?
    current = servo.angle

    # Handle unknown / garbage current readings. On power-up the channel duty
    # cycle can be uninitialised, making .angle return None or a wild value
    # (e.g. 57431). We CANNOT step gently from an unknown position — a range()
    # from garbage would sweep wildly. Instead, establish a known position with
    # a SINGLE direct write to the (safe-clamped) target, but ONLY when the
    # target is within SAFE_LIMITS. Refuse to do a blind write in --unsafe mode.
    if current is None or not (0 <= current <= SERVO_MAX_ANGLE):
        if allow_unsafe:
            print(f"REFUSING: {channel_name(channel)} is in an unknown state "
                  f"(angle={current}) and --unsafe was given. Establish a known "
                  f"position first with a plain --angle inside SAFE_LIMITS.")
            return
        print(f"{channel_name(channel)} position unknown (angle={current}). "
              f"Establishing a known position with ONE direct write to "
              f"{effective} (within SAFE_LIMITS). Watch the joint.")
        servo.angle = effective
        print(f"  {channel_name(channel)} set directly to {effective}")
        await asyncio.sleep(0.5)
        print(f"Reached {effective}. If it moved sharply, that was the one-time "
              f"recovery write; subsequent moves will step gently.")
        return

    # Normal case: step gently one degree at a time from the known current pos.
    current = round(current)
    print(f"{channel_name(channel)} (ch{channel}): {current} -> {effective}  [{note}]")

    step = 1 if effective >= current else -1
    for angle in range(current, int(effective) + step, step):
        angle = max(0, min(SERVO_MAX_ANGLE, angle))
        servo.angle = angle
        print(f"  {channel_name(channel)} @ {angle}")
        await asyncio.sleep(step_delay)

    print(f"Reached {effective}. Watch for binding. "
          f"Ctrl-C or cut power immediately if the joint strains.")


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate a single servo's safe angle by gentle probing.")
    parser.add_argument('--channel', type=int,
                        help='Servo channel index (e.g. 1 = NECK_TILT). '
                             'Required for all actions except --health.')
    parser.add_argument('--angle', type=int,
                        help='Absolute target angle in degrees.')
    parser.add_argument('--nudge', type=int,
                        help='Move this many degrees RELATIVE to the current '
                             'angle (e.g. -3 to creep down, +3 to creep up). '
                             'Safer than --angle for probing limits.')
    parser.add_argument('--read', action='store_true',
                        help='Just print the current angle and exit (no move).')
    parser.add_argument('--health', action='store_true',
                        help='Run the PCA9685 health check (PWM frequency) and '
                             'exit. Does not move any servo. Use this to '
                             'diagnose a browned-out / loose-VCC board.')
    parser.add_argument('--step-delay', type=float, default=0.20,
                        help='Seconds between 1-degree steps (default 0.20). '
                             'Increase for slower, safer probing.')
    parser.add_argument('--unsafe', action='store_true',
                        help='Bypass SAFE_LIMITS to probe past known-safe range. '
                             'Use only with a hand on the power switch.')
    parser.add_argument('--hold', type=float, default=3.0,
                        help='Seconds to hold at the target before exiting '
                             '(default 3).')
    args = parser.parse_args()

    trunk = TrunkController("Calibration")

    # Health check: report PCA9685 PWM frequency state and exit. No motion, and
    # no channel required. The check also runs automatically in __init__ above,
    # so this mainly gives a clean standalone diagnostic.
    if args.health:
        ok = trunk.health_check()
        print(f"Board health: {'OK' if ok else 'PROBLEM (see messages above)'}")
        return

    if args.channel is None:
        parser.error("--channel is required (except with --health).")

    if args.read:
        # Read-only: report the servo's current angle. Reading .angle reads the
        # existing duty cycle back and does NOT command motion. We read inside a
        # try so a transient I2C hiccup prints a clear message instead of a
        # traceback.
        try:
            angle = trunk.kit.servo[args.channel].angle
        except Exception as e:
            print(f"Could not read {channel_name(args.channel)} "
                  f"(ch{args.channel}): {e}")
            return
        lo, hi = constants.SAFE_LIMITS.get(args.channel, (0, SERVO_MAX_ANGLE))
        print(f"{channel_name(args.channel)} (ch{args.channel}) current angle: "
              f"{angle}  | SAFE_LIMITS=({lo},{hi})")
        return

    if args.angle is None and args.nudge is None:
        parser.error("Provide --angle, --nudge, or --read.")
    if args.angle is not None and args.nudge is not None:
        parser.error("Use either --angle or --nudge, not both.")

    # Resolve a --nudge into an absolute target based on the current angle.
    target = args.angle
    if args.nudge is not None:
        current = trunk.kit.servo[args.channel].angle
        if current is None:
            parser.error("Cannot --nudge: servo has no known position yet. "
                         "Set an absolute --angle first.")
        # Guard against a garbage readback: on power-up the channel's duty cycle
        # can be out of range, making .angle report nonsense (e.g. 57431). If
        # the reading isn't a physically plausible angle, refuse to nudge —
        # nudging from garbage would compute a wild target and slam the servo.
        if not (0 <= current <= SERVO_MAX_ANGLE):
            parser.error(
                f"Cannot --nudge: current angle reads {current}, which is out "
                f"of range [0,{SERVO_MAX_ANGLE}]. The servo is in an unknown "
                f"state. Set a known-safe absolute --angle first (this commands "
                f"a real position), then --nudge from there.")
        target = round(current) + args.nudge
        print(f"nudge {args.nudge:+d} from {round(current)} -> target {target}")

    try:
        with servo_lock():
            try:
                asyncio.run(gentle_move(trunk, args.channel, target,
                                        args.step_delay, args.unsafe))
                # Hold so you can observe, then leave the servo where it is.
                import time
                time.sleep(args.hold)
            except KeyboardInterrupt:
                print("\nInterrupted — leaving servo at its current angle. "
                      "Cut power if it is straining.")
    except ServoBusyError:
        print("Servos busy — another routine is running. Aborting.")
        sys.exit(BUSY_EXIT_CODE)


if __name__ == '__main__':
    main()
