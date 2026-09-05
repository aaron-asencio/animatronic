"""Collision-proxy generation for the Maximus kinematic collision model.

Converts each link's URDF ``<visual>`` primitive (box / cylinder / sphere) into a
conservative bounding volume -- a capsule (segment + radius) or a sphere -- that
fully encloses the raw geometry expanded by a non-negative inflation margin.

Design intent (see design.md, Requirement 4):
    - Every proxy fully encloses the raw geometry expanded by the margin.
    - A proxy is never smaller than the raw geometry (conservative / over-approx).
    - Increasing the margin never shrinks the enclosed volume (monotonic).

Cross-task decoupling note:
    The ``VisualPrimitive`` dataclass is authored in ``kinematics.py`` by a
    concurrent task. To avoid a hard import dependency / import race, this module
    does NOT import ``VisualPrimitive``. Instead, ``make_proxy`` is *duck-typed*:
    it accepts any object exposing ``.kind`` (str), ``.dims`` (tuple of floats),
    and ``.origin`` (4x4 numpy transform). This keeps ``proxies.py`` importable
    and testable independently of the FK engine.

This module imports only ``numpy``, ``math``, and ``dataclasses`` -- no hardware
libraries and no other kinematics submodules (Requirement 11.3).
"""

import math
from dataclasses import dataclass

import numpy as np


# Near-cubic threshold: when a box's largest dimension is within this factor of
# its smallest dimension, a sphere encloses it more tightly than a long capsule,
# so the box rule falls back to a Sphere. See make_proxy() box handling below.
_NEAR_CUBIC_RATIO = 1.3


@dataclass(frozen=True)
class Capsule:
    """A capsule proxy: all points within ``radius`` of the segment p0 -> p1.

    The capsule is the swept sphere of ``radius`` moved along the line segment
    from ``p0`` to ``p1`` (a cylinder with hemispherical end caps). The radius
    already includes any inflation margin applied at construction time.

    Attributes:
        p0: Segment start point, numpy array of shape (3,), in the link frame.
        p1: Segment end point, numpy array of shape (3,), in the link frame.
        radius: Capsule radius in meters (already includes the inflation margin).
    """

    p0: np.ndarray
    p1: np.ndarray
    radius: float


@dataclass(frozen=True)
class Sphere:
    """A sphere proxy: all points within ``radius`` of ``center``.

    The radius already includes any inflation margin applied at construction
    time.

    Attributes:
        center: Sphere center, numpy array of shape (3,), in the link frame.
        radius: Sphere radius in meters (already includes the inflation margin).
    """

    center: np.ndarray
    radius: float


def apply_transform(transform, point):
    """Applies a 4x4 homogeneous transform to a 3D point.

    Args:
        transform: A 4x4 homogeneous transform, numpy array of shape (4, 4).
        point: A 3D point, array-like of length 3, expressed in the transform's
            local frame.

    Returns:
        A numpy array of shape (3,): the point expressed in the transform's
        parent frame.
    """
    local = np.asarray(point, dtype=float)
    homogeneous = np.array([local[0], local[1], local[2], 1.0], dtype=float)
    transformed = np.asarray(transform, dtype=float) @ homogeneous
    return transformed[:3]


def make_proxy(primitive, margin):
    """Builds a conservative proxy fully enclosing ``primitive`` plus ``margin``.

    The returned proxy is expressed in the primitive's PARENT / link frame: every
    local proxy point is mapped through ``primitive.origin`` (the 4x4 visual
    origin transform).

    Mapping rules (per design.md):
        - sphere(r): Sphere(center = origin translation, radius = r + margin).
        - cylinder(length L, radius r): Capsule along the cylinder's local Z axis
          (the URDF cylinder convention). Endpoints at the origin-transformed
          local points (0, 0, -L/2) and (0, 0, +L/2); radius = r + margin. The
          spherical end caps conservatively enclose the flat cylinder ends.
        - box(x, y, z): Capsule along the box's longest axis. Endpoints at
          +-(longest / 2) from the box center along that axis (origin-transformed);
          radius = margin + half-diagonal of the OTHER two dimensions
          (sqrt((a/2)^2 + (b/2)^2)), so the capsule fully contains every box
          corner. When the box is near-cubic (longest <= 1.3 x shortest), a
          Capsule is a poor fit, so instead return a Sphere at the box center
          with radius = margin + half the space-diagonal.

    Because the margin is applied additively to a radius that already fully
    encloses the raw geometry, the proxy is never smaller than the raw geometry
    and a larger margin never shrinks the enclosed volume.

    Args:
        primitive: A duck-typed visual primitive exposing ``.kind`` (one of
            "box", "cylinder", "sphere"), ``.dims`` (tuple of floats: box
            (x, y, z); cylinder (length, radius); sphere (radius,)), and
            ``.origin`` (a 4x4 numpy homogeneous transform for the visual origin).
        margin: Non-negative inflation distance in meters.

    Returns:
        A Capsule or Sphere expressed in the primitive's parent / link frame.

    Raises:
        ValueError: If ``margin`` is negative, or if ``primitive.kind`` is not a
            recognized geometry kind, or if ``primitive.dims`` has the wrong
            arity for its kind.
    """
    if margin < 0:
        raise ValueError(f"inflation margin must be >= 0, got {margin}")

    kind = primitive.kind
    dims = tuple(float(d) for d in primitive.dims)
    origin = np.asarray(primitive.origin, dtype=float)
    print(f"[proxies] make_proxy kind={kind} dims={dims} margin={margin}")

    if kind == "sphere":
        if len(dims) != 1:
            raise ValueError(f"sphere dims must be (radius,), got {dims}")
        (radius,) = dims
        center = apply_transform(origin, (0.0, 0.0, 0.0))
        return Sphere(center=center, radius=radius + margin)

    if kind == "cylinder":
        if len(dims) != 2:
            raise ValueError(f"cylinder dims must be (length, radius), got {dims}")
        length, radius = dims
        half = length / 2.0
        p0 = apply_transform(origin, (0.0, 0.0, -half))
        p1 = apply_transform(origin, (0.0, 0.0, +half))
        return Capsule(p0=p0, p1=p1, radius=radius + margin)

    if kind == "box":
        if len(dims) != 3:
            raise ValueError(f"box dims must be (x, y, z), got {dims}")
        return _box_proxy(dims, origin, margin)

    raise ValueError(f"unknown primitive kind: {kind!r}")


def _box_proxy(dims, origin, margin):
    """Builds a conservative proxy for a box primitive.

    Chooses a sphere when the box is near-cubic, otherwise a capsule along the
    box's longest axis whose radius covers the corner diagonal of the other two
    dimensions. See ``make_proxy`` for the full rule description.

    Args:
        dims: Box dimensions (x, y, z) in meters.
        origin: The 4x4 homogeneous transform of the box's visual origin.
        margin: Non-negative inflation distance in meters.

    Returns:
        A Capsule or Sphere expressed in the box's parent / link frame.
    """
    half = [d / 2.0 for d in dims]
    longest_axis = int(np.argmax(dims))
    max_dim = max(dims)
    min_dim = min(dims)

    # Half the space-diagonal: the distance from the box center to any corner.
    space_half_diag = math.sqrt(sum(h * h for h in half))

    if max_dim <= _NEAR_CUBIC_RATIO * min_dim:
        # Near-cubic: a sphere at the center covering every corner is the
        # tightest conservative fit.
        center = apply_transform(origin, (0.0, 0.0, 0.0))
        return Sphere(center=center, radius=space_half_diag + margin)

    # Elongated box: capsule along the longest axis. The two "other" half-extents
    # form a rectangle whose half-diagonal is the radius needed to reach every
    # corner off the axis.
    other_half = [half[i] for i in range(3) if i != longest_axis]
    cross_half_diag = math.sqrt(other_half[0] ** 2 + other_half[1] ** 2)

    end_local_neg = [0.0, 0.0, 0.0]
    end_local_pos = [0.0, 0.0, 0.0]
    end_local_neg[longest_axis] = -half[longest_axis]
    end_local_pos[longest_axis] = +half[longest_axis]

    p0 = apply_transform(origin, end_local_neg)
    p1 = apply_transform(origin, end_local_pos)
    return Capsule(p0=p0, p1=p1, radius=cross_half_diag + margin)
