"""Tests for the kinematic collision model's calibration transform and store.

Covers the already-implemented pieces of ``src/kinematics/calibration.py``:
  - the servo-degree <-> URDF-radian transform (round-trip, zero-crossing)
  - the editable JSON Calibration_Store (seed/create on missing file, JSON
    round-trip, re-application of edited values, and error reporting for
    missing / non-numeric fields)

Property tests use hypothesis (``max_examples=150`` each) and are tagged with
the design's Correctness Properties by number. Example-based unit tests use
pytest. Run with:

    .venv/bin/python -m pytest tests/test_calibration.py -q
"""

import json
import math
import os
import sys
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.calibration import (  # noqa: E402
    Calibration_Store,
    CalibrationError,
)

# The six real servo channels the seed defaults calibrate.
_SEED = Calibration_Store.seed_defaults()
_CHANNELS = sorted(cal.servo_channel for cal in _SEED.values())


def _write_calibration_json(payload):
    """Write a calibration payload to a fresh temp JSON file and return its path.

    Self-contained (uses ``tempfile`` rather than a function-scoped fixture) so
    it is safe to call from inside a hypothesis ``@given`` test.

    Args:
        payload: The JSON-serializable calibration mapping to write.

    Returns:
        The filesystem path to the written temporary JSON file.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(payload, handle, indent=2)
    finally:
        handle.close()
    return handle.name


def _payload_from_calibrations(by_joint):
    """Build the store's on-disk JSON schema from a joint -> calibration mapping.

    Args:
        by_joint: Mapping of URDF joint name to a dict with servo_channel,
            sign, offset_deg, and scale.

    Returns:
        A dict matching the Calibration_Store JSON schema.
    """
    return {
        urdf_joint: {
            "servo_channel": fields["servo_channel"],
            "sign": fields["sign"],
            "offset_deg": fields["offset_deg"],
            "scale": fields["scale"],
        }
        for urdf_joint, fields in by_joint.items()
    }


# ---------------------------------------------------------------------------
# Task 2.2 — Property 1: Calibration transform round-trips
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    channel=st.sampled_from(_CHANNELS),
    servo_deg=st.floats(
        min_value=-360.0, max_value=360.0, allow_nan=False, allow_infinity=False
    ),
    sign=st.sampled_from([1.0, -1.0]),
    offset_deg=st.floats(
        min_value=-360.0, max_value=360.0, allow_nan=False, allow_infinity=False
    ),
    scale=st.floats(
        min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    negate_scale=st.booleans(),
)
def test_property1_transform_round_trips(
    channel, servo_deg, sign, offset_deg, scale, negate_scale
):
    """Feature: kinematic-collision-model, Property 1: Calibration transform round-trips.

    For any joint calibration with sign in {+1, -1}, a finite offset_deg, a
    non-zero finite scale, and any finite servo angle, converting to radians and
    back with ``to_servo_deg`` recovers the original servo angle within a tight
    tolerance.

    Validates: Requirements 2.1
    """
    if negate_scale:
        scale = -scale  # exercise negative (but non-zero) scales too

    # Build a store seeded from a temp file, then override the drawn channel's
    # calibration so the round-trip is exercised across the full parameter space.
    payload = _payload_from_calibrations(
        {
            cal.urdf_joint: {
                "servo_channel": cal.servo_channel,
                "sign": cal.sign,
                "offset_deg": cal.offset_deg,
                "scale": cal.scale,
            }
            for cal in _SEED.values()
        }
    )
    target_joint = next(
        joint for joint, cal in _SEED.items() if cal.servo_channel == channel
    )
    payload[target_joint] = {
        "servo_channel": channel,
        "sign": sign,
        "offset_deg": offset_deg,
        "scale": scale,
    }

    store = Calibration_Store(_write_calibration_json(payload))

    urdf_rad = store.to_radians(channel, servo_deg)
    recovered = store.to_servo_deg(channel, urdf_rad)

    assert math.isclose(recovered, servo_deg, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Task 2.3 — Property 2: Offset is the zero-crossing
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(channel=st.sampled_from(_CHANNELS))
def test_property2_offset_is_zero_crossing(channel):
    """Feature: kinematic-collision-model, Property 2: Offset is the zero-crossing.

    For every calibrated joint, transforming a servo angle equal to that joint's
    ``offset_deg`` yields exactly 0.0 radians, regardless of sign or scale.

    Validates: Requirements 2.5
    """
    # A store built from the seed defaults (missing file -> seeded + written).
    store = Calibration_Store(tempfile.mktemp(suffix=".json"))

    target_joint = next(
        joint for joint, cal in _SEED.items() if cal.servo_channel == channel
    )
    offset_deg = _SEED[target_joint].offset_deg

    assert store.to_radians(channel, offset_deg) == 0.0


# ---------------------------------------------------------------------------
# Task 2.4 — Property 3: Calibration JSON serialization round-trips
# ---------------------------------------------------------------------------


@st.composite
def _calibration_mapping(draw):
    """Build a valid calibration mapping over the six real joints via hypothesis.

    Keeps each joint's real ``servo_channel`` and generates a sign in {-1, 1},
    a finite ``offset_deg``, and a non-zero finite ``scale`` per joint.

    Returns:
        A dict mapping URDF joint name to its per-field calibration dict.
    """
    mapping = {}
    for urdf_joint, cal in _SEED.items():
        sign = draw(st.sampled_from([-1.0, 1.0]))
        offset_deg = draw(
            st.floats(
                min_value=-360.0,
                max_value=360.0,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        magnitude = draw(
            st.floats(
                min_value=0.01,
                max_value=100.0,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        scale = magnitude if draw(st.booleans()) else -magnitude
        mapping[urdf_joint] = {
            "servo_channel": cal.servo_channel,
            "sign": sign,
            "offset_deg": offset_deg,
            "scale": scale,
        }
    return mapping


@settings(max_examples=150)
@given(mapping=_calibration_mapping())
def test_property3_calibration_json_round_trips(mapping):
    """Feature: kinematic-collision-model, Property 3: Calibration JSON serialization round-trips.

    For any valid calibration mapping over the six joints, writing it to a JSON
    file in the store's schema and constructing a Calibration_Store from it
    yields per-joint sign / offset_deg / scale equal to what was written.

    Validates: Requirements 3.1
    """
    payload = _payload_from_calibrations(mapping)
    store = Calibration_Store(_write_calibration_json(payload))

    for urdf_joint, fields in mapping.items():
        channel = fields["servo_channel"]
        cal = store._by_channel[channel]
        assert cal.urdf_joint == urdf_joint
        assert cal.sign == fields["sign"]
        assert cal.offset_deg == fields["offset_deg"]
        assert cal.scale == fields["scale"]


# ---------------------------------------------------------------------------
# Task 2.5 — Unit / edge tests for the calibration store
# ---------------------------------------------------------------------------


def test_missing_file_is_created_and_seeded(tmp_path):
    """Requirement 3.3: a missing calibration file is created and seeded.

    Constructing a store on a non-existent path writes the seed defaults to the
    file (so it now exists) and loads those seed values.
    """
    path = tmp_path / "new_calibration.json"
    assert not path.exists()

    store = Calibration_Store(path)

    # The file was created on disk.
    assert path.exists()

    # Spot-check a couple of seeded values through the transform's zero-crossing:
    # yawl_neck (ch 0) seeds offset 90, shoulder_pitch (ch 7) seeds offset 0.
    assert store.to_radians(0, 90.0) == 0.0
    assert store.to_radians(7, 0.0) == 0.0

    # And the raw seeded JSON matches the seed defaults.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["yawl_neck_joint"]["offset_deg"] == 90.0
    assert on_disk["shoulder_pitch_joint"]["scale"] == 0.6296


def test_changed_value_reapplies_on_reinit(tmp_path):
    """Requirement 3.5: editing the JSON and re-initializing changes behavior.

    No code change is required; a re-constructed store reflects the edited value.
    """
    path = tmp_path / "calibration.json"

    # First construction seeds the file (yawl_neck_joint offset_deg == 90).
    first = Calibration_Store(path)
    assert first.to_radians(0, 90.0) == 0.0

    # Edit the JSON on disk: move yawl_neck_joint's offset to 100 degrees.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["yawl_neck_joint"]["offset_deg"] = 100.0
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # A freshly-constructed store applies the new value (no code change).
    reloaded = Calibration_Store(path)
    assert reloaded.to_radians(0, 100.0) == 0.0
    # The old offset no longer maps to zero.
    assert reloaded.to_radians(0, 90.0) != 0.0


def test_missing_field_raises_naming_joint_and_field(tmp_path):
    """Requirement 3.4: a missing required field raises naming the joint + field."""
    path = tmp_path / "calibration.json"

    # Seed a valid file, then drop 'scale' from one joint.
    Calibration_Store(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["shoulder_pitch_joint"]["scale"]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises(CalibrationError) as excinfo:
        Calibration_Store(path)

    message = str(excinfo.value)
    assert "shoulder_pitch_joint" in message
    assert "scale" in message


def test_non_numeric_field_raises_naming_joint_and_field(tmp_path):
    """Requirement 3.4: a non-numeric required field raises naming the joint + field."""
    path = tmp_path / "calibration.json"

    # Seed a valid file, then set 'sign' to a string on one joint.
    Calibration_Store(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["elbow_yaw_joint"]["sign"] = "positive"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises(CalibrationError) as excinfo:
        Calibration_Store(path)

    message = str(excinfo.value)
    assert "elbow_yaw_joint" in message
    assert "sign" in message
