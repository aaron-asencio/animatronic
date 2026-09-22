"""Integration test: the ``brains`` performance's combined extreme poses are SAFE.

The ``brains`` routine drives ``menacing_reach`` (right arm, channels 4-7)
concurrently with ``look_around_random`` (neck pan/tilt, channels 0-1). The two
gestures own disjoint channel sets and run at the same time, so the physically
realizable pose space is the Cartesian product of the arm's extremes and the
neck's extremes. This test feeds every such COMBINED extreme to the offline
kinematic collision model (``CollisionModel.is_pose_safe``) and asserts each
combination is SAFE -- i.e. running the arm's menacing swing at the same instant
the head is at any scan corner never produces a self-collision.

This is an example/integration test (no Hypothesis), kept separate from the fast
property tests in ``tests/test_performance.py`` so the slow URDF-backed model
build isn't tangled with the PBTs. It mirrors the import / sys.path / module-scope
model conventions of ``tests/test_collision.py``. Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_brains_collision.py -q --maxfail=1
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


# --- Arm (menacing_reach) extreme poses --------------------------------------
#
# The reach pose holds elbow_tilt=0, elbow_rotator=0 while the menace swing
# drives RT_SHOULDER_TILT across its centering-transition band extremes
# {26, 43, 60} (LT=26 / CENTER=43 / RT=60, the three logical swing positions)
# AND layers a smooth shoulder-rotator jitter of _MR_ROT_OUT (209) +/- up to 4,
# i.e. rotator extremes {205, 209, 213}. Endpoint jitter (+/- ~4 deg) is clamped
# back into the [26, 60] band, so the extreme set still bounds every reachable
# pose. Every combination of a tilt extreme with a rotator extreme is physically
# realizable during a swing, so both axes are enumerated. See src/movements.py
# _MR_ROT_OUT / _MR_TILT_CENTER_ANGLE / _MR_TILT_HALF_RANGE /
# _MR_FOREARM_OUT / _MR_ELBOW_OUT.
_MR_FOREARM_OUT = 0
_MR_ELBOW_OUT = 0
_ARM_TILT_EXTREMES = (26, 43, 60)
_ARM_ROT_EXTREMES = (205, 209, 213)

_ARM_POSES = [
    {
        constants.RT_SHOULDER_ROTATOR: rot,
        constants.RT_SHOULDER_TILT: tilt,
        constants.RT_ELBOW_TILT: _MR_ELBOW_OUT,
        constants.RT_ELBOW_ROTATOR: _MR_FOREARM_OUT,
    }
    for tilt in _ARM_TILT_EXTREMES
    for rot in _ARM_ROT_EXTREMES
]

# --- Neck (look_around_random) extreme poses ---------------------------------
#
# Scan bounds: NECK_PAN in [40, 140], NECK_TILT in [85, 115]. The extremes are
# the four corners of that box plus the neutral center (90, 90). See
# src/movements.py _LOOK_PAN_* / _LOOK_TILT_*.
_NECK_POSES = [
    {constants.NECK_PAN: pan, constants.NECK_TILT: tilt}
    for pan in (40, 140)
    for tilt in (85, 115)
] + [{constants.NECK_PAN: 90, constants.NECK_TILT: 90}]

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
        A compact id string, e.g. "pan40_tilt85_shoulderTilt25".
    """
    return (
        f"pan{pose[constants.NECK_PAN]}"
        f"_neckTilt{pose[constants.NECK_TILT]}"
        f"_shTilt{pose[constants.RT_SHOULDER_TILT]}"
        f"_shRot{pose[constants.RT_SHOULDER_ROTATOR]}"
    )


@pytest.mark.parametrize("pose", _COMBINATIONS, ids=[_pose_id(p) for p in _COMBINATIONS])
def test_brains_combined_extreme_poses_safe(pose):
    """Feature: audio-synced-concurrent-gestures — combined extreme poses are SAFE.

    Feeds each COMBINED extreme of the ``brains`` performance -- an arm
    (menacing_reach) extreme pose merged with a neck (look_around_random) extreme
    corner/center pose -- to the offline collision model and asserts the model
    classifies it SAFE. Because the arm and neck gestures run concurrently on
    disjoint channels, the arm can be at any swing extreme while the head is at
    any scan corner; none of those simultaneous extremes may self-collide.

    On failure the offending pose and the model's colliding link/joint pairs are
    included in the message so a real collision risk in the brains choreography
    is actionable (do NOT weaken this test to make it pass).

    Validates: Requirements 10.2, 10.3
    """
    result = _MODEL.is_pose_safe(pose)

    assert result.ok, (
        f"combined extreme pose reported UNSAFE by the collision model: {pose}; "
        f"colliding_pairs={result.colliding_pairs}"
    )
