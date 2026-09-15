"""CollisionModel facade and result data models for the kinematic collision model.

This module ties the four building blocks of the Maximus kinematic collision model
together behind a single facade: the ``Calibration_Store`` (servo degrees -> URDF
radians), the ``Kinematics_Engine`` (URDF forward kinematics), collision-proxy
generation (``make_proxy``), and the ``Collision_Detector`` (analytic proxy
intersection + offending-joint mapping).

Design intent (see design.md, Requirements 6.x / 7.x / 10.x / 11.3):
    - ``is_pose_safe`` is a pure, deterministic classification of a single servo
      pose given fixed calibration + URDF: no randomness, no shared-state
      mutation between calls (Requirement 6.1, Property 10).
    - The returned ``PoseResult`` always satisfies ``ok == (colliding_pairs is
      empty)`` (Requirements 6.2, 6.3, Property 11).
    - Missing required channels or non-numeric angles raise ``PoseInputError``
      naming the offending channel (Requirement 6.4).
    - ``check_sequence`` classifies poses in order; the sequence is ``unsafe``
      iff any pose is unsafe (Requirements 7.2, 7.5, Property 12).
    - The facade exposes NO servo-write or gating interface and leaves the
      ``TrunkController`` write path untouched (Requirements 10.1-10.3).

This module imports no hardware libraries (Requirements 6.5, 11.3): only the
kinematics submodules and ``constants`` (for human-readable servo names). It does
not import or touch ``TrunkController``.
"""

from dataclasses import dataclass, field

import constants
from kinematics.calibration import Calibration_Store
from kinematics.collision import Collision_Detector
from kinematics.kinematics import Kinematics_Engine
from kinematics.proxies import make_proxy


@dataclass(frozen=True)
class OffendingJoint:
    """A joint implicated in a colliding link pair.

    Attributes:
        urdf_joint: URDF revolute joint name (e.g. "shoulder_yaw_joint").
        servo_channel: PCA9685 channel number driving the joint.
        servo_name: Human-readable servo name from ``constants.servos`` (falls
            back to the string form of the channel when unknown).
    """

    urdf_joint: str
    servo_channel: int
    servo_name: str


@dataclass(frozen=True)
class CollisionPair:
    """A reported colliding link pair.

    Attributes:
        link_a: First link name.
        link_b: Second link name.
        offending_joints: The joints on the tree path between the two links, in
            the order reported by the detector.
    """

    link_a: str
    link_b: str
    offending_joints: list


@dataclass(frozen=True)
class PoseResult:
    """The verdict for a single pose.

    The invariant ``ok == (colliding_pairs is empty)`` always holds.

    Attributes:
        ok: True when no colliding pairs were found.
        colliding_pairs: The colliding link pairs (empty when ``ok`` is True).
    """

    ok: bool
    colliding_pairs: list = field(default_factory=list)


@dataclass(frozen=True)
class SequenceResult:
    """The verdict for a pose sequence.

    Attributes:
        per_pose: The ``PoseResult`` for each input pose, in order.
        unsafe: True when any pose in the sequence is unsafe.
    """

    per_pose: list
    unsafe: bool


class PoseInputError(Exception):
    """Raised when a pose is missing a required channel or has a non-numeric angle.

    The message names the offending servo channel so the caller can correct the
    pose (Requirement 6.4).
    """


class CollisionModel:
    """Facade tying calibration, kinematics, proxies, and detection together.

    Offline gesture-authoring aid only: it classifies servo poses as SAFE or
    COLLISION and exposes no servo-write or gating interface (Requirements
    10.1-10.3). It imports no hardware libraries (Requirements 6.5, 11.3).
    """

    def __init__(
        self,
        urdf_path="src/config/maximus.urdf",
        calibration_path="src/config/calibration.json",
        inflation_margin=0.005,
    ):
        """Initializes the model, loading the URDF, calibration, and proxies.

        Builds the calibration store and forward-kinematics engine, generates a
        conservative collision proxy for every link with visual geometry
        (inflated by ``inflation_margin``), and wires up the collision detector.

        Args:
            urdf_path: Path to the Maximus URDF.
            calibration_path: Path to the editable calibration JSON (auto-seeded
                if missing).
            inflation_margin: Non-negative proxy inflation (safety) distance in
                meters. Defaults to 0.005 (5 mm): the raw Maximus geometry is
                collision-free at the rest pose, and 5 mm keeps that pose clear
                while still providing a conservative safety buffer.
        """
        self._calibration = Calibration_Store(calibration_path)
        self._engine = Kinematics_Engine(urdf_path)

        proxies = {
            link: make_proxy(primitive, inflation_margin)
            for link, primitive in self._engine.visual_geometry().items()
        }
        self._detector = Collision_Detector(self._engine, proxies)

        # Required channels are exactly the calibrated servo channels ({0,1,4,5,6,7}).
        self._required_channels = set(self._calibration._by_channel.keys())
        print(
            f"[model] CollisionModel ready: {len(proxies)} proxies, "
            f"required channels {sorted(self._required_channels)}"
        )

    def is_pose_safe(self, servo_angles):
        """Classifies a single servo pose as safe or colliding.

        Validates that every required servo channel is present with a numeric
        angle, converts the pose to URDF radians, runs forward kinematics and
        self-collision detection, and maps each detected pair to a
        ``CollisionPair`` with servo-resolved offending joints.

        Args:
            servo_angles: PCA9685 channel -> angle in degrees. Must contain the
                six mapped channels with numeric values.

        Returns:
            A ``PoseResult`` where ``ok`` is True iff ``colliding_pairs`` is
            empty.

        Raises:
            PoseInputError: If a required channel is missing or an angle is
                non-numeric (booleans are rejected); the message names the
                offending channel.
        """
        self._validate_pose(servo_angles)

        joint_radians = self._calibration.joint_radians(servo_angles)
        link_transforms = self._engine.link_transforms(joint_radians)
        detected = self._detector.check(link_transforms)

        colliding_pairs = [self._to_collision_pair(pair) for pair in detected]

        # Hard guard for measured multi-axis danger zones the decoupled model
        # under-predicts (e.g. the hand-to-face fold). See
        # constants.FORBIDDEN_COMBINATIONS.
        colliding_pairs.extend(self._forbidden_combination_pairs(servo_angles))

        return PoseResult(ok=(len(colliding_pairs) == 0), colliding_pairs=colliding_pairs)

    def _forbidden_combination_pairs(self, servo_angles):
        """Returns a CollisionPair for each forbidden joint combination matched.

        Complements the geometric detector with explicit, measured multi-axis
        rules (``constants.FORBIDDEN_COMBINATIONS``). A rule matches when every
        listed channel is within its inclusive ``(min, max)`` range. Each match
        becomes a ``CollisionPair`` whose links name the rule and whose
        offending joints are the channels that triggered it, so it flows through
        the same ``ok == (colliding_pairs empty)`` invariant, CLI output, and
        preview highlighting as a geometric collision.

        Args:
            servo_angles: The pose mapping of servo channel -> angle in degrees.

        Returns:
            A list of ``CollisionPair`` (empty when no forbidden rule matches).
        """
        pairs = []
        for rule in getattr(constants, "FORBIDDEN_COMBINATIONS", []):
            ranges = rule["ranges"]
            matched = all(
                channel in servo_angles
                and lo <= servo_angles[channel] <= hi
                for channel, (lo, hi) in ranges.items()
            )
            if not matched:
                continue
            offending = [
                OffendingJoint(
                    urdf_joint=self._channel_to_urdf_joint(channel),
                    servo_channel=channel,
                    servo_name=constants.servos.get(channel, str(channel)),
                )
                for channel in ranges
            ]
            pairs.append(
                CollisionPair(
                    link_a="forbidden-combination",
                    link_b=rule["reason"],
                    offending_joints=offending,
                )
            )
        return pairs

    def _channel_to_urdf_joint(self, channel):
        """Returns the URDF joint name for a servo channel, or the channel string.

        Args:
            channel: PCA9685 channel number.

        Returns:
            The URDF joint name calibrated for that channel, or ``str(channel)``
            when the channel is not in the calibration store.
        """
        cal = self._calibration._by_channel.get(channel)
        return cal.urdf_joint if cal else str(channel)

    def check_sequence(self, poses):
        """Classifies an ordered sequence of poses.

        Runs ``is_pose_safe`` on each pose in order and aggregates the verdicts.

        Args:
            poses: Ordered list of servo poses (each a channel -> degrees dict).

        Returns:
            A ``SequenceResult`` with a per-pose result list (in order) and an
            ``unsafe`` flag that is True iff any pose is unsafe.

        Raises:
            PoseInputError: If any pose is missing a required channel or has a
                non-numeric angle; the message names the offending channel.
        """
        per_pose = [self.is_pose_safe(pose) for pose in poses]
        unsafe = any(not result.ok for result in per_pose)
        return SequenceResult(per_pose=per_pose, unsafe=unsafe)

    def _validate_pose(self, servo_angles):
        """Validates that all required channels are present and numeric.

        Args:
            servo_angles: The pose mapping of servo channel -> angle in degrees.

        Raises:
            PoseInputError: If a required channel is missing, or its value is not
                numeric. Booleans are rejected because ``bool`` is a subclass of
                ``int``. The message names the offending channel.
        """
        for channel in sorted(self._required_channels):
            if channel not in servo_angles:
                raise PoseInputError(
                    f"pose is missing required servo channel {channel}"
                )
            value = servo_angles[channel]
            # bool is a subclass of int; reject it as a non-numeric angle.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PoseInputError(
                    f"servo channel {channel} angle must be numeric, got {value!r}"
                )

    def _to_collision_pair(self, detected_pair):
        """Maps a detector ``DetectedPair`` to a servo-resolved ``CollisionPair``.

        For each URDF joint name reported by the detector, resolves its servo
        channel from the calibration store and its human-readable name from
        ``constants.servos``, preserving the joint order.

        Args:
            detected_pair: A ``DetectedPair`` from ``Collision_Detector.check``.

        Returns:
            A ``CollisionPair`` naming the two links and their offending joints.
        """
        offending_joints = []
        for urdf_joint in detected_pair.joints:
            channel = self._calibration._by_joint[urdf_joint].servo_channel
            servo_name = constants.servos.get(channel, str(channel))
            offending_joints.append(
                OffendingJoint(
                    urdf_joint=urdf_joint,
                    servo_channel=channel,
                    servo_name=servo_name,
                )
            )
        return CollisionPair(
            link_a=detected_pair.link_a,
            link_b=detected_pair.link_b,
            offending_joints=offending_joints,
        )
