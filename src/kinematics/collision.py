"""Analytic collision distances for the Maximus kinematic collision model.

This module provides the cheap, closed-form geometric distance routines used on
the collision-verdict path -- no meshing, only ``numpy`` vector math (design.md,
"Capsule distance math"). Two capsules reduce to a segment-segment distance, a
capsule and a sphere to a point-segment distance, and two spheres to a plain
center distance; each surface-gap function subtracts the involved radii so a
value ``<= 0`` means the proxies intersect (Requirement 5.2).

For capsule/sphere surface-gap functions a negative return value is meaningful:
it is the (negated) penetration depth between the two proxy surfaces.

The ``Collision_Detector`` class (which places proxies in world coordinates,
tests non-adjacent link pairs, and maps collisions to offending joints) lands
next in task 6.2; this module intentionally ships only the distance primitives
for now.

This module imports only ``numpy`` and the ``Capsule`` / ``Sphere`` proxy
dataclasses -- no hardware libraries (Requirement 11.3).
"""

import numpy as np

from kinematics.proxies import Capsule, Sphere


def segment_segment_distance(p0, p1, q0, q1):
    """Minimum Euclidean distance between two 3D line segments.

    Computes the shortest distance between segment ``P(s) = p0 + s*(p1 - p0)``
    and segment ``Q(t) = q0 + t*(q1 - q0)`` for ``s, t`` in ``[0, 1]``, using the
    clamped closest-point-between-segments algorithm (Ericson, "Real-Time
    Collision Detection", ch. 5). Degenerate (zero-length) segments collapse to
    the point-segment / point-point cases, and the parallel case is handled by
    clamping ``s`` then re-solving ``t`` (and clamping ``s`` again if needed).

    Args:
        p0: Start point of the first segment, array-like of length 3.
        p1: End point of the first segment, array-like of length 3.
        q0: Start point of the second segment, array-like of length 3.
        q1: End point of the second segment, array-like of length 3.

    Returns:
        The minimum distance (float, >= 0) between the two segments.
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    q0 = np.asarray(q0, dtype=float)
    q1 = np.asarray(q1, dtype=float)

    d1 = p1 - p0  # Direction of segment P.
    d2 = q1 - q0  # Direction of segment Q.
    r = p0 - q0

    a = float(np.dot(d1, d1))  # Squared length of segment P.
    e = float(np.dot(d2, d2))  # Squared length of segment Q.
    f = float(np.dot(d2, r))

    eps = 1e-12

    if a <= eps and e <= eps:
        # Both segments degenerate to points.
        return float(np.linalg.norm(p0 - q0))

    if a <= eps:
        # First segment degenerates to a point; clamp t on segment Q.
        s = 0.0
        t = _clamp(f / e, 0.0, 1.0)
    else:
        c = float(np.dot(d1, r))
        if e <= eps:
            # Second segment degenerates to a point; clamp s on segment P.
            t = 0.0
            s = _clamp(-c / a, 0.0, 1.0)
        else:
            # General non-degenerate case.
            b = float(np.dot(d1, d2))
            denom = a * e - b * b  # Always >= 0.

            if denom > eps:
                # Segments not parallel: closest point on the infinite lines,
                # then clamp s to [0, 1].
                s = _clamp((b * f - c * e) / denom, 0.0, 1.0)
            else:
                # Parallel segments: pick an arbitrary s (0) and solve t.
                s = 0.0

            t = (b * s + f) / e

            # Clamp t to [0, 1]; if it was clamped, recompute s for that t and
            # clamp s in turn.
            if t < 0.0:
                t = 0.0
                s = _clamp(-c / a, 0.0, 1.0)
            elif t > 1.0:
                t = 1.0
                s = _clamp((b - c) / a, 0.0, 1.0)

    closest_p = p0 + d1 * s
    closest_q = q0 + d2 * t
    return float(np.linalg.norm(closest_p - closest_q))


def point_segment_distance(point, a, b):
    """Distance from a 3D point to a line segment ``a -> b``.

    Projects ``point`` onto the segment, clamps the projection parameter to
    ``[0, 1]`` so the closest point stays on the segment, and returns the
    distance to that clamped point. A zero-length segment reduces to the
    point-to-point distance.

    Args:
        point: The query point, array-like of length 3.
        a: Segment start point, array-like of length 3.
        b: Segment end point, array-like of length 3.

    Returns:
        The distance (float, >= 0) from ``point`` to the segment.
    """
    point = np.asarray(point, dtype=float)
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    ab = b - a
    ab_len_sq = float(np.dot(ab, ab))

    if ab_len_sq <= 1e-12:
        # Degenerate segment: distance to the single point.
        return float(np.linalg.norm(point - a))

    t = _clamp(float(np.dot(point - a, ab)) / ab_len_sq, 0.0, 1.0)
    closest = a + ab * t
    return float(np.linalg.norm(point - closest))


def capsule_capsule_distance(a, b):
    """Surface gap between two capsules.

    The segment-segment distance between the two capsule axes minus both radii.

    Args:
        a: The first capsule (expressed in world coordinates).
        b: The second capsule (expressed in world coordinates).

    Returns:
        The surface gap (float). ``<= 0`` means the capsules intersect; a
        negative magnitude is the penetration depth.
    """
    axis_distance = segment_segment_distance(a.p0, a.p1, b.p0, b.p1)
    return axis_distance - a.radius - b.radius


def capsule_sphere_distance(c, s):
    """Surface gap between a capsule and a sphere.

    The point-segment distance from the sphere center to the capsule axis minus
    both radii.

    Args:
        c: The capsule (expressed in world coordinates).
        s: The sphere (expressed in world coordinates).

    Returns:
        The surface gap (float). ``<= 0`` means they intersect; a negative
        magnitude is the penetration depth.
    """
    axis_distance = point_segment_distance(s.center, c.p0, c.p1)
    return axis_distance - c.radius - s.radius


def sphere_sphere_distance(a, b):
    """Surface gap between two spheres.

    The distance between the two centers minus both radii.

    Args:
        a: The first sphere (expressed in world coordinates).
        b: The second sphere (expressed in world coordinates).

    Returns:
        The surface gap (float). ``<= 0`` means the spheres intersect; a
        negative magnitude is the penetration depth.
    """
    center_distance = float(np.linalg.norm(np.asarray(a.center, dtype=float)
                                            - np.asarray(b.center, dtype=float)))
    return center_distance - a.radius - b.radius


def _clamp(value, low, high):
    """Clamps ``value`` into the closed interval ``[low, high]``.

    Args:
        value: The value to clamp.
        low: Lower bound.
        high: Upper bound.

    Returns:
        ``value`` restricted to ``[low, high]``.
    """
    if value < low:
        return low
    if value > high:
        return high
    return value
