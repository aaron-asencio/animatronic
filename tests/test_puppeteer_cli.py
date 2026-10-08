"""CLI-dispatch tests for Puppeteer Mode (``--action=puppeteer``) in
src/animatronic.py.

Hardware-free: ``SERVO_SIM=1`` is set before importing src so the fake kit is
used, and ``main()``'s hardware path is avoided by mocking ``group_lock`` (so no
real lock file is touched) and ``Animatronic.tracking`` (so no camera/servo loop
runs). The assertions pin the design's dispatch contract:

  - ``build_action_map()`` contains ``'puppeteer'`` mapped to the ``tracking``
    method (allowlist parity — AC2).
  - ``main(--action=puppeteer)`` reaches the dedicated branch and calls
    ``tracking(..., suppress_triggers=True)`` under the Neck_Group lock, and does
    NOT call ``_dispatch_detection_trigger`` (suppression guarantees no pending
    trigger — AC7).
  - An unknown action hits the ``else: Unknown action`` path with no
    ``getattr``/``eval``/``exec``/shell (AC2).

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_puppeteer_cli.py -q --maxfail=1
"""

import argparse
import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import animatronic  # noqa: E402
from animatronic import Animatronic, main  # noqa: E402


def _tracking_args(action):
    """Build an argparse Namespace with the tracking/puppeteer flags main() reads."""
    return argparse.Namespace(
        action=action,
        camera_url="http://localhost:8001",
        max_step=None, deadband=None, conf=None, scan_timeout=None,
        aim_frac=None, tilt_center=None, tilt_min=None, tilt_max=None,
        settle_gain=None,
        nap_timeout_min=1, awake_timeout_min=5, scan_timeout_min=60,
    )


class _NullLock:
    """Context manager stand-in for group_lock(...)/servo_lock() — no I/O."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_puppeteer_in_action_map():
    """'puppeteer' is in the allowlist and maps to the tracking method (AC2)."""
    am = Animatronic().build_action_map()
    assert 'puppeteer' in am
    # Bound method of the same instance -> underlying function is tracking.
    assert am['puppeteer'].__func__ is Animatronic.tracking


def test_main_puppeteer_dispatch_suppresses_triggers(monkeypatch):
    """main(--action=puppeteer) calls tracking(suppress_triggers=True) under the
    Neck_Group lock and dispatches NO detection trigger (AC7)."""
    calls = {"tracking": [], "group_lock": [], "dispatch": 0}

    def fake_group_lock(group):
        calls["group_lock"].append(group)
        return _NullLock()

    def fake_tracking(self, **kwargs):
        calls["tracking"].append(kwargs)
        return None  # suppress_triggers=True always returns None

    monkeypatch.setattr(animatronic, "group_lock", fake_group_lock)
    monkeypatch.setattr(Animatronic, "tracking", fake_tracking)
    monkeypatch.setattr(animatronic, "_dispatch_detection_trigger",
                        lambda *a, **k: calls.__setitem__("dispatch", calls["dispatch"] + 1))

    main(_tracking_args('puppeteer'))

    assert len(calls["tracking"]) == 1
    assert calls["tracking"][0].get("suppress_triggers") is True
    # Dispatched through the Neck_Group lock (not the whole-robot servo_lock()).
    assert calls["group_lock"] == [animatronic.NECK_GROUP]
    # Suppression means no pending trigger -> no dispatch call.
    assert calls["dispatch"] == 0


def test_main_tracking_unchanged_still_arms(monkeypatch):
    """Plain --action=tracking does NOT pass suppress_triggers (FR4): it stays
    the default (False), so triggers are armed exactly as before."""
    seen = {"kwargs": None}

    monkeypatch.setattr(animatronic, "group_lock", lambda g: _NullLock())
    monkeypatch.setattr(Animatronic, "tracking",
                        lambda self, **kwargs: seen.__setitem__("kwargs", kwargs))
    monkeypatch.setattr(animatronic, "_dispatch_detection_trigger", lambda *a, **k: None)

    main(_tracking_args('tracking'))

    assert seen["kwargs"] is not None
    # The tracking branch never passes suppress_triggers (so it defaults False).
    assert 'suppress_triggers' not in seen["kwargs"]


def test_main_unknown_action_rejected(capsys):
    """An unknown action hits the 'Unknown action' path (no getattr/eval/shell)."""
    main(_tracking_args('definitely-not-a-real-action'))
    out = capsys.readouterr().out
    assert 'Unknown action' in out
