"""Focused tests for the eased neck wind-down (R2) and per-frame smoothing (R3).

Two independent concerns are covered:

R2 — ``Animatronic._recenter_neck`` is now an EASED, async wind-down: it drives
the Neck_Group to rest via ``TrunkController.move_to`` (smoothstep) instead of a
one-shot ``set_angle`` snap, yet it MUST keep its never-raises recovery
contract. If the eased ``move_to`` raises, it falls back to the per-channel
one-shot snap and swallows the error — the neck still ends at rest and no
exception propagates.

R3 — ``tracking_controller.smooth_neck_targets`` slew-limits + low-passes the
per-frame neck targets so a single large correction (or a rapid reversal) can no
longer produce an instantaneous jump, while a steady follow still converges.

Per the testing steering this is NOT a collision/limit simulation — SERVO_SIM=1
only lets the hardware-free logic run. No CLAMPED / SAFE_LIMITS-range /
FORBIDDEN_COMBINATIONS checks are performed here.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_neck_easing.py -q --maxfail=1
"""

import asyncio
import os
import sys

# Hardware-free servo path: set BEFORE importing anything from src.
os.environ["SERVO_SIM"] = "1"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import animatronic  # noqa: E402
import constants  # noqa: E402
import tracking_controller as tc  # noqa: E402


class _RecordingTrunk:
    """Records set_angle writes; eased move_to commits each target's end state."""

    def __init__(self):
        self.writes = []
        self.move_to_calls = []

    def set_angle(self, channel, angle):
        self.writes.append((channel, angle))
        return angle

    async def move_to(self, targets, steps=60, delay=0.02, ease=True):
        self.move_to_calls.append(dict(targets))
        for channel, angle in targets.items():
            self.set_angle(channel, angle)


class _RaisingTrunk:
    """move_to raises (simulating a mid-move failure); set_angle still works."""

    def __init__(self):
        self.writes = []

    def set_angle(self, channel, angle):
        self.writes.append((channel, angle))
        return angle

    async def move_to(self, targets, steps=60, delay=0.02, ease=True):
        raise RuntimeError("simulated move_to failure")


def _patch_trunk(monkeypatch, trunk):
    monkeypatch.setattr(animatronic.Movements, "trunkController", trunk)


# ---------------------------------------------------------------------------
# R2 — eased _recenter_neck
# ---------------------------------------------------------------------------

def test_recenter_neck_eases_via_move_to_and_ends_at_rest(monkeypatch):
    """The happy path uses the eased move_to and ends pan=90 / tilt=given."""
    trunk = _RecordingTrunk()
    _patch_trunk(monkeypatch, trunk)

    tilt_center = 95.0
    asyncio.run(animatronic.Animatronic._recenter_neck(tilt_angle=tilt_center))

    # The eased path was taken (move_to called), not a bare snap.
    assert trunk.move_to_calls, "recenter must ease via move_to"
    targets = trunk.move_to_calls[0]
    assert targets[constants.NECK_PAN] == constants.REST_POSITIONS[constants.NECK_PAN]
    assert targets[constants.NECK_TILT] == tilt_center
    # ONLY the Neck_Group channels are driven.
    assert set(targets) == {constants.NECK_PAN, constants.NECK_TILT}
    # Final writes land the neck at rest.
    final = dict(trunk.writes)
    assert final[constants.NECK_PAN] == constants.REST_POSITIONS[constants.NECK_PAN]
    assert final[constants.NECK_TILT] == tilt_center


def test_recenter_neck_defaults_tilt_to_rest(monkeypatch):
    """With tilt_angle=None the tilt target falls back to REST_POSITIONS."""
    trunk = _RecordingTrunk()
    _patch_trunk(monkeypatch, trunk)

    asyncio.run(animatronic.Animatronic._recenter_neck())

    targets = trunk.move_to_calls[0]
    assert targets[constants.NECK_TILT] == constants.REST_POSITIONS[constants.NECK_TILT]


def test_recenter_neck_never_raises_and_snaps_on_move_to_failure(monkeypatch):
    """If move_to raises, the fallback one-shot snap still rests the neck."""
    trunk = _RaisingTrunk()
    _patch_trunk(monkeypatch, trunk)

    # Must NOT raise despite move_to blowing up.
    asyncio.run(animatronic.Animatronic._recenter_neck(tilt_angle=92.0))

    # Fallback snap drove each Neck_Group channel to rest via set_angle.
    final = dict(trunk.writes)
    assert final[constants.NECK_PAN] == constants.REST_POSITIONS[constants.NECK_PAN]
    assert final[constants.NECK_TILT] == 92.0
    assert set(final) == {constants.NECK_PAN, constants.NECK_TILT}


# ---------------------------------------------------------------------------
# R3 — smooth_neck_targets
# ---------------------------------------------------------------------------

def test_smooth_large_offset_is_not_an_instant_jump():
    """A big single-frame correction is spread, not snapped to the raw target."""
    raw = {constants.NECK_PAN: 150.0}  # +60 deg from 90 in one frame
    out = tc.smooth_neck_targets(raw, 90.0, 90.0)
    # Strictly between the current and the raw target — no instant jump.
    assert 90.0 < out[constants.NECK_PAN] < 150.0
    # Bounded by the slew cap: at most half the cap after the EMA blend.
    assert out[constants.NECK_PAN] <= 90.0 + tc._TRACK_MAX_SLEW_DEG


def test_smooth_only_touches_present_channels():
    """A targets dict with one axis yields a result with just that axis."""
    out = tc.smooth_neck_targets({constants.NECK_TILT: 100.0}, 90.0, 90.0)
    assert set(out) == {constants.NECK_TILT}
    assert 90.0 < out[constants.NECK_TILT] < 100.0


def test_smooth_empty_targets_returns_empty():
    assert tc.smooth_neck_targets({}, 90.0, 90.0) == {}


def test_smooth_converges_toward_target_over_frames():
    """Feeding the output back as the current angle converges to the target."""
    cur = 90.0
    target = 120.0
    for _ in range(40):
        out = tc.smooth_neck_targets({constants.NECK_PAN: target}, cur, 90.0)
        cur = out[constants.NECK_PAN]
    # After enough frames the commanded angle has essentially reached target.
    assert abs(cur - target) < 0.5
