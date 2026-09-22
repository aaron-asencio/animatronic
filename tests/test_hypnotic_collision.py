"""Integration test: the ``hypnotic`` performance's combined extreme poses are SAFE.

The ``hypnotic`` routine drives ``hypnotic_arm`` (right arm, channels 4-7)
concurrently with the gentle limited head sway ``hyp_head_sway`` (neck pan/tilt,
channels 0-1). The two gestures own disjoint channel sets and run at the same
time, so the physically realizable pose space is the Cartesian product of the
arm's extremes and the neck's extremes. This test feeds every such COMBINED
extreme to the offline kinematic collision model (``CollisionModel.is_pose_safe``)
and asserts each combination is SAFE.

This is the KEY safety gate for the new hypnotic arm pose: shoulder tilt drops to
0 (below the global SAFE_LIMITS floor) and the shoulder rotator holds ~230 (within
the hand-to-face rotator danger band 210-270). That combination is only safe
because the elbow tilt stays at 0 (far below the 150 flexion floor), so the
hand-to-face FORBIDDEN_COMBINATION never triggers. This test proves it across
every combined extreme -- especially tilt=0 / rotator=234.

This is an example/integration test (no Hypothesis), kept separate from the fast
property tests in ``tests/test_performance.py`` so the slow URDF-backed model
build isn't tangled with the PBTs. It mirrors the import / sys.path / module-scope
model conventions of ``tests/test_blah_collision.py``. Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_hypnotic_collision.py -q --maxfail=1
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


# --- Arm (hypnotic_arm) extreme poses ----------------------------------------
#
# The hypnotic hold pose keeps elbow_rotator=0 and elbow_tilt=0 (arm straight)
# while the sway drives RT_SHOULDER_TILT across its band extremes {0, 15, 30}
# (0/30 = sway span endpoints, 15 = center) and the rotator jitters across
# {226, 230, 234} (230 +/- 4). Every one of those combinations is realizable
# during a sway, so all are enumerated. See src/movements.py _HYP_ROT_OUT /
# _HYP_TILT_CENTER / _HYP_TILT_HALF_RANGE / _HYP_ELBOW_OUT / _HYP_FOREARM_OUT.
_ARM_SHOULDER_TILT_EXTREMES = (0, 15, 30)
_ARM_SHOULDER_ROTATOR_EXTREMES = (226, 230, 234)
_HYP_ELBOW_TILT = 0
_HYP_ELBOW_ROTATOR = 0

_ARM_POSES = [
    {
        constants.RT_SHOULDER_ROTATOR: rotator,
        constants.RT_SHOULDER_TILT: tilt,
        constants.RT_ELBOW_TILT: _HYP_ELBOW_TILT,
        constants.RT_ELBOW_ROTATOR: _HYP_ELBOW_ROTATOR,
    }
    for tilt in _ARM_SHOULDER_TILT_EXTREMES
    for rotator in _ARM_SHOULDER_ROTATOR_EXTREMES
]

# --- Neck (hyp_head_sway) extreme poses --------------------------------------
#
# The gentle hypnotic sway roams pan in [80, 100] and tilt in [80, 100] (center
# 90 +/- 10). The extremes plus the center are enumerated on both axes. See
# src/movements.py _HYP_PAN_MIN/MAX and _HYP_TILT_MIN/MAX.
_NECK_POSES = [
    {constants.NECK_PAN: pan, constants.NECK_TILT: tilt}
    for pan in (80, 90, 100)
    for tilt in (80, 90, 100)
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
        A compact id string, e.g. "pan80_neckTilt90_shTilt0_shRot234".
    """
    return (
        f"pan{pose[constants.NECK_PAN]}"
        f"_neckTilt{pose[constants.NECK_TILT]}"
        f"_shTilt{pose[constants.RT_SHOULDER_TILT]}"
        f"_shRot{pose[constants.RT_SHOULDER_ROTATOR]}"
    )


@pytest.mark.parametrize("pose", _COMBINATIONS, ids=[_pose_id(p) for p in _COMBINATIONS])
def test_hypnotic_combined_extreme_poses_safe(pose):
    """Feature: hypnotic routine — hypnotic_arm + head sway combined poses SAFE.

    Feeds each COMBINED extreme of the ``hypnotic`` performance -- an arm
    (hypnotic_arm) extreme pose (shoulder tilt in {0,15,30}, rotator in
    {226,230,234}, elbow straight) merged with a neck (hyp_head_sway) extreme
    pan/tilt pose -- to the offline collision model and asserts the model
    classifies it SAFE. Because the arm and neck gestures run concurrently on
    disjoint channels, the shoulder can be at any sway extreme while the head is
    at any sway extreme; none of those simultaneous extremes may self-collide.

    In particular this proves the sub-floor shoulder tilt=0 held with rotator
    ~230 is SAFE (the hand-to-face rule needs elbow tilt >= 150 too, and the
    elbow stays at 0). On failure the offending pose and the model's colliding
    link/joint pairs are included so a real collision risk is actionable (do NOT
    weaken this test to make it pass).
    """
    result = _MODEL.is_pose_safe(pose)

    assert result.ok, (
        f"combined extreme pose reported UNSAFE by the collision model: {pose}; "
        f"colliding_pairs={result.colliding_pairs}"
    )
