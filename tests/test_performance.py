"""Property tests for the Performance_Framework loop-until-audio coordination.

Covers the Loop_Until_Audio behaviour of ``src/performance.py``'s
``PerformanceRunner._run_movement`` (the single place the loop guard lives):

  - Property 6: Loop bodies repeat for the audio duration and are never cut off.
    A movement flagged ``loop_for_audio`` must repeat its ``loop_body`` while
    playback is active, check ``Playback_Active`` ONLY between whole iterations
    (so a started iteration always runs to completion, never cut off mid-move),
    and start NO new iteration once playback becomes inactive -- proceeding to
    its return phase instead (Requirements 5.3, 7.1, 7.2, 7.3).

The test drives the real ``_run_movement`` coroutine (the actual coordination
logic) with a fake ``PlaybackController`` and fake phase callables that record
their invocation order and count, so the property genuinely exercises the loop
guard without any real audio or servo hardware. The runner's async methods are
driven with ``asyncio.run(...)`` at the top of the stack, per project
convention. Debug output uses ``print()``.

Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_performance.py -q --maxfail=1 -k performance
"""

import asyncio
import os
import random
import sys

# Hardware-free servo path: set before importing anything from src that may
# reach the trunkcontroller/hardware stack.
os.environ["SERVO_SIM"] = "1"

from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in tests/test_collision.py).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from performance import (  # noqa: E402
    MovementSpec,
    PerformanceRunner,
)


class FakePlayback:
    """Fake ``PlaybackController`` whose activity flips false after N checks.

    Models the two state predicates the loop guard consults. ``has_started()``
    returns ``True`` immediately (audio started at time zero, i.e. no gate), so
    the guard's "has not started yet" clause never keeps the loop alive
    artificially. ``is_active()`` returns ``True`` for the first
    ``active_checks`` calls, then ``False`` forever after -- modelling the audio
    thread finishing after a random number of between-iteration checks.

    Because the runner checks ``is_active()`` exactly once per completed
    iteration, ``active_checks`` is the number of full iterations after which
    the loop should stop.

    Args:
        active_checks: How many ``is_active()`` calls return ``True`` before the
            controller reports inactive (>= 0).
    """

    def __init__(self, active_checks: int) -> None:
        self.active_checks = active_checks
        self.is_active_calls = 0

    def has_started(self) -> bool:
        """Report the track as already started (time-zero, ungated audio).

        Returns:
            Always ``True``.
        """
        return True

    def is_active(self) -> bool:
        """Report active for the first ``active_checks`` calls, then inactive.

        Returns:
            ``True`` for the first ``active_checks`` invocations, ``False``
            afterwards.
        """
        self.is_active_calls += 1
        active = self.is_active_calls <= self.active_checks
        return active


class PhaseRecorder:
    """Records the order and counts of a movement's phase invocations.

    Each phase callable appends a marker to ``events`` when it runs, and the
    loop body additionally increments ``loop_calls``. A boolean tracks whether
    the loop guard ever left an iteration half-run (it never should): the loop
    body is atomic here, so partial execution cannot happen within a single
    ``await``, but the ordering assertions below confirm the guard's placement.

    Attributes:
        events: Ordered list of phase markers ("lead_in", "loop", "return").
        loop_calls: Number of completed ``loop_body`` iterations.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.loop_calls = 0

    async def lead_in(self) -> None:
        """Record the one-time lead-in phase running to completion."""
        self.events.append("lead_in")

    async def loop_body(self) -> None:
        """Record one full loop-body iteration running to completion."""
        # A started iteration must always finish before the guard is consulted;
        # appending after any (here trivial) work models that atomic completion.
        self.loop_calls += 1
        self.events.append("loop")

    async def do_return(self) -> None:
        """Record the return phase running after looping stops."""
        self.events.append("return")


@settings(max_examples=100, deadline=None)
@given(active_checks=st.integers(min_value=0, max_value=50))
def test_property6_loop_bodies_repeat_for_audio_and_are_never_cut_off(active_checks):
    # Feature: audio-synced-concurrent-gestures, Property 6: Loop bodies repeat for the audio duration and are never cut off
    """Feature: audio-synced-concurrent-gestures, Property 6: Loop bodies repeat for the audio duration and are never cut off.

    For any number of "active" between-iteration checks ``N`` (0..50), driving a
    ``loop_for_audio`` movement through the real ``_run_movement`` coordination
    with a fake playback whose ``is_active()`` returns ``True`` N times then
    ``False``:

      (a) REPEAT WHILE ACTIVE: the loop body runs exactly ``N + 1`` times -- one
          guaranteed first iteration plus one more for each check that observed
          playback still active. Every looping movement runs at least once
          (Requirements 5.3, 7.1).
      (b) CHECKED ONLY BETWEEN WHOLE ITERATIONS: ``is_active()`` is consulted
          exactly once per completed iteration (``is_active_calls`` equals the
          number of loop iterations), so a started iteration is never cut off
          mid-move -- the guard only ever runs between whole ``loop_body``
          completions (Requirement 7.2).
      (c) NO NEW ITERATION ONCE INACTIVE, THEN RETURN: once playback reports
          inactive no further ``loop_body`` runs, and the movement proceeds to
          its return phase -- the recorded event order is lead-in, then exactly
          the loop iterations, then a single return (Requirements 7.2, 7.3).

    Validates: Requirements 5.3, 7.1, 7.2, 7.3
    """
    recorder = PhaseRecorder()
    playback = FakePlayback(active_checks=active_checks)

    movement = MovementSpec(
        name="looping_gesture",
        owned_channels=frozenset({0}),
        lead_in=recorder.lead_in,
        loop_body=recorder.loop_body,
        do_return=recorder.do_return,
        supplies_gate=False,
    )

    # The runner holds a `movements` reference only for later return-to-rest
    # slices; `_run_movement` does not touch it, so a lightweight stub is safe
    # and keeps this test free of the hardware/movements stack.
    runner = PerformanceRunner(definition=None, movements=object(), audio_dir="")

    # Drive the REAL coordination coroutine directly with the fake playback and
    # no gate (ungated ⇒ audio at time zero), per the task's allowance.
    asyncio.run(
        runner._run_movement(
            movement,
            playback,  # type: ignore[arg-type]  # duck-typed fake controller
            gate=None,
            loop_for_audio=True,
        )
    )

    expected_iterations = active_checks + 1

    # (a) REPEAT WHILE ACTIVE: one guaranteed iteration plus one per active check.
    assert recorder.loop_calls == expected_iterations, (
        f"expected {expected_iterations} loop iterations for "
        f"active_checks={active_checks}, got {recorder.loop_calls}"
    )

    # (b) CHECKED ONLY BETWEEN WHOLE ITERATIONS: exactly one guard check per
    # completed iteration -- never mid-iteration.
    assert playback.is_active_calls == expected_iterations, (
        f"expected {expected_iterations} is_active checks (one per whole "
        f"iteration), got {playback.is_active_calls}"
    )

    # (c) NO NEW ITERATION ONCE INACTIVE, THEN RETURN: strict phase ordering --
    # lead-in first, then exactly the loop iterations, then a single return with
    # nothing after it.
    assert recorder.events[0] == "lead_in"
    assert recorder.events[-1] == "return"
    assert recorder.events.count("return") == 1
    assert recorder.events.count("loop") == expected_iterations
    loop_markers = recorder.events[1:-1]
    assert loop_markers == ["loop"] * expected_iterations, (
        f"loop iterations must be contiguous between lead-in and return; "
        f"got {recorder.events}"
    )


# ---------------------------------------------------------------------------
# Near-end per-movement loop cutoff (stop_loop_lead_seconds)
# ---------------------------------------------------------------------------


class NearEndPlayback:
    """Fake playback whose track "nears its end" after a set iteration count.

    Models a still-playing track for the per-movement near-end cutoff. Audio has
    started and ``is_active()`` stays ``True`` forever (the track is NOT modelled
    as finishing), so the ONLY thing that can stop a looping movement here is the
    ``stop_loop_lead_seconds`` near-end check. ``will_finish_within`` returns
    ``False`` for the first ``iterations_until_near_end`` calls, then ``True``
    afterwards -- modelling the remaining audio dropping below the movement's
    lead time after that many between-iteration checks.

    A movement WITHOUT ``stop_loop_lead_seconds`` would loop forever against this
    fake (``is_active()`` never flips), so such a movement must instead be tested
    with a fake whose ``is_active()`` eventually returns ``False`` (see
    ``ActiveThenInactivePlayback``).

    Args:
        iterations_until_near_end: How many ``will_finish_within`` calls report
            "not near the end yet" (``False``) before it flips to ``True``.
    """

    def __init__(self, iterations_until_near_end: int) -> None:
        self.iterations_until_near_end = iterations_until_near_end
        self.will_finish_calls = 0
        self.is_active_calls = 0

    def has_started(self) -> bool:
        """Report the track as already started (time-zero, ungated audio)."""
        return True

    def is_active(self) -> bool:
        """Report the track as still playing on every check."""
        self.is_active_calls += 1
        return True

    def will_finish_within(self, seconds: float) -> bool:
        """Report "near the end" only after the configured iteration count.

        Args:
            seconds: The movement's lead time (unused by this fake beyond
                confirming the runner passes it through).

        Returns:
            ``False`` for the first ``iterations_until_near_end`` calls, then
            ``True``.
        """
        self.will_finish_calls += 1
        return self.will_finish_calls > self.iterations_until_near_end


class ActiveThenInactivePlayback:
    """Fake playback that stays active N checks, then goes inactive forever.

    Companion to ``NearEndPlayback`` for the "no cutoff" control case: a movement
    with ``stop_loop_lead_seconds=None`` never consults ``will_finish_within``,
    so it can only stop when the audio itself finishes. This fake flips
    ``is_active()`` to ``False`` after ``active_checks`` calls. ``will_finish_within``
    is present (returning ``True`` immediately) purely to prove it is NEVER
    consulted for a ``None``-cutoff movement -- if it were, the loop would stop
    at the first check instead of running the full ``active_checks + 1`` bodies.

    Args:
        active_checks: How many ``is_active()`` calls return ``True`` before the
            track reports inactive.
    """

    def __init__(self, active_checks: int) -> None:
        self.active_checks = active_checks
        self.is_active_calls = 0
        self.will_finish_calls = 0

    def has_started(self) -> bool:
        """Report the track as already started (time-zero, ungated audio)."""
        return True

    def is_active(self) -> bool:
        """Report active for the first ``active_checks`` calls, then inactive."""
        self.is_active_calls += 1
        return self.is_active_calls <= self.active_checks

    def will_finish_within(self, seconds: float) -> bool:
        """Would report "near the end" immediately -- but must never be called."""
        self.will_finish_calls += 1
        return True


def test_near_end_cutoff_stops_arm_loop_then_returns():
    # Feature: audio-synced-concurrent-gestures, near-end per-movement loop cutoff
    """Feature: audio-synced-concurrent-gestures, near-end per-movement loop cutoff.

    A looping movement WITH ``stop_loop_lead_seconds`` set stops starting new
    iterations once ``will_finish_within`` reports the audio is near its end,
    even though the track is still active. The in-progress iteration always
    completes (the check runs only between whole iterations), then ``do_return``
    runs exactly once. Here the fake reports "near the end" after 3
    not-near-the-end checks, so the body runs 3 + 1 = 4 times before the cutoff.
    """
    recorder = PhaseRecorder()
    playback = NearEndPlayback(iterations_until_near_end=3)

    movement = MovementSpec(
        name="menacing_reach",
        owned_channels=frozenset({4, 5, 6, 7}),
        lead_in=recorder.lead_in,
        loop_body=recorder.loop_body,
        do_return=recorder.do_return,
        supplies_gate=False,
        stop_loop_lead_seconds=4.0,
    )

    runner = PerformanceRunner(definition=None, movements=object(), audio_dir="")
    asyncio.run(
        runner._run_movement(
            movement,
            playback,  # type: ignore[arg-type]  # duck-typed fake controller
            gate=None,
            loop_for_audio=True,
        )
    )

    # Body ran 3 not-near-end iterations + the one that observed near-end = 4.
    assert recorder.loop_calls == 4, recorder.loop_calls
    # The near-end check ran once per completed iteration -- only between whole
    # iterations, so no swing was cut off mid-body.
    assert playback.will_finish_calls == 4, playback.will_finish_calls
    # Phase order: lead-in, then exactly the 4 loops, then a single return.
    assert recorder.events[0] == "lead_in"
    assert recorder.events[-1] == "return"
    assert recorder.events.count("return") == 1
    assert recorder.events.count("loop") == 4
    assert recorder.events[1:-1] == ["loop"] * 4


def test_no_cutoff_movement_ignores_near_end_and_loops_until_inactive():
    # Feature: audio-synced-concurrent-gestures, near-end per-movement loop cutoff
    """Feature: audio-synced-concurrent-gestures, near-end per-movement loop cutoff.

    A looping movement with ``stop_loop_lead_seconds=None`` (the default, e.g.
    the head scan) is UNAFFECTED by the near-end cutoff: it never consults
    ``will_finish_within`` and keeps looping until the audio itself goes
    inactive. Driven by a fake that stays active for 5 checks then flips
    inactive, the body runs 5 + 1 = 6 times and ``will_finish_within`` is never
    called, then ``do_return`` runs once.
    """
    recorder = PhaseRecorder()
    playback = ActiveThenInactivePlayback(active_checks=5)

    movement = MovementSpec(
        name="look_around_random",
        owned_channels=frozenset({0, 1}),
        lead_in=recorder.lead_in,
        loop_body=recorder.loop_body,
        do_return=recorder.do_return,
        supplies_gate=False,
        stop_loop_lead_seconds=None,
    )

    runner = PerformanceRunner(definition=None, movements=object(), audio_dir="")
    asyncio.run(
        runner._run_movement(
            movement,
            playback,  # type: ignore[arg-type]  # duck-typed fake controller
            gate=None,
            loop_for_audio=True,
        )
    )

    # Loops until audio inactive: one guaranteed iteration + one per active check.
    assert recorder.loop_calls == 6, recorder.loop_calls
    # The near-end check must NEVER be consulted for a None-cutoff movement.
    assert playback.will_finish_calls == 0, playback.will_finish_calls
    assert recorder.events[0] == "lead_in"
    assert recorder.events[-1] == "return"
    assert recorder.events.count("return") == 1
    assert recorder.events.count("loop") == 6


# ---------------------------------------------------------------------------
# Property 3: Channel ownership is enforced before any motion
# ---------------------------------------------------------------------------


async def _noop_phase() -> None:
    """Minimal fake movement phase: an async no-op that issues no motion."""
    return None


def _make_movement_spec(name: str, channels: frozenset) -> MovementSpec:
    """Build a fake ``MovementSpec`` with no-op phases owning ``channels``.

    Only ``owned_channels`` matters for Channel_Ownership validation; the phase
    callables are async no-ops so no servo is ever driven.

    Args:
        name: The movement's identifier (surfaced in the error message).
        channels: The servo channels this fake movement claims to own.

    Returns:
        A ``MovementSpec`` suitable for constructing a ``ConcurrentGroup``.
    """
    return MovementSpec(
        name=name,
        owned_channels=channels,
        lead_in=_noop_phase,
        loop_body=_noop_phase,
        do_return=_noop_phase,
        supplies_gate=False,
    )


@st.composite
def _disjoint_movement_specs(draw):
    """Generate MovementSpecs whose ``owned_channels`` are pairwise DISJOINT.

    Partitions a pool of distinct channel ints across a random number of
    movements so no channel is ever owned by two members. Some movements may end
    up owning an empty set, which is a legitimate disjoint case.

    Returns:
        A tuple of ``MovementSpec``s with mutually disjoint owned channels.
    """
    num_movements = draw(st.integers(min_value=1, max_value=5))
    # A pool of distinct channels to hand out without repetition.
    channel_pool = draw(
        st.lists(
            st.integers(min_value=0, max_value=15),
            min_size=0,
            max_size=12,
            unique=True,
        )
    )
    # Assign each pooled channel to exactly one movement index -> disjoint.
    assignments: list[set] = [set() for _ in range(num_movements)]
    for channel in channel_pool:
        owner = draw(st.integers(min_value=0, max_value=num_movements - 1))
        assignments[owner].add(channel)

    return tuple(
        _make_movement_spec(f"disjoint_{i}", frozenset(chans))
        for i, chans in enumerate(assignments)
    )


@st.composite
def _overlapping_movement_specs(draw):
    """Generate MovementSpecs where >=2 members SHARE one or more channels.

    Draws a non-empty set of "shared" channels and guarantees at least two
    distinct movements own all of them, so construction MUST raise. Extra
    private channels are sprinkled in to vary the shape.

    Returns:
        A tuple of ``MovementSpec``s guaranteed to have a channel overlap,
        together with the frozenset of channels that are shared.
    """
    num_movements = draw(st.integers(min_value=2, max_value=5))
    shared = draw(
        st.sets(
            st.integers(min_value=0, max_value=15),
            min_size=1,
            max_size=4,
        )
    )
    # Pick two distinct movement indices that both own every shared channel.
    idx_a = draw(st.integers(min_value=0, max_value=num_movements - 1))
    idx_b = draw(
        st.integers(min_value=0, max_value=num_movements - 1).filter(
            lambda b: b != idx_a
        )
    )

    # Give every movement some private channels drawn from a disjoint range so
    # the ONLY guaranteed overlap comes from the injected shared set.
    private_pool = draw(
        st.lists(
            st.integers(min_value=16, max_value=40),
            min_size=0,
            max_size=10,
            unique=True,
        )
    )
    owned: list[set] = [set() for _ in range(num_movements)]
    for channel in private_pool:
        owner = draw(st.integers(min_value=0, max_value=num_movements - 1))
        owned[owner].add(channel)

    owned[idx_a] |= shared
    owned[idx_b] |= shared

    specs = tuple(
        _make_movement_spec(f"overlap_{i}", frozenset(chans))
        for i, chans in enumerate(owned)
    )
    return specs, frozenset(shared)


@settings(max_examples=100, deadline=None)
@given(movements=_disjoint_movement_specs())
def test_property3_disjoint_channels_construct_successfully(movements):
    # Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion
    """Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion.

    For any set of ``MovementSpec``s whose ``owned_channels`` are pairwise
    DISJOINT, constructing a ``ConcurrentGroup`` succeeds and raises nothing --
    the group is valid because no channel is commanded by two movements at once
    (Requirements 4.1, 4.2, 10.1).

    Validates: Requirements 4.1, 4.2, 10.1
    """
    from performance import ConcurrentGroup

    # Construction is the validation point; no exception may be raised.
    group = ConcurrentGroup(movements=movements)
    assert group.movements == movements


@settings(max_examples=100, deadline=None)
@given(payload=_overlapping_movement_specs())
def test_property3_overlapping_channels_raise_at_construction(payload):
    # Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion
    """Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion.

    For any set of ``MovementSpec``s where at least two members SHARE one or more
    channels, constructing the ``ConcurrentGroup`` raises
    ``ChannelOwnershipError`` at CONSTRUCTION time -- before any motion is
    issued -- and the error message names the shared channel(s) (Requirements
    4.1, 4.2, 10.1).

    Validates: Requirements 4.1, 4.2, 10.1
    """
    import pytest

    from performance import ChannelOwnershipError, ConcurrentGroup

    movements, shared_channels = payload

    with pytest.raises(ChannelOwnershipError) as exc_info:
        ConcurrentGroup(movements=movements)

    message = str(exc_info.value)
    # The error must name every shared channel so the author can locate it.
    for channel in shared_channels:
        assert f"channel {channel}" in message, (
            f"expected shared channel {channel} named in error, got: {message}"
        )


@settings(max_examples=100, deadline=None)
@given(payload=_overlapping_movement_specs())
def test_property3_runner_revalidates_overlap_fail_fast(payload):
    # Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion
    """Feature: audio-synced-concurrent-gestures, Property 3: Channel ownership is enforced before any motion.

    ``PerformanceRunner._validate_channel_ownership`` re-raises
    ``ChannelOwnershipError`` for a definition containing an overlapping group,
    failing fast before any motion -- so a definition assembled from pre-built
    groups is re-checked up front rather than partway through the performance
    (Requirements 4.1, 4.2, 10.1).

    Validates: Requirements 4.1, 4.2, 10.1
    """
    import pytest

    from performance import (
        ChannelOwnershipError,
        ConcurrentGroup,
        PerformanceDefinition,
        PerformanceStep,
    )

    movements, _shared_channels = payload

    # Build the overlapping group WITHOUT triggering __post_init__ validation,
    # so the ONLY place the overlap is caught is the runner's up-front re-check
    # (this mirrors a definition assembled from a group that slipped through).
    bad_group = ConcurrentGroup.__new__(ConcurrentGroup)
    object.__setattr__(bad_group, "movements", movements)

    definition = PerformanceDefinition(
        name="overlapping_perf",
        audio_file="dummy.wav",
        steps=(PerformanceStep(group=bad_group, loop_for_audio=False),),
        gate=None,
    )
    runner = PerformanceRunner(
        definition=definition, movements=object(), audio_dir=""
    )

    with pytest.raises(ChannelOwnershipError):
        runner._validate_channel_ownership()

# ---------------------------------------------------------------------------
# Property 8: Every owned channel ends at rest
# ---------------------------------------------------------------------------


class FakeTrunkController:
    """Fake ``TrunkController`` recording every safe-rest sweep.

    Stands in for the shared ``TrunkController`` the runner reaches through
    ``self.movements.trunkController``. Its async ``return_to_rest`` records that
    it was invoked and, per the real primitive, sweeps EVERY configured channel
    home -- so the set of channels it rests is the runner's residual "sweep
    everything home" guarantee. It never raises, matching the real recovery
    primitive that must never mask an error (Requirements 8.1, 8.3, 8.4, 10.4).

    Args:
        all_channels: Every servo channel the controller knows about; each
            ``return_to_rest`` call is treated as driving all of them to rest.
    """

    def __init__(self, all_channels: frozenset) -> None:
        self.all_channels = all_channels
        self.rest_calls = 0
        self.rested_channels: set = set()

    async def return_to_rest(self) -> None:
        """Record a safe-rest sweep of every configured channel to rest."""
        self.rest_calls += 1
        self.rested_channels |= set(self.all_channels)


class FakeMovements:
    """Minimal ``Movements`` stand-in exposing only ``trunkController``.

    ``PerformanceRunner`` only ever touches ``self.movements.trunkController``
    for return-to-rest / safe-rest recovery; the phase callables come from the
    ``MovementSpec``s directly. This keeps the test clear of the real
    movements/hardware stack.

    Args:
        trunk_controller: The fake controller the runner rests servos through.
    """

    def __init__(self, trunk_controller: "FakeTrunkController") -> None:
        self.trunkController = trunk_controller


class RestingMovementRecorder:
    """Phase callables for one movement that record the channels it returns.

    ``do_return`` records this movement's owned channels as brought home by the
    movement's OWN return phase, distinct from the runner's final
    ``return_to_rest`` sweep. ``lead_in`` / ``loop_body`` are no-ops that issue
    no motion. Optionally, a chosen phase raises to exercise the exception path.

    Args:
        owned_channels: The channels this movement drives and returns.
        returned_into: Shared set the movement records its returned channels in.
        raise_on: Which phase raises (``"lead_in"``/``"loop_body"``/``"return"``),
            or ``None`` for no failure.
    """

    def __init__(
        self,
        owned_channels: frozenset,
        returned_into: set,
        raise_on: str | None = None,
    ) -> None:
        self.owned_channels = owned_channels
        self.returned_into = returned_into
        self.raise_on = raise_on

    async def lead_in(self) -> None:
        """No-op lead-in (raises if this movement is the failing one)."""
        if self.raise_on == "lead_in":
            raise RuntimeError("lead_in boom")

    async def loop_body(self) -> None:
        """No-op loop body (raises if this movement is the failing one)."""
        if self.raise_on == "loop_body":
            raise RuntimeError("loop_body boom")

    async def do_return(self) -> None:
        """Record this movement's owned channels as returned to rest."""
        if self.raise_on == "return":
            raise RuntimeError("do_return boom")
        self.returned_into |= set(self.owned_channels)


class InactivePlayback:
    """Fake playback that never starts a real audio thread.

    Used to replace ``PerformanceRunner._build_playback`` so ``run()`` drives
    the REAL coordination without a live ``AudioPlayer``. ``start()`` merely
    flips ``has_started()`` true; ``is_active()`` reports inactive immediately,
    so any ``loop_for_audio`` step runs its bodies once and terminates at once.
    """

    def __init__(self) -> None:
        self._started = False

    def start(self) -> None:
        """Mark audio as started without spawning any thread."""
        self._started = True

    def has_started(self) -> bool:
        """Return whether ``start()`` has been called."""
        return self._started

    def is_active(self) -> bool:
        """Report playback inactive so loops terminate after one iteration."""
        return False

    def wait_finished(self, timeout=None) -> None:
        """No-op teardown; there is no real audio thread to join."""
        return None


@st.composite
def _return_to_rest_definition(draw):
    """Generate a valid multi-step ``PerformanceDefinition`` for Property 8.

    Each step's ``ConcurrentGroup`` holds movements with pairwise-disjoint
    channels (drawn from a shared pool without repetition within a step), so
    every group is valid. Movements record the channels their ``do_return``
    returns into a shared set. The union of every step's owned channels is
    returned so the test can assert full rest coverage.

    Returns:
        A tuple of (definition, movements-list-per-step-flattened, returned_set,
        all_owned_channels, trunk_all_channels).
    """
    from performance import (
        ConcurrentGroup,
        PerformanceDefinition,
        PerformanceStep,
    )

    num_steps = draw(st.integers(min_value=1, max_value=3))
    returned_into: set = set()
    all_owned: set = set()
    steps = []

    for step_index in range(num_steps):
        num_movements = draw(st.integers(min_value=1, max_value=3))
        # Distinct channels handed out within THIS step -> disjoint group.
        channel_pool = draw(
            st.lists(
                st.integers(min_value=0, max_value=15),
                min_size=1,
                max_size=9,
                unique=True,
            )
        )
        assignments: list[set] = [set() for _ in range(num_movements)]
        for channel in channel_pool:
            owner = draw(st.integers(min_value=0, max_value=num_movements - 1))
            assignments[owner].add(channel)

        specs = []
        for i, chans in enumerate(assignments):
            owned = frozenset(chans)
            all_owned |= set(owned)
            recorder = RestingMovementRecorder(owned, returned_into)
            specs.append(
                MovementSpec(
                    name=f"s{step_index}_m{i}",
                    owned_channels=owned,
                    lead_in=recorder.lead_in,
                    loop_body=recorder.loop_body,
                    do_return=recorder.do_return,
                    supplies_gate=False,
                )
            )

        loop_for_audio = draw(st.booleans())
        steps.append(
            PerformanceStep(
                group=ConcurrentGroup(movements=tuple(specs)),
                loop_for_audio=loop_for_audio,
            )
        )

    definition = PerformanceDefinition(
        name="rest_perf",
        audio_file="dummy.wav",
        steps=tuple(steps),
        gate=None,
    )
    # The trunk controller knows about every channel any movement owns, so its
    # final sweep can bring residuals home.
    return definition, returned_into, frozenset(all_owned)


@settings(max_examples=100, deadline=None)
@given(payload=_return_to_rest_definition())
def test_property8_every_owned_channel_ends_at_rest_on_completion(payload):
    # Feature: audio-synced-concurrent-gestures, Property 8: Every owned channel ends at rest
    """Feature: audio-synced-concurrent-gestures, Property 8: Every owned channel ends at rest.

    For any valid ``PerformanceDefinition`` (ordered steps of disjoint-channel
    concurrent groups), after the REAL ``run()`` completes normally every servo
    channel driven by the performance ends at its rest position: each movement's
    own ``do_return`` runs AND the runner sweeps residual channels home via
    ``TrunkController.return_to_rest()`` on completion. The union of channels
    rested by ``do_return`` plus the final ``return_to_rest`` sweep therefore
    covers every owned channel, and ``return_to_rest`` is invoked at least once
    on completion (Requirements 8.1, 8.3, 10.4).

    Validates: Requirements 8.1, 8.3, 8.4, 10.4
    """
    definition, returned_into, all_owned = payload

    trunk = FakeTrunkController(all_channels=all_owned)
    movements = FakeMovements(trunk)
    runner = PerformanceRunner(
        definition=definition, movements=movements, audio_dir=""
    )
    # Replace the playback builder so no real AudioPlayer thread is started.
    runner._build_playback = lambda: InactivePlayback()  # type: ignore[method-assign]

    asyncio.run(runner.run())

    # return_to_rest is invoked on completion (the final residual sweep runs).
    assert trunk.rest_calls >= 1, "return_to_rest must run on normal completion"

    # Every owned channel ends at rest: covered by each movement's own do_return
    # AND/OR the runner's final sweep-everything-home return_to_rest.
    rested = set(returned_into) | set(trunk.rested_channels)
    assert all_owned <= rested, (
        f"channels {all_owned - rested} were never returned to rest; "
        f"do_return covered {returned_into}, sweep covered {trunk.rested_channels}"
    )


@settings(max_examples=100, deadline=None)
@given(
    payload=_return_to_rest_definition(),
    raise_phase=st.sampled_from(["lead_in", "loop_body", "return"]),
)
def test_property8_phase_failure_still_rests_and_reraises(payload, raise_phase):
    # Feature: audio-synced-concurrent-gestures, Property 8: Every owned channel ends at rest
    """Feature: audio-synced-concurrent-gestures, Property 8: Every owned channel ends at rest.

    For any valid ``PerformanceDefinition`` where a phase raises during the REAL
    ``run()``, the runner drives all servos to safe rest --
    ``TrunkController.return_to_rest()`` is still invoked (no channel left
    energized against a jam) -- AND the originating exception propagates out of
    ``run()`` rather than being swallowed (Requirements 8.4, 10.4).

    Validates: Requirements 8.1, 8.3, 8.4, 10.4
    """
    import pytest

    from performance import (
        ConcurrentGroup,
        PerformanceDefinition,
        PerformanceStep,
    )

    definition, _returned_into, all_owned = payload

    # Rebuild the FIRST movement of the FIRST step so exactly one phase raises,
    # guaranteeing run() hits its exception recovery path.
    first_step = definition.steps[0]
    first_spec = first_step.group.movements[0]
    failing = RestingMovementRecorder(
        first_spec.owned_channels, set(), raise_on=raise_phase
    )
    failing_spec = MovementSpec(
        name=first_spec.name,
        owned_channels=first_spec.owned_channels,
        lead_in=failing.lead_in,
        loop_body=failing.loop_body,
        do_return=failing.do_return,
        supplies_gate=False,
    )
    new_first_group = ConcurrentGroup(
        movements=(failing_spec,) + first_step.group.movements[1:]
    )
    new_steps = (
        PerformanceStep(
            group=new_first_group, loop_for_audio=first_step.loop_for_audio
        ),
    ) + definition.steps[1:]
    definition = PerformanceDefinition(
        name=definition.name,
        audio_file=definition.audio_file,
        steps=new_steps,
        gate=None,
    )

    trunk = FakeTrunkController(all_channels=all_owned)
    movements = FakeMovements(trunk)
    runner = PerformanceRunner(
        definition=definition, movements=movements, audio_dir=""
    )
    runner._build_playback = lambda: InactivePlayback()  # type: ignore[method-assign]

    # The originating error must propagate -- run() never swallows it.
    with pytest.raises(RuntimeError, match=f"{raise_phase} boom"):
        asyncio.run(runner.run())

    # ... and safe rest was still commanded before the error escaped.
    assert trunk.rest_calls >= 1, (
        "return_to_rest must run on the exception path before re-raising"
    )

# ---------------------------------------------------------------------------
# Property 10: Phased composition reproduces standalone behavior
# ---------------------------------------------------------------------------

import constants  # noqa: E402
from movements import Movements  # noqa: E402
from trunkcontroller import TrunkController  # noqa: E402

# The arm channels menacing_reach owns (4-7). See src/constants.py.
_MENACING_REACH_CHANNELS = (
    constants.RT_ELBOW_ROTATOR,   # 4
    constants.RT_ELBOW_TILT,      # 5
    constants.RT_SHOULDER_TILT,   # 6
    constants.RT_SHOULDER_ROTATOR,  # 7
)


def _record_menacing_reach(run_gesture, start_angles):
    """Run a menacing_reach coroutine factory and capture its servo commands.

    Drives ``run_gesture()`` (an async callable) under the SERVO_SIM fake kit,
    wrapping ``TrunkController.set_angle`` -- the single choke point for EVERY
    servo write -- to record ``(channel, post-clamp angle)`` for each write in
    order. The owned arm channels (4-7) are reset to ``start_angles`` on the
    shared fake kit BEFORE the run so both the standalone and phased paths begin
    from the identical pose (the kit and ``_limit_overrides`` are class-level and
    shared across instances). Asserts the verified-pose override left no residue
    on ``_limit_overrides`` after the run.

    Args:
        run_gesture: Zero-arg async callable that performs the gesture.
        start_angles: Mapping channel -> starting angle for channels 4-7.

    Returns:
        The ordered list of ``(channel, angle)`` commands issued during the run.
    """
    movements = Movements("menacing_reach_test")
    trunk = movements.trunkController

    # The menace swing now draws its randomized tilt/rotator endpoints and timing
    # from the shared ``random`` module. Seed it to a FIXED value immediately
    # before each run so the standalone and phased paths draw the identical
    # sequence of random endpoints and therefore issue identical commands
    # (Requirement 9.3). Both calls in the test use this same seed.
    random.seed(1234)

    # Reset the owned channels to the SAME known starting pose before the run so
    # move_to's "capture current angle" step sees identical starts both times.
    for channel, angle in start_angles.items():
        trunk.kit.servo[channel].angle = angle

    commands: list[tuple[int, int]] = []
    original_set_angle = trunk.set_angle

    def recording_set_angle(servo_num, angle):
        safe = original_set_angle(servo_num, angle)
        commands.append((servo_num, safe))
        return safe

    # The per-step ``asyncio.sleep`` delays in ``move_to`` only pace the physical
    # sweep; they don't affect WHICH angles are commanded. Neutralize them so the
    # (otherwise multi-second) gesture records its command stream near-instantly
    # -- the sequence under test is unchanged.
    real_sleep = asyncio.sleep

    async def _no_delay(_seconds, *args, **kwargs):
        return await real_sleep(0)

    trunk.set_angle = recording_set_angle
    asyncio.sleep = _no_delay
    try:
        asyncio.run(run_gesture(movements))
    finally:
        asyncio.sleep = real_sleep
        trunk.set_angle = original_set_angle

    # The verified_pose_override must be fully released after the run -- the
    # standalone `with` block and the phased AsyncExitStack both scope it to the
    # gesture, so nothing may leak into the class-level overrides.
    assert TrunkController._limit_overrides == {}, (
        f"verified_pose_override leaked: {TrunkController._limit_overrides}"
    )
    return commands


async def _run_standalone(movements):
    """Run the standalone menacing_reach gesture (reach + swing x3 + retract)."""
    await movements.menacing_reach()


async def _run_phased(movements):
    """Run the phased composition: lead_in + loop_body x8 + return.

    The standalone gesture now performs eight menace swings, so the phased path
    runs ``menacing_reach_loop_body`` eight times to reproduce the identical
    command sequence (given the same RNG seed).
    """
    await movements.menacing_reach_lead_in()
    for _ in range(8):
        await movements.menacing_reach_loop_body()
    await movements.menacing_reach_return()


@settings(max_examples=25, deadline=None)
@given(
    start_angles=st.fixed_dictionaries(
        {
            channel: st.integers(min_value=0, max_value=270)
            for channel in _MENACING_REACH_CHANNELS
        }
    )
)
def test_property10_phased_composition_reproduces_standalone(start_angles):
    # Feature: audio-synced-concurrent-gestures, Property 10: Phased composition reproduces standalone behavior
    """Feature: audio-synced-concurrent-gestures, Property 10: Phased composition reproduces standalone behavior.

    For any starting pose of the owned arm channels (4-7), running the phase
    composition ``menacing_reach_lead_in()`` + ``menacing_reach_loop_body()`` x3
    + ``menacing_reach_return()`` issues the IDENTICAL ordered sequence of
    ``(channel, post-clamp angle)`` servo commands as the standalone
    ``menacing_reach()`` gesture (reach, swing three times, retract). Both paths
    are driven from the same reset starting pose, capturing every write at the
    ``TrunkController.set_angle`` choke point, and the verified-pose override
    leaves no residue after either run (Requirement 9.3).

    Validates: Requirements 9.3
    """
    standalone_commands = _record_menacing_reach(_run_standalone, start_angles)
    phased_commands = _record_menacing_reach(_run_phased, start_angles)

    # Both paths delegate to the same reach/swing/retract primitives from the
    # same starting pose, so every commanded (channel, angle) must match exactly
    # and in the same order -- the move_to sweeps make these lists long.
    assert phased_commands == standalone_commands, (
        "phased composition diverged from standalone menacing_reach: "
        f"{len(phased_commands)} phased vs {len(standalone_commands)} standalone "
        f"commands; first mismatch at "
        f"{next((i for i, (a, b) in enumerate(zip(phased_commands, standalone_commands)) if a != b), 'n/a')}"
    )
    # Sanity: the gesture actually issues a substantial command stream (the
    # move_to steps ensure this), so an empty-equals-empty pass can't sneak by.
    assert len(standalone_commands) > 0


# ---------------------------------------------------------------------------
# present_palm: phased composition reproduces standalone behavior
# ---------------------------------------------------------------------------

# The arm channels present_palm owns (4-7). See src/constants.py.
_PRESENT_PALM_CHANNELS = (
    constants.RT_ELBOW_ROTATOR,     # 4
    constants.RT_ELBOW_TILT,        # 5
    constants.RT_SHOULDER_TILT,     # 6
    constants.RT_SHOULDER_ROTATOR,  # 7
)


def _record_present_palm(run_gesture, start_angles):
    """Run a present_palm coroutine factory and capture its servo commands.

    Mirrors ``_record_menacing_reach`` for the ``present_palm`` gesture: drives
    ``run_gesture()`` under the SERVO_SIM fake kit, wrapping
    ``TrunkController.set_angle`` to record ``(channel, post-clamp angle)`` for
    each write in order. The owned arm channels (4-7) are reset to
    ``start_angles`` on the shared fake kit BEFORE the run, and ``random`` is
    seeded to a FIXED value so the standalone and phased paths draw the identical
    bob endpoints/timing and therefore issue identical commands.

    Args:
        run_gesture: Zero-arg async callable (taking the Movements instance).
        start_angles: Mapping channel -> starting angle for channels 4-7.

    Returns:
        The ordered list of ``(channel, angle)`` commands issued during the run.
    """
    movements = Movements("present_palm_test")
    trunk = movements.trunkController

    random.seed(4321)

    for channel, angle in start_angles.items():
        trunk.kit.servo[channel].angle = angle

    commands: list[tuple[int, int]] = []
    original_set_angle = trunk.set_angle

    def recording_set_angle(servo_num, angle):
        safe = original_set_angle(servo_num, angle)
        commands.append((servo_num, safe))
        return safe

    real_sleep = asyncio.sleep

    async def _no_delay(_seconds, *args, **kwargs):
        return await real_sleep(0)

    trunk.set_angle = recording_set_angle
    asyncio.sleep = _no_delay
    try:
        asyncio.run(run_gesture(movements))
    finally:
        asyncio.sleep = real_sleep
        trunk.set_angle = original_set_angle

    return commands


async def _run_present_palm_standalone(movements):
    """Run the standalone present_palm gesture (raise + bob x4 + lower)."""
    await movements.present_palm()


async def _run_present_palm_phased(movements):
    """Run the phased composition: lead_in + loop_body x4 + return.

    The standalone gesture performs four bobs, so the phased path runs
    ``present_palm_loop_body`` four times to reproduce the identical command
    sequence (given the same RNG seed).
    """
    await movements.present_palm_lead_in()
    for _ in range(4):
        await movements.present_palm_loop_body()
    await movements.present_palm_return()


@settings(max_examples=25, deadline=None)
@given(
    start_angles=st.fixed_dictionaries(
        {
            channel: st.integers(min_value=0, max_value=270)
            for channel in _PRESENT_PALM_CHANNELS
        }
    )
)
def test_present_palm_phased_composition_reproduces_standalone(start_angles):
    # Feature: blah refinement — present_palm phased composition reproduces standalone
    """Feature: blah refinement — present_palm phased composition reproduces standalone.

    For any starting pose of the owned arm channels (4-7), running the phase
    composition ``present_palm_lead_in()`` + ``present_palm_loop_body()`` x4 +
    ``present_palm_return()`` issues the IDENTICAL ordered sequence of
    ``(channel, post-clamp angle)`` servo commands as the standalone
    ``present_palm()`` gesture (raise, bob four times, lower). Both paths are
    driven from the same reset starting pose under the same fixed RNG seed,
    capturing every write at the ``TrunkController.set_angle`` choke point.
    """
    standalone_commands = _record_present_palm(_run_present_palm_standalone, start_angles)
    phased_commands = _record_present_palm(_run_present_palm_phased, start_angles)

    assert phased_commands == standalone_commands, (
        "phased composition diverged from standalone present_palm: "
        f"{len(phased_commands)} phased vs {len(standalone_commands)} standalone "
        f"commands; first mismatch at "
        f"{next((i for i, (a, b) in enumerate(zip(phased_commands, standalone_commands)) if a != b), 'n/a')}"
    )
    assert len(standalone_commands) > 0
