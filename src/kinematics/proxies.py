"""Collision-proxy generation for the Maximus kinematic collision model.

Converts each link's URDF ``<visual>`` primitive (box / cylinder / sphere) into a
conservative bounding volume -- a capsule (segment + radius), a sphere, or an
oriented bounding box (OBB) -- that fully encloses the raw geometry expanded by a
non-negative inflation margin.

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

from dataclasses import dataclass

import numpy as np


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


@dataclass(frozen=True)
class Box:
    """An oriented bounding box (OBB) proxy expressed in a link's frame.

    The box is defined by a ``center``, three orthonormal local axes (the
    columns of ``axes``), and the ``half_extents`` along each of those local
    axes. The inflation margin is already BAKED INTO ``half_extents`` at
    construction time (each raw half-size has the margin added to it), so the
    proxy fully encloses the raw slab plus the margin without any further
    inflation.

    A point ``p`` (in the same frame the box is expressed in) is inside the box
    iff, for each local axis ``i``,
    ``abs(dot(p - center, axes[:, i])) <= half_extents[i]``.

    Attributes:
        center: Box center, numpy array of shape (3,), in the box's frame.
        axes: The three orthonormal column vectors (local X/Y/Z of the box)
            expressed in that frame, numpy array of shape (3, 3).
        half_extents: Half-sizes along each local axis, numpy array of shape
            (3,). These ALREADY INCLUDE the inflation margin.
    """

    center: np.ndarray
    axes: np.ndarray
    half_extents: np.ndarray


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
        - box(x, y, z): an oriented bounding Box (OBB) that faithfully encloses
          the slab. center = origin translation; axes = the box's local axes
          (the rotation block of ``origin``); half_extents = (x/2 + margin,
          y/2 + margin, z/2 + margin). This avoids over-enclosing flat slabs
          (e.g. the torso/hand boxes) the way a longest-axis capsule or
          near-cubic sphere would.

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
        A Capsule, Sphere, or Box expressed in the primitive's parent / link
        frame.

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
        x, y, z = dims
        center = apply_transform(origin, (0.0, 0.0, 0.0))
        # The rotation block of the visual origin gives the box's local axes as
        # orthonormal column vectors (URDF origins are rigid transforms).
        axes = origin[:3, :3].copy()
        half_extents = np.array(
            [x / 2.0 + margin, y / 2.0 + margin, z / 2.0 + margin], dtype=float
        )
        return Box(center=center, axes=axes, half_extents=half_extents)

    raise ValueError(f"unknown primitive kind: {kind!r}")
