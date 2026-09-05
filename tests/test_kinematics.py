"""Tests for the kinematic collision model's forward-kinematics engine.

Covers ``src/kinematics/kinematics.py`` (``Kinematics_Engine``,
``KinematicsError``) against the real Maximus URDF fixture
(``src/config/maximus.urdf``):

  - Property 4: forward kinematics yields valid rigid transforms for any set of
    joint angles (rotation blocks orthonormal with det +1, bottom row
    [0, 0, 0, 1], base_link the identity).
  - Unit/edge cases: a missing/unparseable URDF path raises ``KinematicsError``
    naming the path, and a ``<limit>``-exceeding angle still computes FK without
    clamping or error.

The engine is loaded once at module scope because URDF loading is slow; the
property draws joint dictionaries against that shared engine. Property tests use
hypothesis (``max_examples=150``) and are tagged with the design's Correctness
Properties by number. Run with:

    .venv/bin/python -m pytest tests/test_kinematics.py -q --maxfail=1
"""

import os
import sys

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.kinematics import Kinematics_Engine, KinematicsError  # noqa: E402

# The real URDF fixture used for all forward-kinematics tests.
_URDF_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "config", "maximus.urdf")
)

# Load the engine ONCE at module scope: URDF parsing is slow, and the property
# only needs to draw joint dictionaries against a fixed engine.
_ENGINE = Kinematics_Engine(_URDF_PATH)

# The engine's actuated (revolute) joints; the property draws an angle per joint.
_ACTUATED_JOINTS = list(_ENGINE._robot.actuated_joint_names)


def _assert_rigid_transform(transform):
    """Assert a 4x4 matrix is a valid rigid (special-Euclidean) transform.

    Checks the rotation block is orthonormal with determinant +1 and the bottom
    row is exactly ``[0, 0, 0, 1]``.

    Args:
        transform: A 4x4 numpy homogeneous transform to validate.
    """
    assert transform.shape == (4, 4)

    rotation = transform[:3, :3]
    # Orthonormal: R^T R == I.
    assert np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6)
    # Proper rotation (no reflection): det(R) == +1.
    assert np.isclose(np.linalg.det(rotation), 1.0, atol=1e-6)
    # Homogeneous bottom row.
    assert np.allclose(transform[3, :], np.array([0.0, 0.0, 0.0, 1.0]), atol=1e-9)


# ---------------------------------------------------------------------------
# Task 4.2 — Property 4: Forward kinematics yields valid rigid transforms
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    angles=st.lists(
        st.floats(
            min_value=-np.pi,
            max_value=np.pi,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=len(_ACTUATED_JOINTS),
        max_size=len(_ACTUATED_JOINTS),
    )
)
def test_property4_forward_kinematics_yields_valid_rigid_transforms(angles):
    """Feature: kinematic-collision-model, Property 4: Forward kinematics yields valid rigid transforms.

    For any set of joint angles drawn in [-pi, pi] over the engine's actuated
    revolute joints, every returned link transform is a valid rigid transform:
    the 3x3 rotation block is orthonormal with determinant +1, the bottom row is
    [0, 0, 0, 1], and base_link's transform is the 4x4 identity.

    Validates: Requirements 1.3, 1.4
    """
    joint_radians = dict(zip(_ACTUATED_JOINTS, angles))

    transforms = _ENGINE.link_transforms(joint_radians)

    # Every link in the URDF is present in the FK result.
    assert set(transforms) == set(_ENGINE.link_names)

    for link, transform in transforms.items():
        _assert_rigid_transform(transform)

    # base_link is exactly the identity.
    assert np.allclose(transforms[_ENGINE.base_link], np.eye(4), atol=1e-9)


# ---------------------------------------------------------------------------
# Task 4.3 — Unit / edge tests for the engine
# ---------------------------------------------------------------------------


def test_missing_urdf_raises_kinematics_error_naming_path():
    """Requirement 1.2: a missing/unparseable URDF raises KinematicsError naming the path.

    Pointing the engine at a nonexistent path raises ``KinematicsError`` whose
    message contains the offending path so the caller can identify it.
    """
    missing_path = os.path.join(
        os.path.dirname(__file__), "does_not_exist", "nope.urdf"
    )

    with pytest.raises(KinematicsError) as excinfo:
        Kinematics_Engine(missing_path)

    assert missing_path in str(excinfo.value)


def test_limit_exceeding_angle_computes_fk_without_clamping():
    """Requirement 1.5: a <limit>-exceeding angle still computes FK, no clamping/error.

    The URDF ``<limit>`` values are non-credible (servos were reseated), so the
    engine ignores them. Driving a revolute joint far past any URDF limit
    (10.0 rad) must still yield a full set of valid rigid transforms with no
    exception.
    """
    # elbow_pitch_joint has a URDF limit around |2.4| rad; 10.0 rad is well past
    # any joint's declared range.
    over_limit = {"elbow_pitch_joint": 10.0}

    transforms = _ENGINE.link_transforms(over_limit)

    # No clamping/error: a full set of transforms is returned, all still rigid.
    assert set(transforms) == set(_ENGINE.link_names)
    for transform in transforms.values():
        _assert_rigid_transform(transform)

    # A 10.0 rad drive on a revolute joint actually moves the arm chain: the
    # lower_arm_link transform differs from the zero-configuration one, proving
    # the large angle was applied rather than clamped to the limit.
    rest = _ENGINE.link_transforms({})
    assert not np.allclose(
        transforms["lower_arm_link"], rest["lower_arm_link"], atol=1e-6
    )
