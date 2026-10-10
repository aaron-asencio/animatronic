"""Logic tests for the Awake-mode operator response-pool selection + dispatch.

Covers the runtime wiring added to ``src/animatronic.py`` for the Config-tab
"Awake response pool" (the whole-robot analogue of the Scan pool):

  - ``_pick_awake_pool_action`` draws ``(kind, camelCase_name)`` from the saved
    pool, is seed-reproducible, respects weights, and returns ``None`` when the
    pool is empty so the loop can fall back to the default ambient behaviour.
  - The camelCase -> internal translation used by ``_run_awake_pool_action``
    resolves a known routine (clearThroat -> self.clear_throat via action_map())
    and a known gesture (menacingReach -> Movements.menacing_reach via
    ``_AWAKE_POOL_GESTURE_METHODS``).
  - The empty-pool fallback: with no saved pool, ``_run_awake_loop`` uses
    ``_pick_ambient_action`` (today's behaviour) and never touches the pool
    dispatch path.

This is pure selection/translation logic — no servos are driven (the dispatch
methods are patched/inspected, never actually run on hardware). NO
collision/limit simulation is run (the operator validates motion on hardware).

SERVO_SIM=1 is set before importing src so the import stays hardware-free.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_awake_selection.py -q --maxfail=1
"""

import os
import random
import sys

os.environ["SERVO_SIM"] = "1"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import animatronic  # noqa: E402
from movements import Movements  # noqa: E402


def _new_animatronic():
    """An Animatronic instance without running __init__ (no hardware setup)."""
    return animatronic.Animatronic.__new__(animatronic.Animatronic)


# --- weighted operator-pool picker -------------------------------------------


def test_pick_awake_pool_action_empty_returns_none():
    """An empty/unset pool returns None so the loop falls back to ambient."""
    a = _new_animatronic()
    a._awake_pools = {"routine_pool": {}, "gesture_pool": {}}
    assert a._pick_awake_pool_action() is None

    # Missing attribute entirely also yields None (defensive).
    b = _new_animatronic()
    assert b._pick_awake_pool_action() is None


def test_pick_awake_pool_action_tags_kind_and_name():
    """Every pick is a (kind, name) from the pool with the right kind tag."""
    a = _new_animatronic()
    a._awake_pools = {
        "routine_pool": {"startParty": 3, "clearThroat": 2},
        "gesture_pool": {"menacingReach": 4, "wave": 1},
    }
    random.seed(7)
    picks = {a._pick_awake_pool_action() for _ in range(400)}
    # Only the saved (kind, name) tuples ever appear.
    assert picks <= {
        ("routine", "startParty"), ("routine", "clearThroat"),
        ("gesture", "menacingReach"), ("gesture", "wave"),
    }
    # Routine names tagged "routine", gesture names tagged "gesture".
    for kind, name in picks:
        if name in ("startParty", "clearThroat"):
            assert kind == "routine"
        else:
            assert kind == "gesture"


def test_pick_awake_pool_action_is_seed_reproducible():
    """Same random.seed replays an identical pick sequence."""
    a = _new_animatronic()
    a._awake_pools = {
        "routine_pool": {"startParty": 2},
        "gesture_pool": {"wave": 5, "menacingReach": 3},
    }
    random.seed(99)
    first = [a._pick_awake_pool_action() for _ in range(50)]
    random.seed(99)
    second = [a._pick_awake_pool_action() for _ in range(50)]
    assert first == second


def test_pick_awake_pool_action_respects_weight_ratio():
    """Weight 6 is drawn ~3x as often as weight 2 over many seeded draws."""
    a = _new_animatronic()
    a._awake_pools = {
        "routine_pool": {},
        "gesture_pool": {"wave": 6, "menacingReach": 2},
    }
    random.seed(2024)
    draws = [a._pick_awake_pool_action()[1] for _ in range(8000)]
    heavy = draws.count("wave")
    light = draws.count("menacingReach")
    assert light > 0
    ratio = heavy / light
    assert abs(ratio - 3.0) < 0.4


def test_pick_awake_pool_action_includes_full_list_names():
    """A HEAD gesture + a non-arm-only routine (Scan would reject) are pickable."""
    a = _new_animatronic()
    a._awake_pools = {
        "routine_pool": {"startParty": 5},   # not arm-only-safe for Scan
        "gesture_pool": {"shakeHead": 5},    # HEAD gesture (channels 0-1)
    }
    random.seed(1)
    picks = {a._pick_awake_pool_action() for _ in range(200)}
    assert ("routine", "startParty") in picks
    assert ("gesture", "shakeHead") in picks


# --- camelCase -> internal translation (dispatch) ----------------------------


def test_routine_translation_resolves_via_action_map():
    """A pool routine name resolves to the bound Animatronic method."""
    a = animatronic.Animatronic()
    amap = a.build_action_map()
    assert "clearThroat" in amap
    # The dispatch path looks the name up in action_map() exactly like this.
    assert amap["clearThroat"] == a.clear_throat


def test_gesture_translation_resolves_via_gesture_methods():
    """A pool gesture name resolves to a real Movements coroutine method."""
    gmap = animatronic.Animatronic._AWAKE_POOL_GESTURE_METHODS
    assert gmap["menacingReach"] == "menacing_reach"
    # The resolved method name is an actual Movements coroutine.
    mv = Movements("Animatronic")
    assert callable(getattr(mv, gmap["menacingReach"]))
    assert callable(getattr(mv, gmap["wave"]))


def test_gesture_map_matches_full_movement_actions():
    """Every webapp MOVEMENT_ACTIONS gesture is translatable by the pool map.

    This guards the FULL-list contract: the operator pool is seeded from the
    FULL MOVEMENT_ACTIONS, so an arbitrary selected gesture must have an entry
    in _AWAKE_POOL_GESTURE_METHODS (otherwise it would be silently skipped).
    """
    import webapp
    gmap = animatronic.Animatronic._AWAKE_POOL_GESTURE_METHODS
    missing = [n for n in webapp.MOVEMENT_ACTIONS if n not in gmap]
    assert missing == [], f"gestures missing from pool map: {missing}"
    # And every mapped method exists on Movements.
    mv = Movements("Animatronic")
    for camel, method_name in gmap.items():
        assert hasattr(mv, method_name), f"{camel} -> {method_name} not on Movements"


def test_run_awake_pool_action_skips_unknown_name(capsys):
    """An unknown/stale pool name is skipped with a warning, never raises."""
    a = _new_animatronic()
    a._run_awake_pool_action("routine", "definitely_not_a_routine")
    a._run_awake_pool_action("gesture", "definitely_not_a_gesture")
    out = capsys.readouterr().out
    assert "skipping unknown pool routine" in out
    assert "skipping unknown pool gesture" in out


# --- empty-pool fallback in the loop -----------------------------------------


def test_loop_falls_back_to_ambient_when_pool_empty(monkeypatch):
    """With no saved pool, the loop uses the default ambient pick/dispatch path.

    The loop is driven for exactly one action by making the FIRST interrupt
    check return None (run one action) and the SECOND return STOP. Both the
    sensor poll and the inter-action pause are stubbed out so no servo/sensor is
    touched. We assert the ambient path ran and the pool-dispatch path did NOT.
    """
    a = _new_animatronic()
    a._awake_pools = {"routine_pool": {}, "gesture_pool": {}}

    # One action, then stop: interrupt check returns None once, then STOP.
    checks = {"n": 0}

    def fake_check(deadline):
        checks["n"] += 1
        return None if checks["n"] == 1 else a.AWAKE_INTERRUPT_STOP

    monkeypatch.setattr(a, "_check_awake_interrupt", fake_check)
    monkeypatch.setattr(a, "_poll_nap_sensor", lambda: False)
    monkeypatch.setattr(a, "_awake_pause", lambda deadline: None)

    ambient = {"picked": 0, "ran": None}
    pool = {"ran": None}

    monkeypatch.setattr(a, "_pick_ambient_action",
                        lambda: (ambient.__setitem__("picked", ambient["picked"] + 1)
                                 or ("gesture", "_do_look_around_random")))
    monkeypatch.setattr(a, "_run_awake_action",
                        lambda kind, name: ambient.__setitem__("ran", (kind, name)))
    monkeypatch.setattr(a, "_run_awake_pool_action",
                        lambda kind, name: pool.__setitem__("ran", (kind, name)))

    reason = a._run_awake_loop(timeout_seconds=0)
    assert reason == a.AWAKE_INTERRUPT_STOP
    # Ambient path used; pool-dispatch path untouched.
    assert ambient["ran"] == ("gesture", "_do_look_around_random")
    assert pool["ran"] is None


def test_loop_uses_pool_when_configured(monkeypatch):
    """With a saved pool, the loop dispatches via the pool path, not ambient."""
    a = _new_animatronic()
    a._awake_pools = {"routine_pool": {"startParty": 5}, "gesture_pool": {}}

    checks = {"n": 0}

    def fake_check(deadline):
        checks["n"] += 1
        return None if checks["n"] == 1 else a.AWAKE_INTERRUPT_STOP

    monkeypatch.setattr(a, "_check_awake_interrupt", fake_check)
    monkeypatch.setattr(a, "_poll_nap_sensor", lambda: False)
    monkeypatch.setattr(a, "_awake_pause", lambda deadline: None)

    ambient = {"ran": None}
    pool = {"ran": None}
    monkeypatch.setattr(a, "_run_awake_action",
                        lambda kind, name: ambient.__setitem__("ran", (kind, name)))
    monkeypatch.setattr(a, "_run_awake_pool_action",
                        lambda kind, name: pool.__setitem__("ran", (kind, name)))

    random.seed(0)
    reason = a._run_awake_loop(timeout_seconds=0)
    assert reason == a.AWAKE_INTERRUPT_STOP
    # Pool path used (startParty is the only pool member); ambient untouched.
    assert pool["ran"] == ("routine", "startParty")
    assert ambient["ran"] is None
