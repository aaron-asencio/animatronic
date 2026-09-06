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
    - ``show(model, poses, block=True)`` -> builds and displays a scene per pose
      in an interactive window (needs a display / viewer backend).
    - ``save_png(model, pose, out_path, ...)`` -> renders the pose to an image
      file OFFSCREEN via matplotlib's non-interactive ``Agg`` backend, so it
      works on a headless Pi with no display, no X-forwarding, and no pyglet.

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


def _sphere_surface(center, radius, resolution=12):
    """Returns meshgrid X/Y/Z arrays for a sphere surface (for plot_surface).

    Args:
        center: Sphere center, array-like of length 3.
        radius: Sphere radius in meters.
        resolution: Number of samples along each spherical angle.

    Returns:
        A tuple ``(x, y, z)`` of 2D numpy arrays describing the sphere surface.
    """
    center = np.asarray(center, dtype=float)
    u = np.linspace(0.0, 2.0 * np.pi, resolution)
    v = np.linspace(0.0, np.pi, resolution)
    x = center[0] + radius * np.outer(np.cos(u), np.sin(v))
    y = center[1] + radius * np.outer(np.sin(u), np.sin(v))
    z = center[2] + radius * np.outer(np.ones_like(u), np.cos(v))
    return x, y, z


def _capsule_surface(p0, p1, radius, resolution=12):
    """Returns meshgrid X/Y/Z arrays approximating a capsule's cylindrical body.

    Draws just the cylindrical side wall spanning ``p0 -> p1`` (the hemispherical
    end caps are omitted for a lightweight sketch). An orthonormal frame is built
    around the segment axis so the tube follows any orientation.

    Args:
        p0: Segment start point, array-like of length 3.
        p1: Segment end point, array-like of length 3.
        radius: Capsule radius in meters.
        resolution: Number of samples around the circumference / along the axis.

    Returns:
        A tuple ``(x, y, z)`` of 2D numpy arrays describing the tube surface.
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    axis = p1 - p0
    length = float(np.linalg.norm(axis))
    if length <= 1e-12:
        return _sphere_surface(p0, radius, resolution)

    direction = axis / length
    # Pick a reference not parallel to the axis, then build an orthonormal frame.
    reference = np.array([1.0, 0.0, 0.0]) if abs(direction[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u_axis = np.cross(direction, reference)
    u_axis /= np.linalg.norm(u_axis)
    v_axis = np.cross(direction, u_axis)

    theta = np.linspace(0.0, 2.0 * np.pi, resolution)
    t = np.linspace(0.0, length, 2)
    theta_grid, t_grid = np.meshgrid(theta, t)

    ring = (
        radius * np.cos(theta_grid)[..., None] * u_axis
        + radius * np.sin(theta_grid)[..., None] * v_axis
    )
    centers = p0[None, None, :] + t_grid[..., None] * direction
    surface = centers + ring
    return surface[..., 0], surface[..., 1], surface[..., 2]


def _box_faces(center, axes, half_extents):
    """Returns the 6 quad faces (each a list of 4 corner points) of an OBB.

    Args:
        center: Box center, array-like of length 3.
        axes: 3x3 orthonormal column axes of the box.
        half_extents: Half-sizes along each local axis, array-like of length 3.

    Returns:
        A list of 6 faces, each a list of four length-3 numpy corner points.
    """
    center = np.asarray(center, dtype=float)
    axes = np.asarray(axes, dtype=float)
    half = np.asarray(half_extents, dtype=float)

    # 8 corners via all +-half combinations along the local axes.
    corners = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                offset = (
                    sx * half[0] * axes[:, 0]
                    + sy * half[1] * axes[:, 1]
                    + sz * half[2] * axes[:, 2]
                )
                corners.append(center + offset)
    # Corner index order matches (sx,sy,sz) bits: 000,001,010,011,100,101,110,111.
    c = corners
    faces = [
        [c[0], c[1], c[3], c[2]],  # -x
        [c[4], c[5], c[7], c[6]],  # +x
        [c[0], c[1], c[5], c[4]],  # -y
        [c[2], c[3], c[7], c[6]],  # +y
        [c[0], c[2], c[6], c[4]],  # -z
        [c[1], c[3], c[7], c[5]],  # +z
    ]
    return faces


def save_png(model, pose, out_path, elev=20.0, azim=-60.0, dpi=140):
    """Renders a pose's proxies to an image file OFFSCREEN (headless-safe).

    Uses matplotlib's non-interactive ``Agg`` backend so it needs no display,
    no X-forwarding, and no ``pyglet`` -- ideal for a headless Raspberry Pi.
    Proxies are drawn as translucent 3D solids (spheres, capsule tubes, and OBB
    faces); links in any colliding pair are colored red, the rest neutral gray
    (Requirements 8.1, 8.2).

    Args:
        model: An initialized ``CollisionModel``.
        pose: A servo pose (channel -> degrees dict) to render.
        out_path: Filesystem path for the output image (e.g. ``preview.png``).
        elev: Camera elevation angle in degrees.
        azim: Camera azimuth angle in degrees.
        dpi: Output image resolution.

    Returns:
        The ``out_path`` written.

    Raises:
        RuntimeError: If ``matplotlib`` cannot be imported; the message is
            actionable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend: no display required.
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 (registers 3d projection)
    except ImportError as error:
        raise RuntimeError(
            "Saving a preview image requires 'matplotlib'; install it to use "
            f"--preview-out. Original import error: {error}"
        ) from error

    world_proxies = _link_world_proxies(model, pose)
    highlighted = _colliding_links(model, pose)
    print(
        f"[preview] rendering PNG: {len(world_proxies)} proxied links, "
        f"{len(highlighted)} highlighted (colliding) -> {out_path}"
    )

    figure = plt.figure(figsize=(8, 8))
    axes = figure.add_subplot(111, projection="3d")

    all_points = []
    for link, proxy in world_proxies.items():
        color = "red" if link in highlighted else "gray"
        alpha = 0.5 if link in highlighted else 0.25
        if isinstance(proxy, Sphere):
            x, y, z = _sphere_surface(proxy.center, proxy.radius)
            axes.plot_surface(x, y, z, color=color, alpha=alpha, linewidth=0)
            all_points.append(np.asarray(proxy.center, dtype=float))
        elif isinstance(proxy, Capsule):
            x, y, z = _capsule_surface(proxy.p0, proxy.p1, proxy.radius)
            axes.plot_surface(x, y, z, color=color, alpha=alpha, linewidth=0)
            all_points.append(np.asarray(proxy.p0, dtype=float))
            all_points.append(np.asarray(proxy.p1, dtype=float))
        elif isinstance(proxy, Box):
            faces = _box_faces(proxy.center, proxy.axes, proxy.half_extents)
            collection = Poly3DCollection(
                faces, facecolor=color, alpha=alpha, edgecolor="k", linewidths=0.3
            )
            axes.add_collection3d(collection)
            all_points.extend(faces[0] + faces[1])

    # Equal aspect: expand to a cubic bounding box around all drawn points.
    if all_points:
        pts = np.asarray(all_points, dtype=float)
        mins = pts.min(axis=0)
        maxs = pts.max(axis=0)
        center = (mins + maxs) / 2.0
        span = float((maxs - mins).max()) / 2.0 + 0.05
        axes.set_xlim(center[0] - span, center[0] + span)
        axes.set_ylim(center[1] - span, center[1] + span)
        axes.set_zlim(center[2] - span, center[2] + span)

    axes.set_xlabel("x")
    axes.set_ylabel("y")
    axes.set_zlabel("z")
    verdict = "COLLISION" if highlighted else "SAFE"
    axes.set_title(f"Maximus pose preview - {verdict}")
    axes.view_init(elev=elev, azim=azim)

    figure.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)
    print(f"[preview] wrote {out_path}")
    return out_path
