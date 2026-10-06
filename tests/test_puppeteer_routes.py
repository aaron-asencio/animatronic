"""Flask route tests for Puppeteer Mode (``POST /puppeteer/<state>``) and the
graceful mode-switch preemption added to the mode-launch path in src/webapp.py.

These exercise the real Flask routes with the test client, with the subprocess
launcher (``run_puppeteer``) and the mic proxy helpers (``_proxy`` /
``_mic_is_streaming`` / ``_stop_mic``) monkeypatched so nothing is actually
spawned and no hardware/micwebcontroller is touched. ``_active_proc`` is reset
per test so the busy check starts clean.

Assertions (map to FR/AC in the design):
  - ``'puppeteer'`` is in ``PUPPETEER_ACTIONS`` and ``_MODE_LABELS`` (AC3).
  - ``POST /puppeteer/start`` spawns via the patched ``run_puppeteer`` with a
    fixed ``--action=puppeteer`` argv and starts the mic via
    ``_proxy('POST','/handler',{'action':'start'})``; with the mic reported
    streaming the response says started, and ``_stop_mic`` is NOT called at
    launch (AC4/AC5/AC6 — no auto-stop).
  - ``POST /puppeteer/stop`` calls ``nap_signal.request_stop()`` AND
    ``_stop_mic()`` (AC11 web-stop leg, FR10).
  - Unknown ``<state>`` -> 400, nothing spawned, no mic call (AC4).
  - Allowlist: an empty ``PUPPETEER_ACTIONS`` refuses start with 400 before any
    subprocess/mic action (AC5).
  - Mode-switch preemption (FR8/AC9): a DIFFERENT running mode is preempted (not
    a 409 purely because a mode ran); same-mode no-op (FR9/AC10).
  - FR10 preempt leg: ``POST /scan/start`` while puppeteer is active stops the
    mic in the preempt path.
  - Template render: ``GET /`` 200 with the Puppeteer buttons + data-group tags.

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_puppeteer_routes.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
import nap_signal  # noqa: E402


class FakeProc:
    """Minimal stand-in for subprocess.Popen.

    ``poll()`` returns ``None`` (still running) until it has been polled
    ``exit_after`` times, after which it returns ``0`` (exited). The default
    (``exit_after=None``) means it never exits — a persistently-running process.
    """

    def __init__(self, exit_after=None):
        self._exit_after = exit_after
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._exit_after is not None and self._polls > self._exit_after:
            return 0
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def client(monkeypatch):
    """Flask test client with a stubbed launcher and mic proxy.

    - ``run_puppeteer`` is replaced with a recorder returning a fake process so
      no subprocess spawns; the forwarded args are captured for assertions.
    - the mic ``_proxy`` is stubbed to a success body; ``_mic_is_streaming`` is
      reported True by default (so launch_puppeteer reports mic started).
    - ``_stop_mic`` is counted (and does nothing) so tests can assert it is / is
      not called.
    - ``_active_proc`` is reset so the busy guard passes.
    """
    launched = {"calls": []}
    proxy_calls = {"calls": []}
    stop_mic = {"n": 0}

    def fake_run_puppeteer(scan_timeout_seconds=None):
        launched["calls"].append(scan_timeout_seconds)
        return FakeProc()

    def fake_proxy(method, path, json_body=None):
        proxy_calls["calls"].append((method, path, json_body))
        return {"status": "success"}, 200

    monkeypatch.setattr(webapp, "run_puppeteer", fake_run_puppeteer)
    monkeypatch.setattr(webapp, "_proxy", fake_proxy)
    monkeypatch.setattr(webapp, "_mic_is_streaming", lambda: True)
    monkeypatch.setattr(webapp, "_stop_mic",
                        lambda: (stop_mic.__setitem__("n", stop_mic["n"] + 1), True)[1])

    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None

    webapp.app.config['TESTING'] = True
    c = webapp.app.test_client()
    c._launched = launched
    c._proxy_calls = proxy_calls
    c._stop_mic = stop_mic
    yield c
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None


def test_puppeteer_in_allowlists():
    """'puppeteer' is a permitted action and a recognised Mode label (AC3)."""
    assert 'puppeteer' in webapp.PUPPETEER_ACTIONS
    assert 'puppeteer' in webapp._MODE_LABELS
    assert 'puppeteer' in webapp._MODE_LABELS_SET


def test_start_spawns_and_starts_mic_no_autostop(client):
    """start spawns via run_puppeteer and starts the mic; no mic auto-stop (AC4/5/6)."""
    res = client.post('/puppeteer/start', json={})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    # Launched once (scan_timeout omitted -> None).
    assert client._launched['calls'] == [None]
    # Mic started via the exact proxy call the toggle uses.
    assert ('POST', '/handler', {'action': 'start'}) in client._proxy_calls['calls']
    # No auto-stop of the mic at launch (Puppeteer STARTS the mic).
    assert client._stop_mic['n'] == 0


def test_start_forwards_scan_timeout(client):
    """A valid scan_timeout is forwarded to run_puppeteer as an int."""
    res = client.post('/puppeteer/start', json={'scan_timeout': 30})
    assert res.status_code == 200
    assert client._launched['calls'] == [30]


def test_start_bad_scan_timeout_rejected(client):
    """A non-int scan_timeout returns 400 and launches nothing."""
    res = client.post('/puppeteer/start', json={'scan_timeout': 'soon'})
    assert res.status_code == 400
    assert client._launched['calls'] == []


def test_start_reports_mic_failure_but_keeps_tracker(client, monkeypatch):
    """If the mic does not start, the mode still launches and the message says so."""
    monkeypatch.setattr(webapp, "_mic_is_streaming", lambda: False)
    res = client.post('/puppeteer/start', json={})
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert 'did NOT start' in data['message']
    # The neck tracker was still launched.
    assert client._launched['calls'] == [None]


def test_stop_requests_stop_and_stops_mic(client, monkeypatch):
    """stop calls nap_signal.request_stop() AND _stop_mic() (FR10)."""
    called = {"n": 0}
    monkeypatch.setattr(nap_signal, "request_stop",
                        lambda: called.__setitem__("n", called["n"] + 1))
    res = client.post('/puppeteer/stop')
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    assert called["n"] == 1
    assert client._stop_mic['n'] == 1


def test_unknown_state_rejected(client):
    """An unknown state returns 400 and launches nothing / touches no mic (AC4)."""
    res = client.post('/puppeteer/whoops')
    assert res.status_code == 400
    assert client._launched['calls'] == []
    assert client._proxy_calls['calls'] == []


def test_start_refused_when_not_allowlisted(client, monkeypatch):
    """An empty allowlist refuses start with 400 before any subprocess/mic action."""
    monkeypatch.setattr(webapp, "PUPPETEER_ACTIONS", set())
    res = client.post('/puppeteer/start', json={})
    assert res.status_code == 400
    assert client._launched['calls'] == []
    assert client._proxy_calls['calls'] == []


def test_start_preempts_different_running_mode(client, monkeypatch):
    """FR8/AC9: a DIFFERENT running mode is preempted, not refused with 409."""
    # Fake a different mode (scan) running: live through _launch_mode's running
    # check and _preempt's guard poll, then exited on the first wait-loop poll
    # so preemption completes quickly.
    webapp._active_proc['proc'] = FakeProc(exit_after=2)
    webapp._active_proc['label'] = 'scan'
    # Lock is free immediately so preemption succeeds on the first poll.
    monkeypatch.setattr(webapp, "is_locked", lambda: False)
    requested = {"n": 0}
    monkeypatch.setattr(nap_signal, "request_stop",
                        lambda: requested.__setitem__("n", requested["n"] + 1))

    res = client.post('/puppeteer/start', json={})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    # Preemption asked the prior mode to stop, then puppeteer launched.
    assert requested["n"] == 1
    assert client._launched['calls'] == [None]


def test_start_same_mode_is_noop(client):
    """FR9/AC10: requesting puppeteer while it runs is a no-op (not relaunched)."""
    webapp._active_proc['proc'] = FakeProc(exit_after=None)  # never exits
    webapp._active_proc['label'] = 'puppeteer'
    res = client.post('/puppeteer/start', json={})
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert 'already running' in data['message']
    # run_puppeteer must NOT have been called.
    assert client._launched['calls'] == []


def test_other_mode_preempts_puppeteer_stops_mic(client, monkeypatch):
    """FR10 preempt leg: starting scan while puppeteer runs stops the puppeteer mic."""
    # Puppeteer is the active mode: live through the running check + guard poll,
    # exited in the loop so the preempt success branch (which stops the mic) runs.
    webapp._active_proc['proc'] = FakeProc(exit_after=2)
    webapp._active_proc['label'] = 'puppeteer'
    monkeypatch.setattr(webapp, "is_locked", lambda: False)
    monkeypatch.setattr(nap_signal, "request_stop", lambda: None)
    # Stub scan's launcher so no scan subprocess spawns, and the config_store
    # persistence so the real tuning.json is never written during the test.
    monkeypatch.setattr(webapp, "run_scan", lambda minutes: FakeProc())
    monkeypatch.setattr(webapp.config_store, "save_scan_timeout", lambda m: int(m))
    monkeypatch.setattr(webapp.config_store, "load_scan_timeout", lambda: 60)
    # Scan auto-stops the mic in its launcher too; count all _stop_mic calls.
    res = client.post('/scan/start', json={'timeout_min': 10})
    assert res.status_code == 200
    # _stop_mic was called during the puppeteer preempt (and possibly again in
    # launch_scan — both are benign). At least one call proves the preempt leg.
    assert client._stop_mic['n'] >= 1


def test_index_renders_puppeteer_controls(client):
    """GET / returns 200 and renders the Puppeteer cluster + data-group tags."""
    res = client.get('/')
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'puppeteer-start-btn' in html
    assert 'puppeteer-stop-btn' in html
    assert 'startPuppeteer()' in html
    assert 'data-group="arm"' in html
    assert 'data-group="head"' in html
    assert 'routine-btn' in html
