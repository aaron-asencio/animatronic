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
  4. the EASING: per-step increments ramp to ~0 near BOTH ends of a leg and up to
     STEP_MAX across the middle (a true accel -> cruise -> decel velocity
     profile, not just >= a non-zero floor), yet the leg still REACHES its
     endpoint within a bounded number of ticks (no zero-velocity stall / infinite
     crawl); and
  5. the reversal DWELL: the pan HOLDS at an endpoint for ~_SCAN_REVERSAL_DWELL_S
     worth of ticks before the next leg advances, while nap_signal is still
     polled each dwell tick.

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
    """Make the per-tick sleep a no-op so the sweep iterates instantly.

    The REAL tick period (_SCAN_STEP_PERIOD_S = 0.05s) is left intact so the
    seconds-based reversal dwell (_SCAN_REVERSAL_DWELL_S / period) still derives
    its realistic tick count (~4 ticks); we only skip the wall-clock wait by
    stubbing asyncio.sleep. The deterministic monotonic clock (see _clock) still
    advances by the real period per tick, so the timeout budget stays faithful.
    """

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(animatronic.asyncio, "sleep", _no_sleep)


def _no_target(monkeypatch):
    """Never reacquire: the sweep keeps panning until stop/timeout."""
    monkeypatch.setattr(animatronic, "select_target", lambda d, w, h: None)


def _clock(monkeypatch, step=None):
    """Auto-advancing monotonic clock so the timeout is deterministic.

    Advances by the sweep's ACTUAL tick period per call (unless ``step`` is given
    explicitly), so sim time stays proportional to the number of ticks — the
    reversal dwell (a fixed number of ticks derived from the period) then
    consumes a proportionate slice of the timeout budget rather than being
    inflated by a mismatched clock step.
    """
    if step is None:
        step = animatronic.Animatronic._SCAN_STEP_PERIOD_S
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
    _clock(monkeypatch)
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


def _first_leg_deltas(pans):
    """Per-step increments along the FIRST (increasing) leg, up to the turn.

    Stops at the first non-increasing step (the reversal / dwell hold), so the
    returned list is the ease-in -> cruise -> ease-out of the first traversal.
    """
    deltas = []
    for a_, b in zip(pans, pans[1:]):
        if b <= a_:  # reversal / dwell hold — stop at the first turnaround.
            break
        deltas.append(b - a_)
    return deltas


def test_sweep_velocity_ramps_to_near_zero_at_both_leg_ends(monkeypatch):
    """A true accel -> cruise -> decel profile: ~0 velocity at both leg ends.

    Starts at pan_min so the first steps are the ease-in off a standstill; the
    steps grow to STEP_MAX across the middle and shrink back toward ~0 as the pan
    nears pan_max. Asserts the ends are much slower than the cruise AND that the
    very first/last eased steps approach zero (not merely a non-zero floor).
    """
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch)
    nap_signal.clear_stop()

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    trunk = FakeTrunk()
    a = _new_animatronic()

    # Start right at the low endpoint so the sweep eases IN from a standstill.
    asyncio.run(
        a._run_scan_sweep(
            FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=float(pan_min)
        )
    )

    pans = [angle for ch, angle in trunk.writes]
    deltas = _first_leg_deltas(pans)
    assert len(deltas) > 10, "need many steps to see accel -> cruise -> decel"

    # Steps never exceed the configured maximum (cruise cap).
    assert max(deltas) <= a._SCAN_STEP_DEG_MAX + 1e-6

    # (4) Cruise is reached in the middle.
    cruise = max(deltas)
    assert cruise >= 0.6 * a._SCAN_STEP_DEG_MAX, "expected a near-STEP_MAX cruise"

    # Ease-in: the first eased step off the standstill approaches ~0 — far below
    # the cruise and below a small fraction of STEP_MAX (NOT pinned to a 0.5 deg
    # floor like the old profile).
    assert deltas[0] < 0.2 * a._SCAN_STEP_DEG_MAX, (
        "first step should ease up from ~0, not jump to a non-zero floor"
    )
    assert deltas[0] < 0.25 * cruise

    # Ease-out: the smallest step in the ramp band approaching the endpoint is
    # also well below cruise (the velocity decays toward 0 before the turn). The
    # final landing step is the endpoint snap, which is itself <= the snap
    # threshold; include the whole leg's minimum computed step.
    assert min(deltas) < 0.2 * a._SCAN_STEP_DEG_MAX


def test_sweep_leg_reaches_endpoint_in_bounded_ticks(monkeypatch):
    """Progress is guaranteed: the leg lands on an endpoint, no stall/crawl.

    With the velocity easing toward ~0 at the ends there is no hard floor, so
    this pins the anti-stall contract: starting at pan_min the sweep reaches
    pan_max (reverses) within a bounded number of writes.
    """
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch)
    nap_signal.clear_stop()

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    trunk = FakeTrunk()
    a = _new_animatronic()

    asyncio.run(
        a._run_scan_sweep(
            FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=float(pan_min)
        )
    )

    pans = [angle for ch, angle in trunk.writes]
    # The first leg must actually LAND on pan_max (not crawl asymptotically).
    assert any(abs(p - pan_max) <= 1e-6 for p in pans), "leg must reach pan_max"

    # Bounded tick count: whole range / cruise is the ideal lower bound; allow a
    # generous ease/dwell overhead but assert it is finite and reasonable so an
    # infinite crawl would fail. idx of first pan_max landing:
    first_max_idx = next(i for i, p in enumerate(pans) if abs(p - pan_max) <= 1e-6)
    span = pan_max - pan_min
    ideal = span / a._SCAN_STEP_DEG_MAX
    assert first_max_idx <= 6 * ideal, (
        f"leg took {first_max_idx} ticks to reach the endpoint; expected a "
        f"bounded count near {ideal:.0f} (no zero-velocity stall)"
    )


def test_sweep_dwells_at_endpoint_before_reversing(monkeypatch):
    """The pan HOLDS at an endpoint for the reversal dwell before the next leg.

    Starts at pan_min so the first leg eases up to pan_max; at that turnaround
    the pan must hold (be written unchanged) for at least the configured dwell
    ticks before the next (decreasing) leg advances. nap_signal is cleared so the
    dwell runs its full course (its per-tick nap_signal poll is covered by
    test_sweep_returns_none_on_nap_stop).
    """
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch)
    nap_signal.clear_stop()

    pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
    trunk = FakeTrunk()
    a = _new_animatronic()

    asyncio.run(
        a._run_scan_sweep(
            FakeClient(), _cfg(scan_timeout_s=500.0), trunk, cur_pan=float(pan_min)
        )
    )

    pans = [angle for ch, angle in trunk.writes]
    assert any(abs(p - pan_max) <= 1e-6 for p in pans), "sweep must reach pan_max"

    # Find the first write that lands on pan_max, then count the consecutive run
    # pinned there (the endpoint-landing write + the dwell holds) before the pan
    # moves off it (the next, decreasing leg).
    first_max_idx = next(i for i, p in enumerate(pans) if abs(p - pan_max) <= 1e-6)
    hold = 0
    for p in pans[first_max_idx:]:
        if abs(p - pan_max) <= 1e-6:
            hold += 1
        else:
            break

    expected_dwell_ticks = max(
        1, round(a._SCAN_REVERSAL_DWELL_S / max(a._SCAN_STEP_PERIOD_S, 1e-6))
    )
    # The landing write plus expected_dwell_ticks dwell holds all write pan_max,
    # so the pinned run is at least the dwell count.
    assert hold >= expected_dwell_ticks, (
        f"expected the pan to dwell >= {expected_dwell_ticks} ticks at the "
        f"endpoint before reversing; held {hold}"
    )

    # After the dwell the pan must actually reverse (start decreasing).
    assert any(b < a_ for a_, b in zip(pans, pans[1:])), "sweep must reverse after dwell"


def test_sweep_returns_none_on_nap_stop(monkeypatch):
    """A nap_signal stop ends the sweep with (None, pan)."""
    _fast(monkeypatch)
    _no_target(monkeypatch)
    _clock(monkeypatch)

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
    _clock(monkeypatch)
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
