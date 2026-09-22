"""Integration test: the ``blah`` performance's combined extreme poses are SAFE.

The ``blah`` routine drives ``present_palm`` (right arm, channels 4-7)
concurrently with the randomized head shake ``shake_no`` (neck pan/tilt,
channels 0-1). The two gestures own disjoint channel sets and run at the same
time, so the physically realizable pose space is the Cartesian product of the
arm's extremes and the neck's extremes. This test feeds every such COMBINED
extreme to the offline kinematic collision model (``CollisionModel.is_pose_safe``)
and asserts each combination is SAFE -- i.e. presenting the palm-up bob at the
same instant the head is at any shake extreme never produces a self-collision.

This is an example/integration test (no Hypothesis), kept separate from the fast
property tests in ``tests/test_performance.py`` so the slow URDF-backed model
build isn't tangled with the PBTs. It mirrors the import / sys.path / module-scope
model conventions of ``tests/test_brains_collision.py``. Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_blah_collision.py -q --maxfail=1
"""

import os
import sys

import pytest

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import constants  # noqa: E402
from kinematics.model import CollisionModel  # noqa: E402

# Absolute paths resolved relative to the repo root: the model loads the real
# Maximus URDF + calibration fixtures, which is slow, so build it ONCE at module
# scope and reuse it across every parametrized combination.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_URDF_PATH = os.path.join(_REPO_ROOT, "src", "config", "maximus.urdf")
_CALIBRATION_PATH = os.path.join(_REPO_ROOT, "src", "config", "calibration.json")

_MODEL = CollisionModel(urdf_path=_URDF_PATH, calibration_path=_CALIBRATION_PATH)


# --- Arm (present_palm) extreme poses ----------------------------------------
#
# The present pose holds shoulder_rotator=35, shoulder_tilt=55, elbow_rotator=235
# (palm up, inward) while the bob drives RT_ELBOW_TILT across its band
# extremes {85, 120, 155} (85/155 = bob span endpoints, 120 = raised center).
# Every one of those elbow-tilt values is realizable during a bob, so all three
# are enumerated. See src/movements.py _PP_ROT_UP / _PP_TILT_REST /
# _PP_FOREARM_UP / _PP_ELBOW_UP / _PP_BOB_LO / _PP_BOB_HI.
_PP_ROT_UP = 35
_PP_TILT_REST = 55
_PP_FOREARM_UP = 235
_ARM_ELBOW_TILT_EXTREMES = (85, 120, 155)

_ARM_POSES = [
    {
        constants.RT_SHOULDER_ROTATOR: _PP_ROT_UP,
        constants.RT_SHOULDER_TILT: _PP_TILT_REST,
        constants.RT_ELBOW_TILT: elbow_tilt,
        constants.RT_ELBOW_ROTATOR: _PP_FOREARM_UP,
    }
    for elbow_tilt in _ARM_ELBOW_TILT_EXTREMES
]

# --- Neck (shake_no) extreme poses -------------------------------------------
#
# The randomized shake sweeps NECK_PAN across [25, 155] (right extreme ~[25,35],
# left extreme ~[145,155]) with NECK_TILT held at center (90). The extremes are
# the widest pan endpoints plus the neutral center. See src/movements.py
# _SN_RIGHT_* / _SN_LEFT_*.
_NECK_POSES = [
    {constants.NECK_PAN: pan, constants.NECK_TILT: 90}
    for pan in (25, 90, 155)
]

# Cartesian product of arm x neck extremes: every combined pose the two
# concurrent gestures can realize at their extremes simultaneously.
_COMBINATIONS = [
    {**arm, **neck} for arm in _ARM_POSES for neck in _NECK_POSES
]


def _pose_id(pose):
    """Builds a readable pytest id from a combined servo pose.

    Args:
        pose: A servo-channel -> angle dict covering all required channels.

    Returns:
        A compact id string, e.g. "pan25_neckTilt90_elbowTilt71".
    """
    return (
        f"pan{pose[constants.NECK_PAN]}"
        f"_neckTilt{pose[constants.NECK_TILT]}"
        f"_elbowTilt{pose[constants.RT_ELBOW_TILT]}"
        f"_elbowRot{pose[constants.RT_ELBOW_ROTATOR]}"
    )


@pytest.mark.parametrize("pose", _COMBINATIONS, ids=[_pose_id(p) for p in _COMBINATIONS])
def test_blah_combined_extreme_poses_safe(pose):
    """Feature: blah refinement — present_palm + head shake combined poses SAFE.

    Feeds each COMBINED extreme of the ``blah`` performance -- an arm
    (present_palm) extreme pose merged with a neck (shake_no) extreme pan
    endpoint/center pose -- to the offline collision model and asserts the model
    classifies it SAFE. Because the arm and neck gestures run concurrently on
    disjoint channels, the forearm can be at any bob extreme while the head is at
    any shake endpoint; none of those simultaneous extremes may self-collide.

    On failure the offending pose and the model's colliding link/joint pairs are
    included in the message so a real collision risk in the blah choreography is
    actionable (do NOT weaken this test to make it pass).
    """
    result = _MODEL.is_pose_safe(pose)

    assert result.ok, (
        f"combined extreme pose reported UNSAFE by the collision model: {pose}; "
        f"colliding_pairs={result.colliding_pairs}"
    )
