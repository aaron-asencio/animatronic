"""Focused suppression test (FR3 vs FR4) and the ARM_ONLY_MOVEMENTS channel
guardrail for Puppeteer Mode.

Hardware-free: ``SERVO_SIM=1`` is set before importing src. The suppression test
exercises ``Animatronic._evaluate_trigger`` — the pure decision seam the tracking
loop calls each frame — with no camera/servo. The guardrail test is pure-logic
channel-metadata reasoning (it moves no servos), consistent with the testing
steering (no SERVO_SIM collision/limit sim).

Suppression contract:
  - FR3 (AC7): with the maps forced to ``None`` (what ``tracking`` does when
    ``suppress_triggers=True``), ``_evaluate_trigger`` returns ``None`` for a
    frame that WOULD otherwise fire a rule — no Routine is ever armed.
  - FR4 (AC8): with real (default) maps, the SAME frame makes ``_evaluate_trigger``
    return the expected ``{'action','rule','routine_map'}`` — i.e. plain tracking
    still arms the trigger, so suppression does not leak into tracking.

Guardrail (AC12 support):
  - Every ``webapp.ARM_ONLY_MOVEMENTS`` member's VERIFIED standalone channel
    footprint is a subset of ``ARM_ONLY_CHANNELS`` {3,4,5,6,7} and disjoint from
    the Neck_Group {0,1}. The authoritative source is the fixture below (read
    from each gesture's movements.py "Channels:" docstring), NOT
    ``SCAN_GESTURE_CHANNELS`` (the scan variant, which diverges — e.g. ``wave`` is
    {3,4,5,6,7} there but drives the neck standalone).
  - Known neck-driving gestures are NOT in ``ARM_ONLY_MOVEMENTS``.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_puppeteer_suppression.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
from animatronic import Animatronic  # noqa: E402
from detection_routine_map import (  # noqa: E402
    DetectionRoutineMap,
    DetectionRule,
    ARM_ONLY_CHANNELS,
    SCAN_GESTURE_CHANNELS,
)
from vision_models import Detection, PERSON_LABEL  # noqa: E402


# A frame with a single person — enough to satisfy a person rule.
def _person_frame():
    return [Detection(label=PERSON_LABEL, score=0.9, x1=100, y1=100, x2=200, y2=300)]


# ── Trigger suppression (FR3) vs plain tracking (FR4) ────────────────────────
def test_suppressed_maps_never_arm_trigger():
    """FR3/AC7: maps forced None -> _evaluate_trigger returns None (no dispatch)."""
    a = Animatronic()
    # This is exactly the state tracking() leaves after suppress_triggers=True.
    assert a._evaluate_trigger(_person_frame(), None, None) is None


def test_plain_tracking_still_arms_trigger():
    """FR4/AC8: with real maps the SAME frame arms the expected pending trigger."""
    a = Animatronic()
    # A self-contained person rule + an action_map that allowlists its action,
    # so the test does not depend on the seed map's action being in the default
    # allowlist. This mirrors what plain --action=tracking builds (non-None maps).
    routine_map = DetectionRoutineMap([
        DetectionRule(required_classes=(PERSON_LABEL,), action='wave'),
    ])
    action_map = {'wave': lambda: None}

    pending = a._evaluate_trigger(_person_frame(), routine_map, action_map)
    assert pending is not None
    assert pending['action'] == 'wave'
    assert pending['routine_map'] is routine_map
    assert pending['rule'].action == 'wave'


def test_suppression_is_the_only_difference():
    """The ONLY thing distinguishing FR3 from FR4 is whether the maps are None:
    the same frame + same rule arms when maps are present and is silent when
    forced None (proving suppression doesn't change the rule logic itself)."""
    a = Animatronic()
    routine_map = DetectionRoutineMap([
        DetectionRule(required_classes=(PERSON_LABEL,), action='wave'),
    ])
    action_map = {'wave': lambda: None}
    frame = _person_frame()

    assert a._evaluate_trigger(frame, routine_map, action_map) is not None
    assert a._evaluate_trigger(frame, None, None) is None


# ── ARM_ONLY_MOVEMENTS channel-safety guardrail ──────────────────────────────
# STANDALONE (/movement/<name> -> controller.py) footprints — the authoritative
# source for the guardrail. NOT SCAN_GESTURE_CHANNELS (that is the scan variant).
# Read from each gesture's movements.py "Channels:" docstring.
ARM_ONLY_STANDALONE_FOOTPRINT = {
    'comeHere':         frozenset({3, 4, 5, 6, 7}),
    'beckon':           frozenset({3, 4, 5, 6, 7}),
    'menacingReach':    frozenset({4, 5, 6, 7}),
    'tapSide':          frozenset({3, 4, 5, 6, 7}),
    'talkingWithHands': frozenset({3, 4, 5, 6, 7}),  # owned {3,5,6,7}; ch4 held at 270 (a write)
}

NECK_GROUP_CHANNELS = frozenset({0, 1})


def test_arm_only_movements_have_a_known_footprint():
    """Every ARM_ONLY_MOVEMENTS member has a verified standalone footprint."""
    assert set(webapp.ARM_ONLY_MOVEMENTS) == set(ARM_ONLY_STANDALONE_FOOTPRINT)


@pytest.mark.parametrize("name", sorted(webapp.ARM_ONLY_MOVEMENTS))
def test_arm_only_movement_is_neck_free(name):
    """Each arm-only member's footprint is in {3,4,5,6,7} and disjoint from {0,1}."""
    footprint = ARM_ONLY_STANDALONE_FOOTPRINT[name]
    assert footprint <= ARM_ONLY_CHANNELS, f"{name} escapes arm channels"
    assert footprint.isdisjoint(NECK_GROUP_CHANNELS), f"{name} writes the Neck_Group"


@pytest.mark.parametrize("name", ['wave', 'fanButt', 'fanNose', 'handVisor',
                                  'yawnCover', 'facePalm', 'swivelHead'])
def test_known_neck_drivers_not_arm_only(name):
    """Known neck-driving gestures must NOT be classified arm-only (fail-safe)."""
    assert name not in webapp.ARM_ONLY_MOVEMENTS


def test_scan_channels_cross_check_where_standalone_equals_scan():
    """Optional cross-check: for the names where standalone == scan
    (comeHere/beckon/tapSide), the fixture agrees with SCAN_GESTURE_CHANNELS.

    NOTE: SCAN_GESTURE_CHANNELS is the scan-variant footprint and is NOT a proxy
    for the standalone form — e.g. 'wave' is {3,4,5,6,7} there but drives the
    neck standalone — so it is only cross-checked for the names that match.
    """
    for name in ('comeHere', 'beckon', 'tapSide'):
        assert ARM_ONLY_STANDALONE_FOOTPRINT[name] == SCAN_GESTURE_CHANNELS[name]
