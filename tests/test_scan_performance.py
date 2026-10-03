"""Logic tests for the arm-only scan variants of brains/hypnotic performances.

Covers the ``scan=True`` branch of ``Animatronic._brains_performance`` and
``Animatronic._hypnotic_performance`` added for Scan Mode:

  - The scan variants own EXACTLY the arm channels ``{4,5,6,7}`` (no neck 0/1),
    so the neck stays free for the scan tracker, and construct with NO
    ``ChannelOwnershipError``.
  - The hypnotic scan variant has ``gate is None`` and no ``supplies_gate=True``
    spec (its gate-supplying head sway is dropped).
  - The standalone (``scan=False``) variants are structurally unchanged — they
    still own ``{0,1,4,5,6,7}``.
  - A deliberately mis-tagged group that puts a neck channel alongside the arm
    spec STILL raises ``ChannelOwnershipError`` (the framework's disjointness
    guard is intact).

Pure construction/logic only — no runner is executed, so no servo motion and no
collision/limit simulation. Follows the SERVO_SIM import convention so the
``Movements`` instance backing the phase callables uses the fake kit.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_performance.py -q --maxfail=1
"""

import os
import sys

import pytest

# Hardware-free servo path: set before importing anything from src.
os.environ["SERVO_SIM"] = "1"

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import constants  # noqa: E402
from animatronic import Animatronic  # noqa: E402
from movements import Movements  # noqa: E402
from performance import (  # noqa: E402
    ChannelOwnershipError,
    ConcurrentGroup,
    MovementSpec,
)

ARM_CHANNELS = frozenset({
    constants.RT_ELBOW_ROTATOR,
    constants.RT_ELBOW_TILT,
    constants.RT_SHOULDER_TILT,
    constants.RT_SHOULDER_ROTATOR,  # 4,5,6,7
})
NECK_CHANNELS = frozenset({constants.NECK_PAN, constants.NECK_TILT})  # 0,1
FULL_CHANNELS = ARM_CHANNELS | NECK_CHANNELS  # {0,1,4,5,6,7}


def _owned_union(defn):
    """Union every ``owned_channels`` across every group in a definition.

    Args:
        defn: A ``PerformanceDefinition`` to inspect.

    Returns:
        The ``frozenset`` union of all member ``owned_channels``.
    """
    owned = set()
    for step in defn.steps:
        for spec in step.group.movements:
            owned |= set(spec.owned_channels)
    return frozenset(owned)


def _any_supplies_gate(defn):
    """Whether any MovementSpec in the definition supplies the audio gate.

    Args:
        defn: A ``PerformanceDefinition`` to inspect.

    Returns:
        True if at least one spec has ``supplies_gate=True``.
    """
    return any(
        spec.supplies_gate
        for step in defn.steps
        for spec in step.group.movements
    )


@pytest.fixture()
def animatronic():
    """An ``Animatronic`` and a shared ``Movements`` for building definitions."""
    return Animatronic(), Movements("Animatronic")


# --- brains scan variant -----------------------------------------------------


def test_brains_scan_owns_only_arm_channels(animatronic):
    """brains scan=True owns exactly {4,5,6,7} (neck dropped) and constructs cleanly."""
    anim, mv = animatronic
    defn = anim._brains_performance(mv, scan=True)
    owned = _owned_union(defn)
    assert owned == ARM_CHANNELS
    assert constants.NECK_PAN not in owned
    assert constants.NECK_TILT not in owned


def test_brains_standalone_owns_arm_and_neck(animatronic):
    """brains scan=False is unchanged — still owns {0,1,4,5,6,7}."""
    anim, mv = animatronic
    defn = anim._brains_performance(mv, scan=False)
    assert _owned_union(defn) == FULL_CHANNELS


# --- hypnotic scan variant ---------------------------------------------------


def test_hypnotic_scan_owns_only_arm_channels(animatronic):
    """hypnotic scan=True owns exactly {4,5,6,7} (head sway dropped)."""
    anim, mv = animatronic
    defn = anim._hypnotic_performance(mv, scan=True)
    owned = _owned_union(defn)
    assert owned == ARM_CHANNELS
    assert constants.NECK_PAN not in owned
    assert constants.NECK_TILT not in owned


def test_hypnotic_scan_has_no_gate_and_no_gate_supplier(animatronic):
    """hypnotic scan=True drops the gate-supplying head sway: gate=None, no supplier."""
    anim, mv = animatronic
    defn = anim._hypnotic_performance(mv, scan=True)
    assert defn.gate is None
    assert not _any_supplies_gate(defn)


def test_hypnotic_standalone_owns_arm_and_neck_and_keeps_gate(animatronic):
    """hypnotic scan=False is unchanged — owns {0,1,4,5,6,7} and keeps its gate."""
    anim, mv = animatronic
    defn = anim._hypnotic_performance(mv, scan=False)
    assert _owned_union(defn) == FULL_CHANNELS
    assert defn.gate is not None
    assert defn.gate.movement_name == "hyp_head_sway"
    assert _any_supplies_gate(defn)


# --- the framework guard still bites a mis-tagged group ----------------------


def test_mis_tagged_neck_channel_still_raises_channel_ownership_error(animatronic):
    """A group pairing the arm spec with a neck-channel spec still raises.

    This proves dropping the neck movement for scan does NOT weaken the
    framework's disjointness guard: re-adding a neck channel on a second spec
    that overlaps nothing would be fine, but overlapping the SAME channel — or,
    as here, adding a neck spec that collides with an arm spec that is wrongly
    tagged to also own a neck channel — must raise at construction time.
    """
    anim, mv = animatronic
    scan_defn = anim._brains_performance(mv, scan=True)
    arm_spec = scan_defn.steps[0].group.movements[0]

    # Deliberately mis-tag: build a spec that claims a neck channel the arm spec
    # does NOT own, then tag a SECOND spec to also own that neck channel so the
    # two overlap. Here we overlap on NECK_PAN by giving two specs that channel.
    bad_arm = MovementSpec(
        name="bad_arm_claims_neck",
        owned_channels=arm_spec.owned_channels | {constants.NECK_PAN},
        lead_in=arm_spec.lead_in,
        loop_body=arm_spec.loop_body,
        do_return=arm_spec.do_return,
        supplies_gate=False,
    )
    neck_spec = MovementSpec(
        name="neck_tracker",
        owned_channels=frozenset({constants.NECK_PAN}),
        lead_in=None,
        loop_body=arm_spec.loop_body,
        do_return=None,
        supplies_gate=False,
    )

    with pytest.raises(ChannelOwnershipError):
        ConcurrentGroup(movements=(bad_arm, neck_spec))
