"""Interactive calibration harness for the kinematic collision model.

Walks you joint-by-joint through confirming (and correcting) the three
per-joint calibration values the 3D collision model depends on:

    urdf_rad = sign * (servo_deg - offset_deg) * (pi/180) * scale

  - ``sign``      -- does the joint move the direction the model expects?
  - ``offset_deg``-- which servo angle is the joint's zero-landmark?
  - ``scale``     -- how many physical degrees per commanded servo degree?

For each joint it drives the servo through a small, SAFE_LIMITS-clamped sweep,
asks you what you observed, and writes any corrections back into the calibration
JSON in place (with a timestamped backup). No code change is needed for the
model to pick up the new values -- ``Calibration_Store`` reloads the file.

SAFETY
------
This tool moves real servos as root (I2C/GPIO). A wrong sign or scale can drive
a joint toward the body. Precautions built in:

  - Every write goes through ``TrunkController.set_angle``, which clamps to
    ``constants.SAFE_LIMITS`` (never into a mechanical jam).
  - Sweeps step one degree at a time with a pause, and only ONE joint moves at
    a time; all others stay at their resting positions.
  - The affected servo is returned to rest after each joint and on exit/error.
  - Run a dry pass first with ``SERVO_SIM=1`` (logs every angle, moves nothing).

USAGE
-----
    # Dry run (no hardware writes):
    SERVO_SIM=1 PYTHONPATH=src .venv/bin/python -m calibrate_joints --dry-run

    # Live calibration of every joint (root for GPIO/I2C):
    sudo PYTHONPATH=src .venv/bin/python -m calibrate_joints

    # Only specific joints, by servo channel:
    sudo PYTHONPATH=src .venv/bin/python -m calibrate_joints --channels 5 4

This tool lives at ``src/`` level (like ``eyetest.py`` / ``jawtest.py``): an
operator utility that touches hardware, kept OUT of the hardware-free
``kinematics`` package.
"""

import argparse
import asyncio
import datetime
import json
import os
import shutil

import constants
from kinematics.calibration import Calibration_Store

# NOTE: ``trunkcontroller`` is imported LAZILY inside run() rather than here.
# It reads ``SERVO_SIM`` from the environment and builds the shared ServoKit at
# import time, so --dry-run must be able to set SERVO_SIM=1 in the environment
# BEFORE that module is first imported. Importing it here would lock in real
# hardware before the flag is applied.


# Default calibration store path (matches CollisionModel's default).
_DEFAULT_CALIBRATION = "src/config/calibration.json"

# Per-joint probe plan: the two servo angles to sweep between when confirming
# direction, plus what the model EXPECTS as the commanded angle INCREASES from
# low to high, and the zero-landmark used for the offset. Endpoints are
# re-clamped to SAFE_LIMITS at write time, so these are hints, not overrides.
_PROBE_PLAN = {
    constants.NECK_PAN: {
        "low": 60, "high": 120,
        "expect_increase": "head turns to its LEFT",
        "zero_landmark_deg": 90,
        "zero_landmark_desc": "head faces straight forward",
    },
    constants.NECK_TILT: {
        "low": 70, "high": 110,
        "expect_increase": "head LOWERS (chin toward chest)",
        "zero_landmark_deg": 90,
        "zero_landmark_desc": "head level",
    },
    constants.RT_SHOULDER_TILT: {
        "low": 60, "high": 130,
        "expect_increase": "arm RAISES up/away from the side (abduction)",
        "zero_landmark_deg": 135,
        "zero_landmark_desc": "arm straight out horizontally from the side",
    },
    constants.RT_SHOULDER_ROTATOR: {
        # Full 0->270 sweep so the geared physical arc (~170 deg) is measured in
        # one pass for an accurate scale, rather than a partial 100 deg sweep.
        "low": 0, "high": 270,
        "expect_increase": "arm moves UP (away from the side)",
        "zero_landmark_deg": 0,
        "zero_landmark_desc": "arm resting at the side of the body",
    },
    constants.RT_ELBOW_ROTATOR: {
        "low": 120, "high": 200,
        "expect_increase": "forearm rotates toward PALM UP",
        "zero_landmark_deg": 150,
        "zero_landmark_desc": "hand parallel to the side (palm inward)",
    },
    constants.RT_ELBOW_TILT: {
        "low": 5, "high": 90,
        "expect_increase": "elbow FLEXES (bends, forearm toward upper arm)",
        "zero_landmark_deg": 2,
        "zero_landmark_desc": "elbow straight (arm fully extended)",
    },
}

# Calibration order: neck first (lowest collision risk), then arm shoulder-out.
_JOINT_ORDER = [
    constants.NECK_PAN,
    constants.NECK_TILT,
    constants.RT_SHOULDER_TILT,
    constants.RT_SHOULDER_ROTATOR,
    constants.RT_ELBOW_ROTATOR,
    constants.RT_ELBOW_TILT,
]


def _prompt(message, default=None):
    """Prompts the operator and returns the stripped reply.

    Args:
        message: The prompt text to display.
        default: Value returned when the reply is empty (Enter pressed).

    Returns:
        The operator's reply (stripped), or ``default`` when the reply is empty.
    """
    suffix = f" [{default}]" if default is not None else ""
    reply = input(f"{message}{suffix}: ").strip()
    if not reply and default is not None:
        return str(default)
    return reply


def _prompt_yes_no(message, default=True):
    """Prompts a yes/no question and returns a bool.

    Args:
        message: The question to display.
        default: The value returned for an empty reply.

    Returns:
        True for yes, False for no.
    """
    hint = "Y/n" if default else "y/N"
    while True:
        reply = input(f"{message} ({hint}): ").strip().lower()
        if not reply:
            return default
        if reply in ("y", "yes"):
            return True
        if reply in ("n", "no"):
            return False
        print("  please answer y or n")


def _prompt_float(message, default=None):
    """Prompts for a floating-point number, re-asking until valid.

    Args:
        message: The prompt text.
        default: Value returned for an empty reply (or None to require input).

    Returns:
        The parsed float, or ``default`` when provided and the reply is empty.
    """
    while True:
        reply = _prompt(message, default)
        try:
            return float(reply)
        except (TypeError, ValueError):
            print("  please enter a number")


def _channel_name(channel):
    """Returns the human-readable servo name for a channel.

    Args:
        channel: PCA9685 channel number.

    Returns:
        The name from ``constants.servos`` or a ``chN`` fallback.
    """
    return constants.servos.get(channel, f"ch{channel}")


def _load_calibration_raw(path):
    """Loads the calibration JSON as a plain dict (joint -> fields).

    Args:
        path: Path to the calibration JSON file.

    Returns:
        The parsed JSON mapping.
    """
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _backup_and_write(path, data):
    """Writes calibration ``data`` to ``path`` after backing up the original.

    A timestamped copy of the existing file is made alongside it so a bad edit
    is always recoverable.

    Args:
        path: Path to the calibration JSON file.
        data: The full calibration mapping to write.

    Returns:
        The backup path written, or None when there was no existing file.
    """
    backup_path = None
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{path}.{stamp}.bak"
        shutil.copy2(path, backup_path)
        print(f"[calibrate] backed up existing calibration -> {backup_path}")

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"[calibrate] wrote updated calibration -> {path}")
    return backup_path


def _channel_to_joint(store, channel):
    """Returns the URDF joint name calibrated for a servo channel.

    Args:
        store: A ``Calibration_Store``.
        channel: PCA9685 channel number.

    Returns:
        The URDF joint name, or None when the channel is not calibrated.
    """
    cal = store._by_channel.get(channel)
    return cal.urdf_joint if cal else None


async def _sweep(controller, channel, start, stop, delay):
    """Sweeps a single servo one degree at a time between two angles.

    Every write goes through ``TrunkController.set_angle`` which clamps to
    ``SAFE_LIMITS``. Only this one channel moves.

    Args:
        controller: The ``TrunkController`` driving the hardware.
        channel: PCA9685 channel number to move.
        start: Starting angle in degrees.
        stop: Ending angle in degrees.
        delay: Seconds to pause between each one-degree step.
    """
    start = int(round(controller.clamp_angle(channel, start)))
    stop = int(round(controller.clamp_angle(channel, stop)))
    step = 1 if stop >= start else -1
    for angle in range(start, stop + step, step):
        controller.set_angle(channel, angle)
        await asyncio.sleep(delay)


async def _rest_channel(controller, channel, delay=0.03):
    """Returns a single channel to its resting position gently.

    Args:
        controller: The ``TrunkController``.
        channel: PCA9685 channel number.
        delay: Seconds between one-degree steps.
    """
    rest = constants.REST_POSITIONS.get(channel)
    if rest is None:
        return
    await controller.return_to_start(channel, rest, delay=delay)


async def _calibrate_direction(controller, channel, plan, delay):
    """Confirms the joint's ``sign`` by observing the direction of motion.

    Sweeps from the plan's low angle to its high angle (commanded angle
    INCREASING) and asks whether the expected physical motion happened. If it
    moved the opposite way, the sign must be flipped.

    Args:
        controller: The ``TrunkController``.
        channel: PCA9685 channel number.
        plan: The probe plan entry for this channel.
        delay: Seconds between one-degree steps.

    Returns:
        +1.0 if the motion matched the model's expectation, -1.0 if reversed.
    """
    name = _channel_name(channel)
    print(f"\n[{name}] DIRECTION check")
    print(f"  Sweeping {plan['low']} -> {plan['high']} deg (commanded angle increasing).")
    print(f"  The model EXPECTS: as the angle increases, {plan['expect_increase']}.")
    input("  Press Enter to move the joint (keep clear / power within reach)...")

    await _rest_channel(controller, channel, delay=delay)
    await _sweep(controller, channel, plan["low"], plan["high"], delay)

    matched = _prompt_yes_no(
        f"  Did the joint do this: '{plan['expect_increase']}'?", default=True
    )
    await _sweep(controller, channel, plan["high"], plan["low"], delay)

    if matched:
        print("  -> direction matches; sign stays as-is.")
        return 1.0
    print("  -> direction is REVERSED; sign will be flipped.")
    return -1.0


async def _calibrate_scale(controller, channel, plan, delay):
    """Estimates the joint's ``scale`` from a measured physical arc.

    Sweeps a known commanded range and asks the operator to measure the actual
    physical arc (degrees). ``scale = measured_arc / commanded_span``. Direct-
    drive joints should come out near 1.0; a geared joint comes out well below.

    Args:
        controller: The ``TrunkController``.
        channel: PCA9685 channel number.
        plan: The probe plan entry for this channel.
        delay: Seconds between one-degree steps.

    Returns:
        The measured scale as a positive float, or None if skipped.
    """
    name = _channel_name(channel)
    low = int(round(controller.clamp_angle(channel, plan["low"])))
    high = int(round(controller.clamp_angle(channel, plan["high"])))
    commanded_span = abs(high - low)

    print(f"\n[{name}] SCALE check")
    if commanded_span == 0:
        print("  Commanded span is zero after clamping; skipping scale (locked joint).")
        return None
    print(f"  I'll move {low} -> {high} deg (a {commanded_span} deg command).")
    print("  Measure the ACTUAL physical arc the joint travels (protractor / phone")
    print("  angle app / reference marks).")
    if not _prompt_yes_no("  Measure scale for this joint now?", default=True):
        print("  -> skipping scale (keeps the current value).")
        return None

    input("  Press Enter to move the joint...")
    await _rest_channel(controller, channel, delay=delay)
    await _sweep(controller, channel, low, high, delay)

    measured_arc = _prompt_float(
        f"  Measured physical arc in degrees over that {commanded_span} deg command"
    )
    await _sweep(controller, channel, high, low, delay)

    scale = abs(measured_arc) / commanded_span
    print(f"  -> measured scale = {measured_arc} / {commanded_span} = {scale:.4f}")
    return scale


async def _calibrate_offset(controller, channel, plan, current_offset, delay):
    """Confirms or updates the joint's ``offset_deg`` (zero-landmark).

    Drives the servo to the currently-stored ``offset_deg`` so you can SEE
    whether that commanded angle actually places the joint at its zero-landmark
    (e.g. head straight forward). You can then accept it, jog the joint live in
    small steps until it looks right and capture that angle, or type an exact
    angle. This avoids confusing the stored calibration value with the servo's
    live commanded angle -- they are different things.

    Args:
        controller: The ``TrunkController`` (drives the joint to each candidate).
        channel: PCA9685 channel number.
        plan: The probe plan entry for this channel.
        current_offset: The offset currently stored for this joint.
        delay: Seconds between one-degree steps when driving.

    Returns:
        The confirmed/updated offset in degrees.
    """
    name = _channel_name(channel)
    print(f"\n[{name}] OFFSET (zero-landmark) check")
    print(f"  The zero-landmark is the servo angle where: {plan['zero_landmark_desc']}.")
    print(f"  Stored offset_deg = {current_offset} (this is the CONFIG value, not a")
    print("  live servo reading). I'll drive the joint to that angle so you can see")
    print("  whether it actually lands on the zero-landmark.")

    candidate = int(round(controller.clamp_angle(channel, current_offset)))
    await _sweep(controller, channel, candidate, candidate, delay)
    live = controller.kit.servo[channel].angle
    print(f"  Joint is now commanded to {candidate} deg (servo reads {live}).")

    if _prompt_yes_no(f"  Does the joint sit at its zero-landmark ({plan['zero_landmark_desc']})?",
                      default=True):
        print(f"  -> keeping offset_deg = {current_offset}.")
        return current_offset

    # Live jog loop: nudge until it looks right, then capture the angle.
    print("  Jog the joint to the zero-landmark. Enter a step in degrees (e.g. 5")
    print("  or -5) to nudge, a bare number prefixed with '=' to jump to an exact")
    print("  angle (e.g. =92), or press Enter when it looks right to capture it.")
    position = candidate
    while True:
        reply = input(f"    [{name}] at {position} deg -> step / =angle / Enter to accept: ").strip()
        if not reply:
            print(f"  -> captured offset_deg = {position}.")
            return float(position)
        try:
            if reply.startswith("="):
                target = int(round(controller.clamp_angle(channel, float(reply[1:]))))
            else:
                target = int(round(controller.clamp_angle(channel, position + float(reply))))
        except ValueError:
            print("    enter a number like 5, -5, or =92 (or Enter to accept)")
            continue
        await _sweep(controller, channel, position, target, delay)
        position = target


async def calibrate_joint(controller, store, raw, channel, plan, delay, do_scale):
    """Runs the full sign/offset/scale calibration flow for one joint.

    Updates the ``raw`` calibration mapping in place (not written to disk here).

    Args:
        controller: The ``TrunkController``.
        store: The loaded ``Calibration_Store`` (for current values).
        raw: The mutable raw calibration dict (joint -> fields) to update.
        channel: PCA9685 channel number to calibrate.
        plan: The probe plan entry for this channel.
        delay: Seconds between one-degree steps.
        do_scale: Whether to run the (motion-heavy) scale measurement.

    Returns:
        True if the joint was calibrated, False if skipped.
    """
    name = _channel_name(channel)
    joint = _channel_to_joint(store, channel)
    if joint is None or joint not in raw:
        print(f"\n[{name}] not in the calibration store; skipping.")
        return False

    print("\n" + "=" * 68)
    print(f"Calibrating {name} (channel {channel}, URDF joint '{joint}')")
    print("=" * 68)
    if not _prompt_yes_no(f"Calibrate {name} now?", default=True):
        print(f"  skipping {name}.")
        return False

    current = raw[joint]
    current_sign = float(current.get("sign", 1.0))

    # 1) Direction -> sign. Flip whatever sign is stored if motion is reversed.
    observed_sign = await _calibrate_direction(controller, channel, plan, delay)
    new_sign = current_sign if observed_sign > 0 else -current_sign

    # 2) Offset (zero-landmark). Drives the joint to the candidate so you can see
    # it and jog to the true landmark if needed.
    new_offset = await _calibrate_offset(
        controller, channel, plan, float(current.get("offset_deg", 0.0)), delay
    )

    # 3) Scale (optional; motion-heavy).
    new_scale = float(current.get("scale", 1.0))
    if do_scale:
        measured = await _calibrate_scale(controller, channel, plan, delay)
        if measured is not None and measured > 0:
            new_scale = measured

    # Park the joint before moving on.
    await _rest_channel(controller, channel, delay=delay)

    print(f"\n[{name}] proposed calibration:")
    print(f"    sign:       {current_sign} -> {new_sign}")
    print(f"    offset_deg: {current.get('offset_deg')} -> {new_offset}")
    print(f"    scale:      {current.get('scale')} -> {new_scale:.4f}")
    if _prompt_yes_no("  Apply these values?", default=True):
        current["sign"] = new_sign
        current["offset_deg"] = new_offset
        current["scale"] = round(new_scale, 4)
        print(f"  -> {name} updated (not yet written to disk).")
        return True
    print(f"  -> {name} left unchanged.")
    return False


async def run(channels, calibration_path, delay, do_scale):
    """Runs the interactive calibration harness for the selected channels.

    Args:
        channels: Ordered list of PCA9685 channel numbers to calibrate.
        calibration_path: Path to the calibration JSON to read and update.
        delay: Seconds between one-degree servo steps.
        do_scale: Whether to run the scale-measurement step per joint.
    """
    # Import here (not at module top) so a --dry-run that set SERVO_SIM=1 takes
    # effect: trunkcontroller reads SERVO_SIM and builds the ServoKit on import.
    from trunkcontroller import SERVO_SIM, TrunkController

    if SERVO_SIM:
        print("SERVO_SIM=1 -> DRY RUN: no servo will physically move.\n")

    # Build the store (seeds + writes the file if missing) then load raw JSON.
    store = Calibration_Store(calibration_path)
    raw = _load_calibration_raw(calibration_path)

    controller = TrunkController("calibration-harness")

    print("\nSAFETY: one joint moves at a time, clamped to SAFE_LIMITS. Keep the")
    print("arm clear of the body and power within reach. Ctrl-C parks and exits.\n")

    changed = False
    try:
        # Park everything to a known rest state first.
        await controller.return_to_rest()

        for channel in channels:
            plan = _PROBE_PLAN.get(channel)
            if plan is None:
                print(f"\n[{_channel_name(channel)}] no probe plan; skipping.")
                continue
            if await calibrate_joint(controller, store, raw, channel, plan, delay, do_scale):
                changed = True
    except KeyboardInterrupt:
        print("\n[calibrate] interrupted; parking servos...")
    finally:
        try:
            await controller.return_to_rest()
        except Exception as error:  # noqa: BLE001 - never mask the original flow
            print(f"[calibrate] warning: could not fully park servos: {error}")

    if not changed:
        print("\n[calibrate] no changes made.")
        return

    if SERVO_SIM:
        print("\n[calibrate] DRY RUN complete; calibration file NOT modified.")
        print("            Re-run without SERVO_SIM to apply on hardware.")
        return

    if _prompt_yes_no("\nWrite the updated calibration to disk?", default=True):
        _backup_and_write(calibration_path, raw)
        print("\n[calibrate] Done. The collision model uses these values on its next")
        print("            run (Calibration_Store reloads the JSON). Sanity-check a")
        print("            known SAFE pose:")
        print("              PYTHONPATH=src .venv/bin/python -m kinematics.cli \\")
        print("                --pose '{\"0\":90,\"1\":90,\"4\":150,\"5\":5,\"6\":55,\"7\":0}'")
    else:
        print("\n[calibrate] changes discarded (nothing written).")


def main(argv=None):
    """CLI entry point for the calibration harness.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        prog="python -m calibrate_joints",
        description=(
            "Interactive per-joint calibration for the kinematic collision "
            "model. Confirms sign/offset/scale by driving one joint at a time "
            "and writes corrections back into the calibration JSON."
        ),
    )
    parser.add_argument(
        "--channels", type=int, nargs="+", metavar="CH",
        help="Servo channels to calibrate (default: all, neck-first order).",
    )
    parser.add_argument(
        "--calibration", default=_DEFAULT_CALIBRATION, metavar="PATH",
        help=f"Calibration JSON path (default: {_DEFAULT_CALIBRATION}).",
    )
    parser.add_argument(
        "--delay", type=float, default=0.02, metavar="SECONDS",
        help="Seconds between each one-degree servo step (default: 0.02).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Force simulation (no hardware writes). Same as SERVO_SIM=1.",
    )
    parser.add_argument(
        "--no-scale", action="store_true",
        help="Skip the motion-heavy scale-measurement step (sign+offset only).",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        os.environ["SERVO_SIM"] = "1"

    channels = args.channels if args.channels else list(_JOINT_ORDER)

    asyncio.run(
        run(
            channels=channels,
            calibration_path=args.calibration,
            delay=args.delay,
            do_scale=not args.no_scale,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
