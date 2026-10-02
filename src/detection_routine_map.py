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

from typing import Iterable, List, Optional

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
