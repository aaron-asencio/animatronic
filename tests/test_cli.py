"""Example-based unit tests for the kinematic collision model's CLI.

Exercises ``src/kinematics/cli.py``'s ``main(argv)`` entry point directly,
capturing stdout/stderr with pytest's ``capsys`` fixture. These are
example-based tests (not property tests): they pin the documented CLI
behavior for a rest pose, a known elbow-flexed collision, malformed input,
and a mixed sequence.

To keep the tests independent of the current working directory and from
mutating the repo's checked-in calibration file, every ``main`` invocation
passes an absolute ``--urdf`` path and a throwaway ``--calibration`` path
under ``tmp_path``.

Run with:

    .venv/bin/python -m pytest tests/test_cli.py -q
"""

import json
import os
import sys

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.cli import main  # noqa: E402

# Absolute path to the shipped URDF so tests do not depend on the cwd.
_URDF_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "config", "maximus.urdf")
)

# A known-safe rest pose and a known elbow-flexed colliding pose (channel 5 =
# RT_ELBOW_TILT bent to 145 degrees, a right-angle flexion).
_REST_POSE = {"0": 90, "1": 90, "4": 150, "5": 5, "6": 55, "7": 0}
_COLLISION_POSE = {"0": 90, "1": 90, "4": 150, "5": 145, "6": 55, "7": 0}


def _base_argv(tmp_path):
    """Builds the shared argv prefix pinning the URDF and calibration paths.

    Args:
        tmp_path: pytest ``tmp_path`` fixture used for a throwaway calibration
            file so tests never touch the repo's checked-in calibration.

    Returns:
        A list of CLI arguments (URDF + calibration overrides) to prepend to
        the pose/sequence arguments under test.
    """
    calibration_path = str(tmp_path / "calibration.json")
    return ["--urdf", _URDF_PATH, "--calibration", calibration_path]


def test_rest_pose_prints_safe(tmp_path, capsys):
    """Requirement 7.1: a single safe pose prints SAFE and exits 0."""
    argv = _base_argv(tmp_path) + ["--pose", json.dumps(_REST_POSE)]

    code = main(argv)

    out = capsys.readouterr().out
    assert code == 0
    # Assert on the bare "SAFE" verdict line (not incidental debug output).
    assert "SAFE" in out.splitlines()


def test_known_collision_prints_pair_and_joints(tmp_path, capsys):
    """Requirement 7.3: a colliding pose names the link pair and offending joints."""
    argv = _base_argv(tmp_path) + ["--pose", json.dumps(_COLLISION_POSE)]

    code = main(argv)

    out = capsys.readouterr().out
    assert code == 1
    # Find the actual COLLISION verdict line (not incidental debug output).
    verdict = next(
        (line for line in out.splitlines() if line.startswith("COLLISION:")), None
    )
    assert verdict is not None
    assert "upper_arm_link" in verdict
    assert "lower_arm_link" in verdict
    # The offending joints must include one of the elbow servos.
    assert "RT_ELBOW_TILT" in verdict or "RT_ELBOW_ROTATOR" in verdict


def test_malformed_json_prints_format_message_nonzero(tmp_path, capsys):
    """Requirement 7.4: unparseable input prints the expected format on stderr."""
    argv = _base_argv(tmp_path) + ["--pose", "not json"]

    code = main(argv)

    err = capsys.readouterr().err
    assert code != 0
    # The format help describes a JSON object of channel -> degrees.
    assert "JSON" in err
    assert "channel" in err or "object" in err or "degrees" in err


def test_sequence_with_one_collision_reports_unsafe(tmp_path, capsys):
    """Requirement 7.2 / 7.5: an ordered sequence prints per-pose verdicts.

    A sequence containing one safe pose followed by one colliding pose prints
    a SAFE verdict then a COLLISION verdict (order preserved) and reports the
    sequence as unsafe (exit 1).
    """
    sequence_path = tmp_path / "sequence.json"
    sequence_path.write_text(
        json.dumps([_REST_POSE, _COLLISION_POSE]), encoding="utf-8"
    )
    argv = _base_argv(tmp_path) + ["--sequence", str(sequence_path)]

    code = main(argv)

    out = capsys.readouterr().out
    assert code == 1

    # Isolate the verdict lines from the module's diagnostic ``print`` output:
    # a SAFE verdict is the bare line "SAFE"; a COLLISION verdict line starts
    # with "COLLISION:". (Debug lines like "[collision] COLLISION ..." are
    # excluded so we assert on the real verdicts, in order.)
    verdicts = [
        line
        for line in out.splitlines()
        if line == "SAFE" or line.startswith("COLLISION:")
    ]
    assert verdicts == ["SAFE"] + [
        line for line in verdicts if line.startswith("COLLISION:")
    ]
    assert verdicts[0] == "SAFE"
    assert any(line.startswith("COLLISION:") for line in verdicts[1:])


def test_no_poses_returns_nonzero(tmp_path, capsys):
    """Requirement 7.4: invoking with no poses prints the format help on stderr."""
    argv = _base_argv(tmp_path)

    code = main(argv)

    err = capsys.readouterr().err
    assert code != 0
    assert "JSON" in err
