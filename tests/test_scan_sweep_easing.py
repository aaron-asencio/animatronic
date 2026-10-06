"""Focused tests for the eased leave-frame Scan_Sweep velocity profile.

The Scan_Sweep (``Animatronic._run_scan_sweep``, shared by plain Tracking and by
Puppeteer) pans NECK_PAN to reacquire a lost person. It no longer advances at a
constant velocity: each step follows an ease-in/ease-out profile so the head
glides off a standstill and settles toward each turnaround instead of jerking.

These tests drive the REAL ``_run_scan_sweep`` coroutine with:

  - a fake detections source (a stub ``client`` with ``get_detections``),
  - ``select_target`` patched so the loop takes the no-target branch (or finds
    one on cue, to exercise the reacquire path), and
  - a fake ``TrunkController`` recording every ``set_angle`` write,

and assert the behavioural contract that matters:

  1. the sweep stays within [pan_min, pan_max] (SAFE_LIMITS) and reverses at the
     endpoints;
  2. it returns (None, pan) on a nap_signal stop, (False, pan) on timeout, and
     (True, pan) on reacquire, with pan consistent with the last written value;
  3. every write goes through set_angle (NECK_PAN only);
  4. the EASING: per-step increments are smaller near the endpoints than across
     the middle, and the STEP floor keeps the pan progressing (no zero-velocity
     stall).

Per the testing steering, this is NOT a collision/limit simulation — SERVO_SIM=1
only lets the hardware-free logic run. No CLAMPED / SAFE_LIMITS-range /
FORBIDDEN_COMBINATIONS checks are performed here.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_sweep_easing.py -q --maxfail=1
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
from vision_models import TrackingConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeTrunk:
    """Records every ``set_angle`` write and echoes the commanded angle.

    The real ``set_angle`` returns the post-clamp value; here the commanded
    angles are always inside SAFE_LIMITS (the sweep bounds itself), so echoing
    is faithful. ``writes`` lets a test read which channels were driven and the
    exact pan sequence.
    """

    def __init__(self):
        self.writes = []

    def set_angle(self, channel, angle):
        self.writes.append((channel, angle))
        return angle


class FakeClient:
    """Fake Camera_Service client returning a scripted detections stream.

    ``get_detections`` returns ``(detections, frame_w, frame_h)``. The sweep
    only cares about frame size being > 0 and what ``select_target`` yields, so
    the detections payload itself is irrelevant here.
    """

    def __init__(self, frame_w=640, frame_h=480):
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.calls = 0

    def get_detections(self):
        self.calls += 1
        return [], self.frame_w, self.frame_h


def _new_animatronic():
    """An Animatronic instance without running __init__ (no hardware setup)."""
    return animatronic.Animatronic.__new__(animatronic.Animatronic)


def _cfg(scan_timeout_s=10.0):
    cfg = TrackingConfig()
    # TrackingConfig is a (non-frozen) dataclass; __post_init__ clamps
    # scan_timeout_s to [1, 120], but these tests need a larger deadline so the
    # sweep can traverse the whole range many times. Set it AFTER construction
    # (bypassing the clamp) — the value is a loop deadline here, not a servo
    # command, so it is not a safety value.
    cfg.scan_timeout_s = scan_timeout_s
    return cfg


def _fast(monkeypatch):
    """Zero the inter-step sleep so the sweep iterates quickly."""
    monkeypatch.setattr(animatronic.Animatronic, "_SCAN_STEP_PERIOD_S", 0.0)


def _no_target(monkeypatch):
    """Never reacquire: the sweep keeps panning until stop/timeout."""
    monkeypatch.setattr(animatronic, "select_target", lambda d, w, h: None)


def _clock(monkeypatch, step=0.05):
    """Auto-advancing monotonic clock so the timeout is deterministic."""
    state = {"t": 0.0}

    def monotonic():
        state["t"] += step
        return state["t"]

    monkeypatch.setattr(animatronic.time, "monotonic", monotonic)
    return state


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_sweep_stays_in_range_and_reverses(monkeypatch):
    """The sweep stays within SAFE_LIMITS and bounces at both endpoints."""
    _fast(monkeypatch)
    _no_target(monkeypatch)
    # Long enough deadline to traverse the whole range and reverse at least once.
    _clock(monkeypatch, step=0.05)
    nap_signal.clear_stop()

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    trunk = FakeTrunk()
    a = _new_animatronic()

    # Big timeout budget (clock steps 0.05 per monotonic call; the loop makes a
    # bounded number of calls per tick) so we exercise many steps.
    reacquired, pan = asyncio.run(
        a._run_scan_sweep(FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=90.0)
    )

    assert reacquired is False  # timed out with no reacquire
    pans = [angle for ch, angle in trunk.writes]
    assert pans, "sweep wrote at least one NECK_PAN angle"

    # (3) every write is NECK_PAN only, clamped inside SAFE_LIMITS.
    assert all(ch == constants.NECK_PAN for ch, _ in trunk.writes)
    assert all(pan_min <= p <= pan_max for p in pans)

    # (1) reverses at the endpoints: the pan both rises and falls over the run.
    assert max(pans) > min(pans)
    went_up = any(b > a_ for a_, b in zip(pans, pans[1:]))
    went_down = any(b < a_ for a_, b in zip(pans, pans[1:]))
    assert went_up and went_down, "sweep must reverse direction (bounce)"

    # Returned pan matches the last written (post-clamp) value.
    assert pan == pans[-1]


def test_sweep_eases_slower_at_endpoints_than_middle(monkeypatch):
    """Per-step increments ramp: small near endpoints, larger across the middle.

    Starts at pan_min so the first steps are the ease-in off the endpoint; by the
    time the pan reaches the middle of the range the steps have grown. Also
    asserts the STEP floor keeps every step strictly progressing (no stall).
    """
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch, step=0.05)
    nap_signal.clear_stop()

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    mid = (pan_min + pan_max) / 2.0
    trunk = FakeTrunk()
    a = _new_animatronic()

    # Start right at the low endpoint so the sweep eases IN from a standstill.
    asyncio.run(
        a._run_scan_sweep(
            FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=float(pan_min)
        )
    )

    pans = [angle for ch, angle in trunk.writes]
    # Per-step deltas along the first (increasing) traversal, up to the first
    # turnaround. The LAST increasing step lands exactly on pan_max (clamp
    # truncation), so its magnitude can be below STEP_MIN — that is the endpoint
    # clamp, not an eased step. Exclude it from the floor check below.
    deltas = []
    for a_, b in zip(pans, pans[1:]):
        if b <= a_:  # reversal — stop at the first turnaround.
            break
        deltas.append(b - a_)

    assert len(deltas) > 5, "need several increasing steps to compare the ramp"

    # (4a) STEP floor: no COMPUTED step stalls to (near) zero; every step except
    # the final clamp-to-endpoint landing is >= the configured minimum (minus a
    # tiny float tolerance), so the pan always progresses and never crawls.
    computed_deltas = deltas[:-1]  # drop the endpoint-landing (clamped) step
    assert computed_deltas, "expected several computed (non-clamp) steps"
    assert min(computed_deltas) >= a._SCAN_STEP_DEG_MIN - 1e-6

    # (4b) Easing: the first few steps (near the start endpoint) are SMALLER than
    # the steps taken once the pan is near the middle of the range.
    first_steps = deltas[:3]
    mid_steps = [
        b - a_
        for a_, b in zip(pans, pans[1:])
        if a_ < b and abs(a_ - mid) <= a._SCAN_RAMP_DEG / 2.0
    ]
    assert mid_steps, "expected steps taken near the mid-range"
    assert max(first_steps) < max(mid_steps), (
        "ease-in steps near the endpoint should be smaller than mid-range steps"
    )
    # Steps never exceed the configured maximum.
    assert max(deltas) <= a._SCAN_STEP_DEG_MAX + 1e-6


def test_sweep_returns_none_on_nap_stop(monkeypatch):
    """A nap_signal stop ends the sweep with (None, pan)."""
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch, step=0.05)

    trunk = FakeTrunk()
    a = _new_animatronic()

    # Request a stop up front so the first loop check returns immediately.
    nap_signal.request_stop()
    try:
        reacquired, pan = asyncio.run(
            a._run_scan_sweep(FakeClient(), _cfg(), trunk, cur_pan=90.0)
        )
    finally:
        nap_signal.clear_stop()

    assert reacquired is None
    assert pan == 90.0  # no step taken before the stop was honored


def test_sweep_returns_true_on_reacquire(monkeypatch):
    """A reappearing person ends the sweep with (True, pan)."""
    _fast(monkeypatch)
    _clock(monkeypatch, step=0.05)
    nap_signal.clear_stop()

    # select_target finds a person on the 2nd poll.
    calls = {"n": 0}

    def finder(detections, w, h):
        calls["n"] += 1
        return object() if calls["n"] >= 2 else None

    monkeypatch.setattr(animatronic, "select_target", finder)

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    trunk = FakeTrunk()
    a = _new_animatronic()

    reacquired, pan = asyncio.run(
        a._run_scan_sweep(FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=90.0)
    )

    assert reacquired is True
    assert pan_min <= pan <= pan_max
    pans = [angle for ch, angle in trunk.writes]
    if pans:
        assert pan == pans[-1]
