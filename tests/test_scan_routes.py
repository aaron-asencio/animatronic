"""Flask route tests for Scan Mode (``POST /scan/<state>``) in src/webapp.py.

These exercise the real Flask routes with the test client, with the subprocess
launcher (``run_scan``) and the mic auto-stop (``_stop_mic`` / ``_mic_is_streaming``)
monkeypatched so nothing is actually spawned and no hardware is touched. The
persisted timeout uses a per-test temp config (``config_store._default_store``
pointed at a tmp file) so round-trips don't touch the real tuning.json.

Assertions:
  - ``POST /scan/start`` with a valid minutes value persists it and succeeds.
  - 0 is accepted (no timeout): it launches with 0 and persists 0.
  - A non-int and out-of-range (121) value returns 400 and launches nothing.
  - Omitting the value reads the persisted/default 60.
  - ``POST /scan/stop`` calls ``nap_signal.request_stop()``.
  - ``'scan'`` is in ``SCAN_ACTIONS`` and ``_MODE_LABELS``.
  - ``launch_scan`` attempts ``_stop_mic()`` (Scan owns the jaw/audio path).

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_routes.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
import config_store  # noqa: E402
import nap_signal  # noqa: E402


class FakeProc:
    """Minimal stand-in for subprocess.Popen (poll() reports still-running)."""

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with a temp config and a stubbed launcher/mic.

    - ``config_store._default_store`` is repointed at a tmp tuning file so the
      module-level ``load_scan_timeout`` / ``save_scan_timeout`` wrappers (used
      by the route) read/write the temp file.
    - ``run_scan`` is replaced with a recorder returning a fake process so no
      subprocess spawns.
    - the mic is reported idle by default (so ``_stop_mic`` short-circuits).
    - ``_active_proc`` is reset so the busy check passes.
    """
    cfg_path = os.path.join(str(tmp_path), "tuning.json")
    monkeypatch.setattr(config_store, "_default_store",
                        config_store.ConfigStore(config_path=cfg_path))

    launched = {"calls": []}

    def fake_run_scan(timeout_min):
        launched["calls"].append(int(timeout_min))
        return FakeProc()

    monkeypatch.setattr(webapp, "run_scan", fake_run_scan)
    # Mic idle by default; a test that cares flips this.
    monkeypatch.setattr(webapp, "_mic_is_streaming", lambda: False)
    # Reset the active-process tracker so the busy guard passes.
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None

    webapp.app.config['TESTING'] = True
    c = webapp.app.test_client()
    c._launched = launched  # expose for assertions
    yield c
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None


def test_scan_in_allowlists():
    """'scan' is a permitted action and a recognised Mode label."""
    assert 'scan' in webapp.SCAN_ACTIONS
    assert 'scan' in webapp._MODE_LABELS


def test_start_valid_minutes_persists_and_succeeds(client):
    """A valid minutes value launches scan and is persisted."""
    res = client.post('/scan/start', json={'timeout_min': 42})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    # Launched once with the persisted value.
    assert client._launched['calls'] == [42]
    # Persisted to the temp config.
    assert config_store.load_scan_timeout() == 42


def test_start_omitted_reads_persisted_default(client):
    """Omitting timeout_min uses the persisted (default 60) value."""
    # Fresh temp config -> default 60.
    res = client.post('/scan/start', json={})
    assert res.status_code == 200
    assert client._launched['calls'] == [60]
    assert config_store.load_scan_timeout() == 60


def test_start_omitted_reads_previously_persisted(client):
    """Omitting timeout_min after a save reads the saved value."""
    config_store.save_scan_timeout(99)
    res = client.post('/scan/start', json={})
    assert res.status_code == 200
    assert client._launched['calls'] == [99]


@pytest.mark.parametrize("bad", [121, -5, 1000])
def test_start_out_of_range_rejected(client, bad):
    """Out-of-range minutes return 400 and launch nothing."""
    res = client.post('/scan/start', json={'timeout_min': bad})
    assert res.status_code == 400
    assert client._launched['calls'] == []


def test_start_zero_accepted_no_timeout(client):
    """0 (no timeout) is accepted: it launches with 0 and persists 0."""
    res = client.post('/scan/start', json={'timeout_min': 0})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    assert client._launched['calls'] == [0]
    assert config_store.load_scan_timeout() == 0


@pytest.mark.parametrize("bad", ["60", 1.5, True])
def test_start_non_int_rejected(client, bad):
    """Non-int minutes (string, float, bool) return 400 and launch nothing."""
    res = client.post('/scan/start', json={'timeout_min': bad})
    assert res.status_code == 400
    assert client._launched['calls'] == []


def test_stop_requests_stop(client, monkeypatch):
    """POST /scan/stop calls nap_signal.request_stop()."""
    called = {"n": 0}
    monkeypatch.setattr(nap_signal, "request_stop", lambda: called.__setitem__("n", called["n"] + 1))
    res = client.post('/scan/stop')
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    assert called["n"] == 1


def test_unknown_state_rejected(client):
    """An unknown state returns 400 and launches nothing."""
    res = client.post('/scan/whoops')
    assert res.status_code == 400
    assert client._launched['calls'] == []


def test_launch_scan_attempts_stop_mic(client, monkeypatch):
    """launch_scan calls _stop_mic() when the mic is streaming (owns jaw/audio)."""
    stopped = {"n": 0}
    monkeypatch.setattr(webapp, "_mic_is_streaming", lambda: True)

    def fake_stop_mic():
        stopped["n"] += 1
        return True  # report the mic stopped so launch proceeds

    monkeypatch.setattr(webapp, "_stop_mic", fake_stop_mic)
    res = client.post('/scan/start', json={'timeout_min': 30})
    assert res.status_code == 200
    assert stopped["n"] == 1
    assert client._launched['calls'] == [30]
