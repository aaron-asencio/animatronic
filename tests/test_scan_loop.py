"""Tests for the Scan Mode loop (``Animatronic._run_scan_loop`` + helpers).

Scan Mode layers a continuous neck tracker (channels 0/1) over a concurrent
arm-only responder (one response in flight at a time). These tests drive the
REAL ``_run_scan_loop`` / ``_dispatch_scan_response`` coroutines with:

  - a fake detections source (a stub ``client`` with ``get_detections``),
  - stubbed response callables (so no real servo/audio runs), and
  - a fake ``TrunkController`` recording every ``set_angle`` write,

and assert the behavioural contract:

  1. the neck tracker keeps issuing NECK_PAN/NECK_TILT writes ACROSS a response;
  2. only ONE response is in flight at a time;
  3. the responder logs the person+dog placeholder line when the dog label is
     present and dispatches ``choose_scan_action()`` (not ``rule.action``);
  4. the Gesture subset guard logs-and-skips a non-subset / missing name
     (fail-closed), never crashing the loop;
  5. a cancelled in-flight task is awaited during wind-down BEFORE the neck is
     recentered.

Per the testing steering, this is NOT a collision/limit simulation — SERVO_SIM=1
only lets the hardware-free logic run. No CLAMPED / SAFE_LIMITS-range /
FORBIDDEN_COMBINATIONS checks are performed here.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_loop.py -q --maxfail=1
"""

import asyncio
import os
import sys

# Hardware-free servo path: set BEFORE importing anything from src.
os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import animatronic  # noqa: E402
import constants  # noqa: E402
import nap_signal  # noqa: E402
from detection_routine_map import (  # noqa: E402
    ScanActionKind,
    ARM_ONLY_CHANNELS,
    SCAN_GESTURE_CHANNELS,
)
from vision_models import DetectionRule, TrackingConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeTrunk:
    """Records every ``set_angle`` write and supports a restricted rest.

    ``set_angle`` echoes the commanded angle back (the real one returns the
    post-clamp value) and appends ``(channel, angle)`` to ``writes`` so a test
    can read which channels were driven. ``return_to_rest(channels=...)`` records
    the rested channel set for the fail-path assertion.
    """

    def __init__(self):
        self.writes = []
        self.rested = []

    def set_angle(self, channel, angle):
        self.writes.append((channel, angle))
        return angle

    async def return_to_rest(self, channels=None):
        self.rested.append(frozenset(channels) if channels is not None else None)


class FakeClient:
    """Fake Camera_Service client returning a scripted detections stream.

    ``get_detections`` returns ``(detections, frame_w, frame_h)`` — the same
    scripted frame every call so the loop sees a stable scene.
    """

    def __init__(self, detections, frame_w=640, frame_h=480):
        self.detections = detections
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.calls = 0

    def get_detections(self):
        self.calls += 1
        return list(self.detections), self.frame_w, self.frame_h


def _person_rule():
    return DetectionRule(required_classes=("person",), action="scan", cooldown_s=8)


def _person_dog_rule():
    return DetectionRule(required_classes=("person", "dog"), action="scan", cooldown_s=8)


class FixedRoutineMap:
    """Deterministic stand-in for DetectionRoutineMap.

    ``select_action`` returns the configured rule for the first ``fire_times``
    calls then ``None``. ``mark_completed`` records the completed rule.
    """

    def __init__(self, rule, fire_times=1):
        self.rule = rule
        self.fire_times = fire_times
        self.selects = 0
        self.completed = []

    def select_action(self, detections, now):
        self.selects += 1
        if self.selects <= self.fire_times:
            return self.rule
        return None

    def mark_completed(self, rule, now):
        self.completed.append(rule)


def _cfg():
    return TrackingConfig()


def _new_animatronic():
    """An Animatronic instance without running __init__ (no hardware setup)."""
    return animatronic.Animatronic.__new__(animatronic.Animatronic)


def _fast_loop(monkeypatch):
    """Make the loop iterate quickly: zero the inter-iteration/sweep sleeps."""
    monkeypatch.setattr(animatronic.Animatronic, "_TRACKING_LOOP_PERIOD_S", 0.0)
    monkeypatch.setattr(animatronic.Animatronic, "_SCAN_STEP_PERIOD_S", 0.0)


def _clock(monkeypatch, step=0.05):
    """Install an auto-advancing monotonic clock.

    Every ``time.monotonic()`` call returns a value that increases by ``step``,
    so a deadline is reached deterministically after a bounded number of calls
    regardless of real wall-clock timing.
    """
    state = {"t": 0.0}

    def monotonic():
        state["t"] += step
        return state["t"]

    monkeypatch.setattr(animatronic.time, "monotonic", monotonic)
    return state


def _patch_neck_math(monkeypatch, target_present=True):
    """Make the neck tracker deterministic.

    Patches ``select_target`` to return a sentinel (or None) and
    ``compute_offset`` / ``next_neck_targets`` so the loop writes BOTH neck
    channels each iteration when a target is present.
    """
    sentinel = object() if target_present else None
    monkeypatch.setattr(animatronic, "select_target", lambda d, w, h: sentinel)
    monkeypatch.setattr(animatronic, "compute_offset", lambda t, w, h, c: (0, 0))
    monkeypatch.setattr(
        animatronic,
        "next_neck_targets",
        lambda off, pan, tilt, cfg, w, h: {
            constants.NECK_PAN: 90.0,
            constants.NECK_TILT: float(cfg.tilt_center_deg),
        },
    )


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_neck_keeps_stepping_across_a_response(monkeypatch):
    """The neck tracker issues NECK_PAN/NECK_TILT writes across a response.

    A slow (never-finishing until cancelled) response is dispatched on the first
    frame. While it is in flight the loop must keep driving the neck every
    iteration. A deadline stops the loop after several iterations; the recorded
    writes must include neck writes made AFTER the response task was created.
    """
    monkeypatch.setattr(nap_signal, "stop_requested", lambda: False)
    monkeypatch.setattr(nap_signal, "clear_stop", lambda: None)
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _patch_neck_math(monkeypatch, target_present=True)
    monkeypatch.setattr(animatronic, "choose_scan_action", lambda: "beckon")

    a = _new_animatronic()

    started = {"n": 0}

    async def slow_response():
        started["n"] += 1
        await asyncio.sleep(10)  # stays in flight until cancelled

    scan_responses = {"beckon": slow_response}
    client = FakeClient([object()])
    routine_map = FixedRoutineMap(_person_rule(), fire_times=1)
    cfg = _cfg()

    _clock(monkeypatch, step=0.05)
    # ~ a handful of iterations before the deadline is reached.
    deadline = 0.5

    asyncio.run(
        a._run_scan_loop(client, cfg, routine_map, scan_responses,
                         animatronic.Movements("x"), deadline)
    )

    assert started["n"] == 1  # response started exactly once
    pan_writes = [w for w in fake.writes if w[0] == constants.NECK_PAN]
    tilt_writes = [w for w in fake.writes if w[0] == constants.NECK_TILT]
    # Includes the up-front seed write + per-iteration writes while the response
    # was in flight.
    assert len(pan_writes) >= 2
    assert len(tilt_writes) >= 2


def test_only_one_response_in_flight(monkeypatch):
    """Only ONE response runs at a time even if a rule keeps matching."""
    monkeypatch.setattr(nap_signal, "stop_requested", lambda: False)
    monkeypatch.setattr(nap_signal, "clear_stop", lambda: None)
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _patch_neck_math(monkeypatch, target_present=True)
    monkeypatch.setattr(animatronic, "choose_scan_action", lambda: "beckon")

    a = _new_animatronic()

    inflight = {"cur": 0, "max": 0, "starts": 0}

    async def tracked_response():
        inflight["starts"] += 1
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        try:
            await asyncio.sleep(10)
        finally:
            inflight["cur"] -= 1

    scan_responses = {"beckon": tracked_response}
    client = FakeClient([object()])
    # Rule matches on EVERY frame — the guard must still allow only one.
    routine_map = FixedRoutineMap(_person_rule(), fire_times=10_000)
    cfg = _cfg()

    _clock(monkeypatch, step=0.05)
    asyncio.run(
        a._run_scan_loop(client, cfg, routine_map, scan_responses,
                         animatronic.Movements("x"), 0.6)
    )

    assert inflight["max"] == 1
    assert inflight["starts"] == 1


def test_person_dog_logs_placeholder_and_uses_weighted_choice(monkeypatch, capsys):
    """person+dog logs the placeholder and dispatches choose_scan_action()."""
    monkeypatch.setattr(nap_signal, "stop_requested", lambda: False)
    monkeypatch.setattr(nap_signal, "clear_stop", lambda: None)
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _patch_neck_math(monkeypatch, target_present=True)

    a = _new_animatronic()

    dispatched = {"name": None}

    async def resp():
        dispatched["name"] = "picked-by-weighted-choice"

    scan_responses = {"wave": resp}
    # The rule's own action is 'scan' — the responder must IGNORE it and use the
    # weighted picker instead. Force the picker to 'wave'.
    monkeypatch.setattr(animatronic, "choose_scan_action", lambda: "wave")

    client = FakeClient([object()])
    routine_map = FixedRoutineMap(_person_dog_rule(), fire_times=1)
    cfg = _cfg()

    _clock(monkeypatch, step=0.05)
    asyncio.run(
        a._run_scan_loop(client, cfg, routine_map, scan_responses,
                         animatronic.Movements("x"), 0.6)
    )

    out = capsys.readouterr().out
    assert "walkYourDog placeholder" in out
    assert dispatched["name"] == "picked-by-weighted-choice"
    # The fired rule's cooldown was started on completion.
    assert routine_map.completed


def test_gesture_subset_guard_fails_closed_on_missing_name(monkeypatch, capsys):
    """A Gesture whose channel set is absent is logged + skipped (no crash)."""
    monkeypatch.setattr(nap_signal, "clear_stop", lambda: None)
    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)

    a = _new_animatronic()

    ran = {"did": False}

    async def resp():
        ran["did"] = True

    # 'phantom' is tagged a GESTURE but has NO SCAN_GESTURE_CHANNELS entry ->
    # fail closed.
    monkeypatch.setitem(
        animatronic.SCAN_SAFE_ARM_ACTIONS, "phantom", ScanActionKind.GESTURE
    )
    try:
        asyncio.run(
            a._dispatch_scan_response(
                "phantom", {"phantom": resp}, animatronic.Movements("x")
            )
        )
    finally:
        animatronic.SCAN_SAFE_ARM_ACTIONS.pop("phantom", None)

    out = capsys.readouterr().out
    assert "REJECTED gesture 'phantom'" in out
    assert ran["did"] is False  # never dispatched


def test_gesture_subset_guard_allows_real_subset(monkeypatch):
    """A real Gesture whose channel set is a subset of ARM_ONLY runs."""
    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    a = _new_animatronic()

    ran = {"did": False}

    async def resp():
        ran["did"] = True

    assert SCAN_GESTURE_CHANNELS["beckon"] <= ARM_ONLY_CHANNELS
    asyncio.run(
        a._dispatch_scan_response("beckon", {"beckon": resp}, animatronic.Movements("x"))
    )
    assert ran["did"] is True


def test_missing_name_is_rejected(capsys):
    """A name absent from scan_responses is rejected (membership guard)."""
    a = _new_animatronic()
    asyncio.run(a._dispatch_scan_response("nope", {}, animatronic.Movements("x")))
    out = capsys.readouterr().out
    assert "REJECTED response 'nope'" in out


def test_gesture_failure_rests_arm_only_and_reraises(monkeypatch):
    """A failing Gesture best-effort rests ONLY the arm channels, then re-raises."""
    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    a = _new_animatronic()

    async def boom():
        raise RuntimeError("gesture died")

    with pytest.raises(RuntimeError):
        asyncio.run(
            a._dispatch_scan_response("beckon", {"beckon": boom}, animatronic.Movements("x"))
        )
    # The arm was rested with exactly ARM_ONLY_CHANNELS (never the neck 0/1).
    assert fake.rested == [frozenset(ARM_ONLY_CHANNELS)]
    assert constants.NECK_PAN not in ARM_ONLY_CHANNELS
    assert constants.NECK_TILT not in ARM_ONLY_CHANNELS


def test_winddown_cancels_and_awaits_before_recenter(monkeypatch):
    """Wind-down cancels+awaits the in-flight task BEFORE recentering the neck."""
    monkeypatch.setattr(nap_signal, "clear_stop", lambda: None)
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _patch_neck_math(monkeypatch, target_present=True)
    monkeypatch.setattr(animatronic, "choose_scan_action", lambda: "beckon")

    a = _new_animatronic()

    order = []

    async def slow_response():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            order.append("response-cancelled")
            raise

    real_recenter = animatronic.Animatronic._recenter_neck

    def spy_recenter(tilt_angle=None):
        order.append("recenter")
        return real_recenter(tilt_angle=tilt_angle)

    monkeypatch.setattr(
        animatronic.Animatronic, "_recenter_neck", staticmethod(spy_recenter)
    )

    scan_responses = {"beckon": slow_response}
    client = FakeClient([object()])
    routine_map = FixedRoutineMap(_person_rule(), fire_times=1)
    cfg = _cfg()

    # Stop requested after the first iteration (so a response is already in
    # flight when wind-down begins).
    calls = {"n": 0}

    def stop_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(nap_signal, "stop_requested", stop_after_one)
    _clock(monkeypatch, step=0.05)

    asyncio.run(
        a._run_scan_loop(client, cfg, routine_map, scan_responses,
                         animatronic.Movements("x"), None)
    )

    assert "response-cancelled" in order
    assert "recenter" in order
    assert order.index("response-cancelled") < order.index("recenter")
