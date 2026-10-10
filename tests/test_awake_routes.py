"""Flask route tests for the Awake response pool (``/awake/pool``) in webapp.py.

These mirror ``tests/test_scan_pool_routes.py`` but exercise the Awake pool,
whose one deliberate difference from Scan is FULL-list seeding: the Awake pool
validates submitted names against the FULL ``ROUTINE_ACTIONS`` /
``MOVEMENT_ACTIONS`` allowlists (because Awake is a whole-robot mode), NOT the
arm-only-safe Scan subset. So this file specifically proves that a HEAD gesture
and a non-arm-only routine that Scan would reject are ACCEPTED here.

The persisted pool uses a per-test temp config (``config_store._default_store``
pointed at a tmp file) so round-trips never touch the real tuning.json. The
handler runs no servo command — it only reads/writes config.

Assertions:
  - ``GET /awake/pool`` returns two empty pools on a fresh config.
  - ``POST /awake/pool`` with valid FULL-list names persists + round-trips, and
    accepts a HEAD gesture + a non-arm-only routine (FULL-list seeding proof).
  - ``POST /awake/pool`` with an unknown routine or gesture name -> 400, nothing
    persisted.
  - A non-dict pool -> 400.
  - str/out-of-range weights are coerced/clamped into [1, 10]; <1 excludes.
  - An index render (``GET /``) contains the #awakepool block, the FULL action
    names, the saveAwakePool JS, AND leaves the Scan pool (#scanpool,
    /scan/pool) untouched.

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_awake_routes.py -q --maxfail=1
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
    module-level ``load_awake_pools`` / ``save_awake_pools`` wrappers used by the
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


def test_get_empty_when_nothing_saved(client):
    """A fresh config yields empty pools (never 500)."""
    res = client.get('/awake/pool')
    assert res.status_code == 200
    assert res.get_json() == {'routine_pool': {}, 'gesture_pool': {}}


def test_get_returns_persisted_pools(client):
    """GET /awake/pool reflects what was persisted via config_store."""
    routine = _pick(webapp.ROUTINE_ACTIONS)
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    config_store.save_awake_pools({routine: 7}, {gesture: 3})

    res = client.get('/awake/pool')
    assert res.status_code == 200
    data = res.get_json()
    assert data['routine_pool'] == {routine: 7}
    assert data['gesture_pool'] == {gesture: 3}


def test_post_valid_persists_and_preserves_timeout(client):
    """Valid FULL-list names persist and awake.timeout_min survives the save."""
    config_store.save_awake_timeout(30)

    routine = _pick(webapp.ROUTINE_ACTIONS)
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    res = client.post('/awake/pool', json={
        'routine_pool': {routine: 8},
        'gesture_pool': {gesture: 2},
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data['status'] == 'success'
    assert data['routine_pool'] == {routine: 8}
    assert data['gesture_pool'] == {gesture: 2}

    assert config_store.load_awake_pools() == {
        'routine_pool': {routine: 8},
        'gesture_pool': {gesture: 2},
    }
    assert config_store.load_awake_timeout() == 30


def test_post_accepts_full_list_names_scan_would_reject(client):
    """FULL-list seeding: a HEAD gesture + non-arm-only routine are accepted.

    'startParty' is a routine not in the arm-only-safe Scan set, and
    'lookAroundRandom'/'shakeHead' are HEAD gestures (channels 0-1) excluded
    from the Scan pool. The Awake pool uses the FULL allowlists so these persist.
    """
    # Guard the premise: these names ARE in the FULL allowlists.
    assert 'startParty' in webapp.ROUTINE_ACTIONS
    head_gesture = 'shakeHead' if 'shakeHead' in webapp.MOVEMENT_ACTIONS \
        else 'lookAroundRandom'
    assert head_gesture in webapp.MOVEMENT_ACTIONS

    res = client.post('/awake/pool', json={
        'routine_pool': {'startParty': 6},
        'gesture_pool': {head_gesture: 4},
    })
    assert res.status_code == 200
    assert config_store.load_awake_pools() == {
        'routine_pool': {'startParty': 6},
        'gesture_pool': {head_gesture: 4},
    }


def test_post_unknown_routine_rejected_and_persists_nothing(client):
    """An unknown routine name rejects the whole request with 400."""
    gesture = _pick(webapp.MOVEMENT_ACTIONS)
    res = client.post('/awake/pool', json={
        'routine_pool': {'definitely_not_a_routine': 5},
        'gesture_pool': {gesture: 5},
    })
    assert res.status_code == 400
    assert config_store.load_awake_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_unknown_gesture_rejected_and_persists_nothing(client):
    """An unknown gesture name rejects the whole request with 400."""
    routine = _pick(webapp.ROUTINE_ACTIONS)
    res = client.post('/awake/pool', json={
        'routine_pool': {routine: 5},
        'gesture_pool': {'definitely_not_a_gesture': 5},
    })
    assert res.status_code == 400
    assert config_store.load_awake_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_non_dict_pool_rejected(client):
    """A non-object pool returns 400."""
    res = client.post('/awake/pool', json={
        'routine_pool': ['not', 'a', 'dict'],
        'gesture_pool': {},
    })
    assert res.status_code == 400
    assert config_store.load_awake_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_empty_pools_ok(client):
    """Empty/omitted pools are valid and persist as empty."""
    res = client.post('/awake/pool', json={})
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    assert config_store.load_awake_pools() == {'routine_pool': {}, 'gesture_pool': {}}


def test_post_weights_coerced_and_clamped(client):
    """String weights coerce; out-of-range clamps to [1,10]; <1 excludes."""
    names = sorted(webapp.ROUTINE_ACTIONS)
    assert len(names) >= 3, "need >=3 routine actions for this test"
    high, low, excluded = names[0], names[1], names[2]
    res = client.post('/awake/pool', json={
        'routine_pool': {
            high: "50",   # string + above max -> clamp to 10
            low: 0,       # below min -> excluded entirely
            excluded: -4,  # negative -> excluded entirely
        },
        'gesture_pool': {},
    })
    assert res.status_code == 200
    stored = config_store.load_awake_pools()['routine_pool']
    assert stored == {high: 10}
    assert low not in stored
    assert excluded not in stored


def test_index_renders_awake_pool_with_full_lists(client):
    """GET / renders the #awakepool block, FULL action names, and saveAwakePool.

    Also confirms the Scan pool (#scanpool, /scan/pool) is still present and
    untouched — the Awake pool is additive, not a replacement.
    """
    res = client.get('/')
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Awake pool block + its save button/JS present.
    assert 'id="awakepool"' in html
    assert 'Awake response pool' in html
    assert 'saveAwakePool' in html
    assert 'awakePoolToggle' in html
    assert "'/awake/pool'" in html

    # FULL-list seeding: a HEAD gesture + a non-arm-only routine appear as
    # awakepool rows (ids are the camelCase action name).
    assert 'id="awakepool-routine-cb-startParty"' in html
    head_gesture = 'shakeHead' if 'shakeHead' in webapp.MOVEMENT_ACTIONS \
        else 'lookAroundRandom'
    assert f'id="awakepool-gesture-cb-{head_gesture}"' in html

    # Scan pool untouched (still rendered, still targets /scan/pool).
    assert 'id="scanpool"' in html
    assert 'saveScanPool' in html
    assert "'/scan/pool'" in html
