"""Interactive boundary-probe harness for validating the collision model.

Validates that the calibrated 3D collision model flags a self-collision AT or
BEFORE the arm/head physically touches. You pick a base pose and one joint to
"probe": the harness steps that joint in small increments toward a suspected
collision, and at EACH step:

  1. Computes the model's verdict for the resulting pose FIRST.
  2. Shows you SAFE / COLLISION (with the offending link pair).
  3. Moves the servo one step (clamped to SAFE_LIMITS).
  4. Asks what you SAW: keep going, or "contact!" (parts just touched).

It records the step where the MODEL first flagged a collision and the step
where YOU observed physical contact, then reports whether the model flagged at
or before contact (the property a conservative safety model must satisfy). Any
pose where model and reality disagree is appended to a log.

The model leads, the hardware follows, and you can stop at any prompt. The
probe joint is returned to its start position on exit / abort / error.

SAFETY
------
Runs as root (I2C/GPIO) and moves a real joint toward a collision on purpose.
Precautions:

  - Only ONE joint moves; every other joint holds the base pose.
  - Every write clamps to ``constants.SAFE_LIMITS`` (no mechanical jam).
  - Small default step (2 deg) with a confirm prompt each step; Enter advances,
    'c' marks contact and STOPS, 'q' aborts and parks.
  - The probe joint returns to its start angle on exit/abort/error.
  - Keep a hand on the power. Dry-run first with SERVO_SIM=1 to rehearse.

USAGE
-----
    # Dry run (rehearse the flow, no hardware writes):
    SERVO_SIM=1 PYTHONPATH=src .venv/bin/python -m probe_collision \
        --base '{"0":90,"1":90,"4":150,"5":5,"6":170,"7":0}' \
        --probe-channel 7 --toward 270 --dry-run

    # Live probe of the shoulder rotator toward "arm up", base = arm out:
    sudo PYTHONPATH=src .venv/bin/python -m probe_collision \
        --base '{"0":90,"1":90,"4":150,"5":5,"6":170,"7":0}' \
        --probe-channel 7 --toward 270

Like calibrate_joints.py, this lives at src/ level (touches hardware) and keeps
the kinematics package hardware-free.
"""

import argparse
import asyncio
import json
import os

import constants
from validate_hardware import boundary_probe_report, compare_verdict, log_disagreement


# Default disagreement log (append-only; JSON lines).
_DEFAULT_LOG = "src/config/probe_disagreements.log"

# Default degrees per probe step and pause between the one-degree sub-steps that
# make up each probe step (motion stays gentle).
_DEFAULT_STEP_DEG = 2
_SUBSTEP_DELAY = 0.02

# Servo electrical actuation range. Even in --allow-beyond-safe mode we NEVER
# drive outside this: the override only relaxes SAFE_LIMITS, not the hardware's
# physical 0-270 range (past which the servo stalls against a stop).
_SERVO_MIN = 0
_SERVO_MAX = 270


def _effective_clamp(controller, channel, angle, override_range):
    """Clamps an angle to the override window if given, else to SAFE_LIMITS.

    When ``override_range`` is provided the angle is clamped to that window
    (intersected with the servo electrical range 0-270), deliberately allowing
    travel BEYOND ``SAFE_LIMITS`` for supervised boundary discovery. When it is
    None this is exactly ``controller.clamp_angle`` (SAFE_LIMITS).

    Args:
        controller: The ``TrunkController`` (for its SAFE_LIMITS clamp).
        channel: PCA9685 channel number.
        angle: Requested angle in degrees.
        override_range: Optional ``(min_deg, max_deg)`` override window, or None.

    Returns:
        The clamped angle.
    """
    if override_range is None:
        return controller.clamp_angle(channel, angle)
    lo = max(_SERVO_MIN, min(override_range))
    hi = min(_SERVO_MAX, max(override_range))
    return max(lo, min(hi, angle))


def _write_angle(controller, channel, angle, override_range):
    """Writes one servo angle, honoring the override window if present.

    In normal mode this defers to ``controller.set_angle`` (which clamps to
    SAFE_LIMITS). In override mode it writes directly to the servo after
    clamping to the override window and the electrical range -- the ONLY place
    that intentionally bypasses SAFE_LIMITS, and only under the operator's
    typed confirmation in :func:`run`.

    Args:
        controller: The ``TrunkController``.
        channel: PCA9685 channel number.
        angle: Requested angle in degrees.
        override_range: Optional ``(min_deg, max_deg)`` override window, or None.

    Returns:
        The angle actually written.
    """
    if override_range is None:
        return controller.set_angle(channel, angle)
    safe = _effective_clamp(controller, channel, angle, override_range)
    controller.kit.servo[channel].angle = safe
    return safe


def _beyond_safe(channel, angle):
    """Returns whether an angle is outside the channel's normal SAFE_LIMITS.

    Args:
        channel: PCA9685 channel number.
        angle: Angle in degrees.

    Returns:
        True if the angle is below the SAFE_LIMITS min or above its max.
    """
    lo, hi = constants.SAFE_LIMITS.get(channel, (_SERVO_MIN, _SERVO_MAX))
    return angle < lo or angle > hi


def _parse_base_pose(raw):
    """Parses the base-pose JSON (channel string -> degrees) into int channels.

    Args:
        raw: A JSON object string mapping servo channel (as string keys) to
            angle in degrees.

    Returns:
        A dict mapping int channel to numeric angle.

    Raises:
        ValueError: If the JSON is not an object or a key is not an integer.
    """
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("base pose must be a JSON object of channel -> degrees")
    pose = {}
    for key, value in decoded.items():
        pose[int(key)] = value
    return pose


def _channel_name(channel):
    """Returns the human-readable servo name for a channel.

    Args:
        channel: PCA9685 channel number.

    Returns:
        The name from ``constants.servos`` or a ``chN`` fallback.
    """
    return constants.servos.get(channel, f"ch{channel}")


def _verdict_line(result):
    """Formats a model PoseResult as a short verdict string.

    Args:
        result: A ``PoseResult`` from ``CollisionModel.is_pose_safe``.

    Returns:
        ``"SAFE"`` or a ``"COLLISION: a <-> b; ..."`` summary line.
    """
    if result.ok:
        return "SAFE"
    pairs = "; ".join(f"{p.link_a} <-> {p.link_b}" for p in result.colliding_pairs)
    return f"COLLISION: {pairs}"


def _prompt_step(name, angle):
    """Prompts the operator at a probe step and returns their choice.

    Args:
        name: The probe joint's human-readable name.
        angle: The servo angle just commanded.

    Returns:
        One of ``"advance"``, ``"contact"``, or ``"abort"``.
    """
    while True:
        reply = input(
            f"    [{name} @ {angle} deg] Enter=advance, 'c'=contact now, 'q'=quit: "
        ).strip().lower()
        if reply in ("", "a", "advance"):
            return "advance"
        if reply in ("c", "contact"):
            return "contact"
        if reply in ("q", "quit", "abort"):
            return "abort"
        print("      please press Enter, or type 'c' or 'q'")


def _probe_angles(controller, channel, start, toward, step_deg, override_range):
    """Builds the ordered list of probe servo angles from start toward a target.

    Endpoints are clamped to the effective range (SAFE_LIMITS, or the override
    window when given) so the probe never generates steps it cannot actually
    drive -- which is what previously produced a wall of redundant CLAMPED
    lines when ``--toward`` ran past SAFE_LIMITS.

    Args:
        controller: The ``TrunkController`` (for clamping).
        channel: PCA9685 channel number.
        start: Starting servo angle (from the base pose).
        toward: Target servo angle to approach.
        step_deg: Positive step magnitude in degrees.
        override_range: Optional ``(min_deg, max_deg)`` override window, or None.

    Returns:
        A list of integer angles from the clamped start to the clamped target
        inclusive, in ``step_deg`` increments (direction inferred).
    """
    start = int(round(_effective_clamp(controller, channel, start, override_range)))
    toward = int(round(_effective_clamp(controller, channel, toward, override_range)))
    if toward == start:
        return [start]
    direction = 1 if toward > start else -1
    step = direction * max(1, int(step_deg))
    angles = list(range(start, toward, step)) + [toward]
    return angles


async def _move_to(controller, channel, target, delay, override_range=None):
    """Moves one servo to ``target`` one degree at a time (clamped).

    Args:
        controller: The ``TrunkController``.
        channel: PCA9685 channel to move.
        target: Desired servo angle in degrees.
        delay: Seconds between one-degree steps.
        override_range: Optional ``(min_deg, max_deg)`` window allowing travel
            beyond SAFE_LIMITS (still clamped to the 0-270 electrical range).
            None uses the normal SAFE_LIMITS clamp.
    """
    current = controller.kit.servo[channel].angle
    target = int(round(_effective_clamp(controller, channel, target, override_range)))
    if current is None:
        _write_angle(controller, channel, target, override_range)
        return
    current = int(round(current))
    step = 1 if target >= current else -1
    for angle in range(current, target + step, step):
        _write_angle(controller, channel, angle, override_range)
        await asyncio.sleep(delay)


async def run(base_pose, probe_channel, toward, step_deg, calibration_path,
              urdf_path, margin, log_path, override_range=None):
    """Runs the interactive boundary probe for one joint.

    Args:
        base_pose: Dict of channel -> degrees held constant except the probe.
        probe_channel: PCA9685 channel to step toward a collision.
        toward: Target servo angle the probe approaches.
        step_deg: Degrees per probe step.
        calibration_path: Path to the calibration JSON.
        urdf_path: Path to the URDF.
        margin: Proxy inflation margin (meters); None uses the model default.
        log_path: Where to append model/reality disagreements.
        override_range: Optional ``(min_deg, max_deg)`` window that lets the
            probe joint travel BEYOND its SAFE_LIMITS (still clamped to the
            0-270 electrical range) for supervised boundary discovery. Requires
            a typed confirmation below before any motion. None = normal mode.
    """
    # Imported here so a --dry-run that set SERVO_SIM=1 takes effect before the
    # ServoKit is constructed at import time.
    from trunkcontroller import SERVO_SIM, TrunkController
    from kinematics.model import CollisionModel, PoseInputError

    if SERVO_SIM:
        print("SERVO_SIM=1 -> DRY RUN: no servo will physically move.\n")

    model_kwargs = {"urdf_path": urdf_path, "calibration_path": calibration_path}
    if margin is not None:
        model_kwargs["inflation_margin"] = margin
    model = CollisionModel(**model_kwargs)

    name = _channel_name(probe_channel)
    if probe_channel not in base_pose:
        raise ValueError(
            f"probe channel {probe_channel} ({name}) must be in the base pose"
        )

    # Warn if --toward runs past the effective range (the annoyance fix): the
    # sweep will stop at the clamp, so say so once instead of spamming CLAMPED.
    safe_lo, safe_hi = constants.SAFE_LIMITS.get(probe_channel, (_SERVO_MIN, _SERVO_MAX))
    clamped_toward = _effective_clamp(controller=None, channel=probe_channel,
                                      angle=int(round(toward)),
                                      override_range=override_range) if override_range \
        else max(safe_lo, min(safe_hi, int(round(toward))))
    if clamped_toward != int(round(toward)):
        if override_range is None:
            print(f"NOTE: --toward {int(toward)} is outside {name} SAFE_LIMITS "
                  f"{(safe_lo, safe_hi)}; the sweep will stop at {clamped_toward}. "
                  f"Use --allow-beyond-safe MIN MAX to probe past the limit.")
        else:
            print(f"NOTE: --toward {int(toward)} is outside the override window "
                  f"{override_range}; the sweep will stop at {clamped_toward}.")

    start_angle = base_pose[probe_channel]

    controller = TrunkController("boundary-probe")

    angles = _probe_angles(controller, probe_channel, start_angle, toward,
                           step_deg, override_range)

    print(f"Boundary probe: {name} (ch {probe_channel}) from {int(start_angle)} "
          f"-> {angles[-1]} deg in ~{step_deg} deg steps ({len(angles)} steps).")
    print("Base pose (other joints held):")
    for ch, deg in sorted(base_pose.items()):
        tag = "  <-- probing" if ch == probe_channel else ""
        print(f"    ch{ch} {_channel_name(ch):20s} = {deg}{tag}")

    if override_range is None:
        print("\nSAFETY: only this joint moves; every write is clamped to SAFE_LIMITS.")
        print("At each step the MODEL verdict is shown BEFORE the move. Press 'c' the")
        print("instant parts touch, 'q' to abort. The joint parks on exit.\n")
        input("Press Enter to begin...")
    else:
        # Supervised limit-override: loud warning + typed confirmation. This is
        # the ONLY mode that can drive a joint into the body, on purpose, to find
        # the true collision boundary.
        print("\n" + "!" * 68)
        print(f"DANGER: --allow-beyond-safe is ON for {name}.")
        print(f"  Normal SAFE_LIMITS: {(safe_lo, safe_hi)}")
        print(f"  Override window:    {tuple(override_range)} (clamped to 0-270)")
        print("  This CAN drive the joint past its safe limit and into the body.")
        print("  It moves in small steps and shows the model verdict first, but YOU")
        print("  are the safety stop: press 'c' the instant parts touch, 'q' to abort,")
        print("  and keep a hand on the power. The joint parks on exit.")
        print("!" * 68)
        confirm = input('Type "YES" (all caps) to arm override mode, anything else cancels: ').strip()
        if confirm != "YES":
            print("[probe] override not confirmed; aborting without moving.")
            return
        print()

    first_collision_index = None   # step where the MODEL first says COLLISION
    contact_index = None           # step where YOU report physical contact
    aborted = False

    try:
        # Establish the full base pose on the hardware first (gentle moves).
        # Base joints always respect SAFE_LIMITS; only the probe joint may use
        # the override window.
        for ch, deg in base_pose.items():
            ov = override_range if ch == probe_channel else None
            await _move_to(controller, ch, deg, _SUBSTEP_DELAY, ov)

        for index, angle in enumerate(angles):
            pose = dict(base_pose)
            pose[probe_channel] = angle
            try:
                result = model.is_pose_safe(pose)
            except PoseInputError as error:
                print(f"  [model] invalid pose at step {index}: {error}")
                break

            verdict = _verdict_line(result)
            danger = "  [BEYOND SAFE_LIMITS]" if _beyond_safe(probe_channel, angle) else ""
            print(f"  step {index:2d}: {name}={angle:>3} deg -> MODEL {verdict}{danger}")
            if not result.ok and first_collision_index is None:
                first_collision_index = index
                print(f"    ^ model FIRST flags a collision at step {index}.")

            # Move the probe joint to this step's angle (override window applies
            # only to the probe joint).
            await _move_to(controller, probe_channel, angle, _SUBSTEP_DELAY, override_range)

            choice = _prompt_step(name, angle)
            if choice == "contact":
                contact_index = index
                print(f"    -> physical contact recorded at step {index}.")
                break
            if choice == "abort":
                aborted = True
                print("    -> aborted by operator.")
                break
    except KeyboardInterrupt:
        aborted = True
        print("\n[probe] interrupted.")
    finally:
        try:
            await _move_to(controller, probe_channel, start_angle, _SUBSTEP_DELAY,
                           override_range)
            print(f"[probe] returned {name} to its start ({int(start_angle)} deg).")
        except Exception as error:  # noqa: BLE001 - never mask the run outcome
            print(f"[probe] warning: could not park {name}: {error}")

    _report(base_pose, probe_channel, angles, first_collision_index,
            contact_index, aborted, log_path, SERVO_SIM)


def _report(base_pose, probe_channel, angles, first_collision_index,
            contact_index, aborted, log_path, sim):
    """Summarizes the probe outcome and logs any model/reality disagreement.

    Args:
        base_pose: The base pose used for the probe.
        probe_channel: The probed channel.
        angles: The ordered probe angles.
        first_collision_index: Step where the model first flagged, or None.
        contact_index: Step where the operator reported contact, or None.
        aborted: True if the operator aborted before contact.
        log_path: Path to append disagreements to.
        sim: Whether this was a dry run (no hardware, no logging).
    """
    name = _channel_name(probe_channel)
    print("\n" + "=" * 68)
    print(f"Boundary probe summary: {name} (ch {probe_channel})")
    print("=" * 68)

    model_flagged = first_collision_index is not None
    if model_flagged:
        print(f"  Model first flagged COLLISION at step {first_collision_index} "
              f"({name}={angles[first_collision_index]} deg).")
    else:
        print("  Model never flagged a collision over the probed range.")

    if contact_index is None:
        if aborted:
            print("  No physical contact was recorded (operator aborted).")
        else:
            print("  No physical contact was recorded (reached the target).")
        print("\n  -> No contact to compare against. If the model flagged but you")
        print("     saw NO contact across the whole sweep, that's a possible false")
        print("     positive (proxies too fat, or a calibration scale/offset off).")
        return

    print(f"  You reported physical CONTACT at step {contact_index} "
          f"({name}={angles[contact_index]} deg).")

    flagged_before = boundary_probe_report(first_collision_index, contact_index)
    if flagged_before:
        margin_steps = contact_index - first_collision_index
        print(f"\n  PASS: the model flagged at or before contact "
              f"(by {margin_steps} step(s)).")
        print("  That's the conservative behavior we want (predict slightly early).")
    else:
        print("\n  FAIL: the model did NOT flag before contact.")
        if model_flagged:
            print("  It flagged LATE (after parts already touched) -> unsafe: the")
            print("  proxies are too small or a calibration value places the arm")
            print("  short of reality. Increase --margin or re-check that joint.")
        else:
            print("  It never flagged at all -> the model misses this collision.")
            print("  Re-check the calibration for the involved joints, or the URDF")
            print("  geometry / proxy sizes for the colliding links.")

    # Record the disagreement (model safe at contact, but contact happened) so
    # patterns can be reviewed. At the contact step the model_ok is (contact
    # step < first flag) -> model said safe while reality collided.
    if sim:
        print("\n  [dry run] not logging (no hardware, no measurement).")
        return
    model_ok_at_contact = not (model_flagged and first_collision_index <= contact_index)
    verdict = compare_verdict(model_ok_at_contact, measured_collision=True)
    if verdict == "disagree":
        contact_pose = dict(base_pose)
        contact_pose[probe_channel] = angles[contact_index]
        log_disagreement(log_path, contact_pose, model_ok_at_contact, True)
        print(f"\n  Logged disagreement -> {log_path}")


def main(argv=None):
    """CLI entry point for the boundary-probe harness.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        prog="python -m probe_collision",
        description=(
            "Interactive boundary probe: step one joint toward a predicted "
            "collision and confirm the model flags at or before contact."
        ),
    )
    parser.add_argument(
        "--base", required=True, metavar="JSON",
        help='Base pose JSON, channel->degrees, e.g. \'{"0":90,...,"7":0}\'.',
    )
    parser.add_argument(
        "--probe-channel", type=int, required=True, metavar="CH",
        help="Servo channel to step toward the collision.",
    )
    parser.add_argument(
        "--toward", type=float, required=True, metavar="DEG",
        help="Servo angle the probe joint approaches.",
    )
    parser.add_argument(
        "--step", type=float, default=_DEFAULT_STEP_DEG, metavar="DEG",
        help=f"Degrees per probe step (default: {_DEFAULT_STEP_DEG}).",
    )
    parser.add_argument(
        "--calibration", default="src/config/calibration.json", metavar="PATH",
        help="Calibration JSON path.",
    )
    parser.add_argument(
        "--urdf", default="src/config/maximus.urdf", metavar="PATH",
        help="URDF path.",
    )
    parser.add_argument(
        "--margin", type=float, default=None, metavar="METERS",
        help="Proxy inflation margin override (meters).",
    )
    parser.add_argument(
        "--log", default=_DEFAULT_LOG, metavar="PATH",
        help=f"Disagreement log path (default: {_DEFAULT_LOG}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Force simulation (no hardware writes). Same as SERVO_SIM=1.",
    )
    parser.add_argument(
        "--allow-beyond-safe", type=float, nargs=2, metavar=("MIN", "MAX"),
        default=None,
        help=(
            "DANGER: let the probe joint travel beyond its SAFE_LIMITS within "
            "MIN..MAX (still clamped to 0-270) to find the true collision "
            "boundary. Requires a typed YES confirmation and supervision."
        ),
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        os.environ["SERVO_SIM"] = "1"

    base_pose = _parse_base_pose(args.base)
    override_range = tuple(args.allow_beyond_safe) if args.allow_beyond_safe else None

    asyncio.run(
        run(
            base_pose=base_pose,
            probe_channel=args.probe_channel,
            toward=args.toward,
            step_deg=args.step,
            calibration_path=args.calibration,
            urdf_path=args.urdf,
            margin=args.margin,
            log_path=args.log,
            override_range=override_range,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
