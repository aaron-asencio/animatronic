"""Tests for Puppeteer's scan-sweep-timeout policy (``end_on_scan_timeout``).

Puppeteer reuses ``Animatronic.tracking`` / ``_run_tracking_loop`` but must NOT
wind down when the camera loses the person (a leave-frame Scan_Sweep timeout).
Plain ``--action=tracking`` MUST still end on that timeout (Req 6.10). These
tests drive the REAL ``_run_tracking_loop`` coroutine with:

  - a fake detections source (a stub ``client`` with ``get_detections``),
  - ``select_target`` patched so the loop always takes the no-target branch,
  - ``_run_scan_sweep`` patched to return a scripted ``(reacquired, pan)``, and
  - a fake ``TrunkController`` recording every ``set_angle`` write,

and assert the behavioural contract:

  1. default (plain tracking): a ``(False, pan)`` scan-sweep timeout ENDS the
     loop with ``TRACKING_INTERRUPT_SCAN_TIMEOUT``;
  2. Puppeteer (``end_on_scan_timeout=False``): a ``(False, pan)`` timeout does
     NOT end the loop — it recenters + idles and keeps running, ending only on
     an explicit ``nap_signal`` stop (``NAP_INTERRUPT_STOP``);
  3. the idle hold recenters the neck to rest and resumes tracking when a person
     reappears.

Per the testing steering, this is NOT a collision/limit simulation — SERVO_SIM=1
only lets the hardware-free logic run. No CLAMPED / SAFE_LIMITS-range /
FORBIDDEN_COMBINATIONS checks are performed here.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_puppeteer_scan_timeout.py -q --maxfail=1
"""

import asyncio
import os
import sys

# Hardware-free servo path: set BEFORE importing anything from src.
os.environ["SERVO_SIM"] = "1"

import pytest  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import animatronic  # noqa: E402
import constants  # noqa: E402
import nap_signal  # noqa: E402
from vision_models import TrackingConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeTrunk:
    """Records every ``set_angle`` write and echoes the commanded angle back."""

    def __init__(self):
        self.writes = []

    def set_angle(self, channel, angle):
        self.writes.append((channel, angle))
        return angle


class FakeClient:
    """Fake Camera_Service client returning the same scripted frame every call."""

    def __init__(self, detections=None, frame_w=640, frame_h=480):
        self.detections = detections if detections is not None else [object()]
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.calls = 0

    def get_detections(self):
        self.calls += 1
        return list(self.detections), self.frame_w, self.frame_h


def _new_animatronic():
    """An Animatronic instance without running __init__ (no hardware setup)."""
    return animatronic.Animatronic.__new__(animatronic.Animatronic)


def _fast_loop(monkeypatch):
    """Zero the inter-iteration sleep so the loop iterates quickly."""
    monkeypatch.setattr(animatronic.Animatronic, "_TRACKING_LOOP_PERIOD_S", 0.0)


def _no_target(monkeypatch):
    """Patch select_target to always return None (always the no-target branch)."""
    monkeypatch.setattr(animatronic, "select_target", lambda d, w, h: None)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_plain_tracking_ends_on_scan_timeout(monkeypatch):
    """Default (plain tracking): (False, pan) ends with SCAN_TIMEOUT."""
    monkeypatch.setattr(nap_signal, "stop_requested", lambda: False)
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _no_target(monkeypatch)

    sweeps = {"n": 0}

    async def fake_sweep(client, cfg, trunk, cur_pan):
        sweeps["n"] += 1
        # Scan_Sweep timed out with no reacquire.
        return False, 95.0

    a = _new_animatronic()
    monkeypatch.setattr(a, "_run_scan_sweep", fake_sweep)

    reason, pending = asyncio.run(
        a._run_tracking_loop(
            FakeClient(), TrackingConfig(), None, None, end_on_scan_timeout=True
        )
    )

    assert reason == animatronic.Animatronic.TRACKING_INTERRUPT_SCAN_TIMEOUT
    assert pending is None
    assert sweeps["n"] == 1  # ended on the first timeout


def test_puppeteer_does_not_end_on_scan_timeout(monkeypatch):
    """Puppeteer (end_on_scan_timeout=False): (False, pan) does NOT end the loop.

    The loop must recenter + idle and keep running; only an explicit nap_signal
    stop ends it (NAP_INTERRUPT_STOP). We let the sweep time out several times
    and only then request a stop — proving the scan timeout alone never ends it.
    """
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)
    _no_target(monkeypatch)

    sweeps = {"n": 0}

    async def fake_sweep(client, cfg, trunk, cur_pan):
        sweeps["n"] += 1
        return False, 95.0  # always a timeout, never a reacquire

    # Stop is requested only after the idle hold has polled a few times, so the
    # mode must have survived multiple scan-sweep timeouts to reach it.
    stop_calls = {"n": 0}

    def stop_after_several():
        stop_calls["n"] += 1
        return stop_calls["n"] > 5

    monkeypatch.setattr(nap_signal, "stop_requested", stop_after_several)

    a = _new_animatronic()
    monkeypatch.setattr(a, "_run_scan_sweep", fake_sweep)

    reason, pending = asyncio.run(
        a._run_tracking_loop(
            FakeClient(), TrackingConfig(), None, None, end_on_scan_timeout=False
        )
    )

    # Ended ONLY via the explicit stop, never the scan timeout.
    assert reason == animatronic.Animatronic.NAP_INTERRUPT_STOP
    assert pending is None
    assert sweeps["n"] >= 1  # at least one timeout was survived, not fatal


def test_puppeteer_idle_hold_recenters_and_resumes(monkeypatch):
    """On a scan timeout Puppeteer recenters to rest, then resumes on reappear.

    After the first (False, pan) timeout the idle hold recenters the neck to
    rest and polls. We make a person reappear on the hold's next poll so the
    helper returns ``(stopped=False, ...)`` and the main loop resumes tracking;
    a stop then ends it. We assert the neck was recentered to the rest pose.
    """
    _fast_loop(monkeypatch)

    fake = FakeTrunk()
    monkeypatch.setattr(animatronic.Movements, "trunkController", fake)

    cfg = TrackingConfig()

    # select_target: None (no target) until the idle-hold poll, then a sentinel
    # (person reappeared) so _puppeteer_idle_hold returns and tracking resumes.
    target_states = iter([None, object()])

    def select_target(detections, w, h):
        try:
            return next(target_states)
        except StopIteration:
            return object()

    monkeypatch.setattr(animatronic, "select_target", select_target)
    # Make the neck math a no-op write once tracking resumes.
    monkeypatch.setattr(animatronic, "compute_offset", lambda t, w, h, c: (0, 0))
    monkeypatch.setattr(
        animatronic,
        "next_neck_targets",
        lambda off, pan, tilt, cfg, w, h: {},
    )

    async def fake_sweep(client, cfg_, trunk, cur_pan):
        return False, 150.0  # timed out at an off-center pan

    # Stop after a couple of loop iterations so the test terminates.
    stop_calls = {"n": 0}

    def stop_after_two():
        stop_calls["n"] += 1
        return stop_calls["n"] > 3

    monkeypatch.setattr(nap_signal, "stop_requested", stop_after_two)

    a = _new_animatronic()
    monkeypatch.setattr(a, "_run_scan_sweep", fake_sweep)

    reason, _ = asyncio.run(
        a._run_tracking_loop(
            FakeClient(), cfg, None, None, end_on_scan_timeout=False
        )
    )

    assert reason == animatronic.Animatronic.NAP_INTERRUPT_STOP
    # The idle hold recentered the neck to rest: pan to global rest, tilt to the
    # tracking level-gaze center (both written through set_angle).
    rest_pan = constants.REST_POSITIONS[constants.NECK_PAN]
    assert (constants.NECK_PAN, float(rest_pan)) in [
        (c, float(ang)) for (c, ang) in fake.writes
    ]
    assert (constants.NECK_TILT, float(cfg.tilt_center_deg)) in [
        (c, float(ang)) for (c, ang) in fake.writes
    ]
