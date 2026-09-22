"""Integration test: the ``sleep``/snore performance's extreme poses are SAFE.

The ``sleep`` routine drives a single movement (``sleep_head``) that owns the
neck (pan/tilt, channels 0-1) and part of the right arm (elbow rotator, shoulder
tilt, shoulder rotator — channels 4, 6, 7). The gesture drops the head fully
asleep (neck tilt to 180), then repeats a gentle head bob within [170, 180]
while the shoulder rotator rocks within [0, 10], all with the arm at its REST
pose (elbow rotator 150, elbow tilt 5, shoulder tilt 55). This test feeds every
such extreme pose to the offline kinematic collision model
(``CollisionModel.is_pose_safe``) and asserts each is SAFE.

This is the KEY safety gate for the sleep pose: NECK_TILT reaches 180 (above the
global SAFE_LIMITS ceiling of 160). That is OPERATOR bench-verified safe and only
reachable via the ``_SLEEP_OVERRIDE`` verified-pose override. RT_SHOULDER_TILT
now holds its rest angle 55 (within the global (45,270) limit).

CAVEAT — like ``yawn_cover`` / ``face_palm``, some of these poses (notably
NECK_TILT=180) sit OUTSIDE the model's calibrated mapping.
The decoupled per-joint model may not faithfully represent them. This test
PREFERS a hard SAFE assert; it only falls back to xfail if the model genuinely
cannot represent the pose (raises ``PoseInputError``) — never weakening the
assertion for a plain UNSAFE verdict.

This is an example/integration test (no Hypothesis), kept separate from the fast
property tests in ``tests/test_performance.py`` so the slow URDF-backed model
build isn't tangled with the PBTs. It mirrors the import / sys.path / module-scope
model conventions of ``tests/test_hypnotic_collision.py``. Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_sleep_collision.py -q --maxfail=1
"""

import os
import sys

import pytest

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import constants  # noqa: E402
from kinematics.model import CollisionModel, PoseInputError  # noqa: E402

# Absolute paths resolved relative to the repo root: the model loads the real
# Maximus URDF + calibration fixtures, which is slow, so build it ONCE at module
# scope and reuse it across every parametrized combination.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_URDF_PATH = os.path.join(_REPO_ROOT, "src", "config", "maximus.urdf")
_CALIBRATION_PATH = os.path.join(_REPO_ROOT, "src", "config", "calibration.json")

_MODEL = CollisionModel(urdf_path=_URDF_PATH, calibration_path=_CALIBRATION_PATH)

# --- Parked-arm pose held throughout the sleep cycle -------------------------
#
# While asleep the arm holds its REST pose: forearm at rest (elbow rotator 150),
# elbow straight (tilt 5), shoulder tilt at rest (55); the neck is centered in
# pan (90). See src/movements.py _SLEEP_ELBOW_ROT_PARK and _sleep_snore_drop.
_SLEEP_ARM = {
    constants.NECK_PAN: 90,
    constants.RT_ELBOW_ROTATOR: 150,
    constants.RT_ELBOW_TILT: 5,
    constants.RT_SHOULDER_TILT: 55,
}

# --- Sleep / bob poses -------------------------------------------------------
#
# Head bob within [170, 180] x shoulder-rotator rock within [0, 10]. Endpoints
# and the mid-point are enumerated on both axes. See src/movements.py
# _SLEEP_BOB_MIN/MAX and _SLEEP_ROCK_MIN/MAX.
_SLEEP_POSES = [
    {
        **_SLEEP_ARM,
        constants.NECK_TILT: tilt,
        constants.RT_SHOULDER_ROTATOR: rot,
    }
    for tilt in (170, 175, 180)
    for rot in (0, 5, 10)
]

# --- Entry endpoints ---------------------------------------------------------
#
# The jerky head drop starts at NECK_TILT=90 (level) and ends at 180 (fully
# dropped); assert both endpoints with the start-pose arm and rotator at rest.
_ENTRY_POSES = [
    {
        **_SLEEP_ARM,
        constants.NECK_TILT: tilt,
        constants.RT_SHOULDER_ROTATOR: 0,
    }
    for tilt in (90, 180)
]

_ALL_POSES = _SLEEP_POSES + _ENTRY_POSES


def _pose_id(pose):
    """Builds a readable pytest id from a combined servo pose.

    Args:
        pose: A servo-channel -> angle dict covering all six required channels.

    Returns:
        A compact id string, e.g. "neckTilt180_shRot10".
    """
    return (
        f"neckTilt{pose[constants.NECK_TILT]}"
        f"_shRot{pose[constants.RT_SHOULDER_ROTATOR]}"
    )


@pytest.mark.parametrize("pose", _ALL_POSES, ids=[_pose_id(p) for p in _ALL_POSES])
def test_sleep_poses_safe(pose):
    """Feature: sleep/snore routine — sleep bob + entry poses are SAFE.

    Feeds each extreme pose of the ``sleep`` performance -- the sleep bob poses
    (neck tilt in {170,175,180} x shoulder rotator in {0,5,10}) and the entry
    endpoints (neck tilt in {90,180}), all with the parked start-pose arm -- to
    the offline collision model and asserts the model classifies it SAFE.

    NECK_TILT=180 is OPERATOR bench-verified safe but sits outside the model's
    calibrated mapping. The hard SAFE assert is preferred; we
    fall back to xfail ONLY if the model genuinely cannot represent the pose
    (raises ``PoseInputError`` — an input-range/calibration limitation, like the
    physically-verified yawn_cover / face_palm poses the decoupled model can't
    clear). We never weaken the assertion for a plain UNSAFE verdict — on failure
    the offending pose and the model's colliding link/joint pairs are reported so
    a real collision risk is actionable.
    """
    try:
        result = _MODEL.is_pose_safe(pose)
    except PoseInputError as exc:
        pytest.xfail(
            f"pose outside the model's calibrated input range (bench-verified "
            f"safe, like yawn_cover/face_palm): {pose}; {exc}"
        )

    assert result.ok, (
        f"sleep pose reported UNSAFE by the collision model: {pose}; "
        f"colliding_pairs={result.colliding_pairs}"
    )
