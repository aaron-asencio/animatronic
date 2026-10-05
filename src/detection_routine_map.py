"""Detection-triggered Routine arbitration with per-rule cooldown (Req 7).

This leaf module owns the pure logic of the Detection_Routine_Map: deciding
which Routine (if any) a frame's detections should trigger. It holds an ordered
list of ``vision_models.DetectionRule`` entries, each mapping one detection
condition (a set of required COCO classes) to one Routine action name.

The module is deliberately logic-only. It does NOT dispatch the chosen action,
does NOT validate the action name against ``action_map``, and never touches a
servo, the camera, or a subprocess. That wiring lives in Tracking_Mode (task
12.2); keeping arbitration pure makes it deterministic and testable (Property 20
arbitration, Property 21 cooldown). Like the other vision leaves it imports only
the standard library plus ``vision_models`` and uses ``print()`` for debug
output.

Two behaviours are implemented here:

* **Arbitration (Req 7.5):** when several conditions are all satisfied by the
  current detections, the most *specific* one wins — the rule requiring the
  greatest number of classes. Ties are broken by definition order (the
  earliest-defined matching rule), which is why the rules are kept in an ordered
  list.
* **Cooldown (Req 7.8):** after a rule's Routine fires, that condition→Routine
  pair is blocked until the rule's ``cooldown_s`` elapses *since the Routine
  completed*. The caller marks completion (not firing) so the cooldown window
  starts at the end of the Routine, and ``now`` is injected so the window is
  deterministic in tests.
"""

import random
from enum import Enum
from typing import Dict, Iterable, List, Optional

from vision_models import Detection, DetectionRule, PERSON_LABEL


# Seed defaults for the Detection_Routine_Map (Req 7.3, 7.4). The order here is
# the definition order used for the arbitration tie-break: the single-class
# ``person`` rule is defined before the two-class ``person+dog`` rule, but since
# arbitration prefers the rule matching the most classes, ``person+dog`` wins
# whenever a dog is also present.
DEFAULT_DOG_LABEL = "dog"
DEFAULT_WAVE_ACTION = "wave"
DEFAULT_WALK_DOG_ACTION = "walkYourDog"


def default_rules() -> List[DetectionRule]:
    """Build the seed Detection_Routine_Map rules in definition order.

    Returns:
        A fresh list of ``DetectionRule`` entries: ``person -> wave`` followed by
        ``person+dog -> walkYourDog`` (Req 7.3, 7.4). A new list of new rule
        objects is returned on every call so callers never share mutable state.
    """
    return [
        DetectionRule(required_classes=(PERSON_LABEL,), action=DEFAULT_WAVE_ACTION),
        DetectionRule(
            required_classes=(PERSON_LABEL, DEFAULT_DOG_LABEL),
            action=DEFAULT_WALK_DOG_ACTION,
        ),
    ]


# --- Scan-mode arm-only response data layer -------------------------------- #
#
# Scan Mode holds the head OFF-center to track a detected person (the neck
# tracker drives NECK_PAN=0 / NECK_TILT=1), then layers an arm-only response on
# top. Everything below is the pure DATA/DEFINITION layer the scan responder
# (FEAT-003) consumes: an allowlist of head-decoupled arm-only Gestures/Routines,
# their declared arm-channel subsets, a weighted picker, and the detection rules.
# No servo, camera, or subprocess access and no scan-specific logging lives here
# — the module stays logic-only.

# The arm/wrist channels — the COMPLEMENT of the neck tracker's channels
# {NECK_PAN=0, NECK_TILT=1}. It INCLUDES RT_WRIST_TILT=3 (so the guardrail bound
# is {3,4,5,6,7}, NOT {4,5,6,7}): beckon/comeHere flex the wrist, so a response
# may legitimately drive channel 3 while scan owns 0/1.
ARM_ONLY_CHANNELS = frozenset({3, 4, 5, 6, 7})

# Per-Gesture channel subsets. Gestures carry no ``owned_channels`` field (unlike
# a Performance MovementSpec), so their arm-channel footprint is declared here as
# the subset guardrail checked against ARM_ONLY_CHANNELS. Verified against
# movements.py: beckon/come_here curl the wrist to 170 and _wave_arm writes
# RT_WRIST_TILT, so each gesture's set spans {3,4,5,6,7}.
SCAN_GESTURE_CHANNELS: Dict[str, frozenset] = {
    "beckon": frozenset({3, 4, 5, 6, 7}),
    "comeHere": frozenset({3, 4, 5, 6, 7}),
    "wave": frozenset({3, 4, 5, 6, 7}),
    "tapSide": frozenset({3, 4, 5, 6, 7}),  # arm-only idle tap (verified arm channels)
}


class ScanActionKind(Enum):
    """Whether a scan-safe action is a Gesture (no audio) or a Routine (audio)."""

    GESTURE = "gesture"
    ROUTINE = "routine"


# Weighting for the scan picker: Gestures are 5x more likely than Routines
# (operator choice — gestures are quieter/safer to layer over tracking).
GESTURE_WEIGHT = 5
ROUTINE_WEIGHT = 1

# The allowlist of head-decoupled, arm-only actions scan may run while the neck
# tracks a person. Gestures carry no audio and never touch the jaw/neck;
# Routines run their arm-only (scan=True) builders so they drive only {4,5,6,7}.
#
# burp is DELIBERATELY withheld — see the FLAGGED commented-out entry below.
SCAN_SAFE_ARM_ACTIONS: Dict[str, ScanActionKind] = {
    "beckon": ScanActionKind.GESTURE,
    "comeHere": ScanActionKind.GESTURE,
    "wave": ScanActionKind.GESTURE,
    "tapSide": ScanActionKind.GESTURE,  # NEW — arm-only {3,4,5,6,7}, no neck term
    "brains": ScanActionKind.ROUTINE,
    "hypnotic": ScanActionKind.ROUTINE,
    # FLAGGED — head-COUPLED, DO NOT ENABLE without operator bench-verification
    # across the FULL off-center tracking envelope (same rationale as burp: scan
    # holds the head OFF-center and FORBIDDEN_COMBINATIONS has no neck term, so
    # nothing would catch a hand colliding with the off-center head). Each would
    # drive/recenter the neck channels (0/1) the tracker owns:
    #   "fanNose":   ScanActionKind.GESTURE,  # start pose writes NECK_PAN/NECK_TILT (ch 0/1)
    #   "snuckUp":   ScanActionKind.ROUTINE,  # startle: head jerk (NECK_TILT) + nervous pan
    #   "moreCandy": ScanActionKind.ROUTINE,  # cover-mouth pose verified only neck-centered; no scan builder
    #   "awaken":    ScanActionKind.ROUTINE,  # wake: lazy head bob on NECK_PAN/NECK_TILT
    #   "facePalm":  ScanActionKind.GESTURE,  # NECK_TILT=140 + NECK_PAN shake (head down)
    #   "burp":      ScanActionKind.ROUTINE,  # coverMouth fold, operator-verified neck-centered only
}

# Seconds a scan response is blocked from re-firing after it completes (per-rule
# cooldown, keyed by rule identity in DetectionRoutineMap).
SCAN_RESPONSE_COOLDOWN_S = 8.0


def weighted_scan_pool(
    allow: Dict[str, ScanActionKind] = SCAN_SAFE_ARM_ACTIONS,
) -> List[str]:
    """Build the weighted selection pool of scan-safe action names.

    Each Gesture name is repeated ``GESTURE_WEIGHT`` times and each Routine name
    ``ROUTINE_WEIGHT`` times, so a uniform ``random.choice`` over the pool yields
    the 5:1 Gesture:Routine bias.

    Args:
        allow: The allowlist mapping action name -> ``ScanActionKind``. Defaults
            to the shipped ``SCAN_SAFE_ARM_ACTIONS``.

    Returns:
        A list of action names with each name repeated by its kind's weight, in
        allowlist iteration order.
    """
    pool: List[str] = []
    for name, kind in allow.items():
        weight = GESTURE_WEIGHT if kind is ScanActionKind.GESTURE else ROUTINE_WEIGHT
        pool.extend([name] * weight)
    return pool


def choose_scan_action(
    allow: Dict[str, ScanActionKind] = SCAN_SAFE_ARM_ACTIONS,
) -> str:
    """Pick one scan-safe action name at random, weighted 5:1 Gesture:Routine.

    Uses the shared stdlib ``random`` module so ``random.seed(x)`` makes the
    selection reproducible (required for the phased-vs-standalone equivalence
    tests).

    Args:
        allow: The allowlist mapping action name -> ``ScanActionKind``. Defaults
            to the shipped ``SCAN_SAFE_ARM_ACTIONS``.

    Returns:
        One action name drawn uniformly from ``weighted_scan_pool(allow)``.
    """
    return random.choice(weighted_scan_pool(allow))


def choose_scan_action_weighted(
    routine_pool: Dict[str, int],
    gesture_pool: Dict[str, int],
    allow: Dict[str, ScanActionKind] = SCAN_SAFE_ARM_ACTIONS,
    rng=random,
):
    """Pick ``(ScanActionKind, name)`` from the operator pool, safety-filtered.

    The operator pools are advisory; this function intersects them with the
    arm-only-safe set ``allow`` *before* the weighted draw so a persisted name
    that is not arm-only-safe can never be returned. Kind must also match the
    pool a name came from: a routine_pool name must map to
    ``ScanActionKind.ROUTINE`` in ``allow`` and a gesture_pool name to
    ``ScanActionKind.GESTURE``; a name whose kind does not match the pool it was
    listed in is dropped.

    Each surviving name is repeated by its (int, >=1) weight to build the
    weighted list, then one is drawn via ``rng.choice``. When the weighted list
    is empty (no pool saved, or nothing survived filtering), falls back to
    ``choose_scan_action(allow)`` — the current default 5:1 behavior. Uses the
    shared stdlib ``random`` module by default so ``random.seed(x)`` reproduces
    the sequence. Never returns ``None``.

    Args:
        routine_pool: Operator routine selection ``{name: int weight}``.
        gesture_pool: Operator gesture selection ``{name: int weight}``.
        allow: The arm-only-safe allowlist mapping name -> ``ScanActionKind``.
            Defaults to the shipped ``SCAN_SAFE_ARM_ACTIONS``.
        rng: An injectable random source exposing ``choice`` (defaults to the
            shared ``random`` module so seeding is reproducible).

    Returns:
        A tuple ``(ScanActionKind, name)`` for the chosen action.
    """
    weighted: List[str] = []
    for pool, expected_kind in (
        (routine_pool, ScanActionKind.ROUTINE),
        (gesture_pool, ScanActionKind.GESTURE),
    ):
        if not isinstance(pool, dict):
            continue
        for name, raw_weight in pool.items():
            # Safety intersection: name must be in the safe set AND its declared
            # kind must match the pool it was listed in.
            if allow.get(name) is not expected_kind:
                continue
            try:
                weight = int(raw_weight)
            except (TypeError, ValueError):
                continue
            if weight < 1:
                continue
            weighted.extend([name] * weight)

    if not weighted:
        # Fall back to the current default behavior over the safe set.
        name = choose_scan_action(allow)
        return allow[name], name

    name = rng.choice(weighted)
    return allow[name], name


def scan_rules() -> List[DetectionRule]:
    """Build the scan-mode detection rules (person, and person+dog).

    Both rules use ``action='scan'`` as a dispatch-ignored placeholder: the scan
    responder (FEAT-003) always dispatches ``choose_scan_action()`` regardless of
    which rule fired, and distinguishes the dog case by testing for
    ``DEFAULT_DOG_LABEL`` in the selected rule's ``required_classes``. The rules
    are returned in definition order (person before person+dog); arbitration in
    ``DetectionRoutineMap`` prefers the more specific person+dog rule when a dog
    is also present, and each rule keeps its own ``id()``-keyed cooldown.

    Returns:
        A fresh list of two ``DetectionRule`` entries, each with
        ``cooldown_s=SCAN_RESPONSE_COOLDOWN_S``.
    """
    return [
        DetectionRule(
            required_classes=(PERSON_LABEL,),
            action="scan",
            cooldown_s=SCAN_RESPONSE_COOLDOWN_S,
        ),
        DetectionRule(
            required_classes=(PERSON_LABEL, DEFAULT_DOG_LABEL),
            action="scan",
            cooldown_s=SCAN_RESPONSE_COOLDOWN_S,
        ),
    ]


class DetectionRoutineMap:
    """Ordered map of detection conditions to Routine action names (Req 7).

    Holds an ordered list of ``DetectionRule`` entries. ``select_action``
    inspects the current frame's detections and returns the rule that should
    fire, applying both the specificity arbitration (Req 7.5) and the per-rule
    cooldown (Req 7.8). Definition order is preserved so the arbitration
    tie-break ("earliest-defined wins") is stable.

    The map tracks, per rule, the monotonic timestamp at which that rule's
    Routine last *completed*. A rule stays blocked until ``cooldown_s`` seconds
    have elapsed since that completion time, so the cooldown window measures from
    the end of the Routine rather than from when it was triggered.

    Attributes:
        rules: The ordered ``DetectionRule`` entries considered for a frame.
    """

    def __init__(self, rules: Optional[Iterable[DetectionRule]] = None):
        """Initialise the map with a set of rules.

        Args:
            rules: The ordered detection rules. The iterable's order is the
                definition order used for the arbitration tie-break. When
                ``None`` (the default), the seed defaults from ``default_rules``
                are used (``person -> wave``, ``person+dog -> walkYourDog``).
        """
        self.rules: List[DetectionRule] = (
            list(rules) if rules is not None else default_rules()
        )
        # Maps a rule's identity (id()) to the monotonic timestamp its Routine
        # last completed. A rule absent from this dict has never fired and is
        # therefore never in cooldown.
        self._last_completed = {}

    def _labels_present(self, detections: Iterable[Detection]) -> set:
        """Collect the distinct class labels present in a frame's detections.

        Args:
            detections: The detections for the current frame.

        Returns:
            A set of the ``label`` values. Membership (not count) is what rule
            matching cares about, so duplicates collapse to a single label.
        """
        return {det.label for det in detections}

    def _rule_matches(self, rule: DetectionRule, labels: set) -> bool:
        """Whether every class a rule requires is present in the frame.

        Args:
            rule: The candidate rule.
            labels: The set of labels present in the current frame.

        Returns:
            True if all of ``rule.required_classes`` are in ``labels``. A rule
            with no required classes never matches (there is no condition to
            satisfy), which keeps a misconfigured empty rule from firing on
            every frame.
        """
        if not rule.required_classes:
            return False
        return all(cls in labels for cls in rule.required_classes)

    def _in_cooldown(self, rule: DetectionRule, now: float) -> bool:
        """Whether a rule is still blocked by its cooldown window.

        Args:
            rule: The rule to test.
            now: The current monotonic timestamp (seconds), injected so the
                window is deterministic in tests.

        Returns:
            True if the rule's Routine has completed and fewer than
            ``rule.cooldown_s`` seconds have elapsed since that completion
            (Req 7.8). A rule that has never completed is never in cooldown.
        """
        last = self._last_completed.get(id(rule))
        if last is None:
            return False
        return (now - last) < rule.cooldown_s

    def select_action(
        self, detections: Iterable[Detection], now: float
    ) -> Optional[DetectionRule]:
        """Choose the rule whose Routine should fire for this frame, if any.

        A rule is a candidate when all of its ``required_classes`` are present
        among the detected labels and it is not currently in cooldown. Among the
        candidates the most specific rule wins — the one requiring the greatest
        number of classes — with ties broken by definition order (the
        earliest-defined rule), per Req 7.5.

        Rules still in cooldown are skipped entirely, so a blocked specific rule
        does not suppress a less specific rule that is ready to fire: if
        ``person+dog`` is cooling down but ``person`` is ready, ``person`` is
        selected.

        Args:
            detections: The detections for the current frame.
            now: The current monotonic timestamp (seconds). Injected so cooldown
                evaluation is deterministic and testable.

        Returns:
            The chosen ``DetectionRule`` whose ``action`` the caller should
            dispatch, or ``None`` when no rule matches or every matching rule is
            still in cooldown.
        """
        labels = self._labels_present(detections)

        best_rule: Optional[DetectionRule] = None
        best_index = -1
        for index, rule in enumerate(self.rules):
            if not self._rule_matches(rule, labels):
                continue
            if self._in_cooldown(rule, now):
                continue
            # Prefer the rule matching the most classes (most specific). On a
            # tie, keep the earlier-defined rule: only replace when strictly
            # more specific, since we iterate in definition order.
            if best_rule is None or len(rule.required_classes) > len(
                best_rule.required_classes
            ):
                best_rule = rule
                best_index = index

        if best_rule is None:
            print("DetectionRoutineMap: no rule selected for this frame")
            return None

        print(
            f"DetectionRoutineMap: selected rule #{best_index} "
            f"{best_rule.required_classes} -> '{best_rule.action}'"
        )
        return best_rule

    def mark_completed(self, rule: DetectionRule, now: float) -> None:
        """Record that a rule's Routine has completed, starting its cooldown.

        The cooldown window measures from completion, so the caller invokes this
        when the Routine finishes (not when it is triggered). After this call the
        rule is blocked until ``rule.cooldown_s`` seconds have elapsed past
        ``now`` (Req 7.8).

        Args:
            rule: The rule whose Routine just completed. It should be one of the
                rules held by this map (identity is used to key the cooldown).
            now: The monotonic timestamp (seconds) at which the Routine
                completed.
        """
        self._last_completed[id(rule)] = now
        print(
            f"DetectionRoutineMap: cooldown started for {rule.required_classes} "
            f"-> '{rule.action}' ({rule.cooldown_s}s)"
        )
