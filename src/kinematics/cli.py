"""Command-line authoring aid for the kinematic collision model.

Classifies a servo pose (or an ordered sequence of poses) as SAFE or COLLISION
and prints one verdict line per pose in order. Intended to be run as a module::

    python -m kinematics.cli --pose '{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}'
    python -m kinematics.cli --pose '{...}' --pose '{...}'
    python -m kinematics.cli --sequence poses.json
    python -m kinematics.cli --pose '{...}' --preview

Poses are inline JSON objects mapping servo channel (int, given as a JSON string
key) to angle in degrees. ``--sequence`` reads a JSON file containing a list of
such objects. The overall process exit code is non-zero when any pose collides
or when the input cannot be parsed (Requirements 7.1-7.5).

This module imports no hardware libraries; ``preview.py`` is imported lazily and
only when ``--preview`` is passed, so verdicts still work headless (Requirement
8.3).
"""

import argparse
import json
import sys

from kinematics.model import CollisionModel, PoseInputError

_FORMAT_HELP = (
    "Expected a JSON object mapping servo channel (int) -> degrees, e.g. "
    '\'{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}\', or a JSON list of such '
    "objects (via --sequence)."
)


def _parse_pose(raw):
    """Parses a single inline JSON pose string into a channel -> degrees dict.

    Args:
        raw: A JSON string encoding an object whose keys are servo channel
            numbers (as strings) and whose values are angles in degrees.

    Returns:
        A dict mapping int servo channel to its numeric angle in degrees.

    Raises:
        json.JSONDecodeError: If ``raw`` is not valid JSON.
        PoseInputError: If the decoded value is not a JSON object or a channel
            key is not an integer.
    """
    decoded = json.loads(raw)
    return _coerce_pose(decoded)


def _coerce_pose(decoded):
    """Coerces a decoded JSON value into a channel -> degrees dict.

    Args:
        decoded: A value already parsed from JSON (expected to be a dict).

    Returns:
        A dict mapping int servo channel to its angle in degrees. Angle values
        are passed through unchanged so the model performs numeric validation.

    Raises:
        PoseInputError: If ``decoded`` is not an object or a key is not an
            integer channel.
    """
    if not isinstance(decoded, dict):
        raise PoseInputError("each pose must be a JSON object of channel -> degrees")

    pose = {}
    for key, value in decoded.items():
        try:
            channel = int(key)
        except (TypeError, ValueError):
            raise PoseInputError(f"pose channel key {key!r} is not an integer")
        pose[channel] = value
    return pose


def _load_sequence(path):
    """Loads a JSON file containing a list of pose objects.

    Args:
        path: Filesystem path to a JSON file holding a list of pose objects.

    Returns:
        A list of channel -> degrees dicts, in file order.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        PoseInputError: If the top-level value is not a list, or any element is
            not a valid pose object.
    """
    with open(path, "r", encoding="utf-8") as handle:
        decoded = json.load(handle)

    if not isinstance(decoded, list):
        raise PoseInputError("--sequence file must contain a JSON list of pose objects")

    return [_coerce_pose(element) for element in decoded]


def _format_collision(pair):
    """Formats a colliding link pair as a single COLLISION verdict line.

    Args:
        pair: A ``CollisionPair`` naming the two links and their offending
            joints.

    Returns:
        A parseable one-line string of the form
        ``COLLISION: <link_a> <-> <link_b> (joints: <servo_name>/<urdf_joint>, ...)``.
    """
    joints = ", ".join(
        f"{joint.servo_name}/{joint.urdf_joint}" for joint in pair.offending_joints
    )
    return f"COLLISION: {pair.link_a} <-> {pair.link_b} (joints: {joints})"


def _print_verdicts(per_pose):
    """Prints one verdict block per pose, in order.

    A safe pose prints ``SAFE``; a colliding pose prints one ``COLLISION:`` line
    per colliding pair so each pose's verdict is clearly delimited.

    Args:
        per_pose: Ordered list of ``PoseResult`` objects.
    """
    for result in per_pose:
        if result.ok:
            print("SAFE")
        else:
            for pair in result.colliding_pairs:
                print(_format_collision(pair))


def _run_preview(model, poses):
    """Lazily imports and runs the optional 3D preview.

    The import is performed here (not at module top level) so verdicts work
    headless and without the optional preview extras (Requirement 8.3).

    Args:
        model: The initialized ``CollisionModel``.
        poses: The collected list of poses to preview.
    """
    try:
        from kinematics import preview
    except ImportError as error:
        print(
            f"[cli] --preview unavailable: {error}. Verdicts above are unaffected.",
            file=sys.stderr,
        )
        return

    preview.show(model, poses)


def main(argv=None):
    """CLI entry point: classify poses and print SAFE/COLLISION verdicts.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). Passing it
            explicitly keeps ``main`` unit-testable.

    Returns:
        Process exit code: 0 when every pose is safe, 1 when any pose collides
        or the input cannot be parsed.
    """
    parser = argparse.ArgumentParser(
        prog="python -m kinematics.cli",
        description="Classify a servo pose (or sequence) as SAFE or COLLISION.",
    )
    parser.add_argument(
        "--pose",
        action="append",
        default=[],
        metavar="JSON",
        help="Inline JSON object mapping servo channel -> degrees. Repeatable.",
    )
    parser.add_argument(
        "--sequence",
        metavar="PATH",
        help="Path to a JSON file containing a list of pose objects.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Render an optional 3D preview (lazily imported; headless-safe).",
    )
    parser.add_argument(
        "--urdf",
        metavar="PATH",
        help="Override the URDF path (default: src/config/maximus.urdf).",
    )
    parser.add_argument(
        "--calibration",
        metavar="PATH",
        help="Override the calibration JSON path (default: src/config/calibration.json).",
    )
    parser.add_argument(
        "--margin",
        type=float,
        metavar="METERS",
        help="Override the proxy inflation margin in meters (default: model default).",
    )
    args = parser.parse_args(argv)

    # Collect poses: --pose values (in order), then --sequence poses (in order).
    try:
        poses = [_parse_pose(raw) for raw in args.pose]
        if args.sequence:
            poses.extend(_load_sequence(args.sequence))
    except (json.JSONDecodeError, PoseInputError, FileNotFoundError) as error:
        print(f"[cli] could not parse input: {error}", file=sys.stderr)
        print(_FORMAT_HELP, file=sys.stderr)
        return 1

    if not poses:
        print("[cli] no poses given: pass at least one --pose or a --sequence.", file=sys.stderr)
        print(_FORMAT_HELP, file=sys.stderr)
        return 1

    # Build the model, passing through only the overrides that were provided.
    model_kwargs = {}
    if args.urdf is not None:
        model_kwargs["urdf_path"] = args.urdf
    if args.calibration is not None:
        model_kwargs["calibration_path"] = args.calibration
    if args.margin is not None:
        model_kwargs["inflation_margin"] = args.margin

    model = CollisionModel(**model_kwargs)

    # Classify all poses; a missing channel / non-numeric angle is a parse-level
    # input error described with the expected format (Requirement 7.4).
    try:
        result = model.check_sequence(poses)
    except PoseInputError as error:
        print(f"[cli] invalid pose: {error}", file=sys.stderr)
        print(_FORMAT_HELP, file=sys.stderr)
        return 1

    _print_verdicts(result.per_pose)

    if args.preview:
        _run_preview(model, poses)

    return 1 if result.unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
