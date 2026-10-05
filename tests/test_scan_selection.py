"""Logic tests for the scan-mode arm-only selection + detection-rule data layer.

Covers the pure DATA/DEFINITION layer added to ``src/detection_routine_map.py``
for Scan Mode (consumed by the FEAT-003 responder):

  - ``weighted_scan_pool`` builds a 17-slot pool with the 5:1 Gesture:Routine
    bias and ``choose_scan_action`` draws from it reproducibly under
    ``random.seed`` — and burp is NOT in the shipped pool.
  - ``SCAN_GESTURE_CHANNELS`` subsets stay within ``ARM_ONLY_CHANNELS`` and the
    arm-only channel set is exactly ``{3,4,5,6,7}``.
  - ``scan_rules()`` arbitrates person+dog over person via ``DetectionRoutineMap``,
    each rule keeps its own ``id()``-keyed cooldown, and the dog rule carries
    ``DEFAULT_DOG_LABEL`` in its ``required_classes``.

This is pure logic (no servo/camera/subprocess), so it imports only the leaf
modules. It follows the SERVO_SIM import convention of the other tests/ modules
even though nothing here touches hardware. NO collision/limit simulation is run.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_selection.py -q --maxfail=1
"""

import os
import random
import sys

# Hardware-free servo path: set before importing anything from src (convention).
os.environ["SERVO_SIM"] = "1"

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from detection_routine_map import (  # noqa: E402
    ARM_ONLY_CHANNELS,
    DEFAULT_DOG_LABEL,
    DetectionRoutineMap,
    GESTURE_WEIGHT,
    ROUTINE_WEIGHT,
    SCAN_GESTURE_CHANNELS,
    SCAN_RESPONSE_COOLDOWN_S,
    SCAN_SAFE_ARM_ACTIONS,
    ScanActionKind,
    choose_scan_action,
    scan_rules,
    weighted_scan_pool,
)
from vision_models import Detection, PERSON_LABEL  # noqa: E402


def _det(label):
    """Build a minimal Detection with the given label (bbox values irrelevant).

    Args:
        label: The COCO class label for the detection.

    Returns:
        A ``Detection`` whose geometry is a unit box; only ``label`` matters for
        rule matching.
    """
    return Detection(label=label, score=0.9, x1=0, y1=0, x2=10, y2=10)


# --- weighted pool / picker --------------------------------------------------


def test_weighted_scan_pool_slots_follow_5_to_1_ratio():
    """The shipped pool is 4 Gestures x5 + 2 Routines x1 = 22 slots, 5:1 ratio.

    (Gestures: beckon, comeHere, wave, tapSide; Routines: brains, hypnotic.)
    Counts are derived from the allowlist so adding a scan-safe action keeps the
    invariant asserted without re-hardcoding the total.
    """
    pool = weighted_scan_pool()

    gestures = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.GESTURE]
    routines = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.ROUTINE]
    assert len(gestures) == 4
    assert len(routines) == 2

    # 4 gestures * 5 + 2 routines * 1 = 22.
    assert len(pool) == len(gestures) * GESTURE_WEIGHT + len(routines) * ROUTINE_WEIGHT == 22

    for name in gestures:
        assert pool.count(name) == GESTURE_WEIGHT == 5
    for name in routines:
        assert pool.count(name) == ROUTINE_WEIGHT == 1


def test_burp_absent_from_shipped_pool_and_allowlist():
    """burp is withheld (head-coupled coverMouth), so it never appears."""
    assert "burp" not in SCAN_SAFE_ARM_ACTIONS
    assert "burp" not in weighted_scan_pool()


def test_choose_scan_action_is_seed_reproducible():
    """choose_scan_action replays identically for the same random.seed."""
    random.seed(1234)
    first = [choose_scan_action() for _ in range(50)]
    random.seed(1234)
    second = [choose_scan_action() for _ in range(50)]
    assert first == second
    # Every pick is a known allowlist member.
    assert set(first) <= set(SCAN_SAFE_ARM_ACTIONS)


def test_choose_scan_action_weights_each_gesture_about_5x_each_routine():
    """Over many seeded draws, each Gesture lands ~5x as often as each Routine.

    The 5:1 bias is PER ACTION (each gesture repeated 5 times, each routine once
    in the pool), so comparing the mean per-gesture count to the mean per-routine
    count recovers the ~5:1 weight ratio regardless of how many of each kind the
    allowlist holds.
    """
    random.seed(2024)
    draws = [choose_scan_action() for _ in range(17000)]

    gestures = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.GESTURE]
    routines = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.ROUTINE]

    mean_gesture = sum(draws.count(n) for n in gestures) / len(gestures)
    mean_routine = sum(draws.count(n) for n in routines) / len(routines)

    assert mean_routine > 0
    ratio = mean_gesture / mean_routine
    assert abs(ratio - 5.0) < 0.6


# --- channel guardrails ------------------------------------------------------


def test_arm_only_channels_is_exactly_3_through_7():
    """ARM_ONLY_CHANNELS == {3,4,5,6,7} (includes wrist ch 3, excludes neck 0/1)."""
    assert ARM_ONLY_CHANNELS == frozenset({3, 4, 5, 6, 7})
    assert 0 not in ARM_ONLY_CHANNELS
    assert 1 not in ARM_ONLY_CHANNELS
    assert 3 in ARM_ONLY_CHANNELS


def test_every_scan_gesture_channel_set_is_within_arm_only_channels():
    """Each gesture's declared channel subset stays inside the arm-only bound."""
    assert SCAN_GESTURE_CHANNELS  # non-empty
    for name, channels in SCAN_GESTURE_CHANNELS.items():
        assert channels <= ARM_ONLY_CHANNELS, f"{name} escapes ARM_ONLY_CHANNELS"


# --- detection-rule arbitration + cooldown -----------------------------------


def test_scan_rules_person_plus_dog_beats_person():
    """person+dog (more specific) wins over person when a dog is also present."""
    drm = DetectionRoutineMap(scan_rules())
    detections = [_det(PERSON_LABEL), _det(DEFAULT_DOG_LABEL)]
    chosen = drm.select_action(detections, now=100.0)
    assert chosen is not None
    assert DEFAULT_DOG_LABEL in chosen.required_classes
    assert PERSON_LABEL in chosen.required_classes


def test_scan_rules_person_only_selects_person_rule():
    """A lone person selects the single-class person rule (no dog term)."""
    drm = DetectionRoutineMap(scan_rules())
    chosen = drm.select_action([_det(PERSON_LABEL)], now=0.0)
    assert chosen is not None
    assert chosen.required_classes == (PERSON_LABEL,)
    assert DEFAULT_DOG_LABEL not in chosen.required_classes


def test_scan_rules_keep_independent_per_rule_cooldowns():
    """Each rule's id()-keyed cooldown is independent: dog cooling leaves person ready."""
    rules = scan_rules()
    drm = DetectionRoutineMap(rules)
    person_dog = [_det(PERSON_LABEL), _det(DEFAULT_DOG_LABEL)]

    # Fire + complete the person+dog rule at t=0 -> it enters cooldown.
    dog_rule = drm.select_action(person_dog, now=0.0)
    assert DEFAULT_DOG_LABEL in dog_rule.required_classes
    drm.mark_completed(dog_rule, now=0.0)

    # Still within the dog rule's cooldown: with a dog present the more specific
    # rule is blocked, so the ready person rule is selected instead.
    within = SCAN_RESPONSE_COOLDOWN_S - 1.0
    fallback = drm.select_action(person_dog, now=within)
    assert fallback is not None
    assert fallback.required_classes == (PERSON_LABEL,)

    # Past the dog rule's cooldown it becomes selectable again.
    after = SCAN_RESPONSE_COOLDOWN_S + 1.0
    reacquired = drm.select_action(person_dog, now=after)
    assert DEFAULT_DOG_LABEL in reacquired.required_classes


def test_scan_rules_cooldown_uses_configured_value():
    """Both scan rules clamp to the configured SCAN_RESPONSE_COOLDOWN_S window."""
    for rule in scan_rules():
        assert rule.cooldown_s == int(SCAN_RESPONSE_COOLDOWN_S)
        assert rule.action == "scan"
