"""Logic tests for the scan-mode arm-only selection + detection-rule data layer.

Covers the pure DATA/DEFINITION layer added to ``src/detection_routine_map.py``
for Scan Mode (consumed by the FEAT-003 responder):

  - ``weighted_scan_pool`` builds the pool with the 5:1 Gesture:Routine bias
    (4 gestures x5 + 12 routines x1) and ``choose_scan_action`` draws from it
    reproducibly under ``random.seed`` — and the still-FLAGGED head-coupled
    names (fanNose/snuckUp/moreCandy/facePalm) are NOT in the shipped pool.
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
    choose_scan_action_weighted,
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
    """The shipped pool is 4 Gestures x5 + 13 Routines x1 slots, 5:1 ratio.

    Gestures: beckon, comeHere, wave, tapSide. Routines: brains, hypnotic plus
    the FEAT-002 arm-only variants (awaken, blah, burp, comeGetCandy,
    coughMedium, coughLong, fart, fartGhost, maximus, niceDay, yawn). Counts are
    derived from the allowlist so adding a scan-safe action keeps the invariant
    asserted without re-hardcoding the total.
    """
    pool = weighted_scan_pool()

    gestures = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.GESTURE]
    routines = [n for n, k in SCAN_SAFE_ARM_ACTIONS.items() if k is ScanActionKind.ROUTINE]
    assert len(gestures) == 4
    assert len(routines) == 13

    # 4 gestures * 5 + 13 routines * 1.
    expected = len(gestures) * GESTURE_WEIGHT + len(routines) * ROUTINE_WEIGHT
    assert len(pool) == expected

    for name in gestures:
        assert pool.count(name) == GESTURE_WEIGHT == 5
    for name in routines:
        assert pool.count(name) == ROUTINE_WEIGHT == 1


def test_burp_enabled_in_shipped_pool_and_allowlist():
    """burp is now an enabled arm-only ROUTINE (FEAT-002), so it appears."""
    assert SCAN_SAFE_ARM_ACTIONS.get("burp") is ScanActionKind.ROUTINE
    assert "burp" in weighted_scan_pool()


def test_still_flagged_names_absent_from_pool_and_allowlist():
    """Head-coupled names with no arm-only scan builder stay FLAGGED/withheld."""
    for name in ("fanNose", "snuckUp", "moreCandy", "facePalm"):
        assert name not in SCAN_SAFE_ARM_ACTIONS
        assert name not in weighted_scan_pool()


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


# --- weighted operator-pool picker (FEAT-001) --------------------------------


def test_choose_scan_action_weighted_is_seed_reproducible():
    """Same random.seed replays an identical (kind, name) sequence."""
    routine_pool = {"brains": 4, "hypnotic": 2}
    gesture_pool = {"wave": 5, "beckon": 3}

    random.seed(99)
    first = [choose_scan_action_weighted(routine_pool, gesture_pool) for _ in range(50)]
    random.seed(99)
    second = [choose_scan_action_weighted(routine_pool, gesture_pool) for _ in range(50)]
    assert first == second

    # Every pick is a (ScanActionKind, name) tuple with a safe name.
    for kind, name in first:
        assert isinstance(kind, ScanActionKind)
        assert name in SCAN_SAFE_ARM_ACTIONS
        assert SCAN_SAFE_ARM_ACTIONS[name] is kind


def test_choose_scan_action_weighted_never_returns_unsafe_name():
    """A persisted name NOT in the safe set is never returned."""
    # 'snuckUp' and 'facePalm' remain FLAGGED (not in SCAN_SAFE_ARM_ACTIONS);
    # 'bogus' is unknown. (burp is now an enabled arm-only ROUTINE.)
    routine_pool = {"snuckUp": 9, "bogus": 9, "brains": 3}
    gesture_pool = {"facePalm": 9, "wave": 2}

    random.seed(7)
    picks = {choose_scan_action_weighted(routine_pool, gesture_pool)[1] for _ in range(500)}
    assert "snuckUp" not in picks
    assert "facePalm" not in picks
    assert "bogus" not in picks
    assert picks <= set(SCAN_SAFE_ARM_ACTIONS)
    # Only the two surviving safe names should ever appear.
    assert picks <= {"brains", "wave"}


def test_choose_scan_action_weighted_kind_mismatch_filtered():
    """A gesture name wrongly placed in the routine pool is dropped by kind-match."""
    # 'wave' is a GESTURE; placing it in the routine pool must filter it out.
    routine_pool = {"wave": 9}        # wrong pool for a gesture
    gesture_pool = {"beckon": 5}      # correct

    random.seed(11)
    picks = {choose_scan_action_weighted(routine_pool, gesture_pool)[1] for _ in range(300)}
    assert "wave" not in picks        # kind mismatch -> filtered
    assert picks == {"beckon"}


def test_choose_scan_action_weighted_respects_weight_ratio():
    """Weight 6 is drawn ~3x as often as weight 2 over many seeded draws."""
    # Two gestures only, so the ratio reflects the weights directly.
    gesture_pool = {"wave": 6, "beckon": 2}

    random.seed(2024)
    draws = [choose_scan_action_weighted({}, gesture_pool)[1] for _ in range(8000)]
    heavy = draws.count("wave")
    light = draws.count("beckon")

    assert light > 0
    ratio = heavy / light
    assert abs(ratio - 3.0) < 0.4


def test_choose_scan_action_weighted_empty_pool_falls_back():
    """Empty pools fall back to choose_scan_action over the safe set; never None."""
    random.seed(5)
    result = choose_scan_action_weighted({}, {})
    assert result is not None
    kind, name = result
    assert name in SCAN_SAFE_ARM_ACTIONS
    assert SCAN_SAFE_ARM_ACTIONS[name] is kind

    # The fallback distribution matches choose_scan_action for the same seed.
    random.seed(123)
    fallback = [choose_scan_action_weighted({}, {})[1] for _ in range(30)]
    random.seed(123)
    direct = [choose_scan_action() for _ in range(30)]
    assert fallback == direct


def test_choose_scan_action_weighted_filtered_empty_falls_back():
    """A pool emptied entirely by safety filtering falls back (never None)."""
    # All names are unsafe/unknown -> nothing survives -> fallback. (snuckUp
    # remains FLAGGED; bogus is unknown; facePalm remains FLAGGED.)
    routine_pool = {"snuckUp": 5, "bogus": 9}
    gesture_pool = {"facePalm": 7}

    random.seed(321)
    fallback = [
        choose_scan_action_weighted(routine_pool, gesture_pool)[1] for _ in range(30)
    ]
    random.seed(321)
    direct = [choose_scan_action() for _ in range(30)]
    assert fallback == direct


def test_choose_scan_action_weighted_respects_restricted_allow():
    """When allow is narrowed, only names in that restricted safe set are returned."""
    allow = {"wave": ScanActionKind.GESTURE}  # only wave is dispatchable
    gesture_pool = {"wave": 3, "beckon": 5}   # beckon not in allow

    random.seed(13)
    picks = {
        choose_scan_action_weighted({}, gesture_pool, allow)[1] for _ in range(200)
    }
    assert picks == {"wave"}


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
