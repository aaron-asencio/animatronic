"""Tests for the kinematic collision model's ``CollisionModel`` facade.

Covers ``src/kinematics/model.py`` (``CollisionModel`` and its result data
models) against the real Maximus URDF fixture (``src/config/maximus.urdf``):

  - Property 10: ``is_pose_safe`` is deterministic (Requirement 6.1).
  - Property 11: the ``ok`` flag equals the emptiness of the colliding-pair list
    (Requirements 6.2, 6.3).
  - Property 12: a sequence is unsafe iff any pose is unsafe (Requirements 7.2,
    7.5).
  - Example/edge cases: the rest pose classifies SAFE; an elbow-flexed pose
    classifies COLLISION on the upper/lower-arm pair with elbow offending
    joints; missing channels and non-numeric angles raise ``PoseInputError``
    naming the channel; and the ``src/kinematics`` package imports no hardware
    libraries (Requirements 5.5, 6.4, 6.5, 11.3).

The model is built ONCE at module scope because URDF loading is slow; the
property tests draw poses against that shared instance. Property tests use
hypothesis (``max_examples=150``) and are tagged with the design's Correctness
Properties by number. Run with:

    .venv/bin/python -m pytest tests/test_model.py -q --maxfail=1
"""

import os
import sys
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from kinematics.model import (  # noqa: E402
    CollisionModel,
    PoseResult,
    SequenceResult,
    CollisionPair,
    OffendingJoint,
    PoseInputError,
)

# The real URDF fixture used for all facade tests.
_URDF_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "config", "maximus.urdf")
)

# A module-scoped temp calibration path: the store seeds and writes the file on
# first construction, so the model uses the provisional seed defaults.
_CALIBRATION_PATH = os.path.join(tempfile.mkdtemp(), "calibration.json")

# Build the model ONCE at module scope: URDF loading + proxy generation is slow,
# and the property tests only draw poses against a fixed model instance.
_MODEL = CollisionModel(
    urdf_path=_URDF_PATH,
    calibration_path=_CALIBRATION_PATH,
)

# The six required servo channels the model validates on every pose.
_REQUIRED_CHANNELS = (0, 1, 4, 5, 6, 7)

# Known example poses (channel -> angle in degrees). The collision pose flexes
# the elbow fully (servo 160), folding the hand back onto the shoulder.
_REST_POSE = {0: 90, 1: 90, 4: 150, 5: 5, 6: 55, 7: 0}
_COLLISION_POSE = {0: 90, 1: 90, 4: 150, 5: 160, 6: 55, 7: 0}


def _valid_pose_strategy():
    """Hypothesis strategy for a valid pose over the six required channels.

    Draws a finite servo angle in a plausible 0..270 degree actuation range for
    each required channel.

    Returns:
        A hypothesis strategy producing ``dict[int, float]`` poses.
    """
    # Realistic servo angles: the 0-270 actuation range at a sensible precision.
    # ``allow_subnormal=False`` keeps hypothesis from generating denormalized
    # floats (e.g. 1e-292) that stress the test harness without exercising any
    # real model behavior (every joint is deterministic across the whole range).
    angle = st.floats(
        min_value=0.0,
        max_value=270.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    )
    return st.fixed_dictionaries({channel: angle for channel in _REQUIRED_CHANNELS})


def _offending_key(joint):
    """Return a comparable tuple for an ``OffendingJoint``.

    Args:
        joint: An ``OffendingJoint`` instance.

    Returns:
        A ``(urdf_joint, servo_channel, servo_name)`` tuple.
    """
    return (joint.urdf_joint, joint.servo_channel, joint.servo_name)


def _pair_key(pair):
    """Return a comparable structural key for a ``CollisionPair``.

    Args:
        pair: A ``CollisionPair`` instance.

    Returns:
        A tuple of ``(link_a, link_b, (offending-joint tuples...))``.
    """
    return (
        pair.link_a,
        pair.link_b,
        tuple(_offending_key(joint) for joint in pair.offending_joints),
    )


def _result_key(result):
    """Return a comparable structural key for a ``PoseResult``.

    Compares ``ok``, the pair count, and the full pair/offending-joint content
    without relying on frozen-dataclass ``__eq__`` over nested lists.

    Args:
        result: A ``PoseResult`` instance.

    Returns:
        A tuple ``(ok, pair_count, (pair keys...))``.
    """
    return (
        result.ok,
        len(result.colliding_pairs),
        tuple(_pair_key(pair) for pair in result.colliding_pairs),
    )


# ---------------------------------------------------------------------------
# Task 8.2 — Property 10: is_pose_safe is deterministic
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(pose=_valid_pose_strategy())
def test_property10_is_pose_safe_is_deterministic(pose):
    """Feature: kinematic-collision-model, Property 10: is_pose_safe is deterministic.

    For any valid pose over the six required channels, calling ``is_pose_safe``
    twice on the same model instance yields structurally-equal results: the same
    ``ok`` flag, the same number of colliding pairs, and equal pair content
    (link names and offending-joint tuples).

    Validates: Requirements 6.1
    """
    first = _MODEL.is_pose_safe(pose)
    second = _MODEL.is_pose_safe(pose)

    assert first.ok == second.ok
    assert len(first.colliding_pairs) == len(second.colliding_pairs)
    assert _result_key(first) == _result_key(second)


# ---------------------------------------------------------------------------
# Task 8.3 — Property 11: ok flag equals emptiness of colliding-pair list
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(pose=_valid_pose_strategy())
def test_property11_ok_equals_empty_colliding_pairs(pose):
    """Feature: kinematic-collision-model, Property 11: ok flag equals the emptiness of the colliding-pair list.

    For any valid pose, the ``ok`` flag is True exactly when the colliding-pair
    list is empty.

    Validates: Requirements 6.2, 6.3
    """
    result = _MODEL.is_pose_safe(pose)

    assert result.ok == (len(result.colliding_pairs) == 0)


# ---------------------------------------------------------------------------
# Task 8.4 — Property 12: sequence is unsafe iff any pose is unsafe
# ---------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(poses=st.lists(_valid_pose_strategy(), min_size=0, max_size=5))
def test_property12_sequence_unsafe_iff_any_pose_unsafe(poses):
    """Feature: kinematic-collision-model, Property 12: Sequence is unsafe iff any pose is unsafe.

    For any list of 0..5 valid poses, ``check_sequence`` preserves order and
    length in ``per_pose`` (each entry matching a direct ``is_pose_safe`` call),
    and sets ``unsafe`` True exactly when any per-pose result is not ok.

    Validates: Requirements 7.2, 7.5
    """
    result = _MODEL.check_sequence(poses)

    # (a) length + order preservation: each per-pose result matches a direct call.
    assert len(result.per_pose) == len(poses)
    for i, pose in enumerate(poses):
        expected = _MODEL.is_pose_safe(pose)
        assert _result_key(result.per_pose[i]) == _result_key(expected)

    # (b) aggregation: unsafe iff any per-pose result is not ok.
    assert result.unsafe == any(not r.ok for r in result.per_pose)


# ---------------------------------------------------------------------------
# Task 8.5 — Example/edge tests for the facade
# ---------------------------------------------------------------------------


def test_rest_pose_is_safe():
    """Requirement 6.2/6.3: the rest pose classifies SAFE with no colliding pairs."""
    result = _MODEL.is_pose_safe(_REST_POSE)

    assert result.ok is True
    assert result.colliding_pairs == []


def test_elbow_flexed_pose_collides_on_arm_pair():
    """Requirements 5.5, 6.2/6.3: a fully-flexed elbow folds the hand into the body.

    At full elbow flexion (servo 160) the forearm+hand fold back until the hand
    reaches the shoulder. The result must be COLLISION with at least one pair,
    some pair must involve the hand and an upper-arm/shoulder link, and the
    elbow servos must appear among the offending joints.
    """
    result = _MODEL.is_pose_safe(_COLLISION_POSE)

    assert result.ok is False
    assert len(result.colliding_pairs) >= 1

    # Some reported pair involves the hand folding back onto the shoulder /
    # upper arm.
    hand_into_body = any(
        "hand_link" in {pair.link_a, pair.link_b}
        and {pair.link_a, pair.link_b} & {"shoulder_link", "upper_arm_link"}
        for pair in result.colliding_pairs
    )
    assert hand_into_body

    # The elbow servos appear among the offending joints across all pairs.
    servo_names = {
        joint.servo_name
        for pair in result.colliding_pairs
        for joint in pair.offending_joints
    }
    assert servo_names & {"RT_ELBOW_TILT", "RT_ELBOW_ROTATOR"}


def test_missing_channel_raises_naming_channel():
    """Requirement 6.4: a missing required channel raises PoseInputError.

    The message names the offending (missing) channel.
    """
    with pytest.raises(PoseInputError) as excinfo:
        _MODEL.is_pose_safe({0: 90})

    message = str(excinfo.value)
    # The first missing required channel (sorted) is 1.
    assert "1" in message


def test_non_numeric_angle_raises_naming_channel():
    """Requirement 6.4: a non-numeric angle raises PoseInputError naming the channel."""
    pose = {0: 90, 1: 90, 4: 150, 5: 5, 6: 55, 7: "x"}

    with pytest.raises(PoseInputError) as excinfo:
        _MODEL.is_pose_safe(pose)

    assert "7" in str(excinfo.value)


def test_bool_angle_rejected_as_non_numeric():
    """Requirement 6.4: a bool angle is rejected as non-numeric naming the channel.

    ``bool`` is a subclass of ``int`` but must not be accepted as a servo angle.
    """
    pose = {0: 90, 1: 90, 4: 150, 5: 5, 6: 55, 7: True}

    with pytest.raises(PoseInputError) as excinfo:
        _MODEL.is_pose_safe(pose)

    assert "7" in str(excinfo.value)


def test_kinematics_package_imports_no_hardware_libraries():
    """Requirement 11.3: the kinematics package imports no hardware libraries.

    Importing ``kinematics.model`` in a CLEAN interpreter must not pull in
    ``adafruit_servokit``, ``gpiozero``, or ``pyaudio``, and no source file in
    the package may contain an import of those libraries.

    The runtime check runs in a fresh subprocess rather than inspecting this
    session's ``sys.modules``: other test modules import hardware-touching
    project modules (e.g. ``audio_player``, ``eyetest``) that legitimately load
    ``gpiozero`` into the shared interpreter, so a same-process ``sys.modules``
    scan would be order-dependent and spuriously fail. A clean subprocess
    isolates what ``kinematics.model`` alone imports.
    """
    import glob
    import re
    import subprocess

    forbidden = ("adafruit_servokit", "gpiozero", "pyaudio")

    # (1) Importing kinematics.model in a fresh interpreter must not load any
    # forbidden module. Run in a subprocess so this session's pollution (from
    # other tests importing hardware modules) cannot affect the result.
    src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    probe = (
        "import sys, importlib; importlib.import_module('kinematics.model'); "
        "bad=[n for n in ('adafruit_servokit','gpiozero','pyaudio') "
        "if n in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"kinematics.model imported forbidden hardware module(s): "
        f"{result.stdout.strip()!r} (stderr: {result.stderr.strip()!r})"
    )

    # (2) Static scan: no source file imports a forbidden library.
    package_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "src", "kinematics")
    )
    patterns = [
        re.compile(rf"^\s*import\s+{re.escape(lib)}\b") for lib in forbidden
    ] + [
        re.compile(rf"^\s*from\s+{re.escape(lib)}\b") for lib in forbidden
    ]

    for source_path in glob.glob(os.path.join(package_dir, "*.py")):
        with open(source_path, "r", encoding="utf-8") as handle:
            for line in handle:
                for pattern in patterns:
                    assert not pattern.match(line), (
                        f"forbidden hardware import in {source_path}: {line.strip()}"
                    )
