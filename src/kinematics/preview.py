"""Optional 3D preview for the kinematic collision model.

Builds a 3D scene of the placed link proxies for a pose and highlights the links
involved in any colliding pair (Requirements 8.1, 8.2). It prefers ``trimesh``
for a proper rendered scene and falls back to a ``matplotlib`` 3D wireframe when
trimesh is unavailable or its viewer cannot start.

This module is imported ONLY when ``--preview`` is passed on the CLI (``cli.py``
does ``from kinematics import preview`` lazily inside ``_run_preview``), so
verdict computation never depends on it and never requires a display
(Requirement 8.3). Heavy / display-touching imports (``trimesh``, ``matplotlib``)
are performed inside the functions that need them, so importing this module by
itself does not require a rendering backend.

The public surface is:
    - ``build_scene(model, pose)`` -> a ``trimesh.Scene`` of the pose's proxies,
      colliding links colored red. Display-free, so it can be exercised headless.
    - ``show(model, poses, block=True)`` -> builds and displays a scene per pose.

This module imports no hardware libraries (Requirement 11.3).
"""

import numpy as np

from kinematics.collision import _proxy_to_world
from kinematics.proxies import Box, Capsule, Sphere

# RGBA face colors (0-255). Colliding links are highlighted red; every other
# proxied link is drawn in a neutral gray.
_COLLISION_RGBA = (220, 40, 40, 255)
_NEUTRAL_RGBA = (170, 170, 180, 200)


def _colliding_links(model, pose):
    """Returns the set of link names involved in any colliding pair for ``pose``.

    Runs the model's verdict path (``is_pose_safe``) and collects both links of
    every reported colliding pair, so the preview can highlight them
    (Requirement 8.2).

    Args:
        model: An initialized ``CollisionModel``.
        pose: A servo pose (channel -> degrees dict) to classify.

    Returns:
        A set of link names appearing in at least one colliding pair (empty when
        the pose is safe).
    """
    result = model.is_pose_safe(pose)
    links = set()
    for pair in result.colliding_pairs:
        links.add(pair.link_a)
        links.add(pair.link_b)
    return links


def _link_world_proxies(model, pose):
    """Places every proxied link into world coordinates for ``pose``.

    Converts the pose to URDF radians, runs forward kinematics, and transforms
    each link-local proxy into world coordinates using the shared
    ``_proxy_to_world`` helper from ``collision.py``.

    Args:
        model: An initialized ``CollisionModel``.
        pose: A servo pose (channel -> degrees dict).

    Returns:
        A dict mapping link name to its world-space proxy (``Capsule``,
        ``Sphere``, or ``Box``).
    """
    joint_radians = model._calibration.joint_radians(pose)
    link_transforms = model._engine.link_transforms(joint_radians)

    world_proxies = {}
    for link, proxy in model._detector._proxies.items():
        transform = link_transforms.get(link)
        if transform is None:
            print(f"[preview] no transform for proxied link {link!r}; skipping")
            continue
        world_proxies[link] = _proxy_to_world(proxy, transform)
    return world_proxies


def _capsule_transform(p0, p1):
    """Builds a 4x4 transform placing a Z-aligned unit capsule onto segment p0->p1.

    ``trimesh.creation.capsule`` returns a capsule centered at the origin and
    aligned with the local +Z axis. This computes a rigid transform that rotates
    +Z onto the ``p0 -> p1`` direction and translates the capsule so its segment
    midpoint lands at the segment midpoint of ``p0 -> p1``.

    Args:
        p0: Segment start point, array-like of length 3.
        p1: Segment end point, array-like of length 3.

    Returns:
        A 4x4 numpy homogeneous transform.
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    axis = p1 - p0
    length = float(np.linalg.norm(axis))

    transform = np.eye(4, dtype=float)
    if length <= 1e-12:
        # Degenerate segment: no rotation, sit at p0.
        transform[:3, 3] = p0
        return transform

    direction = axis / length
    z = np.array([0.0, 0.0, 1.0], dtype=float)
    cross = np.cross(z, direction)
    cross_norm = float(np.linalg.norm(cross))
    dot = float(np.dot(z, direction))

    if cross_norm <= 1e-12:
        # Parallel or anti-parallel to +Z.
        rotation = np.eye(3, dtype=float)
        if dot < 0:
            # Flip about X to point along -Z.
            rotation = np.diag([1.0, -1.0, -1.0])
    else:
        # Rodrigues' rotation formula for rotating z onto direction.
        k = cross / cross_norm
        angle = np.arccos(np.clip(dot, -1.0, 1.0))
        kx = np.array(
            [[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]],
            dtype=float,
        )
        rotation = (
            np.eye(3)
            + np.sin(angle) * kx
            + (1.0 - np.cos(angle)) * (kx @ kx)
        )

    transform[:3, :3] = rotation
    transform[:3, 3] = (p0 + p1) / 2.0
    return transform


def _proxy_to_mesh(proxy):
    """Converts a world-space proxy into an approximating ``trimesh`` mesh.

    ``trimesh`` is imported here (not at module top level) so simply importing
    ``preview`` never requires the extra.

    Mapping:
        - ``Sphere`` -> an icosphere at the sphere center.
        - ``Capsule`` -> a Z-aligned capsule of the segment length, rotated and
          translated onto the segment.
        - ``Box`` -> a box of ``2 * half_extents`` placed by center + axes.

    Args:
        proxy: A world-space ``Capsule``, ``Sphere``, or ``Box``.

    Returns:
        A ``trimesh.Trimesh`` approximating the proxy volume.

    Raises:
        TypeError: If ``proxy`` is not a supported proxy type.
    """
    import trimesh

    if isinstance(proxy, Sphere):
        mesh = trimesh.creation.icosphere(radius=float(proxy.radius))
        mesh.apply_translation(np.asarray(proxy.center, dtype=float))
        return mesh

    if isinstance(proxy, Capsule):
        length = float(np.linalg.norm(np.asarray(proxy.p1, dtype=float)
                                      - np.asarray(proxy.p0, dtype=float)))
        mesh = trimesh.creation.capsule(height=length, radius=float(proxy.radius))
        mesh.apply_transform(_capsule_transform(proxy.p0, proxy.p1))
        return mesh

    if isinstance(proxy, Box):
        half_extents = np.asarray(proxy.half_extents, dtype=float)
        mesh = trimesh.creation.box(extents=2.0 * half_extents)
        transform = np.eye(4, dtype=float)
        transform[:3, :3] = np.asarray(proxy.axes, dtype=float)
        transform[:3, 3] = np.asarray(proxy.center, dtype=float)
        mesh.apply_transform(transform)
        return mesh

    raise TypeError(f"unsupported proxy type: {type(proxy).__name__}")


def build_scene(model, pose):
    """Builds a ``trimesh.Scene`` of a pose's link proxies, highlighting collisions.

    Places every proxied link in world coordinates, meshes each proxy, and colors
    links that appear in any colliding pair red while drawing the rest in a
    neutral gray (Requirements 8.1, 8.2). This function does NOT open a viewer, so
    it is safe to call headless (used by ``show`` and by the CLI's display-free
    verification path).

    Args:
        model: An initialized ``CollisionModel``.
        pose: A servo pose (channel -> degrees dict) to render.

    Returns:
        A ``trimesh.Scene`` whose geometry is keyed by link name.

    Raises:
        RuntimeError: If ``trimesh`` (or a transitive extra) cannot be imported;
            the message is actionable.
    """
    try:
        import trimesh
    except ImportError as error:
        raise RuntimeError(
            "3D preview requires the 'trimesh' extra; install trimesh (and its "
            f"dependencies) to use --preview. Original import error: {error}"
        ) from error

    world_proxies = _link_world_proxies(model, pose)
    highlighted = _colliding_links(model, pose)
    print(
        f"[preview] building scene: {len(world_proxies)} proxied links, "
        f"{len(highlighted)} highlighted (colliding)"
    )

    scene = trimesh.Scene()
    for link, proxy in world_proxies.items():
        mesh = _proxy_to_mesh(proxy)
        rgba = _COLLISION_RGBA if link in highlighted else _NEUTRAL_RGBA
        mesh.visual.face_colors = np.array(rgba, dtype=np.uint8)
        scene.add_geometry(mesh, geom_name=link)
    return scene


def _show_matplotlib(model, pose, block):
    """Fallback preview: draws the pose's proxies as a ``matplotlib`` 3D wireframe.

    Used when ``trimesh`` is unavailable or its viewer cannot start. Renders each
    world-space proxy as a coarse point/line sketch (segments for capsules,
    center markers for spheres and boxes), coloring colliding links red
    (Requirements 8.1, 8.2).

    Args:
        model: An initialized ``CollisionModel``.
        pose: A servo pose (channel -> degrees dict).
        block: Whether the ``matplotlib`` window blocks until closed.

    Raises:
        RuntimeError: If ``matplotlib`` cannot be imported or the plot cannot be
            shown; the message is actionable.
    """
    try:
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 (registers 3d projection)
    except ImportError as error:
        raise RuntimeError(
            "3D preview fallback requires the 'matplotlib' extra; install "
            "matplotlib to preview without trimesh. Original import error: "
            f"{error}"
        ) from error

    world_proxies = _link_world_proxies(model, pose)
    highlighted = _colliding_links(model, pose)

    try:
        figure = plt.figure()
        axes = figure.add_subplot(111, projection="3d")
        for link, proxy in world_proxies.items():
            color = "red" if link in highlighted else "gray"
            if isinstance(proxy, Capsule):
                p0 = np.asarray(proxy.p0, dtype=float)
                p1 = np.asarray(proxy.p1, dtype=float)
                axes.plot(
                    [p0[0], p1[0]], [p0[1], p1[1]], [p0[2], p1[2]],
                    color=color, linewidth=2,
                )
            elif isinstance(proxy, Sphere):
                center = np.asarray(proxy.center, dtype=float)
                axes.scatter(center[0], center[1], center[2], color=color, s=40)
            elif isinstance(proxy, Box):
                center = np.asarray(proxy.center, dtype=float)
                axes.scatter(center[0], center[1], center[2], color=color, s=60, marker="s")
        axes.set_xlabel("x")
        axes.set_ylabel("y")
        axes.set_zlabel("z")
        plt.show(block=block)
    except Exception as error:  # noqa: BLE001 - surface any display/backend failure clearly
        raise RuntimeError(
            "3D preview requires a display and a working matplotlib backend; "
            f"rendering failed: {error}"
        ) from error


def show(model, poses, block=True):
    """Renders a 3D preview of one or more poses, highlighting colliding links.

    For each pose in ``poses`` builds a scene of the placed link proxies and
    displays it, coloring links in any colliding pair red (Requirements 8.1,
    8.2). Prefers ``trimesh``; if trimesh is unavailable or its viewer cannot
    start, falls back to a ``matplotlib`` 3D sketch. Because ``show`` is only
    reached under ``--preview``, a missing display or missing extras surfaces
    here as a clear ``RuntimeError`` rather than affecting verdicts (Requirement
    8.3).

    Args:
        model: An initialized ``CollisionModel``.
        poses: A list of servo poses (channel -> degrees dicts). At least the
            first pose is rendered; each pose is shown in turn.
        block: Whether each viewer blocks until closed. Passing ``False`` builds
            the scenes and requests a non-blocking display, which also makes the
            construction path exercisable without holding a window open.

    Raises:
        RuntimeError: If neither ``trimesh`` nor ``matplotlib`` can render (no
            display / missing extras); the message is actionable.
    """
    if not poses:
        print("[preview] no poses to preview")
        return

    for index, pose in enumerate(poses):
        print(f"[preview] rendering pose {index + 1}/{len(poses)}")
        try:
            scene = build_scene(model, pose)
        except RuntimeError as error:
            # trimesh unavailable: fall back to matplotlib. If that also fails,
            # its own RuntimeError propagates.
            print(f"[preview] trimesh scene unavailable ({error}); trying matplotlib")
            _show_matplotlib(model, pose, block)
            continue

        try:
            scene.show(block=block)
        except Exception as error:  # noqa: BLE001 - viewer/backend failure -> fall back
            print(
                f"[preview] trimesh viewer could not start ({error}); "
                "trying matplotlib fallback"
            )
            _show_matplotlib(model, pose, block)
