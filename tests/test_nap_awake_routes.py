"""Flask route tests for Nap and Awake Modes (``POST /nap|/awake/<state>``).

These exercise the real Flask routes with the test client, with the subprocess
launchers (``run_napping`` / ``run_awake``) and the mic auto-stop (``_stop_mic``
/ ``_mic_is_streaming``) monkeypatched so nothing is actually spawned and no
hardware is touched. Mirrors ``tests/test_scan_routes.py``.

Assertions (for both /nap/start and /awake/start):
  - a valid value (30) -> 200, launcher called with 30, "timeout 30 min";
  - 0 (no timeout) -> 200, launcher called with 0, "no timeout";
  - omitted ({}) -> 200, launcher called with 0 (the new default);
  - out-of-range (121, -5) -> 400, launcher NOT called;
  - non-int ("30", 1.5, True) -> 400, launcher NOT called.

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_nap_awake_routes.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
import nap_signal  # noqa: E402


class FakeProc:
    """Minimal stand-in for subprocess.Popen (poll() reports still-running)."""

    # A real Popen exposes returncode (None until it exits); the napping/awake
    # launchers now start a _chain_watch daemon that reads it after wait().
    returncode = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


# Per-mode wiring so one parametrized fixture/test body covers both routes.
# (start_path, launcher_attr, label)
_MODES = {
    "nap": ("/nap/start", "run_napping", "napping"),
    "awake": ("/awake/start", "run_awake", "awake"),
}


@pytest.fixture(params=sorted(_MODES))
def mode_client(request, monkeypatch):
    """Flask test client with the per-mode launcher stubbed and mic idle.

    Yields a (client, start_path, launched) tuple where ``launched["calls"]``
    records the integer minutes each launcher was invoked with. ``run_napping``
    / ``run_awake`` are replaced with recorders returning a fake process so no
    subprocess spawns; the mic is reported idle so ``_stop_mic`` short-circuits;
    ``_active_proc`` is reset so the busy check passes.
    """
    start_path, launcher_attr, _label = _MODES[request.param]

    launched = {"calls": []}

    def fake_launcher(timeout_min):
        launched["calls"].append(int(timeout_min))
        return FakeProc()

    monkeypatch.setattr(webapp, launcher_attr, fake_launcher)
    # Mic idle by default so launch proceeds without stopping anything.
    monkeypatch.setattr(webapp, "_mic_is_streaming", lambda: False)
    # Reset the active-process tracker so the busy guard passes.
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None

    webapp.app.config['TESTING'] = True
    c = webapp.app.test_client()
    yield c, start_path, launched
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None


def test_start_valid_minutes_succeeds(mode_client):
    """A valid value (30) launches the mode with 30 and reports the timeout."""
    client, start_path, launched = mode_client
    res = client.post(start_path, json={'timeout_min': 30})
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert 'timeout 30 min' in data['message']
    assert launched['calls'] == [30]


def test_start_zero_no_timeout(mode_client):
    """0 is accepted (no timeout): launched with 0 and reported as 'no timeout'."""
    client, start_path, launched = mode_client
    res = client.post(start_path, json={'timeout_min': 0})
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert 'no timeout' in data['message']
    assert launched['calls'] == [0]


def test_start_omitted_defaults_to_zero(mode_client):
    """Omitting timeout_min defaults to 0 (No timeout) and launches with 0."""
    client, start_path, launched = mode_client
    res = client.post(start_path, json={})
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert 'no timeout' in data['message']
    assert launched['calls'] == [0]


@pytest.mark.parametrize("bad", [121, -5, 1000])
def test_start_out_of_range_rejected(mode_client, bad):
    """Out-of-range minutes return 400 and launch nothing."""
    client, start_path, launched = mode_client
    res = client.post(start_path, json={'timeout_min': bad})
    assert res.status_code == 400
    assert launched['calls'] == []


@pytest.mark.parametrize("bad", ["30", 1.5, True])
def test_start_non_int_rejected(mode_client, bad):
    """Non-int minutes (string, float, bool) return 400 and launch nothing."""
    client, start_path, launched = mode_client
    res = client.post(start_path, json={'timeout_min': bad})
    assert res.status_code == 400
    assert launched['calls'] == []
