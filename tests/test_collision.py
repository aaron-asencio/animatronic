"""Property tests for the kinematic collision model's detection layer.

Covers ``src/kinematics/collision.py`` against both randomly generated proxies
and the real Maximus URDF fixture (``src/config/maximus.urdf``):

  - Property 7: proxy intersection detection is correct (agrees with a direct
    surface-distance check) and symmetric (swapping arguments preserves both the
    gap and the boolean verdict) across every capsule/sphere combination.
  - Property 8: the detector never reports a directly-adjacent link pair.
  - Property 9: each reported pair's offending joints equal exactly the revolute
    URDF joints on the tree path between the two links, with no duplicates.

The ``Kinematics_Engine`` and ``Collision_Detector`` are built once at module
scope because URDF loading is slow; the pose-based properties draw joint
dictionaries against that shared detector. Property tests use hypothesis
(``max_examples=150``) and are tagged with the design's Correctness Properties by
number. Run with:

    .venv/bin/python -m pytest tests/test_collision.py -q --maxfail=1
"""

import os
import sys

import numpy as np
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.collision import (  # noqa: E402
    Collision_Detector,
    capsule_capsule_distance,
    capsule_sphere_distance,
    sphere_sphere_distance,
)
from kinematics.proxies import Capsule, Sphere, make_proxy  # noqa: E402
from kinematics.kinematics import Kinematics_Engine  # noqa: E402

# The real URDF fixture used for all pose-based collision tests.
_URDF_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "config", "maximus.urdf")
)

# Float comparison tolerance for gap equality / verdict boundary checks.
_TOL = 1e-9

# Build the engine + detector ONCE at module scope: URDF parsing and proxy
# generation are slow, and the pose properties only need to draw joint
# dictionaries against a fixed detector. A modest inflation margin (0.02 m) keeps
# the proxies conservative without being degenerate.
_ENGINE = Kinematics_Engine(_URDF_PATH)
_PROXIES = {
    link: make_proxy(primitive, margin=0.02)
    for link, primitive in _ENGINE.visual_geometry().items()
}
_DETECTOR = Collision_Detector(_ENGINE, _PROXIES)

# The engine's actuated (revolute) joints; the pose properties draw an angle per
# joint in [-pi, pi].
_ACTUATED_JOINTS = list(_ENGINE._robot.actuated_joint_names)


# ---------------------------------------------------------------------------
# Hypothesis strategies for randomly generated proxies (Property 7)
# ---------------------------------------------------------------------------

# Coordinates constrained to a bounded box; radii kept positive and bounded.
_coord = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_radius = st.floats(min_value=0.01, max_value=0.5, allow_nan=False, allow_infinity=False)


@st.composite
def _point(draw):
    """Draws a 3D point as a numpy array with coordinates in [-1, 1].

    Args:
        draw: The hypothesis draw callable.

    Returns:
        A numpy array of shape (3,).
    """
    return np.array([draw(_coord), draw(_coord), draw(_coord)], dtype=float)


@st.composite
def _capsule(draw):
    """Draws a random ``Capsule`` with bounded endpoints and a positive radius.

    Args:
        draw: The hypothesis draw callable.

    Returns:
        A ``Capsule`` proxy.
    """
    return Capsule(p0=draw(_point()), p1=draw(_point()), radius=draw(_radius))


@st.composite
def _sphere(draw):
    """Draws a random ``Sphere`` with a bounded center and a positive radius.

    Args:
        draw: The hypothesis draw callable.

    Returns:
        A ``Sphere`` proxy.
    """
    return Sphere(center=draw(_point()), radius=draw(_radius))


def _proxy():
    """A strategy yielding either a random ``Capsule`` or a random ``Sphere``.

    Returns:
        A hypothesis strategy producing a proxy of either kind.
    """
    return st.one_of(_capsule(), _sphere())


def _surface_gap(a, b):
    """Direct, independent surface-gap cross-check for two proxies.

    Computes the gap from first principles (axis/center distance minus the sum of
    radii) without dispatching through the module under test, so it can validate
    the detection verdict. The primitive distance helpers themselves are the
    thing being checked at the type-dispatch level, so this cross-check recomputes
    the underlying geometric distance directly.

    Args:
        a: The first proxy (``Capsule`` or ``Sphere``).
        b: The second proxy (``Capsule`` or ``Sphere``).

    Returns:
        The surface gap (float). ``<= 0`` means the surfaces intersect.
    """
    a_cap = isinstance(a, Capsule)
    b_cap = isinstance(b, Capsule)
    if a_cap and b_cap:
        axis = _segment_segment(a.p0, a.p1, b.p0, b.p1)
    elif a_cap and not b_cap:
        axis = _point_segment(b.center, a.p0, a.p1)
    elif not a_cap and b_cap:
        axis = _point_segment(a.center, b.p0, b.p1)
    else:
        axis = float(np.linalg.norm(a.center - b.center))
    return axis - a.radius - b.radius


def _point_segment(point, seg_a, seg_b):
    """Distance from a point to a segment via a dense sampling of the segment.

    An independent (brute-force) reference for the analytic distance, kept simple
    so it clearly does not share code with the module under test.

    Args:
        point: The query point, numpy array of shape (3,).
        seg_a: Segment start, numpy array of shape (3,).
        seg_b: Segment end, numpy array of shape (3,).

    Returns:
        The minimum distance (float).
    """
    ts = np.linspace(0.0, 1.0, 400)
    pts = seg_a[None, :] + ts[:, None] * (seg_b - seg_a)[None, :]
    return float(np.min(np.linalg.norm(pts - point[None, :], axis=1)))


def _segment_segment(p0, p1, q0, q1):
    """Distance between two segments via a dense grid sampling of both.

    An independent (brute-force) reference for the analytic distance, kept simple
    so it clearly does not share code with the module under test.

    Args:
        p0: First segment start, numpy array of shape (3,).
        p1: First segment end, numpy array of shape (3,).
        q0: Second segment start, numpy array of shape (3,).
        q1: Second segment end, numpy array of shape (3,).

    Returns:
        The minimum distance (float).
    """
    # Dense sampling of both segments. 600 samples keeps the brute-force
    # reference within a few tenths of a millimeter of the analytic distance
    # even for full-length (|segment| ~ 2 m) inputs, so it stays inside the
    # 5e-3 comparison slack (a coarser grid under-resolves degenerate cases,
    # e.g. a point vs. a long segment).
    ts = np.linspace(0.0, 1.0, 600)
    p_pts = p0[None, :] + ts[:, None] * (p1 - p0)[None, :]
    q_pts = q0[None, :] + ts[:, None] * (q1 - q0)[None, :]
    diffs = p_pts[:, None, :] - q_pts[None, :, :]
    return float(np.min(np.linalg.norm(diffs, axis=2)))


def _dispatch_gap(a, b):
    """Computes the gap for an ordered proxy pair via the module under test.

    Dispatches to the correct analytic distance function, honoring the
    ``capsule_sphere_distance(capsule, sphere)`` argument convention.

    Args:
        a: The first proxy (``Capsule`` or ``Sphere``).
        b: The second proxy (``Capsule`` or ``Sphere``).

    Returns:
        The surface gap (float) reported by the module under test.
    """
    a_cap = isinstance(a, Capsule)
    b_cap = isinstance(b, Capsule)
    if a_cap and b_cap:
        return capsule_capsule_distance(a, b)
    if not a_cap and not b_cap:
        return sphere_sphere_distance(a, b)
    if a_cap:
        return capsule_sphere_distance(a, b)
    return capsule_sphere_distance(b, a)


# ---------------------------------------------------------------------------
# Task 6.3 — Property 7: Proxy intersection detection is correct and symmetric
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(a=_proxy(), b=_proxy())
def test_property7_intersection_detection_correct_and_symmetric(a, b):
    """Feature: kinematic-collision-model, Property 7: Proxy intersection detection is correct and symmetric.

    For any pair of randomly generated proxies (each independently a capsule or a
    sphere with bounded endpoints/center and a positive radius):

      (a) CORRECT: the "intersects" verdict (surface gap <= 0) from the module's
          distance function agrees with an independent geometric cross-check --
          intersection iff the minimum surface distance is <= 0, i.e. the
          axis/center distance is <= the sum of radii.
      (b) SYMMETRIC: swapping the two proxies yields the same gap (within
          tolerance) and the same boolean verdict, including sphere-capsule which
          must equal the (capsule, sphere) result.

    Validates: Requirements 5.2
    """
    gap = _dispatch_gap(a, b)
    gap_swapped = _dispatch_gap(b, a)
    reference = _surface_gap(a, b)

    # (b) SYMMETRIC: same gap within tolerance, and the same boolean verdict.
    assert abs(gap - gap_swapped) <= 1e-6
    assert (gap <= 0.0) == (gap_swapped <= 0.0)

    # (a) CORRECT: the module gap matches the independent reference closely, and
    # (away from the exact boundary) the boolean verdict matches too. The dense
    # sampling reference slightly overestimates distance, so allow a small slack.
    assert abs(gap - reference) <= 5e-3

    # Only compare the boolean verdict when we are clearly off the boundary,
    # since a near-zero gap can straddle the boundary within sampling error.
    if abs(reference) > 5e-3:
        assert (gap <= _TOL) == (reference <= 0.0)


# ---------------------------------------------------------------------------
# Task 6.4 — Property 8: Excluded link pairs are never reported
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
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
def test_property8_adjacent_link_pairs_never_reported(angles):
    """Feature: kinematic-collision-model, Property 8: Excluded (adjacent, rigid, or self-exempt) link pairs are never reported.

    For any pose (a dict of joint radians over the engine's actuated joints, each
    angle in [-pi, pi]), running the shared detector never returns an excluded
    link pair: for every reported pair, ``frozenset({link_a, link_b})`` is not in
    ``engine.excluded_pairs()`` (the superset of adjacent, rigidly-attached, and
    self-collision-exempt-group pairs).

    Validates: Requirements 5.4
    """
    pose = dict(zip(_ACTUATED_JOINTS, angles))
    transforms = _ENGINE.link_transforms(pose)

    detected = _DETECTOR.check(transforms)
    excluded = _ENGINE.excluded_pairs()

    for pair in detected:
        assert frozenset({pair.link_a, pair.link_b}) not in excluded


# ---------------------------------------------------------------------------
# Task 6.5 — Property 9: Offending joints equal the revolute joints between links
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
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
def test_property9_offending_joints_equal_revolute_joints_between_links(angles):
    """Feature: kinematic-collision-model, Property 9: Offending joints equal the revolute joints between the links.

    For any pose, every reported pair's offending-joint tuple equals exactly the
    revolute joints on the tree path between the two links
    (``set(dp.joints) == set(engine.joints_between(dp.link_a, dp.link_b))``) and
    contains no duplicates -- confirming the offending joints are exactly the
    revolute URDF joints between the two links.

    Validates: Requirements 5.3, 5.5
    """
    pose = dict(zip(_ACTUATED_JOINTS, angles))
    transforms = _ENGINE.link_transforms(pose)

    detected = _DETECTOR.check(transforms)

    for pair in detected:
        expected = set(_ENGINE.joints_between(pair.link_a, pair.link_b))
        assert set(pair.joints) == expected
        # No duplicate joints reported.
        assert len(pair.joints) == len(set(pair.joints))
