"""Flask route tests for the Scan response pool (``/scan/pool``) in src/webapp.py.

These exercise the real Flask routes with the test client. The persisted pool
uses a per-test temp config (``config_store._default_store`` pointed at a tmp
file) so round-trips never touch the real tuning.json.

Focus is the SECURITY BOUNDARY on POST /scan/pool: every submitted routine name
is validated against ``ROUTINE_ACTIONS`` and every gesture name against
``MOVEMENT_ACTIONS`` before anything is persisted. If ANY name is unknown the
whole request is rejected with 400 and nothing is written. The handler runs no
servo command — it only reads/writes config.

Assertions:
  - ``GET /scan/pool`` returns the persisted {routine_pool, gesture_pool}.
  - ``POST /scan/pool`` with valid names persists them, returns success, and
    preserves a pre-written ``scan.timeout_min``.
  - ``POST /scan/pool`` with an unknown routine name -> 400, persists nothing.
  - ``POST /scan/pool`` with an unknown gesture name -> 400, persists nothing.
  - A non-dict pool -> 400.
  - str/out-of-range weights are coerced/clamped into [1, 10]; a weight < 1
    excludes the name from the stored pool.

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_scan_pool_routes.py -q --maxfail=1
"""

import os
import sys

os.environ["SERVO_SIM"] = "1"

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import webapp  # noqa: E402
import config_store  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client backed by a temp tuning file.

    ``config_store._default_store`` is repointed at a tmp tuning file so the
    module-level ``load_scan_pools`` / ``save_scan_pools`` wrappers used by the
    routes read/write the temp file instead of the real tuning.json.
    """
    cfg_path = os.path.join(str(tmp_path), "tuning.json")
    monkeypatch.setattr(config_store, "_default_store",
                        config_store.ConfigStore(config_path=cfg_path))
    webapp.app.config['TESTING'] = True
    c = webapp.app.test_client()
    c._cfg_path = cfg_path
    yield c


def _pick(action_set):
    """Return one known-good name from an allowlist (sorted for determinism)."""
    return sorted(action_set)[0]


def test_get_returns_persisted_pools(client):
    """GET /scan/pool reflects what was persisted via config_store."""
    routine = _pick(webapp.ROUTINE_ACTIONS)
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    config_store.save_scan_pools({routine: 7}, {gesture: 3})

    res = client.get('/scan/pool')
    assert res.status_code == 200
    data = res.get_json()
    assert data['routine_pool'] == {routine: 7}
    assert data['gesture_pool'] == {gesture: 3}


def test_get_empty_when_nothing_saved(client):
    """A fresh config yields empty pools (never 500)."""
    res = client.get('/scan/pool')
    assert res.status_code == 200
    assert res.get_json() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_valid_persists_and_preserves_timeout(client):
    """Valid allowlisted names persist and scan.timeout_min survives the save."""
    # Pre-write a timeout so we can prove the pool save merges (not replaces).
    config_store.save_scan_timeout(45)

    routine = _pick(webapp.ROUTINE_ACTIONS)
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    res = client.post('/scan/pool', json={
        'routine_pool': {routine: 8},
        'gesture_pool': {gesture: 2},
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert data['routine_pool'] == {routine: 8}
    assert data['gesture_pool'] == {gesture: 2}

    # Persisted and the pre-written timeout is preserved.
    assert config_store.load_scan_pools() == {
        'routine_pool': {routine: 8},
        'gesture_pool': {gesture: 2},
    }
    assert config_store.load_scan_timeout() == 45


def test_post_unknown_routine_rejected_and_persists_nothing(client):
    """An unknown routine name rejects the whole request with 400."""
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    res = client.post('/scan/pool', json={
        'routine_pool': {'definitely_not_a_routine': 5},
        'gesture_pool': {gesture: 5},
    })
    assert res.status_code == 400
    # Nothing persisted — the valid gesture must not sneak through either.
    assert config_store.load_scan_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_unknown_gesture_rejected_and_persists_nothing(client):
    """An unknown gesture name rejects the whole request with 400."""
    routine = _pick(webapp.ROUTINE_ACTIONS)
    res = client.post('/scan/pool', json={
        'routine_pool': {routine: 5},
        'gesture_pool': {'definitely_not_a_gesture': 5},
    })
    assert res.status_code == 400
    assert config_store.load_scan_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_non_dict_pool_rejected(client):
    """A non-object pool returns 400."""
    res = client.post('/scan/pool', json={
        'routine_pool': ['not', 'a', 'dict'],
        'gesture_pool': {},
    })
    assert res.status_code == 400
    assert config_store.load_scan_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_empty_pools_ok(client):
    """Empty/omitted pools are valid and persist as empty."""
    res = client.post('/scan/pool', json={})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    assert config_store.load_scan_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_weights_coerced_and_clamped(client):
    """String weights coerce; out-of-range clamps to [1,10]; <1 excludes."""
    names = sorted(webapp.ROUTINE_ACTIONS)
    assert len(names) >= 3, "need >=3 routine actions for this test"
    high, low, excluded = names[0], names[1], names[2]
    res = client.post('/scan/pool', json={
        'routine_pool': {
            high: "50",   # string + above max -> clamp to 10
            low: 0,       # below min -> excluded entirely
            excluded: -4,  # negative -> excluded entirely
        },
        'gesture_pool': {},
    })
    assert res.status_code == 200
    stored = config_store.load_scan_pools()['routine_pool']
    assert stored == {high: 10}
    assert low not in stored
    assert excluded not in stored
