"""Per-joint servo-degree to URDF-radian calibration and its editable JSON store.

This module owns the ``Calibration_Transform`` of the kinematic collision model:
the mapping between servo angles (degrees, 0-270, empirically reseated non-zero
centers) and URDF joint angles (radians, zero-centered, per-joint axis signs).

The transform is::

    urdf_rad = sign * (servo_deg - offset_deg) * (pi / 180) * scale

Every per-joint ``sign``, ``offset_deg``, and ``scale`` is a PROVISIONAL seed
derived from the AXIS DIRECTION REFERENCE landmarks in ``constants.py``; none is
trusted until confirmed against the physical robot by the hardware validation
workflow. The ``1.0`` scales for the direct-drive joints are an UNVERIFIED
ASSUMPTION (expected near 1.0 but pending measurement); only
``RT_SHOULDER_ROTATOR`` has been observed strongly geared (~170/270).

Depends only on the standard library (``json``, ``math``, ``dataclasses``,
``pathlib``) and imports no hardware libraries and no other kinematics submodule,
so it stays importable and testable without a Raspberry Pi.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path


# Required numeric fields for each calibration entry in the JSON store.
_REQUIRED_FIELDS = ("servo_channel", "sign", "offset_deg", "scale")


@dataclass(frozen=True)
class JointCalibration:
    """Per-joint servo-degree to URDF-radian calibration.

    Attributes:
        urdf_joint: URDF revolute joint name (e.g. "yawl_neck_joint").
        servo_channel: PCA9685 channel number from constants.py.
        sign: +1 or -1, direction reconciliation between servo and URDF axis.
        offset_deg: Servo angle (deg) that maps to 0 radians in the URDF.
        scale: Multiplier mapping a servo-degree delta to the physical/URDF-radian
            delta. PROVISIONAL until hardware-measured -- expected near 1.0 for
            direct-drive joints and ~170/270 for the geared shoulder rotator, but
            every joint's scale is an empirical unknown that must be validated
            against the physical robot.
    """

    urdf_joint: str
    servo_channel: int
    sign: float
    offset_deg: float
    scale: float


class CalibrationError(Exception):
    """Raised when the calibration store is missing or malformed."""


class Calibration_Store:
    """Loads, seeds, validates, and applies per-joint calibration transforms.

    The store is keyed both by URDF joint name and by servo channel so callers can
    convert in either direction. When the backing JSON file is absent it is created
    seeded with ``seed_defaults()``; when present it is loaded and validated.
    """

    def __init__(self, path):
        """Loads calibration from ``path``, seeding the file if it is absent.

        Args:
            path: Filesystem path (str or Path) to the editable calibration JSON
                file. If the file does not exist it is created seeded with
                ``seed_defaults()``; if it exists it is loaded and validated.

        Raises:
            CalibrationError: If a loaded entry is missing a required field or a
                field is not numeric; the message names the joint and the field.
        """
        self._path = Path(path)

        if not self._path.exists():
            # Requirement 3.3: seed and write the file when it is missing.
            by_joint = self.seed_defaults()
            self._write_seed(by_joint)
            print(f"[calibration] seeded new calibration file at {self._path}")
        else:
            by_joint = self._load_and_validate(self._path)
            print(f"[calibration] loaded calibration from {self._path}")

        # Index by both URDF joint name and servo channel for two-way lookup.
        self._by_joint = by_joint
        self._by_channel = {
            cal.servo_channel: cal for cal in by_joint.values()
        }

    @staticmethod
    def seed_defaults():
        """Returns provisional seed calibrations derived from constants.py.

        Every value is a PROVISIONAL starting estimate from the AXIS DIRECTION
        REFERENCE landmarks. All signs are +1 provisional guesses; all offsets are
        landmark-center estimates; every scale requires hardware validation. The
        ``1.0`` scales for the direct-drive joints are an UNVERIFIED ASSUMPTION
        (expected near 1.0 but must be measured), and the geared shoulder-rotator
        scale (~170/270) is an observation to reconfirm.

        Returns:
            A dict mapping URDF joint name to its provisional ``JointCalibration``.
        """
        seeds = (
            # urdf_joint,           channel, sign, offset_deg, scale
            ("yawl_neck_joint", 0, 1.0, 90.0, 1.0),
            ("pitch_neck_joint", 1, 1.0, 90.0, 1.0),
            ("shoulder_yaw_joint", 6, 1.0, 135.0, 1.0),
            ("shoulder_pitch_joint", 7, 1.0, 0.0, 0.6296),
            ("elbow_yaw_joint", 4, 1.0, 150.0, 1.0),
            ("elbow_pitch_joint", 5, 1.0, 5.0, 1.0),
        )
        return {
            urdf_joint: JointCalibration(
                urdf_joint=urdf_joint,
                servo_channel=channel,
                sign=sign,
                offset_deg=offset_deg,
                scale=scale,
            )
            for urdf_joint, channel, sign, offset_deg, scale in seeds
        }

    def to_radians(self, servo_channel, servo_deg):
        """Converts a servo angle in degrees to URDF radians.

        Args:
            servo_channel: PCA9685 channel number.
            servo_deg: Commanded servo angle in degrees.

        Returns:
            The URDF joint angle in radians:
            ``sign * (servo_deg - offset_deg) * (pi/180) * scale``. When
            ``servo_deg == offset_deg`` the result is exactly 0.0 regardless of
            sign or scale.

        Raises:
            CalibrationError: If ``servo_channel`` is not a calibrated joint.
        """
        cal = self._require_channel(servo_channel)
        return cal.sign * (servo_deg - cal.offset_deg) * (math.pi / 180.0) * cal.scale

    def to_servo_deg(self, servo_channel, urdf_rad):
        """Converts a URDF angle in radians back to a servo angle in degrees.

        Exact inverse of ``to_radians`` (used for round-trip validation). ``sign``
        and ``scale`` are non-zero for every calibrated joint.

        Args:
            servo_channel: PCA9685 channel number.
            urdf_rad: URDF joint angle in radians.

        Returns:
            The commanded servo angle in degrees:
            ``offset_deg + urdf_rad / (sign * (pi/180) * scale)``.

        Raises:
            CalibrationError: If ``servo_channel`` is not a calibrated joint.
        """
        cal = self._require_channel(servo_channel)
        return cal.offset_deg + urdf_rad / (cal.sign * (math.pi / 180.0) * cal.scale)

    def joint_radians(self, servo_angles):
        """Maps a servo-channel pose to a URDF-joint-name -> radians dict.

        Args:
            servo_angles: A dict mapping servo channel number to angle in degrees.

        Returns:
            A dict mapping URDF joint name to angle in radians, containing an entry
            for every calibrated joint whose channel is present in ``servo_angles``.
        """
        result = {}
        for channel, servo_deg in servo_angles.items():
            cal = self._by_channel.get(channel)
            if cal is None:
                continue
            result[cal.urdf_joint] = self.to_radians(channel, servo_deg)
        return result

    def _require_channel(self, servo_channel):
        """Returns the calibration for ``servo_channel`` or raises.

        Args:
            servo_channel: PCA9685 channel number.

        Returns:
            The ``JointCalibration`` registered for that channel.

        Raises:
            CalibrationError: If no calibrated joint uses that channel.
        """
        cal = self._by_channel.get(servo_channel)
        if cal is None:
            known = sorted(self._by_channel.keys())
            raise CalibrationError(
                f"unknown servo channel {servo_channel!r}; "
                f"calibrated channels are {known}"
            )
        return cal

    def _write_seed(self, by_joint):
        """Writes the seeded calibrations to the JSON file as pretty JSON.

        Args:
            by_joint: Mapping of URDF joint name to ``JointCalibration``.
        """
        payload = {
            urdf_joint: {
                "servo_channel": cal.servo_channel,
                "sign": cal.sign,
                "offset_deg": cal.offset_deg,
                "scale": cal.scale,
            }
            for urdf_joint, cal in by_joint.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    @staticmethod
    def _load_and_validate(path):
        """Loads and validates the calibration JSON file.

        Args:
            path: Path to an existing calibration JSON file.

        Returns:
            A dict mapping URDF joint name to a validated ``JointCalibration``.

        Raises:
            CalibrationError: If the file cannot be parsed, or an expected joint's
                entry is missing or is missing/has a non-numeric required field;
                the message names the offending joint and field.
        """
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CalibrationError(
                f"could not read calibration file {path}: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise CalibrationError(
                f"calibration file {path} must be a JSON object of "
                f"joint -> calibration entries"
            )

        by_joint = {}
        # Validate every joint the seeds define; a present file must cover them.
        for urdf_joint in Calibration_Store.seed_defaults():
            entry = raw.get(urdf_joint)
            if entry is None:
                raise CalibrationError(
                    f"joint {urdf_joint!r} is missing from calibration file {path}"
                )
            if not isinstance(entry, dict):
                raise CalibrationError(
                    f"joint {urdf_joint!r} entry must be a JSON object in {path}"
                )

            values = {}
            for field in _REQUIRED_FIELDS:
                if field not in entry:
                    raise CalibrationError(
                        f"joint {urdf_joint!r} is missing required field "
                        f"{field!r} in {path}"
                    )
                value = entry[field]
                # bool is a subclass of int; reject it as a numeric calibration.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise CalibrationError(
                        f"joint {urdf_joint!r} field {field!r} must be numeric, "
                        f"got {value!r} in {path}"
                    )
                values[field] = value

            by_joint[urdf_joint] = JointCalibration(
                urdf_joint=urdf_joint,
                servo_channel=int(values["servo_channel"]),
                sign=float(values["sign"]),
                offset_deg=float(values["offset_deg"]),
                scale=float(values["scale"]),
            )

        return by_joint
