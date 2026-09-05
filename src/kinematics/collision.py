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

from dataclasses import dataclass

import numpy as np

from kinematics.proxies import Capsule, Sphere, apply_transform


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


@dataclass(frozen=True)
class DetectedPair:
    """A detected self-collision between two non-adjacent links.

    Attributes:
        link_a: First link name. ``link_a`` and ``link_b`` are ordered
            deterministically (sorted) so results are stable and symmetric.
        link_b: Second link name (sorts after ``link_a``).
        gap: The surface gap between the two link proxies, in meters. ``<= 0``
            means the proxies intersect; a negative magnitude is the penetration
            depth.
        joints: Tuple of URDF revolute joint names on the tree path between the
            two links (the offending joints). Servo-channel resolution is done
            later at the ``model.py`` facade layer, so only URDF joint names are
            reported here.
    """

    link_a: str
    link_b: str
    gap: float
    joints: tuple


def _proxy_to_world(proxy, transform):
    """Places a link-local proxy into world coordinates.

    Transforms a :class:`Capsule` (both endpoints) or :class:`Sphere` (center)
    through the link's world transform. The radius is a scalar extent and is left
    unchanged (the transforms are rigid).

    Args:
        proxy: A ``Capsule`` or ``Sphere`` expressed in its link's local frame.
        transform: The link's 4x4 world transform.

    Returns:
        A new ``Capsule`` or ``Sphere`` expressed in world coordinates.

    Raises:
        TypeError: If ``proxy`` is neither a ``Capsule`` nor a ``Sphere``.
    """
    if isinstance(proxy, Capsule):
        return Capsule(
            p0=apply_transform(transform, proxy.p0),
            p1=apply_transform(transform, proxy.p1),
            radius=proxy.radius,
        )
    if isinstance(proxy, Sphere):
        return Sphere(
            center=apply_transform(transform, proxy.center),
            radius=proxy.radius,
        )
    raise TypeError(f"unsupported proxy type: {type(proxy).__name__}")


def _pair_gap(proxy_a, proxy_b):
    """Computes the surface gap between two world-space proxies.

    Dispatches to the correct analytic distance function based on the proxy
    types, handling the mixed capsule/sphere ordering (``capsule_sphere_distance``
    expects ``(capsule, sphere)``).

    Args:
        proxy_a: The first world-space ``Capsule`` or ``Sphere``.
        proxy_b: The second world-space ``Capsule`` or ``Sphere``.

    Returns:
        The surface gap (float). ``<= 0`` means the proxies intersect.
    """
    a_is_capsule = isinstance(proxy_a, Capsule)
    b_is_capsule = isinstance(proxy_b, Capsule)

    if a_is_capsule and b_is_capsule:
        return capsule_capsule_distance(proxy_a, proxy_b)
    if not a_is_capsule and not b_is_capsule:
        return sphere_sphere_distance(proxy_a, proxy_b)
    # Mixed: capsule_sphere_distance takes (capsule, sphere).
    if a_is_capsule:
        return capsule_sphere_distance(proxy_a, proxy_b)
    return capsule_sphere_distance(proxy_b, proxy_a)


class Collision_Detector:
    """Detects self-collisions between placed link proxies for a pose.

    Places every link proxy in world coordinates using the engine's link
    transforms, tests every unordered pair of proxied links (skipping directly
    adjacent link pairs), and maps each colliding pair to the offending revolute
    URDF joints on the tree path between the links.

    The detector reports only URDF joint names; the servo-channel mapping is done
    later at the ``model.py`` facade layer, so this class deliberately does not
    import ``Calibration_Store`` (no hard dependency).
    """

    def __init__(self, engine, proxies):
        """Precomputes the adjacency exclusion set from the joint graph.

        Args:
            engine: A ``kinematics.kinematics.Kinematics_Engine`` instance, used
                for its ``adjacency()`` and ``joints_between(...)`` graph queries.
            proxies: Dict mapping ``link_name`` to a proxy object (``Capsule`` or
                ``Sphere`` from ``kinematics.proxies``), each expressed in that
                link's LOCAL frame.
        """
        self._engine = engine
        self._proxies = dict(proxies)
        # Adjacency exclusion set: frozenset({link_a, link_b}) pairs that are
        # directly connected by a single joint and must never be reported.
        self._adjacent = engine.adjacency()
        print(
            f"[collision] Collision_Detector ready: {len(self._proxies)} proxied "
            f"links, {len(self._adjacent)} adjacent pairs excluded"
        )

    def check(self, link_transforms):
        """Detects colliding link pairs for a single pose.

        Places each proxied link into world coordinates, tests every unordered
        pair of proxied links (skipping adjacent pairs), and returns a
        :class:`DetectedPair` for each pair whose surface gap is ``<= 0``.

        Args:
            link_transforms: Dict mapping ``link_name`` to a 4x4 numpy world
                transform (from ``engine.link_transforms(...)``, with
                ``base_link`` = identity).

        Returns:
            A list of :class:`DetectedPair` results, one per colliding pair.
        """
        # Place every proxied link in world coordinates once.
        world_proxies = {}
        for link, proxy in self._proxies.items():
            transform = link_transforms.get(link)
            if transform is None:
                print(f"[collision] no transform for proxied link {link!r}; skipping")
                continue
            world_proxies[link] = _proxy_to_world(proxy, transform)

        links = sorted(world_proxies.keys())
        results = []
        for i in range(len(links)):
            for j in range(i + 1, len(links)):
                link_a = links[i]
                link_b = links[j]

                if frozenset({link_a, link_b}) in self._adjacent:
                    continue

                gap = _pair_gap(world_proxies[link_a], world_proxies[link_b])
                if gap <= 0:
                    joints = tuple(self._engine.joints_between(link_a, link_b))
                    print(
                        f"[collision] COLLISION {link_a!r} <-> {link_b!r} "
                        f"gap={gap:.4f} joints={joints}"
                    )
                    results.append(
                        DetectedPair(
                            link_a=link_a,
                            link_b=link_b,
                            gap=gap,
                            joints=joints,
                        )
                    )

        print(f"[collision] check complete: {len(results)} colliding pair(s)")
        return results
