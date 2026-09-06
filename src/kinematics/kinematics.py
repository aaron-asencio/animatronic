"""Forward-kinematics engine for the Maximus kinematic collision model.

Loads the Maximus URDF (``src/config/maximus.urdf``) via ``yourdfpy`` and exposes
per-link forward kinematics, the link adjacency graph, the revolute joints
between any two links, and each link's ``<visual>`` primitive geometry.

Design intent (see design.md, Requirement 1):
    - Load the URDF with ``yourdfpy``; raise a clear error naming the path and the
      underlying failure when the file is missing or unparseable.
    - Compute every link's 4x4 world transform relative to ``base_link``, with
      ``base_link`` itself being the identity.
    - Unspecified revolute joints default to 0 radians; fixed joints move their
      child links rigidly with the parent chain.
    - The ``<visual>`` geometry is the geometric source (the URDF has no
      ``<collision>`` tags), and ``<limit>`` values are ignored entirely.

Cross-task decoupling note:
    ``proxies.py`` duck-types :class:`VisualPrimitive`, accessing only ``.kind``,
    ``.dims`` and ``.origin``. Those attribute names and their semantics are held
    fixed here so the two modules interoperate without a hard import dependency.

This module imports only ``numpy``, ``yourdfpy``, ``math`` and ``dataclasses`` --
no hardware libraries and no other kinematics submodules (Requirement 11.3).
"""

import math
from dataclasses import dataclass

import numpy as np
import yourdfpy


class KinematicsError(Exception):
    """Raised when the URDF cannot be loaded, parsed, or interrogated.

    The message names the offending URDF path and the underlying failure so the
    caller can distinguish a missing file from a malformed one.
    """


@dataclass(frozen=True)
class VisualPrimitive:
    """A link's ``<visual>`` geometry expressed in the link frame.

    ``proxies.make_proxy`` duck-types this object, reading only ``kind``,
    ``dims`` and ``origin`` -- keep those attribute names and semantics stable.

    Attributes:
        kind: The primitive kind, one of ``"box"``, ``"cylinder"`` or
            ``"sphere"``.
        dims: The primitive dimensions in meters. Box is ``(x, y, z)``; cylinder
            is ``(length, radius)``; sphere is ``(radius,)``.
        origin: A 4x4 numpy homogeneous transform built from the visual's local
            ``xyz`` translation and ``rpy`` (fixed-axis roll, pitch, yaw)
            rotation. Identity when the visual declares no origin.
    """

    kind: str
    dims: tuple
    origin: np.ndarray


def rpy_to_matrix(roll, pitch, yaw):
    """Builds a 3x3 rotation matrix from URDF fixed-axis roll, pitch, yaw.

    URDF ``rpy`` is a fixed-axis (extrinsic) rotation applied in the order roll
    about X, then pitch about Y, then yaw about Z, giving the composed matrix
    ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``.

    Args:
        roll: Rotation about the X axis, in radians.
        pitch: Rotation about the Y axis, in radians.
        yaw: Rotation about the Z axis, in radians.

    Returns:
        A 3x3 numpy rotation matrix.
    """
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)

    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    return rz @ ry @ rx


def _pose_from_xyz_rpy(xyz, rpy):
    """Builds a 4x4 homogeneous transform from an xyz translation and rpy.

    Args:
        xyz: A length-3 array-like translation in meters.
        rpy: A length-3 array-like of fixed-axis (roll, pitch, yaw) in radians.

    Returns:
        A 4x4 numpy homogeneous transform.
    """
    transform = np.eye(4)
    transform[:3, :3] = rpy_to_matrix(float(rpy[0]), float(rpy[1]), float(rpy[2]))
    transform[:3, 3] = np.asarray(xyz, dtype=float)
    return transform


def _decompose_xyz_rpy(matrix):
    """Recovers the (xyz, rpy) of a 4x4 transform in URDF fixed-axis convention.

    Inverts :func:`rpy_to_matrix` for the rotation block so a visual origin
    supplied as a 4x4 matrix can be rebuilt through the project's own rpy helper,
    keeping the ``VisualPrimitive.origin`` construction explicit and uniform.

    Args:
        matrix: A 4x4 numpy homogeneous transform.

    Returns:
        A tuple ``(xyz, rpy)`` where ``xyz`` is a length-3 translation and
        ``rpy`` is ``(roll, pitch, yaw)`` in radians.
    """
    m = np.asarray(matrix, dtype=float)
    xyz = m[:3, 3].copy()

    # Standard Rz*Ry*Rx decomposition. sy = -m[2,0] = sin(pitch).
    sy = -m[2, 0]
    sy = max(-1.0, min(1.0, sy))
    pitch = math.asin(sy)
    if abs(math.cos(pitch)) > 1e-8:
        roll = math.atan2(m[2, 1], m[2, 2])
        yaw = math.atan2(m[1, 0], m[0, 0])
    else:
        # Gimbal lock: fold roll into yaw.
        roll = 0.0
        yaw = math.atan2(-m[0, 1], m[1, 1])
    return xyz, np.array([roll, pitch, yaw])


class Kinematics_Engine:
    """Loads the Maximus URDF and computes per-link forward kinematics.

    Precomputes the link/joint graph on construction: for every joint it records
    its name, type, parent link, child link and axis, plus the parent-of-link and
    children-of-link maps used for path finding and adjacency.
    """

    # Named self-collision-exempt groups (MoveIt SRDF disable_collisions style).
    # Every unordered pair of members within a group is excluded from collision
    # testing. The neck column (base + the three neck links + head) is a coaxial
    # stack of cylinders whose only internal revolute joint is the neck-tilt
    # (pitch_neck_joint); it cannot self-collide in a damaging way, so all of its
    # internal pairs are exempt regardless of vertical spacing.
    SELF_COLLISION_EXEMPT_GROUPS = [
        frozenset(
            {
                "base_link",
                "lower_neck_link",
                "middle_neck_link",
                "upper_neck_link",
                "head_link",
            }
        )
    ]

    def __init__(self, urdf_path):
        """Loads the URDF via ``yourdfpy`` and precomputes the joint graph.

        Args:
            urdf_path: Path to the Maximus URDF (``src/config/maximus.urdf``).

        Raises:
            KinematicsError: If the URDF is missing or fails to parse; the message
                names the path and the underlying parse failure.
        """
        self.urdf_path = urdf_path
        try:
            self._robot = yourdfpy.URDF.load(urdf_path)
        except Exception as exc:  # yourdfpy raises a variety of low-level errors
            raise KinematicsError(
                f"failed to load URDF at {urdf_path!r}: {type(exc).__name__}: {exc}"
            ) from exc

        self.base_link = self._robot.base_link
        self.link_names = list(self._robot.link_map.keys())

        # Joint metadata + graph structures.
        self._joints = {}          # joint_name -> metadata dict
        self._parent_of_link = {}  # child_link -> joint_name
        self._children = {}        # parent_link -> list[child joint_name]
        for name, joint in self._robot.joint_map.items():
            axis = None
            if getattr(joint, "axis", None) is not None:
                axis = np.asarray(joint.axis, dtype=float)
            meta = {
                "name": name,
                "type": joint.type,
                "parent_link": joint.parent,
                "child_link": joint.child,
                "axis": axis,
            }
            self._joints[name] = meta
            self._parent_of_link[joint.child] = name
            self._children.setdefault(joint.parent, []).append(name)

        print(
            f"[kinematics] loaded {urdf_path!r}: "
            f"{len(self.link_names)} links, {len(self._joints)} joints, "
            f"base_link={self.base_link!r}"
        )

    def link_transforms(self, joint_radians):
        """Computes each link's 4x4 world transform relative to ``base_link``.

        Unspecified revolute joints default to 0 radians. Fixed joints are not
        actuated; their child links move rigidly with the parent chain. The
        returned transforms are normalized so ``base_link`` is exactly the
        identity: if the underlying FK reports transforms in a frame where
        ``base_link`` is not the identity, every transform is left-multiplied by
        the inverse of the ``base_link`` transform.

        Args:
            joint_radians: Mapping of URDF revolute joint name to angle in
                radians. Keys for fixed or unknown joints are ignored.

        Returns:
            A dict mapping ``link_name`` to a 4x4 numpy homogeneous transform,
            with ``base_link`` being the identity.
        """
        # Build a configuration for every actuated joint, defaulting to 0.
        cfg = {}
        for name in self._robot.actuated_joint_names:
            cfg[name] = float(joint_radians.get(name, 0.0))
        self._robot.update_cfg(cfg)

        transforms = {}
        for link in self.link_names:
            transforms[link] = np.asarray(
                self._robot.get_transform(frame_to=link, frame_from=self.base_link),
                dtype=float,
            )

        # Normalize so base_link is exactly identity.
        base = transforms[self.base_link]
        if not np.allclose(base, np.eye(4), atol=1e-9):
            base_inv = np.linalg.inv(base)
            for link in transforms:
                transforms[link] = base_inv @ transforms[link]

        return transforms

    def adjacency(self):
        """Returns the set of directly-connected link pairs.

        Every joint -- revolute AND fixed -- contributes its ``{parent, child}``
        link pair, so rigidly-connected links (e.g. ``lower_arm_link`` and
        ``hand_link`` across the fixed wrist) count as adjacent.

        Returns:
            A set of ``frozenset({parent_link, child_link})`` pairs.
        """
        pairs = set()
        for meta in self._joints.values():
            pairs.add(frozenset({meta["parent_link"], meta["child_link"]}))
        return pairs

    def excluded_pairs(self):
        """Returns every link pair that must NEVER be collision-tested.

        This is the superset the collision detector uses in place of raw
        ``adjacency()``. It mirrors a MoveIt SRDF ``disable_collisions`` list and
        is the union of three sources:

            1. Adjacency: every single-joint neighbor from :meth:`adjacency`
               (revolute AND fixed).
            2. Rigid pairs: every unordered pair of links with no revolute joint
               on the tree path between them (``len(joints_between(a, b)) == 0``).
               Such links are connected only through fixed joints, cannot move
               relative to one another, and so can never newly collide. This
               naturally captures coaxial neck pairs like
               ``lower_neck_link``/``middle_neck_link`` and
               ``upper_neck_link``/``head_link``.
            3. Self-exempt groups: every unordered pair of members within each
               group in :data:`SELF_COLLISION_EXEMPT_GROUPS`. Currently only the
               neck column, which is a coaxial stacked assembly driven solely by
               the neck-tilt joint (``pitch_neck_joint``) and cannot self-collide
               in a damaging way -- exactly the situation a MoveIt SRDF
               ``disable_collisions`` entry describes. Only links that exist in
               :attr:`link_names` are added, guarding against typos.

        Returns:
            A set of ``frozenset({link_a, link_b})`` pairs to exclude.
        """
        excluded = set(self.adjacency())

        # 2. Rigidly-attached pairs: no revolute joint between them.
        links = self.link_names
        for i in range(len(links)):
            for j in range(i + 1, len(links)):
                if len(self.joints_between(links[i], links[j])) == 0:
                    excluded.add(frozenset({links[i], links[j]}))

        # 3. Named self-collision-exempt groups (MoveIt SRDF disable_collisions).
        known = set(self.link_names)
        for group in self.SELF_COLLISION_EXEMPT_GROUPS:
            members = [link for link in group if link in known]
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    excluded.add(frozenset({members[i], members[j]}))

        print(
            f"[kinematics] excluded_pairs: {len(excluded)} pairs "
            f"(adjacent + rigid + self-exempt groups)"
        )
        return excluded

    def _path_to_root(self, link):
        """Returns the list of links from ``link`` up to the root, inclusive.

        Args:
            link: The link to walk upward from.

        Returns:
            A list of link names ordered from ``link`` to the root.
        """
        path = [link]
        current = link
        while current in self._parent_of_link:
            joint = self._parent_of_link[current]
            parent = self._joints[joint]["parent_link"]
            path.append(parent)
            current = parent
        return path

    def joints_between(self, link_a, link_b):
        """Returns the revolute joints on the tree path between two links.

        Walks up from each link to their lowest common ancestor and collects the
        joints spanning that path, keeping only revolute joints (fixed joints are
        excluded because they cannot contribute to a collision).

        Args:
            link_a: First link name.
            link_b: Second link name.

        Returns:
            A list of revolute URDF joint names on the path between the two links.
        """
        path_a = self._path_to_root(link_a)
        path_b = self._path_to_root(link_b)
        set_b = set(path_b)

        # Lowest common ancestor: first link on path_a that is also on path_b.
        lca = None
        for link in path_a:
            if link in set_b:
                lca = link
                break

        # Collect the child links on each branch down to (but not including) LCA;
        # the joint feeding each such link is the joint on the path.
        branch_links = []
        for link in path_a:
            if link == lca:
                break
            branch_links.append(link)
        for link in path_b:
            if link == lca:
                break
            branch_links.append(link)

        revolute = []
        for link in branch_links:
            joint = self._parent_of_link.get(link)
            if joint is None:
                continue
            if self._joints[joint]["type"] == "revolute":
                revolute.append(joint)
        return revolute

    def visual_geometry(self):
        """Returns each link's ``<visual>`` primitive.

        Maps the yourdfpy geometry (Box / Cylinder / Sphere) of each link that
        declares a ``<visual>`` into a :class:`VisualPrimitive`, building the
        origin 4x4 from the visual's local ``xyz`` + ``rpy`` via
        :func:`rpy_to_matrix`.

        Returns:
            A dict mapping ``link_name`` to a :class:`VisualPrimitive`. Links
            without visual geometry are omitted.
        """
        result = {}
        for name, link in self._robot.link_map.items():
            visuals = getattr(link, "visuals", None)
            if not visuals:
                continue
            visual = visuals[0]
            geometry = visual.geometry

            # Rebuild the origin through the project's own rpy helper. yourdfpy
            # supplies the origin as a 4x4 (or None => identity); decompose to
            # xyz + rpy and reconstruct so the origin is always built from
            # xyz + rpy as the design specifies.
            if visual.origin is None:
                origin = np.eye(4)
            else:
                xyz, rpy = _decompose_xyz_rpy(visual.origin)
                origin = _pose_from_xyz_rpy(xyz, rpy)

            if geometry.box is not None:
                size = np.asarray(geometry.box.size, dtype=float)
                primitive = VisualPrimitive(
                    kind="box",
                    dims=(float(size[0]), float(size[1]), float(size[2])),
                    origin=origin,
                )
            elif geometry.cylinder is not None:
                primitive = VisualPrimitive(
                    kind="cylinder",
                    dims=(float(geometry.cylinder.length), float(geometry.cylinder.radius)),
                    origin=origin,
                )
            elif geometry.sphere is not None:
                primitive = VisualPrimitive(
                    kind="sphere",
                    dims=(float(geometry.sphere.radius),),
                    origin=origin,
                )
            else:
                print(f"[kinematics] link {name!r} has an unsupported visual geometry; skipping")
                continue

            result[name] = primitive

        return result
