"""Tests for the kinematic collision model's collision-proxy generation.

Covers ``src/kinematics/proxies.py`` -- the ``make_proxy(primitive, margin)``
factory that converts a duck-typed visual primitive (box / cylinder / sphere)
into a conservative bounding volume (``Capsule`` or ``Sphere``) inflated by a
non-negative margin.

Property tests use hypothesis (``max_examples=150`` each) and are tagged with
the design's Correctness Properties by number. Example-based unit tests use
pytest. Run with:

    .venv/bin/python -m pytest tests/test_proxies.py -q --maxfail=1

To keep these tests decoupled from the concurrently-authored FK engine, a tiny
local ``_FakePrimitive`` dataclass mirrors the ``make_proxy`` duck-typing
contract (``.kind`` / ``.dims`` / ``.origin``) rather than importing
``VisualPrimitive``.
"""

import math
import os
import sys
from dataclasses import dataclass

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.proxies import make_proxy, Capsule, Sphere  # noqa: E402

# Absolute floating-point tolerance for "inside the proxy" / "radius grew"
# comparisons. The proxy math is all sums of squares and matrix products, so a
# tight tolerance is sufficient.
_TOL = 1e-9


@dataclass
class _FakePrimitive:
    """Minimal stand-in for the FK engine's ``VisualPrimitive``.

    Mirrors the duck-typing contract ``make_proxy`` relies on so these tests do
    not import the concurrently-authored ``VisualPrimitive`` dataclass.

    Attributes:
        kind: Geometry kind, one of "box", "cylinder", "sphere".
        dims: Dimensions tuple -- box (x, y, z); cylinder (length, radius);
            sphere (radius,).
        origin: 4x4 homogeneous visual-origin transform (numpy array).
    """

    kind: str
    dims: tuple
    origin: np.ndarray


def _point_segment_distance(point, a, b):
    """Euclidean distance from ``point`` to the line segment ``a -> b``.

    Computed inline (rather than imported from ``kinematics.collision``) to keep
    the proxy tests self-contained.

    Args:
        point: The query point, array-like of length 3.
        a: Segment start, array-like of length 3.
        b: Segment end, array-like of length 3.

    Returns:
        The shortest distance from ``point`` to the segment as a float.
    """
    point = np.asarray(point, dtype=float)
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    ab = b - a
    denom = float(ab @ ab)
    if denom == 0.0:
        # Degenerate segment (a == b): distance to the shared endpoint.
        return float(np.linalg.norm(point - a))
    t = float((point - a) @ ab) / denom
    t = max(0.0, min(1.0, t))
    closest = a + t * ab
    return float(np.linalg.norm(point - closest))


def _point_inside(point, proxy):
    """Returns whether ``point`` lies inside ``proxy`` (within tolerance).

    Args:
        point: The query point, array-like of length 3.
        proxy: A ``Capsule`` or ``Sphere`` proxy.

    Returns:
        True if the point is within the proxy's radius (plus ``_TOL``).
    """
    if isinstance(proxy, Sphere):
        return float(np.linalg.norm(np.asarray(point, dtype=float) - proxy.center)) <= (
            proxy.radius + _TOL
        )
    if isinstance(proxy, Capsule):
        return _point_segment_distance(point, proxy.p0, proxy.p1) <= (proxy.radius + _TOL)
    raise AssertionError(f"unexpected proxy type: {type(proxy)!r}")


def _proxy_radius(proxy):
    """Returns the radius of a ``Capsule`` or ``Sphere`` proxy.

    Args:
        proxy: A ``Capsule`` or ``Sphere`` proxy.

    Returns:
        The proxy's radius as a float.
    """
    return float(proxy.radius)


def _translation(x, y, z):
    """Builds a 4x4 homogeneous translation transform.

    Args:
        x: X translation.
        y: Y translation.
        z: Z translation.

    Returns:
        A 4x4 numpy translation matrix.
    """
    transform = np.eye(4)
    transform[:3, 3] = (x, y, z)
    return transform


def _rotation_z(theta):
    """Builds a 4x4 homogeneous rotation about the Z axis.

    Args:
        theta: Rotation angle in radians.

    Returns:
        A 4x4 numpy rotation matrix.
    """
    c, s = math.cos(theta), math.sin(theta)
    transform = np.eye(4)
    transform[0, 0] = c
    transform[0, 1] = -s
    transform[1, 0] = s
    transform[1, 1] = c
    return transform


def _rotation_y(theta):
    """Builds a 4x4 homogeneous rotation about the Y axis.

    Args:
        theta: Rotation angle in radians.

    Returns:
        A 4x4 numpy rotation matrix.
    """
    c, s = math.cos(theta), math.sin(theta)
    transform = np.eye(4)
    transform[0, 0] = c
    transform[0, 2] = s
    transform[2, 0] = -s
    transform[2, 2] = c
    return transform


# Hypothesis strategies ------------------------------------------------------

_POSITIVE_DIM = st.floats(
    min_value=0.005, max_value=2.0, allow_nan=False, allow_infinity=False
)
_MARGIN = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_ANGLE = st.floats(
    min_value=-math.pi, max_value=math.pi, allow_nan=False, allow_infinity=False
)
_COORD = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False)


@st.composite
def _origins(draw):
    """Draws a random 4x4 origin transform (rotation about Y and Z + translation).

    Returns:
        A 4x4 numpy homogeneous transform.
    """
    rot = _rotation_z(draw(_ANGLE)) @ _rotation_y(draw(_ANGLE))
    trans = _translation(draw(_COORD), draw(_COORD), draw(_COORD))
    return trans @ rot


@st.composite
def _primitives(draw):
    """Draws a random visual primitive (box / cylinder / sphere) with an origin.

    Returns:
        A ``_FakePrimitive`` with positive finite dims and a random origin.
    """
    origin = draw(_origins())
    kind = draw(st.sampled_from(["box", "cylinder", "sphere"]))
    if kind == "box":
        dims = (draw(_POSITIVE_DIM), draw(_POSITIVE_DIM), draw(_POSITIVE_DIM))
    elif kind == "cylinder":
        dims = (draw(_POSITIVE_DIM), draw(_POSITIVE_DIM))
    else:
        dims = (draw(_POSITIVE_DIM),)
    return _FakePrimitive(kind=kind, dims=dims, origin=origin)


def _surface_samples(primitive):
    """Returns raw-geometry surface/corner sample points in LOCAL coordinates.

    For a box: all 8 corners. For a cylinder: the two end-circle rims (sampled
    around the circle) plus the two axis endpoints. For a sphere: +-r along each
    axis.

    Args:
        primitive: A ``_FakePrimitive``.

    Returns:
        A list of length-3 numpy arrays in the primitive's local frame.
    """
    kind = primitive.kind
    dims = tuple(float(d) for d in primitive.dims)
    points = []

    if kind == "box":
        hx, hy, hz = (d / 2.0 for d in dims)
        for sx in (-hx, hx):
            for sy in (-hy, hy):
                for sz in (-hz, hz):
                    points.append(np.array([sx, sy, sz], dtype=float))
        return points

    if kind == "cylinder":
        length, radius = dims
        half = length / 2.0
        for z in (-half, half):
            points.append(np.array([0.0, 0.0, z], dtype=float))  # axis endpoint
            for k in range(8):
                theta = 2.0 * math.pi * k / 8.0
                points.append(
                    np.array(
                        [radius * math.cos(theta), radius * math.sin(theta), z],
                        dtype=float,
                    )
                )
        return points

    if kind == "sphere":
        (radius,) = dims
        for axis in range(3):
            for sign in (-1.0, 1.0):
                p = np.zeros(3, dtype=float)
                p[axis] = sign * radius
                points.append(p)
        return points

    raise AssertionError(f"unexpected kind: {kind!r}")


# ---------------------------------------------------------------------------
# Task 5.2 — Property 5: Proxies conservatively enclose their geometry
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(primitive=_primitives(), margin=_MARGIN)
def test_property5_proxies_conservatively_enclose_geometry(primitive, margin):
    """Feature: kinematic-collision-model, Property 5: Proxies conservatively enclose their geometry.

    For any primitive (box / cylinder / sphere) with positive finite dims placed
    by a random origin transform, and any margin >= 0, every surface/corner
    sample point of the raw geometry -- transformed into the parent frame by the
    primitive's origin -- lies inside the returned proxy.

    Validates: Requirements 4.2
    """
    proxy = make_proxy(primitive, margin)

    for local_point in _surface_samples(primitive):
        world_point = primitive.origin @ np.append(local_point, 1.0)
        world_point = world_point[:3]
        assert _point_inside(world_point, proxy), (
            f"sample {local_point} (world {world_point}) fell outside {proxy} "
            f"for {primitive.kind} dims={primitive.dims} margin={margin}"
        )


# ---------------------------------------------------------------------------
# Task 5.3 — Property 6: Proxy inflation is monotonic in margin
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    primitive=_primitives(),
    margin_a=_MARGIN,
    margin_b=_MARGIN,
)
def test_property6_proxy_inflation_is_monotonic_in_margin(primitive, margin_a, margin_b):
    """Feature: kinematic-collision-model, Property 6: Proxy inflation is monotonic in margin.

    For the same primitive and two margins m1 <= m2 (both >= 0), the larger
    margin produces a proxy of the same type whose radius is >= the smaller
    margin's radius (margin only inflates the radius; capsule segment endpoints
    are unaffected). This makes the enclosed volume monotonic in the margin.

    Validates: Requirements 4.4
    """
    m1, m2 = sorted((margin_a, margin_b))

    proxy_small = make_proxy(primitive, m1)
    proxy_large = make_proxy(primitive, m2)

    # Same margin-independent geometry -> identical proxy type for both margins.
    assert type(proxy_small) is type(proxy_large)

    assert _proxy_radius(proxy_large) >= _proxy_radius(proxy_small) - _TOL

    # For capsules, the segment endpoints depend only on the geometry, not the
    # margin, so they must coincide across the two margins.
    if isinstance(proxy_small, Capsule):
        assert np.allclose(proxy_small.p0, proxy_large.p0, atol=_TOL)
        assert np.allclose(proxy_small.p1, proxy_large.p1, atol=_TOL)
    else:
        assert np.allclose(proxy_small.center, proxy_large.center, atol=_TOL)


# ---------------------------------------------------------------------------
# Task 5.4 — Unit / edge tests for proxy generation
# ---------------------------------------------------------------------------


def test_negative_margin_raises_value_error():
    """Requirement 4.3: a negative inflation margin raises ValueError."""
    primitive = _FakePrimitive(kind="sphere", dims=(0.05,), origin=np.eye(4))
    with pytest.raises(ValueError):
        make_proxy(primitive, -0.001)


def test_thin_arm_cylinder_becomes_thin_capsule():
    """Requirement 4.1: a thin arm cylinder maps to a thin capsule.

    A length-0.3, radius-0.02 cylinder at the identity origin becomes a Capsule
    whose radius is r + margin and whose endpoint separation equals the length.
    """
    length, radius, margin = 0.3, 0.02, 0.01
    primitive = _FakePrimitive(kind="cylinder", dims=(length, radius), origin=np.eye(4))

    proxy = make_proxy(primitive, margin)

    assert isinstance(proxy, Capsule)
    assert math.isclose(proxy.radius, radius + margin, abs_tol=_TOL)
    separation = float(np.linalg.norm(proxy.p1 - proxy.p0))
    assert math.isclose(separation, length, abs_tol=_TOL)


def test_head_sphere_maps_to_sphere():
    """Requirement 4.1: a head sphere maps to a Sphere of radius r + margin."""
    radius, margin = 0.08, 0.015
    primitive = _FakePrimitive(kind="sphere", dims=(radius,), origin=_translation(0.1, 0.2, 0.3))

    proxy = make_proxy(primitive, margin)

    assert isinstance(proxy, Sphere)
    assert math.isclose(proxy.radius, radius + margin, abs_tol=_TOL)
    assert np.allclose(proxy.center, np.array([0.1, 0.2, 0.3]), atol=_TOL)


def test_near_cubic_box_maps_to_sphere():
    """Requirement 4.1: a near-cubic box maps to a Sphere.

    Dims (0.1, 0.1, 0.11) have longest <= 1.3 * shortest, so the box rule falls
    back to a Sphere at the box center.
    """
    primitive = _FakePrimitive(kind="box", dims=(0.1, 0.1, 0.11), origin=np.eye(4))

    proxy = make_proxy(primitive, 0.0)

    assert isinstance(proxy, Sphere)


def test_elongated_box_maps_to_capsule_along_long_axis():
    """Requirement 4.1: an elongated box maps to a Capsule along the long axis.

    Dims (0.4, 0.05, 0.05): the X axis is much longer than the others, so the
    proxy is a Capsule whose segment runs along local X with length 0.4.
    """
    primitive = _FakePrimitive(kind="box", dims=(0.4, 0.05, 0.05), origin=np.eye(4))

    proxy = make_proxy(primitive, 0.0)

    assert isinstance(proxy, Capsule)
    # Segment runs along the long (X) axis, spanning the full 0.4 length.
    separation = float(np.linalg.norm(proxy.p1 - proxy.p0))
    assert math.isclose(separation, 0.4, abs_tol=_TOL)
    delta = np.abs(proxy.p1 - proxy.p0)
    assert delta[0] > delta[1] and delta[0] > delta[2]
