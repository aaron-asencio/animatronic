"""Reusable performance framework data models and channel-ownership validation.

This module is the foundation of the Performance_Framework: the reusable
coordination layer that composes existing ``Movements`` gestures over a single
dialog audio track. A performance is declared as data -- an ordered sequence of
``PerformanceStep``s, each running a ``ConcurrentGroup`` of ``MovementSpec``s --
rather than as bespoke coordination code (Requirements 1, 3).

This first slice defines only the declarative data model and the
channel-ownership guarantee that must hold before any servo is driven. The
async ``PerformanceRunner`` and ``PlaybackController`` are added by downstream
tasks; the type signatures here are the contract those pieces build on.

A ``ConcurrentGroup``'s members must own mutually disjoint servo channels
(Channel_Ownership). Overlap is treated as an authoring error and raises
``ChannelOwnershipError`` at construction time -- before any motion is issued
(Requirements 3.2, 4.2, 10.1).

Debug output uses ``print()`` directly, per project convention (no logging
framework).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from movements import Movements


class ChannelOwnershipError(Exception):
    """Raised when concurrent movements do not own disjoint servo channels.

    Two ``MovementSpec``s scheduled to run at the same time must not both drive
    the same servo channel; otherwise the channel would be commanded from two
    movements at once. This error is raised while a ``ConcurrentGroup`` is being
    constructed, so a bad performance never reaches the servos (Requirements
    4.2, 10.1).
    """


@dataclass(frozen=True)
class MovementSpec:
    """Binds one movement's phase callables and owned channels for a performance.

    A ``MovementSpec`` describes a Simple_Movement as the framework sees it: the
    servo channels it drives, its separable phases (optional lead-in, a
    repeatable loop body, and an optional return), and whether its lead-in
    completion supplies the audio gate. The framework treats the movement as
    opaque -- it only knows these phases, channels, and the gate flag
    (Requirements 3.1, 9.1). The same spec may be shared by reference across
    performances rather than copied (Requirement 3.3).

    Args:
        name: Human-readable identifier for the movement (e.g.
            ``"menacing_reach"``). Also matched by ``GateSpec.movement_name``.
        owned_channels: The servo channels this movement drives. Used to enforce
            Channel_Ownership within a ``ConcurrentGroup``.
        lead_in: Optional one-time entry phase run before looping (may be
            ``None`` for movements that start looping at time zero).
        loop_body: One repeatable iteration of the movement's motion.
        do_return: Optional phase that brings the owned channels back to rest
            (may be ``None``).
        supplies_gate: When ``True``, this movement's ``lead_in`` completing
            opens the audio gate. Defaults to ``False``.
        stop_loop_lead_seconds: When set on a looping movement, stop starting
            NEW loop iterations once the audio track will finish within this
            many seconds (the in-progress iteration always completes, then
            ``do_return`` runs), so the movement can return to rest before the
            audio ends. ``None`` (default) means loop until audio is fully
            inactive.
    """

    name: str
    owned_channels: frozenset[int]
    lead_in: Callable[[], Awaitable[None]] | None
    loop_body: Callable[[], Awaitable[None]]
    do_return: Callable[[], Awaitable[None]] | None
    supplies_gate: bool = False
    stop_loop_lead_seconds: float | None = None


@dataclass(frozen=True)
class GateSpec:
    """Names the movement whose lead-in opens the audio gate.

    The framework starts audio playback when the named movement signals its gate
    (its ``lead_in`` completing, or an explicit event). A ``PerformanceDefinition``
    with ``gate=None`` starts audio at time zero instead (Requirements 6.1, 6.4).

    Args:
        movement_name: The ``MovementSpec.name`` whose lead-in supplies the gate.
    """

    movement_name: str


@dataclass(frozen=True)
class ConcurrentGroup:
    """One or more movements that run at the same time within a step.

    A group of one is simply a single movement (Requirement 2.5). At
    construction the members' ``owned_channels`` must be pairwise disjoint; any
    overlap raises ``ChannelOwnershipError`` naming the shared channel(s) before
    any motion is issued (Requirements 4.1, 4.2, 10.1).

    Args:
        movements: The movements to run concurrently within one step.
    """

    movements: tuple[MovementSpec, ...]

    def __post_init__(self) -> None:
        """Validate that members own mutually disjoint servo channels.

        Raises:
            ChannelOwnershipError: If any two members share one or more servo
                channels, naming the shared channel(s).
        """
        seen: dict[int, str] = {}
        shared: dict[int, list[str]] = {}
        for movement in self.movements:
            for channel in movement.owned_channels:
                if channel in seen:
                    owners = shared.setdefault(channel, [seen[channel]])
                    owners.append(movement.name)
                else:
                    seen[channel] = movement.name

        if shared:
            details = ", ".join(
                f"channel {channel} owned by {' and '.join(owners)}"
                for channel, owners in sorted(shared.items())
            )
            print(f"[performance] channel-ownership violation: {details}")
            raise ChannelOwnershipError(
                f"Concurrent movements must own disjoint channels: {details}"
            )


@dataclass(frozen=True)
class PerformanceStep:
    """One entry in a performance's ordered sequence.

    A step runs its ``ConcurrentGroup`` to completion before the next step
    begins (Requirement 2.2). ``loop_for_audio`` marks whether this step's loop
    bodies repeat for the audio duration, so multi-step performances need not
    assume a single looping step (Requirement 7.4).

    Args:
        group: The concurrent group this step runs.
        loop_for_audio: When ``True``, repeat this step's loop bodies while
            playback is active. Defaults to ``False`` (bodies run once).
    """

    group: ConcurrentGroup
    loop_for_audio: bool = False


@dataclass(frozen=True)
class PerformanceDefinition:
    """The complete declarative description of one performance.

    Adding a performance means writing one of these -- an ordered list of steps,
    the single audio track, and which movement (if any) gates the audio -- with
    no changes to the framework's coordination logic (Requirements 1.3, 3.1,
    3.4). ``gate is None`` means audio starts at time zero (Requirement 6.4).

    Args:
        name: The performance's registered action name.
        audio_file: The dialog track filename (resolved against the audio dir).
        steps: The ordered performance steps, run one after another.
        gate: Which movement supplies the audio gate, or ``None`` to start audio
            at time zero. Defaults to ``None``.
        player_options: Optional kwargs forwarded to the ``AudioPlayer`` (e.g.
            disable the jaw or envelope-driven eyes: ``{"drive_jaw": False,
            "drive_eyes": False}``). ``None`` (default) => default player
            (jaw + envelope-driven eyes). The dict is mutable, so the
            default is ``None`` (never a mutable default) and it is copied where
            it is consumed (``PlaybackController``).
        followup_audio_files: Optional additional tracks played back-to-back
            AFTER ``audio_file`` finishes, in the given order, on the same audio
            thread with no gap (e.g. ``hypnotic`` plays ``in_my_power.wav``
            immediately after ``hypnotic.wav``). Each entry is either a bare
            filename (played with this definition's ``player_options``) or a
            ``(filename, options)`` pair whose ``options`` override the
            ``AudioPlayer`` kwargs for THAT track only -- e.g. keep the jaw
            silent on the lead track but turn it on for a follow-on with
            ``("in_my_power.wav", {"drive_jaw": True, "drive_eyes": False})``.
            Each filename is resolved against the audio dir like ``audio_file``.
            The performance's ``PlaybackController`` stays ``is_active()`` across
            the whole chain, and its duration is the SUM of every track, so
            looping movements keep running -- and their ``stop_loop_lead_seconds``
            near-end cutoff fires against the end of the LAST track, not the
            first. ``None`` (default) => a single-track performance, unchanged.
            Kept as the LAST field so existing positional construction is
            unaffected.
    """

    name: str
    audio_file: str
    steps: tuple[PerformanceStep, ...]
    gate: GateSpec | None = None
    player_options: dict | None = None
    followup_audio_files: tuple | None = None


class PlaybackController:
    """Bridges the thread-based ``AudioPlayer`` to the async performance runner.

    A performance plays one or more dialog tracks back-to-back. This controller
    wraps the pattern already used by ``Animatronic.run_action_and_audio``:
    build an ``AudioPlayer`` and run its blocking ``play_audio_file`` in a
    ``daemon=True`` thread so the async gesture coroutines run alongside it
    (Requirement 5.1). When more than one track is given, the same thread calls
    ``play_audio_file`` once per track in order, so the tracks play with no gap
    and the thread stays alive across the whole chain.

    The chain is started once and never restarted or switched between steps
    (Requirement 5.2). ``is_active()`` derives ``Playback_Active`` from the audio
    thread being alive; it is the single source of truth the runner's
    loop-until-audio check consults (Requirement 7.1), matching the behaviour of
    ``AudioPlayer``'s own blocking playback loop, which returns only once the
    stream drains -- and, for a multi-track chain, only once the LAST track
    drains. ``time_remaining`` / ``will_finish_within`` are computed against the
    SUM of every track's duration, so a movement's near-end loop cutoff fires
    relative to the end of the final track, not the first.

    Per-track player options: a chain may play different tracks with DIFFERENT
    ``AudioPlayer`` options -- e.g. ``hypnotic`` plays ``hypnotic.wav`` with the
    jaw OFF and then ``in_my_power.wav`` with the jaw ON. Pass ``(path, options)``
    pairs to vary options per track; a fresh ``AudioPlayer`` is built for each
    track and the previous one is ``close()``d first so its GPIO pins (e.g. the
    jaw motor) are released before the next player claims them.

    Args:
        audio_paths: The track(s) to play, in order. Accepts any of: a single
            path string (the common single-track case); a sequence of path
            strings (all played with ``player_options``); or a sequence of
            ``(path, options)`` pairs, where ``options`` is a per-track
            ``AudioPlayer`` kwargs dict (or ``None`` to use ``player_options``).
            The forms may be mixed within one sequence.
        player_options: Default kwargs forwarded to the ``AudioPlayer``
            constructor for any track that does not supply its own (e.g.
            ``{"drive_jaw": False, "drive_eyes": False}`` to silence the jaw and
            free the eye pin for a separate blinker). ``None`` (default) => a
            default ``AudioPlayer`` (jaw + envelope-driven eyes). Copied on
            construction so a caller's dict is never mutated.
    """

    def __init__(
        self,
        audio_paths: "str | tuple | list",
        player_options: dict | None = None,
    ) -> None:
        self.player_options = dict(player_options or {})

        # Normalize the various accepted forms into a list of (path, options)
        # tuples, one per track, played in order on one thread. A bare string
        # (single track) or a sequence of bare strings use the default
        # player_options; a (path, options) pair overrides them for that track.
        def _normalize(entry):
            if isinstance(entry, str):
                return (entry, dict(self.player_options))
            path, options = entry
            return (path, dict(options) if options is not None else dict(self.player_options))

        if isinstance(audio_paths, str):
            self._tracks: list[tuple[str, dict]] = [_normalize(audio_paths)]
        else:
            self._tracks = [_normalize(entry) for entry in audio_paths]

        # Ordered list of just the paths, for duration and debug output.
        self.audio_paths: tuple[str, ...] = tuple(path for path, _ in self._tracks)
        # Kept for debug/log messages and the idempotent-restart notice; the
        # first track names the chain.
        self.audio_path = self.audio_paths[0] if self.audio_paths else ""
        self._thread: threading.Thread | None = None
        self._start_monotonic: float | None = None

        # Read each track's duration from the WAV header with the stdlib
        # ``wave`` module and SUM them so the runner can tell how much audio is
        # left across the whole chain (used only as an authoring/coordination
        # aid for per-movement near-end loop cutoffs; it is NOT sample-accurate).
        # Any failure on ANY track -- unreadable/missing file, a non-WAV path,
        # or a zero frame rate -- leaves ``_duration = None`` so callers fall
        # back to the loop-until-inactive guard and never stop early. No audio
        # hardware is imported here.
        self._duration: float | None = None
        try:
            total = 0.0
            for path in self.audio_paths:
                with wave.open(path, "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                if rate == 0:
                    raise ValueError(f"frame rate is zero for {path}")
                total += frames / rate
            self._duration = total
        except Exception as error:  # noqa: BLE001 - any failure ⇒ unknown duration
            self._duration = None
            print(f"[performance] could not read audio duration for {self.audio_paths}: {error}")

    def _play_chain(self) -> None:
        """Play every track in order on the calling thread, back-to-back.

        Runs on the daemon audio thread. For each track it builds an
        ``AudioPlayer`` with that track's own options and calls the blocking
        ``play_audio_file``, so the next track begins the instant the previous
        one drains -- no gap. Between tracks the finished player is ``close()``d
        so any GPIO pin it held (e.g. the jaw motor) is released before the next
        player is constructed; this is what lets consecutive tracks use
        different ``drive_jaw`` / ``drive_eyes`` settings without a gpiozero
        "pin already in use" clash. Because this method only returns once the
        LAST track finishes, ``is_active()`` stays ``True`` across the whole
        chain.
        """
        # Import here rather than at module load so the framework's pure
        # coordination logic (and its fake-controller tests) never pulls in the
        # hardware/audio stack.
        from audio_player import AudioPlayer

        for path, options in self._tracks:
            player = AudioPlayer(**options)
            try:
                print(f"[performance] playing audio: {path} (options={options})")
                player.play_audio_file(path)
            finally:
                # Release this track's GPIO pins before the next player claims
                # them, so a jaw-off track can be followed by a jaw-on one.
                player.close()

    def start(self) -> None:
        """Start audio playback in a daemon thread (idempotent — starts once).

        Runs the track chain on a ``daemon=True`` thread so it does not keep the
        process alive on its own. Every track shares one ``AudioPlayer`` and is
        played back-to-back by ``_play_chain``. Calling ``start()`` more than
        once is a no-op: the chain is never restarted (Requirement 5.2).
        """
        if self._thread is not None:
            print(f"[performance] audio already started; ignoring restart of {self.audio_path}")
            return

        self._thread = threading.Thread(
            target=self._play_chain,
            daemon=True,
        )
        # Record the monotonic start the moment the audio thread begins, so
        # ``time_remaining`` measures elapsed playback from this point. Set only
        # on the real (first) start, consistent with the idempotent guard above.
        self._start_monotonic = time.monotonic()
        self._thread.start()

    def has_started(self) -> bool:
        """Return whether ``start()`` has been called (the track has begun).

        Distinguishes "audio has not started yet" (e.g. a gated performance
        still running its lead-in) from "audio has finished". ``is_active()``
        is ``False`` in both cases, but only the latter should end a looping
        movement -- the former means the loop must keep running until audio
        starts (Requirements 6.1, 7.1). The loop-until-audio guard in
        ``_run_movement`` uses this to avoid exiting before the gate opens.

        Returns:
            ``True`` once ``start()`` has spawned the audio thread, ``False``
            before any ``start()`` call.
        """
        return self._thread is not None

    def is_active(self) -> bool:
        """Return whether the audio track is still playing (``Playback_Active``).

        Returns:
            ``True`` while the daemon audio thread is alive, ``False`` before
            ``start()`` is called or once playback has finished.
        """
        return self._thread is not None and self._thread.is_alive()

    def time_remaining(self) -> float | None:
        """Return approximate seconds of audio left, or ``None`` if unknowable.

        Computed as ``duration - (now - start)`` against a monotonic clock, so
        it reflects wall-clock elapsed playback rather than samples actually
        drained. This is an authoring/coordination aid for near-end loop
        cutoffs -- NOT sample-accurate -- and never goes negative.

        Fallback semantics: returns ``None`` when the duration could not be read
        from the WAV header OR audio has not started yet. Callers must treat
        ``None`` as "cannot tell" and fall back to the loop-until-inactive guard
        rather than stopping early.

        Returns:
            ``max(0.0, remaining)`` seconds when the duration is known and audio
            has started, else ``None``.
        """
        if self._duration is None or self._start_monotonic is None:
            return None
        return max(0.0, self._duration - (time.monotonic() - self._start_monotonic))

    def will_finish_within(self, seconds: float) -> bool:
        """Return whether the audio track will finish within ``seconds``.

        Used by the runner's per-movement near-end cutoff to stop starting new
        loop iterations while there is still time for ``do_return`` to run
        before the track ends.

        Fallback semantics: returns ``False`` whenever the outcome cannot be
        determined -- audio has not started, or the duration could not be read
        from the WAV header -- so behaviour falls back to the existing
        loop-until-inactive guard and a movement is NEVER stopped early when we
        cannot tell how much audio remains. Like ``time_remaining``, this is an
        authoring/coordination aid and is not sample-accurate.

        Args:
            seconds: The lead time to test the remaining audio against.

        Returns:
            ``True`` iff audio has started, the duration is known, and the
            remaining time is ``<= seconds``; ``False`` otherwise.
        """
        remaining = self.time_remaining()
        if remaining is None:
            return False
        return remaining <= seconds

    def wait_finished(self, timeout: float | None = None) -> None:
        """Block until the audio thread finishes (or ``timeout`` elapses).

        Used during teardown so audio resources are released even if the gesture
        side finishes first. Safe to call when playback was never started.

        Args:
            timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        """
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class PerformanceRunner:
    """Runs a ``PerformanceDefinition`` -- the single place all coordination lives.

    The runner is the one component that turns a declarative performance into
    motion (Requirement 1.2). Given a definition, the ``Movements`` instance that
    supplies the phase callables, and the audio directory, it validates channel
    ownership up front and then executes the performance's steps.

    This slice implements the foundation of ``run()``:

    * **Validation before motion** -- every ``ConcurrentGroup``'s
      Channel_Ownership is re-checked before any servo is driven, so a bad
      definition never reaches the hardware (Requirements 4.1, 4.2, 10.1).
    * **Ordered steps** -- ``PerformanceStep``s run in declared order, each fully
      completing before the next begins; the final step ending ends the
      performance (Requirements 2.2, 2.3, 2.4).
    * **Concurrent groups** -- a step's ``MovementSpec`` members run together via
      ``asyncio.gather`` (Requirements 2.1, 4.1).

    Each movement's phases are driven generically as ``lead_in`` -> ``loop_body``
    -> ``do_return`` (lead-in and return optional). Audio start is gated on
    movement progress (task 3.4): ungated movements start their loops at time
    zero, concurrently with the gate-supplying movement's ``lead_in``; the moment
    that ``lead_in`` completes it opens the gate (an ``asyncio.Event``) and the
    ``PlaybackController`` is started immediately, with no fixed idle sleep
    between the gate signal and ``start()`` (Requirements 6.1, 6.2, 6.3). When
    ``definition.gate is None`` the audio starts at time zero, before any
    movement runs (Requirement 6.4). A step flagged ``loop_for_audio`` repeats
    its loop bodies for the audio duration (Loop_Until_Audio, task 5.1);
    unflagged steps run each body exactly once. After the steps, each movement's
    ``do_return`` has already run; the runner returns driven channels to rest
    between sequential steps, sweeps residual channels home via
    ``TrunkController.return_to_rest()`` on completion, and drives all servos to
    safe rest on any phase failure before re-raising (task 6.1, Requirements
    8.1, 8.3, 8.4, 10.4).

    ``asyncio.run`` is only ever called at the top of the stack (in
    ``animatronic.py``), never here -- ``run()`` is a coroutine that assumes it is
    already inside an event loop (project async convention).

    Args:
        definition: The declarative performance to execute.
        movements: The ``Movements`` instance whose phase methods the
            definition's ``MovementSpec`` callables are bound to. Its shared
            ``TrunkController`` drives return-to-rest / safe-rest recovery.
        audio_dir: Directory containing the performance's audio track.
            ``definition.audio_file`` is resolved against it (via
            ``os.path.join``) to build the single ``PlaybackController``.
        ambient: Optional callable taking the ``PlaybackController`` and
            returning an awaitable/coroutine, or ``None``. When provided,
            ``run()`` launches it as a concurrent task alongside the performance
            steps (created right after playback is set up), passing it the
            performance's ``PlaybackController`` so the ambient coroutine can
            bind itself to the audio window (e.g. start/stop with the audio).
            Once the steps finish -- on BOTH the normal-completion and exception
            paths -- ``run()`` cancels it if still running and awaits it,
            swallowing ``CancelledError``, as a SAFETY NET. The task never
            prevents ``run()`` from returning. The runner treats the coroutine as
            opaque (it knows nothing about LEDs or what the ambient does); it just
            hands it the playback handle. ``hypnotic`` uses it to blink the eyes
            bound to the audio window, leaving the LED off when the performance
            ends.
    """

    def __init__(
        self,
        definition: PerformanceDefinition,
        movements: "Movements",
        audio_dir: str,
        ambient: Callable[[PlaybackController], Awaitable[None]] | None = None,
    ) -> None:
        self.definition = definition
        self.movements = movements
        self.audio_dir = audio_dir
        self.ambient = ambient

    def _validate_channel_ownership(self) -> None:
        """Re-check every group's Channel_Ownership before any motion is issued.

        ``ConcurrentGroup`` already validates disjoint channels at construction,
        but the runner re-validates every group in the definition up front so a
        definition assembled from pre-built groups still fails fast -- before a
        single servo write -- rather than partway through the performance
        (Requirements 4.1, 4.2, 10.1). Constructing a fresh ``ConcurrentGroup``
        from each group's members re-runs that disjointness check and raises
        ``ChannelOwnershipError`` on overlap.

        Raises:
            ChannelOwnershipError: If any group's members share a servo channel.
        """
        for step in self.definition.steps:
            ConcurrentGroup(movements=step.group.movements)

    def _build_playback(self) -> PlaybackController:
        """Build the single ``PlaybackController`` for this performance.

        Resolves ``definition.audio_file`` against ``self.audio_dir`` with
        ``os.path.join`` -- the same relative-filename-in-audio-dir convention
        the rest of the project uses -- and wraps it in a ``PlaybackController``.
        The performance plays one track, or -- when the definition declares
        ``followup_audio_files`` -- that track followed by each follow-on track
        back-to-back. Either way exactly one controller is built and started at
        most once (Requirement 5.1); a multi-track chain still runs on a single
        audio thread.

        A follow-on entry may be a bare filename (played with the definition's
        default ``player_options``) or a ``(filename, options)`` pair whose
        ``options`` override the player options for THAT track only -- e.g.
        ``hypnotic`` plays ``in_my_power.wav`` with ``{"drive_jaw": True}`` so
        the jaw articulates on the follow-on track while the lead track keeps it
        silent.

        Returns:
            The controller for this performance's dialog track(s).
        """
        # The lead track uses the definition's default player_options.
        tracks: list = [os.path.join(self.audio_dir, self.definition.audio_file)]
        for followup in self.definition.followup_audio_files or ():
            if isinstance(followup, str):
                tracks.append(os.path.join(self.audio_dir, followup))
            else:
                filename, options = followup
                tracks.append((os.path.join(self.audio_dir, filename), options))
        return PlaybackController(
            tracks, player_options=self.definition.player_options
        )

    async def _run_movement(
        self,
        movement: MovementSpec,
        playback: PlaybackController,
        gate: asyncio.Event | None,
        loop_for_audio: bool,
    ) -> None:
        """Drive one movement's phases, looping the body for the audio duration.

        Runs the optional ``lead_in``, then the ``loop_body`` (once, or repeated
        for the audio duration when ``loop_for_audio`` is set), then the optional
        ``do_return``. When this movement ``supplies_gate`` and a ``gate`` event
        is in play, the event is set the instant its ``lead_in`` completes --
        opening the audio gate with no fixed idle sleep in between (Requirements
        6.1, 6.2). Ungated movements simply proceed into their loop body
        immediately, so they begin at time zero alongside the gated movement's
        lead-in (Requirement 6.3).

        Loop_Until_Audio (Requirements 5.3, 7.1, 7.2, 7.3):

        * When ``loop_for_audio`` is ``False`` the loop body runs exactly once,
          preserving the unflagged-step behaviour (Requirement 7.4).
        * When ``loop_for_audio`` is ``True`` the body repeats while playback is
          active, with ``Playback_Active`` checked **between** whole iterations
          so a started iteration always runs to completion and is never cut off
          mid-move (Requirement 7.2). Once playback is no longer active no new
          iteration starts and the movement proceeds to its return phase
          (Requirement 7.3).

        Per-movement near-end cutoff: when the movement declares
        ``stop_loop_lead_seconds``, the loop ALSO stops starting new iterations
        once ``playback.will_finish_within(stop_loop_lead_seconds)`` is true --
        i.e. the audio will end within that lead time -- so ``do_return`` has
        room to run and the movement returns to rest before the track ends. This
        is per-movement: a movement with ``stop_loop_lead_seconds is None``
        (the default) is unaffected and loops until audio is fully inactive.
        Like the audio-done check, it is evaluated only between whole iterations,
        so the in-progress swing always completes; and because
        ``will_finish_within`` returns ``False`` when the duration is unknown or
        audio has not started, an unknowable duration falls back to the plain
        loop-until-inactive behaviour and never stops the movement early.

        Gate-vs-loop-start ordering: for a gated performance ``is_active()`` is
        ``False`` until the gate opens and the audio thread comes alive, and
        ungated movements begin looping at time zero while the gated movement is
        still in its ``lead_in``. A raw ``while playback.is_active():`` guard
        would therefore exit before audio ever starts. Instead the loop keeps
        going while audio *has not started yet* (``not playback.has_started()``)
        OR is *currently active* (``playback.is_active()``), and terminates only
        once audio has both started and finished. Every looping movement runs at
        least one iteration, and none exits before the track begins.

        Args:
            movement: The movement whose phases to run.
            playback: The single playback controller for the performance. Its
                ``has_started()`` / ``is_active()`` state is the source of truth
                for the loop-until-audio guard (Requirement 7.1), and its
                ``will_finish_within()`` drives the optional per-movement
                near-end cutoff.
            gate: The audio gate event, or ``None`` when audio is not gated on a
                movement (``definition.gate is None``). Only the gate-supplying
                movement sets it.
            loop_for_audio: When ``True`` (the movement's step is flagged),
                repeat the loop body for the audio duration; when ``False`` run
                it exactly once (Requirement 7.4).
        """
        if movement.lead_in is not None:
            await movement.lead_in()

        # Open the gate the moment the gate-supplying movement's lead_in
        # finishes -- no fixed idle sleep between the signal and the eventual
        # PlaybackController.start() done by the gate task (Requirements 6.1,
        # 6.2). A movement with no lead_in that supplies the gate opens it at
        # time zero.
        if movement.supplies_gate and gate is not None:
            print(f"[performance] gate opened by '{movement.name}'")
            gate.set()

        if not loop_for_audio:
            # Unflagged step: the body runs exactly once (Requirement 7.4).
            await movement.loop_body()
        else:
            # Loop_Until_Audio: run at least one iteration, then repeat while
            # playback has not started yet OR is still active. Playback_Active
            # is checked only here, between whole iterations, so an in-progress
            # iteration always completes (Requirements 5.3, 7.1, 7.2, 7.3). The
            # "has not started yet" clause keeps the loop alive across the gated
            # lead-in window so it never exits before audio begins.
            while True:
                await movement.loop_body()
                if playback.has_started() and not playback.is_active():
                    # Audio fully done: no new iteration, proceed to do_return.
                    break
                elif (
                    movement.stop_loop_lead_seconds is not None
                    and playback.will_finish_within(movement.stop_loop_lead_seconds)
                ):
                    # Near the end: this movement declared a lead time, and the
                    # track will finish within it. Stop starting NEW iterations
                    # so do_return has time to run before the audio ends. The
                    # in-progress iteration already completed above; checked only
                    # between whole iterations, never mid-move.
                    print(
                        f"[performance] '{movement.name}' stopping loop "
                        f"~{movement.stop_loop_lead_seconds}s before audio ends"
                    )
                    break

        if movement.do_return is not None:
            await movement.do_return()

    async def _run_step(
        self,
        step: PerformanceStep,
        playback: PlaybackController,
        gate: asyncio.Event | None,
    ) -> None:
        """Run one step's concurrent group to completion.

        Every ``MovementSpec`` in the group runs concurrently via
        ``asyncio.gather``; the step completes only when all members finish
        (Requirements 2.1, 4.1). Because the members own disjoint channels
        (validated up front), interleaving their awaits drives distinct servos
        with no contention. The gate event (if any) and the playback controller
        are threaded through to each movement so a gate-supplying movement can
        signal audio start (Requirement 6.3).

        The step's ``loop_for_audio`` flag is threaded through to every member
        so a flagged step loops its bodies for the audio duration while an
        unflagged step runs each body exactly once (Requirement 7.4).

        Args:
            step: The performance step to run.
            playback: The single playback controller for the performance.
            gate: The audio gate event, or ``None`` when audio is not gated.
        """
        await asyncio.gather(
            *(
                self._run_movement(movement, playback, gate, step.loop_for_audio)
                for movement in step.group.movements
            )
        )

    async def _return_to_rest(self, phase: str) -> None:
        """Best-effort: drive every servo to its safe rest position.

        Mirrors ``Animatronic._safe_rest`` / ``controller``'s recovery path but
        stays inside the already-running event loop: ``run()`` is a coroutine
        (project async convention), so this awaits the shared
        ``TrunkController.return_to_rest()`` directly rather than spinning up a
        fresh ``asyncio.run`` loop. ``return_to_rest`` itself drives every
        configured channel to its ``REST_POSITIONS`` value and never raises, so
        residual channels a movement's ``do_return`` did not cover are still
        brought home (Requirements 8.1, 8.3, 10.4).

        This is a recovery/cleanup path and must never mask a real error: any
        unexpected failure while resting is caught and logged, not propagated
        (Requirement 8.4).

        Args:
            phase: Human-readable label for what was running when rest was
                requested (e.g. ``"step 2"`` or ``"final cleanup"``), used only
                for debug output.
        """
        try:
            await self.movements.trunkController.return_to_rest()
        except Exception as error:  # noqa: BLE001 - recovery path, never mask
            print(f"[performance] return_to_rest failed during {phase}: {error}")

    async def _start_audio_when_gated(
        self, playback: PlaybackController, gate: asyncio.Event
    ) -> None:
        """Await the gate signal, then start audio immediately.

        This coordinator runs concurrently with the movements. It blocks only on
        the gate ``Event`` -- there is no fixed idle ``sleep`` between the gate
        being set and ``PlaybackController.start()`` -- so audio begins the
        instant the gate-supplying movement's ``lead_in`` completes and never
        before (Requirements 6.1, 6.2).

        Args:
            playback: The controller to start once the gate opens.
            gate: The gate event set by the gate-supplying movement.
        """
        await gate.wait()
        playback.start()

    async def _stop_ambient(self, ambient_task: "asyncio.Task[None] | None") -> None:
        """Cancel and await the optional ambient task, swallowing cancellation.

        Mirrors the defensive gate-task lifecycle: called after the steps on
        both the normal-completion and exception paths. If the ambient task is
        still running it is cancelled; the task is then awaited so its own
        cleanup (e.g. an eye blinker turning its LED off and closing it in a
        ``finally`` block) completes before ``run()`` returns. ``CancelledError``
        from the cancellation, and any error the ambient itself raised, are
        swallowed so the ambient never leaks and never masks the performance's
        own result. A ``None`` task (no ambient configured) is a no-op.

        Args:
            ambient_task: The concurrent ambient task, or ``None`` when the
                performance declared no ambient coroutine.
        """
        if ambient_task is None:
            return
        if not ambient_task.done():
            ambient_task.cancel()
        try:
            await ambient_task
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001 - ambient must never mask result
            print(f"[performance] ambient task error (ignored): {error}")

    async def run(self) -> None:
        """Execute the performance: validate, gate audio, run each step in order.

        Validates every group's Channel_Ownership before issuing any motion,
        then runs the ``PerformanceStep``s in declared order -- each step fully
        completing before the next begins -- with the final step ending the
        performance (Requirements 2.2, 2.3, 2.4, 4.2, 10.1).

        Audio start is gated on movement progress (Requirements 6.1-6.4):

        * ``definition.gate is None`` -- start the single ``PlaybackController``
          at time zero, before any movement runs (Requirement 6.4).
        * ``definition.gate`` set -- run an ``asyncio.Event`` gate task alongside
          the steps; the gate-supplying movement sets the event when its
          ``lead_in`` completes, and the gate task starts audio immediately, with
          no fixed idle sleep in between (Requirements 6.1, 6.2). Ungated
          movements start their loops at time zero, concurrently with that
          lead-in (Requirement 6.3).

        Loop_Until_Audio (Requirements 5.3, 7.1-7.4): a step flagged
        ``loop_for_audio`` repeats each of its movements' loop bodies while
        ``PlaybackController`` playback is active -- checking between whole
        iterations so a started iteration always completes -- and stops issuing
        new iterations once playback ends; an unflagged step runs each body
        exactly once.

        Ambient task lifecycle: when the runner was constructed with an
        ``ambient`` callable, ``run()`` launches it as a concurrent task right
        after playback is set up (before the steps), passing it the
        ``PlaybackController`` so the ambient can bind itself to the audio window,
        so it runs alongside the steps. Once the steps finish -- on BOTH the
        normal and exception paths -- the task is cancelled if still running and
        awaited (swallowing ``CancelledError``) via ``_stop_ambient`` as a SAFETY
        NET, so it never leaks and its own cleanup (e.g. an eye blinker's LED-off
        ``finally``) always runs before ``run()`` returns. The runner treats the
        coroutine as opaque; it just hands it the playback handle.

        Return-to-rest / safe-rest recovery (Requirements 8.1, 8.3, 8.4, 10.4):
        each movement's own ``do_return`` runs inside ``_run_movement``; between
        sequential steps driven channels are returned to a known rest before the
        next step's lead-in; on normal completion any residual channels are
        swept home via ``TrunkController.return_to_rest()``. The whole step loop
        is wrapped so any phase failure drives all servos to safe rest (mirroring
        ``Animatronic._safe_rest``) and the error is re-raised, never swallowed.
        """
        # Fail fast on any channel-ownership violation BEFORE any servo moves.
        self._validate_channel_ownership()

        print(f"[performance] starting '{self.definition.name}'")

        playback = self._build_playback()

        # No gate ⇒ audio starts at time zero, before any movement (Req 6.4).
        # With a gate ⇒ a concurrent task waits on the gate Event and starts
        # audio the instant it is set (Reqs 6.1-6.3). The gate task is created
        # before the steps so it is already waiting when the gate opens.
        gate: asyncio.Event | None = None
        gate_task: asyncio.Task[None] | None = None
        if self.definition.gate is None:
            playback.start()
        else:
            gate = asyncio.Event()
            gate_task = asyncio.create_task(
                self._start_audio_when_gated(playback, gate)
            )

        # Optional ambient coroutine: runs CONCURRENTLY with the steps (e.g.
        # hypnotic's eye blink bound to the audio window). Created here -- before
        # the steps, mirroring the gate task's lifecycle -- so it is already
        # running alongside the very first step. It receives the
        # PlaybackController so it can bind itself to the audio window
        # (has_started()/is_active()). It is cancelled and awaited after the
        # steps on BOTH the normal and exception paths (see the finally-style
        # handling below) as a SAFETY NET, so it never leaks and its finally
        # block (LED off) always runs. run() knows nothing about what it does.
        ambient_task: asyncio.Task[None] | None = None
        if self.ambient is not None:
            ambient_task = asyncio.create_task(self.ambient(playback))

        # Run the steps in order. The whole body is wrapped so that ANY failure
        # in a phase (lead_in, loop_body, do_return) drives every servo to safe
        # rest before the error propagates -- a stalled servo must never be left
        # energized against a jam (Requirements 8.4, 10.4). The error is logged
        # with its offending step and re-raised, never swallowed silently.
        total = len(self.definition.steps)
        try:
            for index, step in enumerate(self.definition.steps):
                print(f"[performance] step {index + 1}/{total}")
                await self._run_step(step, playback, gate)

                # Between sequential steps, bring driven channels back to a known
                # rest before the next step's lead-in, so a step never inherits
                # the previous step's pose (Requirements 8.1, 8.3). Skipped after
                # the final step, which falls through to the residual rest below.
                if index + 1 < total:
                    await self._return_to_rest(f"inter-step rest after step {index + 1}")
        except Exception as error:
            # A phase raised. Drive all servos to safe rest, log the offending
            # phase, cancel any ambient task, then re-raise so callers still see
            # the failure (Req 8.4). The ambient task is stopped here too so its
            # cleanup (e.g. LED off) runs even when the performance fails.
            print(f"[performance] error during '{self.definition.name}': {error}")
            await self._return_to_rest("exception recovery")
            await self._stop_ambient(ambient_task)
            raise
        else:
            # Normal completion: after each movement's own do_return has run,
            # sweep any residual channels home so every channel ends at its rest
            # position (Requirements 8.1, 8.3, 10.4).
            await self._return_to_rest("final cleanup")
            # Stop the ambient task so it never outlives the performance and its
            # finally block (LED off) runs before run() returns.
            await self._stop_ambient(ambient_task)

        # Ensure the gate task is resolved before returning. Under normal flow
        # the gate has been set by its supplying movement; awaiting here simply
        # collects the completed task. Cancel defensively if a definition names
        # a gate no movement ever supplies, so run() never hangs.
        if gate_task is not None:
            if gate is not None and gate.is_set():
                await gate_task
            elif not gate_task.done():
                print(
                    "[performance] gate never opened; cancelling audio start "
                    f"for '{self.definition.name}'"
                )
                gate_task.cancel()

        print(f"[performance] finished '{self.definition.name}'")
