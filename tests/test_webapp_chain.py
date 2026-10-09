"""Unit tests for the Sleep/Awake mode auto-chaining plumbing in src/webapp.py.

These exercise the chain watcher decision logic and the surrounding route /
launcher plumbing with ``_launch_mode``, ``_await_lock_free`` and the mic
helpers monkeypatched so nothing spawns and no hardware is touched. A per-test
temp config (``config_store._default_store`` at a tmp file) keeps round-trips off
the real tuning.json.

Covers:
  - ``_chain_watch`` chains on EXIT_TIMEOUT / EXIT_SENSOR
  - does NOT chain on EXIT_STOP, crash (1), or busy (3)
  - no-ops when generation moved (stop/preempt) or when _active_proc replaced
  - decrements remaining per transition and stops launching at 0
  - the correct OTHER mode is launched (napping<->awake only)
  - operator Start on an already-running mode (the _launch_mode no-op) does NOT
    re-seed remaining
  - _terminate_if_still kills only the exact proc, no-op on replacement
  - _launch_mode forwards reset_chain only to the chainable launchers
  - /chain/config validation + persistence; /status additive chain object

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:
    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_webapp_chain.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
import config_store  # noqa: E402
import mode_exit  # noqa: E402


class ExitedProc:
    """A subprocess stand-in that has already exited with a given code."""

    def __init__(self, returncode=mode_exit.EXIT_TIMEOUT):
        self.returncode = returncode
        self._terminated = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._terminated = True


class RunningProc:
    """A subprocess stand-in that is still running (poll() -> None)."""

    pid = 12345

    def __init__(self):
        self.returncode = None
        self._terminated = False

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated = True
        self.returncode = -15

    def kill(self):
        self._terminated = True
        self.returncode = -9


@pytest.fixture(autouse=True)
def _temp_config(tmp_path, monkeypatch):
    """Repoint the config store and reset chain/active state per test."""
    cfg_path = os.path.join(str(tmp_path), "tuning.json")
    monkeypatch.setattr(config_store, "_default_store",
                        config_store.ConfigStore(config_path=cfg_path))
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None
    webapp._chain.update({'remaining': 0, 'generation': 0,
                          'nap_timeout_min': 0, 'awake_timeout_min': 0})
    yield
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None


@pytest.fixture
def launch_recorder(monkeypatch):
    """Replace _launch_mode and _await_lock_free so Phase B/C don't block/spawn."""
    calls = {"launch": []}

    def fake_launch_mode(launch_fn, *args, reset_chain=False):
        calls["launch"].append((launch_fn, args, reset_chain))
        return True, "launched"

    monkeypatch.setattr(webapp, "_launch_mode", fake_launch_mode)
    monkeypatch.setattr(webapp, "_await_lock_free", lambda proc, timeout_s=15: True)
    return calls


# ── _chain_watch decision logic ──────────────────────────────────────────────

@pytest.mark.parametrize("code", [mode_exit.EXIT_TIMEOUT, mode_exit.EXIT_SENSOR])
def test_watch_chains_on_natural_end(launch_recorder, code):
    """A natural end (timeout/sensor) with budget launches the OTHER mode."""
    proc = ExitedProc(code)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'napping'
    webapp._chain['remaining'] = 3
    webapp._chain['generation'] = 5

    webapp._chain_watch(proc, 'napping', generation=5)

    assert len(launch_recorder["launch"]) == 1
    launch_fn, args, reset_chain = launch_recorder["launch"][0]
    assert launch_fn is webapp.launch_awake      # napping -> awake
    assert reset_chain is False                   # chained hop continues chain
    assert webapp._chain['remaining'] == 2        # decremented before launch


@pytest.mark.parametrize("code", [mode_exit.EXIT_STOP, 1, 3, 0])
def test_watch_does_not_chain_on_non_chainable(launch_recorder, code):
    """Stop (12), crash (1), busy (3), clean (0) all end the chain (fail-safe)."""
    proc = ExitedProc(code)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'awake'
    webapp._chain['remaining'] = 3
    webapp._chain['generation'] = 2

    webapp._chain_watch(proc, 'awake', generation=2)

    assert launch_recorder["launch"] == []
    assert webapp._chain['remaining'] == 3        # not decremented
    assert webapp._active_proc['proc'] is None    # slot cleared


def test_watch_no_op_when_generation_moved(launch_recorder):
    """A stop/preempt that bumped generation invalidates the chain."""
    proc = ExitedProc(mode_exit.EXIT_TIMEOUT)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'napping'
    webapp._chain['remaining'] = 3
    webapp._chain['generation'] = 9              # moved past the watcher's 7

    webapp._chain_watch(proc, 'napping', generation=7)

    assert launch_recorder["launch"] == []
    assert webapp._chain['remaining'] == 3


def test_watch_no_op_when_active_proc_replaced(launch_recorder):
    """If the slot no longer points at our proc, the watcher drops out."""
    proc = ExitedProc(mode_exit.EXIT_TIMEOUT)
    other = RunningProc()
    webapp._active_proc['proc'] = other          # replaced by something else
    webapp._active_proc['label'] = 'awake'
    webapp._chain['remaining'] = 3
    webapp._chain['generation'] = 4

    webapp._chain_watch(proc, 'napping', generation=4)

    assert launch_recorder["launch"] == []
    assert webapp._chain['remaining'] == 3
    # The replacing proc's slot is untouched.
    assert webapp._active_proc['proc'] is other


def test_watch_no_launch_when_budget_exhausted(launch_recorder):
    """remaining == 0 ends the chain with no transition."""
    proc = ExitedProc(mode_exit.EXIT_SENSOR)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'napping'
    webapp._chain['remaining'] = 0
    webapp._chain['generation'] = 1

    webapp._chain_watch(proc, 'napping', generation=1)

    assert launch_recorder["launch"] == []
    assert webapp._chain['remaining'] == 0
    assert webapp._active_proc['proc'] is None


def test_watch_awake_chains_to_napping(launch_recorder):
    """awake -> napping is the OTHER direction."""
    proc = ExitedProc(mode_exit.EXIT_TIMEOUT)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'awake'
    webapp._chain['remaining'] = 1
    webapp._chain['generation'] = 0

    webapp._chain_watch(proc, 'awake', generation=0)

    assert len(launch_recorder["launch"]) == 1
    launch_fn, _args, _reset = launch_recorder["launch"][0]
    assert launch_fn is webapp.launch_napping


def test_watch_abandons_when_lock_stuck(monkeypatch):
    """If _await_lock_free times out, the chain is abandoned (no launch)."""
    calls = []
    monkeypatch.setattr(webapp, "_launch_mode",
                        lambda *a, **k: calls.append((a, k)) or (True, "x"))
    monkeypatch.setattr(webapp, "_await_lock_free", lambda proc, timeout_s=15: False)

    proc = ExitedProc(mode_exit.EXIT_TIMEOUT)
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'napping'
    webapp._chain['remaining'] = 2
    webapp._chain['generation'] = 0

    webapp._chain_watch(proc, 'napping', generation=0)

    assert calls == []                            # never reached Phase C
    # remaining was decremented in Phase A before the lock-wait failed.
    assert webapp._chain['remaining'] == 1


# ── _terminate_if_still identity semantics ───────────────────────────────────

def test_terminate_if_still_kills_exact_proc():
    """_terminate_if_still terminates and clears only the matching proc."""
    proc = RunningProc()
    webapp._active_proc['proc'] = proc
    webapp._active_proc['label'] = 'awake'

    assert webapp._terminate_if_still(proc) is True
    assert proc._terminated is True
    assert webapp._active_proc['proc'] is None


def test_terminate_if_still_no_op_on_replacement():
    """If the slot holds a different object, it is a no-op (operator untouched)."""
    stale = RunningProc()
    current = RunningProc()
    webapp._active_proc['proc'] = current
    webapp._active_proc['label'] = 'napping'

    assert webapp._terminate_if_still(stale) is False
    assert current._terminated is False           # operator's action survives
    assert webapp._active_proc['proc'] is current


# ── _launch_mode reset_chain forwarding (Finding B) ──────────────────────────

def test_launch_mode_forwards_reset_chain_only_to_chainable(monkeypatch):
    """reset_chain reaches launch_napping/launch_awake but not the other three."""
    seen = {}

    def fake_napping(timeout_min, reset_chain=False):
        seen['napping'] = reset_chain
        return True, "ok"

    def fake_tracking(*args):
        seen['tracking'] = args
        return True, "ok"

    monkeypatch.setattr(webapp, "launch_napping", fake_napping)
    monkeypatch.setattr(webapp, "launch_awake", fake_napping)
    monkeypatch.setattr(webapp, "launch_tracking", fake_tracking)
    # Rebuild the launcher->label map so the identity checks match the fakes.
    monkeypatch.setitem(webapp._LAUNCHER_LABEL, fake_napping, 'napping')
    monkeypatch.setitem(webapp._LAUNCHER_LABEL, fake_tracking, 'tracking')
    # Nothing running so no preemption path is taken.
    webapp._active_proc['proc'] = None
    webapp._active_proc['label'] = None

    ok, _ = webapp._launch_mode(fake_napping, 5, reset_chain=True)
    assert ok and seen['napping'] is True

    # tracking takes no reset_chain kwarg; forwarding it would TypeError.
    ok, _ = webapp._launch_mode(fake_tracking, None, reset_chain=True)
    assert ok and seen['tracking'] == (None,)


# ── Already-running no-op does NOT re-seed the budget (Finding 5/D) ──────────

def test_launch_mode_already_running_does_not_reseed(monkeypatch):
    """A redundant Start on the running mode is a no-op and leaves remaining alone."""
    spawned = {"count": 0}

    def fake_napping(timeout_min, reset_chain=False):
        spawned["count"] += 1
        return True, "spawned"

    monkeypatch.setattr(webapp, "launch_napping", fake_napping)
    monkeypatch.setitem(webapp._LAUNCHER_LABEL, fake_napping, 'napping')

    # napping already running; an in-flight chain has remaining == 2.
    running = RunningProc()
    webapp._active_proc['proc'] = running
    webapp._active_proc['label'] = 'napping'
    webapp._chain['remaining'] = 2

    ok, msg = webapp._launch_mode(fake_napping, 5, reset_chain=True)

    assert ok and 'already running' in msg
    assert spawned["count"] == 0                  # no real spawn
    assert webapp._chain['remaining'] == 2        # budget NOT re-seeded


# ── /chain/config route ──────────────────────────────────────────────────────

@pytest.fixture
def client():
    webapp.app.config['TESTING'] = True
    return webapp.app.test_client()


def test_chain_config_persists_valid(client):
    r = client.post('/chain/config', json={'max_transitions': 6})
    assert r.status_code == 200
    assert r.get_json()['max_transitions'] == 6
    assert config_store.load_chain_max_transitions() == 6


@pytest.mark.parametrize("body", [
    {'max_transitions': True},        # bool rejected
    {'max_transitions': 'x'},         # non-int rejected
    {'max_transitions': 21},          # out of range
    {'max_transitions': -1},          # out of range
    {},                               # missing -> None, non-int
])
def test_chain_config_rejects_bad(client, body):
    r = client.post('/chain/config', json=body)
    assert r.status_code == 400


# ── /status additive chain object ────────────────────────────────────────────

def test_status_includes_chain(client, monkeypatch):
    """GET /status adds an additive chain object without disturbing other keys."""
    monkeypatch.setattr(webapp, "_proxy", lambda method, path, json_body=None: ({}, 200))
    webapp._chain['remaining'] = 2
    r = client.get('/status')
    assert r.status_code == 200
    data = r.get_json()
    assert data['chain'] == {'remaining': 2, 'active': True}
    # Existing keys are untouched.
    assert 'last_action' in data and 'servos_busy' in data and 'mic' in data


def test_status_chain_inactive_when_zero(client, monkeypatch):
    monkeypatch.setattr(webapp, "_proxy", lambda method, path, json_body=None: ({}, 200))
    webapp._chain['remaining'] = 0
    data = client.get('/status').get_json()
    assert data['chain'] == {'remaining': 0, 'active': False}
