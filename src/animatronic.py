"""
animatronic.py

Top-level named routines that pair servo gestures with audio playback.

Each public method on Animatronic calls run_action_and_audio(), which starts
audio via AudioPlayer in a background thread then runs the named async
coroutine to completion.  For live mic passthrough, AudioStreamer handles
both input capture and jaw-sync output.

Gesture coroutines on this class are thin wrappers — they add an idle delay
(so audio starts before movement) then delegate entirely to Movements.  All
composition logic lives in movements.py, not here.

Usage (run as root for GPIO / audio hardware):
    sudo /usr/bin/python3 animatronic.py --action=<action_name>

Available actions — see action_map in main() for the full list.

Note on asyncio:
    asyncio.run() is called inside run_action_and_audio() so each routine
    gets a fresh event loop.  Never call asyncio.run() from within a
    running event loop.
"""

from movements import Movements
from audio_player import AudioPlayer
from audio_streamer import AudioStreamer
from servo_lock import servo_lock, ServoBusyError, BUSY_EXIT_CODE
from performance import (
    ConcurrentGroup,
    GateSpec,
    MovementSpec,
    PerformanceDefinition,
    PerformanceRunner,
    PerformanceStep,
)
import nap_signal
import constants
from range_sensor import ApproachDetector
import asyncio
import threading
import argparse
import random
import sys
import os
import time
import wave


class Animatronic:
    """Pairs named audio tracks with matching servo gesture routines."""

    # Audio directory — resolves to the repo's own ``audio/`` directory (the
    # source of truth, version-controlled), computed relative to this module so
    # it is identical regardless of the invoking user (sudo/pi/aaron). This
    # removes the old ~/Music deploy step. Override with ANIMATRONIC_AUDIO_DIR.
    @staticmethod
    def _resolve_audio_dir():
        override = os.environ.get('ANIMATRONIC_AUDIO_DIR')
        if override:
            return override
        # <repo>/audio, where this module lives at <repo>/src/animatronic.py.
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'audio')



    # Audio file list — indices referenced by the routine methods below.
    music = [
        'beetel-exorcist.wav',     # 0
        'blah.wav',                # 1
        'krusty-laugh.wav',        # 2
        'sb_party_switch.wav',     # 3
        'spongebob-torture.wav',   # 4
        'vader-beaten.wav',        # 5
        'vader-father.wav',        # 6
        'were-waiting.wav',        # 7
        'yoda-900.wav',            # 8
        None,                      # 9  (removed: yoda / yoda-agent-evil.wav)
        'yoda-fear.wav',           # 10
        None,                      # 11 (removed: hello / hello-everyone.wav)
        None,                      # 12 (removed: happyHalloween / happy-halloween.wav)
        None,                      # 13 (removed: niceDay / walk.wav)
        None,                      # 14 (removed: howYallDoin / how-yall.wav)
        None,                      # 15 (removed: cantHear / cant-hear.wav)
        'evil-laugh.wav',          # 16
        'vincent-price-laugh.wav', # 17
        None,                      # 18 (removed: owl / owl.wav)
        'yawn.wav',                # 19
        'brains.wav',              # 20
        'hypnotic.wav',            # 21
        'snore.wav',               # 22
        'more_candy.wav',          # 23
        'sb_snore.wav',            # 24
        'snuck_up.wav',            # 25  (snuckUp "you snuck up on me!" reaction)
        'awakened.wav',            # 26  (awaken groggy "just woke up" reaction)
    ]

    # Seconds to pause before movement begins, giving audio time to start.
    idle = 3

    # ------------------------------------------------------------------ #
    # Gesture coroutines — thin wrappers over Movements                   #
    # Each one: (1) waits idle seconds, (2) delegates to Movements.       #
    # ------------------------------------------------------------------ #

    async def _run(self, coro):
        """Wait idle seconds then run a Movements coroutine.

        Args:
            coro: An awaitable returned by a Movements method.
        """
        await asyncio.sleep(self.idle)
        await coro

    async def _run_quick(self, coro):
        """Wait 1 second then run a Movements coroutine (shorter lead-in).

        Args:
            coro: An awaitable returned by a Movements method.
        """
        await asyncio.sleep(1)
        await coro

    async def _run_lead(self, coro, seconds):
        """Wait ``seconds`` then run a Movements coroutine (custom lead-in).

        Use for short audio clips where the default 3s idle would let the sound
        finish before the gesture even starts.

        Args:
            coro: An awaitable returned by a Movements method.
            seconds: Lead-in delay before the gesture begins.
        """
        await asyncio.sleep(seconds)
        await coro

    # ------------------------------------------------------------------ #
    # Core audio + movement runner                                         #
    # ------------------------------------------------------------------ #

    def run_action_and_audio(self, method_name, audio_file, audio_delay=0.0):
        """Play an audio file via AudioPlayer while running a gesture coroutine.

        AudioPlayer runs in a background thread so the gesture coroutine can
        start immediately after the idle delay.  The thread is joined after
        the coroutine completes so resources are always cleaned up.

        Args:
            method_name: Name of an async method on this class (e.g. '_do_wave').
            audio_file:  Filename (not full path) of the audio file in audio_dir.
            audio_delay: Seconds to GATE (delay) audio start after the gesture
                begins. The audio thread sleeps this long before playing, so the
                motion leads and the sound comes in ``audio_delay`` seconds later
                — the simple-runner analogue of the Performance Framework's
                GateSpec. Default 0.0 (audio starts immediately, ungated).
        """
        audio_path = os.path.join(self._resolve_audio_dir(), audio_file)
        player = AudioPlayer()

        def _play():
            # Gate: hold the audio for audio_delay seconds so the gesture leads.
            if audio_delay > 0:
                time.sleep(audio_delay)
            player.play_audio_file(audio_path)

        audio_thread = threading.Thread(target=_play, daemon=True)
        audio_thread.start()
        print(f"Playing audio: {audio_path} (gated {audio_delay}s)")
        try:
            asyncio.run(getattr(self, method_name)())
        except Exception as e:
            print(f"Error during gesture '{method_name}': {e}")
            # SAFETY: a gesture that raised (e.g. I2C brownout from a stalled
            # servo) may have left a servo energized against a mechanical jam.
            # Drive everything back to safe resting positions before returning.
            self._safe_rest()
        finally:
            audio_thread.join(timeout=2)

    @staticmethod
    async def _blink_eyes(playback, on_time=0.25, off_time=0.25):
        """Blink the eye LED, bound to the audio window, until audio ends.

        Owns ``EYE_LIGHT_PIN`` independently of ``AudioPlayer`` so it can drive
        the eyes on a fixed rhythm that is NOT tied to the audio envelope. Used
        by ``hypnotic`` as the runner's ambient task while audio plays with the
        AudioPlayer's eye/jaw drive disabled (so this blinker owns the pin
        without a gpiozero "pin already in use" clash).

        Rather than blinking for the whole performance, this blink is bound to
        the AUDIO window via the ``playback`` controller:

        1. WAIT FOR AUDIO START: before blinking, poll ``playback.has_started()``
           with a short ``asyncio.sleep(0.02)`` loop so the first blink is delayed
           until audio actually begins (the hypnotic gate is ~100ms). The blink
           thus STARTS with the audio, not at t=0.
        2. BLINK WHILE ACTIVE: loop while ``playback.is_active()`` --
           on()/sleep(on_time)/off()/sleep(off_time) -- checking ``is_active()``
           only between whole on/off cycles so a cycle is never cut mid-blink.
           When audio finishes (``is_active()`` is ``False``) the blink STOPS,
           ending WITH the audio even though the performance may still be
           retracting/recentering.

        The performance runner also cancels this task at performance end as a
        SAFETY NET, so ``CancelledError`` is caught (this also makes the
        wait-for-start loop safe if audio never starts -- it will simply be
        cancelled). In a ``finally`` block the LED is turned off and closed so the
        eyes are always left off and the pin is released. ``gpiozero.LED`` is
        imported and constructed INSIDE this function so importing this module in
        a non-Pi/test environment never requires the pin to exist; if the pin is
        unavailable the blinker logs and no-ops rather than crashing the routine.

        Args:
            playback: The performance's ``PlaybackController``. Its
                ``has_started()`` gates the first blink and its ``is_active()``
                bounds the blink to the audio window.
            on_time: Seconds the eyes stay lit each cycle. Defaults to 0.25.
            off_time: Seconds the eyes stay dark each cycle. Defaults to 0.25.
        """
        try:
            from gpiozero import LED
            led = LED(constants.EYE_LIGHT_PIN)
        except Exception as e:
            print(f"_blink_eyes: could not acquire eye LED, blinking disabled: {e}")
            return

        try:
            # WAIT FOR AUDIO START: hold until playback begins (~100ms gate) so
            # the blink starts WITH the audio, not at t=0. Cancellable by the
            # runner if audio never starts.
            while not playback.has_started():
                await asyncio.sleep(0.02)

            # BLINK WHILE ACTIVE: check is_active() only between whole on/off
            # cycles so a cycle is never cut mid-blink; stop when audio ends.
            while playback.is_active():
                led.on()
                await asyncio.sleep(on_time)
                led.off()
                await asyncio.sleep(off_time)
        except asyncio.CancelledError:
            pass
        finally:
            led.off()
            led.close()

    @staticmethod
    def _safe_rest():
        """Best-effort: return all servos to safe rest after a failed gesture.

        Runs its own event loop since the gesture's asyncio.run() loop is gone
        by the time we get here. Never raises — this is a recovery path.
        """
        try:
            asyncio.run(Movements.trunkController.return_to_rest())
        except Exception as e:
            print(f"_safe_rest failed: {e}")

    # ------------------------------------------------------------------ #
    # Named routines — gesture + audio pairings                           #
    # ------------------------------------------------------------------ #

    # --- Wave routines ---

    def start_party(self):
        """Party switch audio — smooth wave + swivel head (returns to rest)."""
        self.run_action_and_audio("_do_wave_and_swivel_smooth", self.music[3])

    # --- Beckon routines ---

    def waiting(self):
        """"We're waiting" audio — beckon + look around."""
        self.run_action_and_audio("_do_come_and_look", self.music[7])

    def exorcist(self):
        """Exorcist audio — beckon + look around."""
        self.run_action_and_audio("_do_come_and_look", self.music[0])

    def vader_father(self):
        """"I am your father" audio — beckon + look around."""
        self.run_action_and_audio("_do_come_and_look", self.music[6])

    def torture(self):
        """SpongeBob torture audio — beckon + look around."""
        self.run_action_and_audio("_do_come_and_look", self.music[4])

    # --- Patrol / ambient routines ---

    def krusty(self):
        """Krusty laugh audio — neck ellipse."""
        self.run_action_and_audio("_do_neck_ellipse", self.music[2])

    def vader_beaten(self):
        """Vader beaten audio — patrol (ellipse + small look)."""
        self.run_action_and_audio("_do_patrol", self.music[5])

    # --- Reaction routines ---

    def blah(self):
        """"Blah" audio — concurrent randomized head shake + palm-present arm.

        Like ``brains``, ``blah`` is driven by the Performance_Framework rather
        than ``run_action_and_audio``: it declares a single-step
        ``PerformanceDefinition`` whose concurrent group runs the randomized head
        shake (neck channels 0-1) and the palm-present forearm bob (arm channels
        4-7) at the same time, and loops both for the duration of ``blah.wav``.

        The two movements own disjoint channels ({0,1} vs {4,5,6,7}), so the
        group is valid. Audio is GATED to start 250ms AFTER the routine begins:
        the head shake supplies the gate (``gate=GateSpec("head_shake")`` +
        ``supplies_gate=True``), and its ``shake_no_lead_in`` sleeps 250ms before
        completing — the framework starts audio the instant that lead-in
        finishes, so playback begins at t≈0.25s. The palm-present arm is ungated
        (``supplies_gate=False``), so its lead-in raises the arm at t=0. Both
        loop bodies repeat while playback is active — the head keeps sweeping and
        the forearm keeps bobbing — then both return to rest (neck recenters, arm
        lowers), with the runner sweeping any residual channels home on
        completion or failure.

        A single ``Movements`` instance backs every ``MovementSpec`` phase
        callable so the neck and arm adapters share one ``TrunkController``. The
        runner is a coroutine, launched with ``asyncio.run`` here at the top of
        the call stack (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        BLAH = PerformanceDefinition(
            name="blah",
            audio_file=self.music[1],            # blah.wav
            # Head shake supplies the gate: its lead-in sleeps 250ms before
            # completing, so audio starts ~250ms after the routine begins.
            gate=GateSpec(movement_name="head_shake"),
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="head_shake",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                            }),
                            lead_in=mv.shake_no_lead_in,      # 250ms gate + center
                            loop_body=mv.shake_no_loop_body,  # pan sweep + tilt centering (82-98)
                            do_return=mv.shake_no_return,     # neck to center
                            supplies_gate=True,               # opens the audio gate
                        ),
                        MovementSpec(
                            name="present_palm",
                            owned_channels=frozenset({
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.present_palm_lead_in,      # raise arm (t=0)
                            loop_body=mv.present_palm_loop_body,  # one gentle bob
                            do_return=mv.present_palm_return,     # lower arm
                            supplies_gate=False,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(BLAH, mv, audio_dir).run())

    def yoda_fear(self):
        """Yoda fear audio — beckon + look around."""
        self.run_action_and_audio("_do_come_and_look", self.music[10])

    # --- New gesture routines ---

    def evil_laugh(self):
        """Evil laugh audio — wave + swivel head."""
        self.run_action_and_audio("_do_wave_and_swivel", self.music[16])

    def vincent_price(self):
        """Vincent Price laugh audio — smooth reach + flowing head look-around.

        Uses the eased ``reach_and_look_smooth`` gesture so the arm and head move
        smoothly and flow for the whole laugh, then return to rest.
        """
        self.run_action_and_audio("_do_reach_and_look_smooth", self.music[17])

    def yawn(self):
        """Yawn audio + cover-mouth gesture (jaw syncs to the yawn.wav).

        Audio is GATED 300ms: the cover-mouth gesture leads and the yawn sound
        comes in 0.3s later, so the arm is already rising when the yawn begins.
        """
        self.run_action_and_audio("_do_yawn", self.music[19], audio_delay=0.3)

    def snuck_up(self):
        """Snuck-up reaction — head jerk + arm recoil + a "snuck up" gasp.

        The Routine the napping Mode runs when the proximity sensor wakes it
        (see ``_startle``), also runnable on its own via ``--action=snuckUp``.
        Pairs the ``snuck_up`` gesture with ``snuck_up.wav``.
        """
        self.run_action_and_audio("_do_snuck_up", self.music[25])  # snuck_up.wav

    def awaken(self):
        """Awaken reaction — groggy stir + lazy head bob, paired with awakened.wav.

        The Routine the napping Mode runs when it is interrupted (see
        ``_startle``), also runnable on its own via ``--action=awaken``. Reuses
        ``snuck_up``'s arm motion with the shoulder channels halved (a sleepy
        stir, not a startle) and lolls the head around lazily until the
        ``awakened.wav`` audio finishes, then lowers the arm to rest. Ungated:
        motion and audio start together.
        """
        self.run_action_and_audio("_do_awaken", self.music[26])  # awakened.wav

    @staticmethod
    def _audio_duration_seconds(audio_file, default=7.0):
        """Return the length of an audio file in seconds (best-effort).

        Used so a routine can drive motion for exactly the clip's duration
        (e.g. awaken's lazy head bob runs until awakened.wav ends). Falls back to
        ``default`` if the file can't be read.

        Args:
            audio_file: Filename (not full path) in the resolved audio dir.
            default:    Seconds to return if the file can't be measured.
        """
        try:
            path = os.path.join(Animatronic._resolve_audio_dir(), audio_file)
            with wave.open(path, 'rb') as wf:
                return wf.getnframes() / float(wf.getframerate())
        except Exception as e:
            print(f"[awaken] could not measure {audio_file} ({e}); "
                  f"using {default}s")
            return default

    # --- Performance-framework routines ---

    def brains(self):
        """"Brains" audio — concurrent menacing reach + head scan, audio-synced.

        Unlike the other routines (which pair one gesture coroutine with an
        AudioPlayer thread via run_action_and_audio), ``brains`` is driven by the
        reusable Performance_Framework: it declares a single-step
        ``PerformanceDefinition`` whose concurrent group runs ``menacing_reach``
        (arm channels 4-7) and ``look_around_random`` (neck channels 0-1) at the
        same time, loops both for the duration of ``brains.wav``, and starts
        audio ungated at t=0 (no gate, no pause).

        The two movements own disjoint channels ({4,5,6,7} vs {0,1}), so the
        concurrent group is valid. Audio is ungated (``gate=None``), so playback
        begins at t=0 alongside both movements — the arm reach and the head scan
        both also begin at t=0 (neither ``supplies_gate``). Both loop bodies
        repeat while playback is active, but with a per-movement cutoff on the
        arm: ``menacing_reach`` sets ``stop_loop_lead_seconds=4.0``, so it stops
        starting new swings once the audio is within ~4s of ending and retracts
        in time (the in-progress swing always completes first). The head scan
        (``look_around_random``) has no such cutoff, so it keeps looping until
        the audio fully ends. Both then return to rest — the arm retracts, the
        neck centers — with the runner sweeping any residual channels home on
        completion or failure.

        A single ``Movements`` instance backs every ``MovementSpec`` phase
        callable so the arm and neck adapters share one ``TrunkController``. The
        runner is a coroutine, so it is launched with ``asyncio.run`` here at the
        top of the call stack (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        BRAINS = PerformanceDefinition(
            name="brains",
            audio_file=self.music[20],  # brains.wav — ~15.2 s
            gate=None,                  # ungated: audio starts at t=0
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="menacing_reach",
                            owned_channels=frozenset({
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.menacing_reach_lead_in,      # reach out
                            loop_body=mv.menacing_reach_loop_body,  # one menace swing
                            do_return=mv.menacing_reach_return,     # retract
                            supplies_gate=False,
                            # Stop starting new swings once brains.wav (~15.2s)
                            # is within 4s of ending, so the last swing plus the
                            # ~40%-faster retract finish before the audio does.
                            stop_loop_lead_seconds=4.0,
                        ),
                        MovementSpec(
                            name="look_around_random",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                            }),
                            lead_in=None,                     # starts at t=0
                            loop_body=mv.look_scan_loop_body, # one random glance
                            do_return=mv.look_scan_return,    # neck to center
                            supplies_gate=False,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(BRAINS, mv, audio_dir).run())

    def more_candy(self):
        """"More candy" audio — jittery sugar-rush shakes, audio-synced.

        Driven by the Performance_Framework: a single-step
        ``PerformanceDefinition`` whose concurrent group runs FOUR independent
        single-joint randomized-centering shakes at a quick, jittery tempo so
        the character reads as over-sugared / trembling:

            - elbow tilt (ch5) oscillating in [160, 190] (peak 175)
            - shoulder rotator (ch7) in [35, 45] (peak 40)
            - neck tilt (ch1) in [80, 100] (peak 90)
            - neck pan (ch0) in [85, 95] (peak 90)

        The four movements own disjoint channels — the elbow movement also owns
        the static elbow rotator (ch4), and the shoulder movement the static
        shoulder tilt (ch6), so every arm channel is covered: {4,5} / {6,7} /
        {1} / {0}. All bodies loop for the duration of ``more_candy.wav``, then
        each returns its channels to rest.

        The elbow-tilt band (160-190) sits ABOVE the global RT_ELBOW_TILT ceiling
        (160); it is operator bench-verified safe in this upright-forearm pose,
        so the elbow movement widens just that channel via verified_pose_override
        held across its lead-in -> loop -> return span.

        Audio is gated 250ms: the elbow movement ``supplies_gate`` and its
        lead-in sleeps 250ms before completing, so ``more_candy.wav`` starts
        ~250ms after the routine begins (the other three shakes begin at t=0).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        MORE_CANDY = PerformanceDefinition(
            name="more_candy",
            audio_file=self.music[23],  # more_candy.wav
            # Elbow shake supplies the gate: its lead-in sleeps 250ms before
            # completing, so audio starts ~250ms after the routine begins.
            gate=GateSpec(movement_name="candy_elbow"),
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="candy_elbow",
                            owned_channels=frozenset({
                                constants.RT_ELBOW_ROTATOR,
                                constants.RT_ELBOW_TILT,      # 4,5
                            }),
                            lead_in=mv.more_candy_elbow_lead_in,      # 250ms gate + start
                            loop_body=mv.more_candy_elbow_loop_body,  # one quick shake
                            do_return=mv.more_candy_elbow_return,     # lower + release override
                            supplies_gate=True,                       # opens the audio gate
                        ),
                        MovementSpec(
                            name="candy_shoulder",
                            owned_channels=frozenset({
                                constants.RT_SHOULDER_TILT,
                                constants.RT_SHOULDER_ROTATOR,  # 6,7
                            }),
                            lead_in=mv.more_candy_shoulder_lead_in,      # start (t=0)
                            loop_body=mv.more_candy_shoulder_loop_body,  # one quick shake
                            do_return=mv.more_candy_shoulder_return,     # lower arm
                            supplies_gate=False,
                        ),
                        MovementSpec(
                            name="candy_neck_tilt",
                            owned_channels=frozenset({constants.NECK_TILT}),  # 1
                            lead_in=mv.more_candy_neck_tilt_lead_in,      # start (t=0)
                            loop_body=mv.more_candy_neck_tilt_loop_body,  # one quick shake
                            do_return=mv.more_candy_neck_tilt_return,     # tilt to level
                            supplies_gate=False,
                        ),
                        MovementSpec(
                            name="candy_neck_pan",
                            owned_channels=frozenset({constants.NECK_PAN}),  # 0
                            lead_in=mv.more_candy_neck_pan_lead_in,      # start (t=0)
                            loop_body=mv.more_candy_neck_pan_loop_body,  # one quick shake
                            do_return=mv.more_candy_neck_pan_return,     # pan to center
                            supplies_gate=False,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(MORE_CANDY, mv, audio_dir).run())

    def hypnotic(self):
        """"Hypnotic" audio — arm sway + head sway, jaw OFF, eyes blink steadily.

        Like ``brains``, ``hypnotic`` is driven by the Performance_Framework: it
        declares a single-step ``PerformanceDefinition`` whose concurrent group
        runs a PARAMETERIZED brains-style arm (channels 4-7) and a gentle limited
        head sway (neck channels 0-1) at the same time, looping both for the
        duration of ``hypnotic.wav``. It borrows brains' arm sway + neck scan
        machinery but with a different arm pose (rotator 230, shoulder-tilt sway
        within [0, 30]), a limited neck range (+/-10 from center on both axes),
        and a 100ms audio gate. It does NOT change ``brains`` — it uses
        parameterized ``hypnotic_arm_*`` / ``hyp_scan_*`` adapters with their own
        pose, band, state, and bounds.

        Audio behaviour, UNIQUE to hypnotic among the routines: the definition
        sets ``player_options={"drive_jaw": False, "drive_eyes": False}`` so the
        ``AudioPlayer`` plays the track with the jaw motor silent and does NOT
        claim ``EYE_LIGHT_PIN``. Instead the eyes are driven by the runner's
        AMBIENT task -- ``_blink_eyes(pb, 0.25, 0.25)`` -- which blinks them on a
        0.25s-on / 0.25s-off cadence (twice as fast) INDEPENDENT of the audio
        envelope, but BOUND TO THE AUDIO WINDOW: the blink STARTS when the audio
        starts (~100ms gate) and STOPS when the audio ends, not with the whole
        performance (which may still be retracting/recentering afterward). The
        ambient factory receives the ``PlaybackController`` so the blinker can
        consult ``has_started()`` / ``is_active()``. Because the AudioPlayer
        released the eye pin, the blinker owns it cleanly. The runner also cancels
        the ambient task when the performance ends as a safety net, and the
        blinker turns the eyes off (and releases the pin) in its ``finally``
        block. Every OTHER routine keeps the default player (jaw + envelope-driven
        eyes) and no ambient task.

        The two movements own disjoint channels ({4,5,6,7} vs {0,1}), so the
        concurrent group is valid. Audio is GATED to start 100ms AFTER the routine
        begins: the head sway supplies the gate (``gate=GateSpec("hyp_head_sway")``
        + ``supplies_gate=True``), and its ``hyp_scan_lead_in`` sleeps 100ms
        before completing — the framework starts audio the instant that lead-in
        finishes, so playback begins at t≈0.1s. The arm is ungated
        (``supplies_gate=False``), so its lead-in reaches out at t=0.

        Because ``hypnotic.wav`` is SHORT (~5.05s), the arm sets
        ``stop_loop_lead_seconds=1.5`` so it stops starting new sways once the
        audio is within ~1.5s of ending and its last sway (~1s) plus the
        ~40%-faster retract (~0.8s) finish before the audio does. The head sway
        has no cutoff, so it keeps looping until the audio fully ends. Both then
        return to rest — the arm retracts, the neck centers — with the runner
        sweeping any residual channels home on completion or failure.

        A single ``Movements`` instance backs every ``MovementSpec`` phase
        callable so the arm and neck adapters share one ``TrunkController``. The
        runner is a coroutine, launched with ``asyncio.run`` here at the top of
        the call stack (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        HYPNOTIC = PerformanceDefinition(
            name="hypnotic",
            audio_file=self.music[21],  # hypnotic.wav — ~5.05 s
            # Head sway supplies the gate: its lead-in sleeps 100ms before
            # completing, so audio starts ~100ms after the routine begins.
            gate=GateSpec(movement_name="hyp_head_sway"),
            # Jaw silent; AudioPlayer does NOT claim the eye pin so the ambient
            # blinker (below) can own EYE_LIGHT_PIN without a clash.
            player_options={"drive_jaw": False, "drive_eyes": False},
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="hypnotic_arm",
                            owned_channels=frozenset({
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.hypnotic_arm_lead_in,      # reach out (t=0)
                            loop_body=mv.hypnotic_arm_loop_body,  # one sway
                            do_return=mv.hypnotic_arm_return,     # retract
                            supplies_gate=False,
                            # hypnotic.wav is short (~5.05s); stop starting new
                            # sways within 1.5s of the end so the last sway (~1s)
                            # plus the ~40%-faster retract (~0.8s) finish in time.
                            stop_loop_lead_seconds=1.5,
                        ),
                        MovementSpec(
                            name="hyp_head_sway",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                            }),
                            lead_in=mv.hyp_scan_lead_in,      # 100ms gate + center
                            loop_body=mv.hyp_scan_loop_body,  # one gentle glance
                            do_return=mv.hyp_scan_return,     # neck to center
                            supplies_gate=True,               # opens the audio gate
                        ),
                    )),
                ),
            ),
        )

        # Ambient task: 0.25s/0.25s eye blink BOUND to the audio window -- the
        # factory receives the PlaybackController so the blink starts when the
        # audio starts (~100ms gate) and stops when the audio ends, not with the
        # whole performance. The runner also cancels it at performance end as a
        # safety net; the blinker leaves the eyes off in its finally block.
        asyncio.run(
            PerformanceRunner(
                HYPNOTIC, mv, audio_dir,
                ambient=lambda pb: self._blink_eyes(pb, 0.25, 0.25),
            ).run()
        )

    def snore(self):
        """"Snore" audio — jerky heavy-head drop gates the snore, then sleep.

        Like ``hypnotic``, ``snore`` is driven by the Performance_Framework: it
        declares a single-step ``PerformanceDefinition`` whose single movement
        (``sleep_head``) owns the neck + arm channels
        ({NECK_PAN, NECK_TILT, RT_ELBOW_ROTATOR, RT_SHOULDER_TILT,
        RT_SHOULDER_ROTATOR}) and runs the sleep/snore choreography, looping the
        sleep bob+rock cycle for the duration of ``snore.wav`` (~11.1 s).

        The routine is GATED by the movement itself: ``sleep_snore_lead_in``
        performs the JERKY, heavy-head drop (neck tilt sinking 90 -> 180 with
        random pauses / jerk-ups) and, because it ``supplies_gate=True``, the
        framework starts ``snore.wav`` the instant that lead-in finishes — so the
        snore begins the moment the head has fully dropped "asleep". The loop
        body then repeats a gentle head bob ([170, 180]) + shoulder rotator rock
        ([0, 10]) until the audio ends, and the return phase wakes the figure
        back to rest.

        Audio behaviour: the definition sets
        ``player_options={"drive_jaw": False}`` so the jaw motor is SILENCED
        (a snoring figure's mouth stays shut), while the eyes remain
        envelope-driven (``drive_eyes`` defaults True) — tracking the audio
        envelope as normal. There is no ambient task.

        SAFETY: the sleep pose drives NECK_TILT to 180 (above the global
        SAFE_LIMITS ceiling of 160). That is OPERATOR bench-verified safe in this
        heavy-head-drop pose, so the movement opens ``Movements._SLEEP_OVERRIDE``
        ({NECK_TILT: (30, 180)}) via an AsyncExitStack held across the
        lead-in -> loop -> return span and released in the return phase, so the
        widened clamp never leaks past this routine. The arm holds its REST pose
        (elbow rotator 150, elbow tilt 5, shoulder tilt 55 — all within the
        global limits), so no arm override is needed.

        A single ``Movements`` instance backs every ``MovementSpec`` phase
        callable so all adapters share one ``TrunkController``. The runner is a
        coroutine, launched with ``asyncio.run`` here at the top of the call
        stack (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        SLEEP = PerformanceDefinition(
            name="sleep",
            audio_file=self.music[22],  # snore.wav — ~11.1 s
            # The sleep movement supplies the gate: its lead-in performs the
            # jerky head drop and, on completion, opens the audio gate — so the
            # snore starts the instant the head has fully dropped "asleep".
            gate=GateSpec(movement_name="sleep_head"),
            # Silence the jaw motor for the snore (a snoring figure's mouth stays
            # shut); the eyes still track the audio envelope (drive_eyes defaults True).
            player_options={"drive_jaw": False},
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="sleep_head",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,
                                constants.RT_ELBOW_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_SHOULDER_ROTATOR,
                            }),
                            lead_in=mv.sleep_snore_lead_in,     # jerky head drop
                            loop_body=mv.sleep_snore_loop_body,  # one bob+rock
                            do_return=mv.sleep_snore_return,     # wake to rest
                            supplies_gate=True,                  # opens the audio gate
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(SLEEP, mv, audio_dir).run())

    # ------------------------------------------------------------------ #
    # Napping — a MODE (continuous background behaviour until interrupted) #
    # ------------------------------------------------------------------ #

    # Interruption reasons returned by the nap loop.
    NAP_INTERRUPT_TIMEOUT = "timeout"
    NAP_INTERRUPT_SENSOR = "sensor"
    NAP_INTERRUPT_STOP = "stop"       # external stop request (e.g. web app)

    # Snore tracks the nap randomly alternates between, one per sleep segment.
    _NAP_SNORE_TRACKS = ("snore.wav", "sb_snore.wav")

    # Distance (meters) beyond which an object never wakes the nap. An approach
    # is only significant once the object is within this gate AND getting closer
    # across several readings (see ApproachDetector / _poll_nap_sensor).
    NAP_WAKE_GATE_M = 3.0

    # How often (seconds) the nap loop checks the latched sensor flag WHILE a
    # sleep cycle is running, so an approach is caught mid-cycle. This only reads
    # an in-memory flag (the sensor itself is sampled by the detector's own
    # background thread), so it can be frequent and cheap.
    NAP_SENSOR_POLL_INTERVAL_S = 0.1

    def _open_nap_sensor(self):
        """Open the HC-SR04 approach detector for a nap run (best-effort).

        Called once at the start of a nap. Constructs an ``ApproachDetector``
        (gated at ``NAP_WAKE_GATE_M``) and stores it on the instance so
        ``_poll_nap_sensor`` can read it between sleep cycles. If the sensor
        can't be opened (not wired, no GPIO access), logs and leaves the
        detector as None so the nap still runs and simply won't sensor-wake.
        """
        self._nap_detector = None
        try:
            self._nap_detector = ApproachDetector(gate_m=self.NAP_WAKE_GATE_M)
            # Sample in the background so the (blocking) sensor read never stalls
            # the async movement loop; the loop just checks the latched flag.
            self._nap_detector.start_polling()
            print(f"[nap] approach sensor armed (wake if an object approaches "
                  f"within {self.NAP_WAKE_GATE_M} m)")
        except Exception as e:
            print(f"[nap] approach sensor unavailable, no sensor-wake: {e}")
            self._nap_detector = None

    def _close_nap_sensor(self):
        """Release the nap approach detector's GPIO pins (best-effort)."""
        detector = getattr(self, "_nap_detector", None)
        if detector is not None:
            try:
                detector.close()
            except Exception as e:
                print(f"[nap] could not release approach sensor: {e}")
        self._nap_detector = None

    def _poll_nap_sensor(self):
        """Check the HC-SR04 approach detector's latched wake flag.

        Non-blocking: the actual sensor sampling runs in the detector's
        background poller (started in ``_open_nap_sensor``), so this just reads
        the latched flag and is cheap enough to call frequently — including
        BETWEEN individual moves within a sleep cycle for a snappy wake. The
        flag latches True once an *approaching* object is confirmed: three
        consecutive samples each closer than the last, all within
        ``NAP_WAKE_GATE_M`` (objects beyond the gate never trigger). See
        ``range_sensor.ApproachDetector`` for the exact rule.

        Returns:
            True when an approaching object has been confirmed (fire the
            startle), else False. Always False when no sensor is armed.
        """
        detector = getattr(self, "_nap_detector", None)
        if detector is None:
            return False
        return detector.triggered()

    def napping(self, timeout_seconds=60):
        """NAPPING mode: yawn, then snore with the head lowered until interrupted.

        A Mode (per the animation vocabulary) is a continuous background
        behaviour that runs until interrupted. Napping:

        1. Plays the ``yawn`` routine once (cover-mouth gesture + yawn.wav).
        2. Lowers the head "asleep" and loops the sleep bob/rock choreography
           (reusing the ``sleep`` routine's movement primitives) while audio
           plays. Unlike ``sleep`` — which plays snore.wav once — napping keeps
           going and RANDOMLY ALTERNATES the snore track between ``snore.wav``
           and ``sb_snore.wav`` each sleep segment, repeating until interrupted.

        Interruption signals (checked between whole sleep cycles):

        - **Timeout** (``timeout_seconds``, default 60, configurable): the nap
          ends and the head is RAISED exactly as at the end of the ``sleep``
          routine (``sleep_snore_return``).
        - **Sensor** (HC-SR04 approach, see ``_poll_nap_sensor``): when an
          object is confirmed approaching (three consecutive closer readings
          within ``NAP_WAKE_GATE_M``), runs the ``_startle`` response instead of
          the calm wake. Objects farther than the gate never interrupt.
        - **External stop** (``nap_signal`` — e.g. the web app wants to run
          another action): the nap winds down like the timeout case (calm wake),
          then exits so the servo lock frees for the requested action.

        This is NOT built on the Performance_Framework: that framework plays
        exactly one audio track per performance, whereas napping alternates two
        tracks across an open-ended number of segments. So the mode drives the
        audio directly (an ``AudioPlayer`` per segment, jaw silenced) around the
        shared ``sleep_snore_*`` movement primitives.

        Args:
            timeout_seconds: How long to nap before the timeout interruption
                raises the head. Default 60s; configurable via the CLI.
        """
        # Clear any stale stop request from a previous run so we start clean.
        nap_signal.clear_stop()

        # Arm the proximity wake sensor for this nap (best-effort; the nap still
        # runs and simply won't sensor-wake if the sensor can't be opened).
        self._open_nap_sensor()

        # 1. Yawn first (gesture + yawn.wav), reusing the standard runner.
        # Audio gated 300ms so the cover gesture leads the yawn sound.
        print("[nap] yawning before the nap...")
        self.run_action_and_audio("_do_yawn", self.music[19], audio_delay=0.3)  # yawn.wav

        # 2. Head-lowered snore loop until an interruption signal.
        try:
            reason = asyncio.run(self._run_nap_loop(timeout_seconds))
        except Exception as e:
            print(f"[nap] error during nap loop: {e}")
            self._safe_rest()
            self._close_nap_sensor()
            nap_signal.clear_stop()
            return

        # 3. React to how the nap ended.
        if reason == self.NAP_INTERRUPT_SENSOR:
            print("[nap] sensor interrupt -> startle response")
            self._startle()
        else:
            # Timeout or external stop: calm wake, head raised like sleep's end.
            print(f"[nap] {reason} interrupt -> calm wake (head raised)")

        # Release the sensor's GPIO pins so a following routine/startle can use
        # them, and clear the stop signal on exit so the next Mode starts clean
        # and the requesting web-app action can proceed once the lock frees.
        self._close_nap_sensor()
        nap_signal.clear_stop()

    async def _run_nap_loop(self, timeout_seconds):
        """Drive the head-lowered snore loop until interrupted; return the reason.

        Lowers the head "asleep" (``sleep_snore_lead_in``, which also opens the
        neck-tilt verified_pose_override for the whole span), then repeats sleep
        bob/rock cycles while a randomly-chosen snore track plays, starting a
        fresh randomly-alternated track whenever the previous one finishes.
        Between whole cycles it checks the stop and timeout signals; the sensor
        approach flag is ALSO polled on a short interval WHILE each cycle runs,
        so an approaching object wakes the nap mid-cycle (after the in-flight
        cycle finishes — no sweep is cut mid-stroke). On any interruption it
        performs the calm wake (``sleep_snore_return``, which
        also releases the override) UNLESS the reason is a sensor trigger, in
        which case the caller runs the startle response instead (and this method
        still releases the override so the widened clamp never leaks).

        Args:
            timeout_seconds: Seconds after which the timeout interruption fires.

        Returns:
            One of NAP_INTERRUPT_TIMEOUT / NAP_INTERRUPT_SENSOR /
            NAP_INTERRUPT_STOP.
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()
        deadline = time.monotonic() + max(1, timeout_seconds)

        # Head drops asleep and the neck-tilt override opens (held until wake).
        await mv.sleep_snore_lead_in()

        # DISCARD any approach accumulated during setup. The sensor's background
        # poller has been running since _open_nap_sensor() — through the whole
        # yawn and the head-drop — so the figure's OWN moving head/arm (or startup
        # sensor noise) can pass through the sensor cone and prime the approach
        # streak before the figure is even still. Reset here, once the head has
        # settled asleep, so the wake only measures motion from NOW on.
        detector = getattr(self, "_nap_detector", None)
        if detector is not None:
            detector.reset()
            print("[nap] approach detector reset after head-drop; now watching")

        # Build ONE AudioPlayer for the whole nap and reuse it for every snore
        # segment. play_audio_file opens/closes its own PyAudio stream per call,
        # so a single player can play many clips sequentially. Constructing a
        # NEW AudioPlayer per segment would re-claim the eye LED GPIO pin while
        # the previous player still held it -> lgpio 'GPIO busy' on the 2nd clip,
        # which previously killed the nap after ~2 snores regardless of timeout.
        # Jaw silenced (a snoring figure's mouth stays shut); eyes still track
        # the envelope (drive_eyes defaults True).
        player = AudioPlayer(drive_jaw=False)

        reason = None
        audio_thread = None
        try:
            while True:
                # Check interruptions BETWEEN whole cycles so a bob/rock is
                # never cut mid-move (matches the framework's loop semantics).
                if nap_signal.stop_requested():
                    reason = self.NAP_INTERRUPT_STOP
                    break
                if self._poll_nap_sensor():
                    reason = self.NAP_INTERRUPT_SENSOR
                    break
                if time.monotonic() >= deadline:
                    reason = self.NAP_INTERRUPT_TIMEOUT
                    break

                # Start a fresh randomly-alternated snore track whenever none is
                # playing (first cycle, or the previous clip has finished),
                # reusing the single shared player above.
                if audio_thread is None or not audio_thread.is_alive():
                    track = random.choice(self._NAP_SNORE_TRACKS)
                    audio_path = os.path.join(audio_dir, track)
                    audio_thread = threading.Thread(
                        target=player.play_audio_file,
                        args=(audio_path,),
                        daemon=True,
                    )
                    audio_thread.start()
                    print(f"[nap] snoring: {track}")

                # One sleep cycle (head bob + arm rock). Run it as a task and
                # watch the (non-blocking, latched) sensor flag on a short
                # interval WHILE it runs, so a confirmed approach is caught
                # mid-cycle instead of only at the next cycle boundary. We do
                # NOT cancel the move mid-stroke (that could leave a servo
                # part-way through a sweep) — we let the in-flight cycle finish,
                # then break. The bob/rock cycle is short (~2.5-3.5s) and made of
                # sub-second moves, so "finish the current cycle then wake" is
                # already far snappier than the old whole-cycles-only check and
                # keeps every sweep intact.
                cycle = asyncio.ensure_future(mv.sleep_snore_loop_body())
                sensor_tripped = False
                while not cycle.done():
                    if not sensor_tripped and self._poll_nap_sensor():
                        sensor_tripped = True  # latched; wake after this cycle
                    await asyncio.sleep(self.NAP_SENSOR_POLL_INTERVAL_S)
                await cycle  # propagate any error; cycle already complete
                if sensor_tripped:
                    reason = self.NAP_INTERRUPT_SENSOR
                    break
        finally:
            # Wake the head to rest and release the neck-tilt override. On a
            # sensor interrupt the startle response follows in the caller, but
            # we STILL wake+release here so the override never leaks and the
            # figure is at a known rest pose before startle runs.
            await mv.sleep_snore_return()
            # Let the in-flight snore clip's audio thread finish before releasing
            # the eye pin, so closing the LED can't race with the thread still
            # driving it from the envelope. The wake above already took a couple
            # seconds; bound the extra wait so a long clip can't stall the exit.
            if audio_thread is not None:
                audio_thread.join(timeout=3)
            # Release the eye LED pin the shared player claimed, so a following
            # routine/startle can drive the eyes without a 'GPIO busy' clash.
            self._release_player(player)

        return reason

    @staticmethod
    def _release_player(player):
        """Best-effort release of an AudioPlayer's GPIO pins (eye LED / jaw).

        gpiozero devices hold their pin until closed; releasing them lets a
        following owner (another routine, or the startle response) claim the
        same pins without an lgpio 'GPIO busy' error. Never raises.

        Args:
            player: The AudioPlayer whose devices to close (may hold None pins).
        """
        for attr in ("led_eye_light", "jaw_motor"):
            device = getattr(player, attr, None)
            if device is not None:
                try:
                    device.off()
                    device.close()
                except Exception as e:
                    print(f"[nap] could not release {attr}: {e}")

    def _startle(self):
        """Wake response to a nap interruption: run the AWAKEN Routine.

        When the nap is interrupted, the figure reacts with a groggy "just woke
        up" — a gentle arm stir (snuck_up's arm with the shoulder motion halved)
        while the head lolls around lazily until the ``awakened.wav`` audio
        finishes, then the arm lowers to rest. The nap loop's wake
        (``sleep_snore_return``) has already brought the figure to REST and
        released the sleep pose override before this runs, which is the start
        pose the ``awaken`` gesture expects.

        This runs INSIDE the napping mode, which already holds the servo lock,
        so it must NOT re-acquire it — ``run_action_and_audio`` does not take the
        lock, so delegating to the shared runner (rather than duplicating the
        audio-thread logic) is safe here. The runner drives everything back to
        REST on error, so the figure never ends energized against a jam.
        """
        print("[nap] awaken: groggy wake-up")
        self.run_action_and_audio("_do_awaken", self.music[26])  # awakened.wav

    # ------------------------------------------------------------------ #
    # Awake — a MODE (continuous active "filler" behaviour until interrupted) #
    # ------------------------------------------------------------------ #

    # Interruption reasons returned by the awake loop (mirrors the nap reasons).
    AWAKE_INTERRUPT_TIMEOUT = "timeout"
    AWAKE_INTERRUPT_SENSOR = "sensor"
    AWAKE_INTERRUPT_STOP = "stop"       # external stop request (e.g. web app)

    # Filler Routines the awake loop cycles through, picked at random each
    # iteration. These are the "idle-but-alive" ambient routines (patrol via
    # vader_beaten, plus other ambient performers). Keep to routines that return
    # to rest cleanly. MORE TBD — extend this pool as new ambient routines land.
    _AWAKE_ROUTINE_POOL = ("vader_beaten", "krusty", "waiting", "start_party")

    def awake(self, timeout_seconds=300):
        """AWAKE mode: perform ambient Routines on a loop until interrupted.

        A Mode (per the animation vocabulary) is a continuous background
        behaviour that runs until interrupted. Awake mode is the active
        counterpart to napping: instead of resting, the figure performs a random
        ambient "filler" Routine (see ``_AWAKE_ROUTINE_POOL`` — patrol via
        ``vader_beaten``, and others TBD), then picks another, and so on, filling
        the time until a more deliberate action is wanted.

        Interruption signals (checked BETWEEN whole routines so one is never cut
        off mid-performance):

        - **Timeout** (``timeout_seconds``, default 300): the mode ends after the
          current routine finishes.
        - **Sensor** (HC-SR04 approach, reused from napping via
          ``_open_nap_sensor`` / ``_poll_nap_sensor``): an approaching object
          ends the mode. (Response TBD — for now it just winds down.)
        - **External stop** (``nap_signal`` — e.g. the web app wants to run a
          requested action): the mode ends and exits so the servo lock frees for
          the requested action. This is what makes a web action button "arouse"
          the mode (see the animation-vocabulary Awake mode).

        Unlike napping, this loop is SYNCHRONOUS: each routine it runs goes
        through ``run_action_and_audio`` which calls ``asyncio.run`` internally,
        so the loop itself must not be inside an event loop. It checks the three
        interrupt signals between routines (all non-blocking: the sensor poll
        reads a latched flag and the timeout is a monotonic deadline).

        Runs INSIDE ``servo_lock()`` (taken by the CLI ``awake`` branch) for its
        whole life, exactly like napping — the routines it calls use
        ``run_action_and_audio`` which does NOT re-take the lock.

        Args:
            timeout_seconds: How long to stay awake before the timeout ends the
                mode. Default 300s; configurable via the CLI.
        """
        # Clear any stale stop request from a previous Mode run so we start clean.
        nap_signal.clear_stop()

        # Arm the proximity sensor (best-effort; the mode still runs and simply
        # won't sensor-interrupt if the sensor can't be opened).
        self._open_nap_sensor()

        print(f"[awake] entering awake mode (timeout {int(timeout_seconds)}s)")
        try:
            reason = self._run_awake_loop(timeout_seconds)
        except Exception as e:
            print(f"[awake] error during awake loop: {e}")
            self._safe_rest()
            self._close_nap_sensor()
            nap_signal.clear_stop()
            return

        print(f"[awake] {reason} interrupt -> winding down")
        # Ensure a clean, unloaded rest pose on exit regardless of how the last
        # routine ended.
        self._safe_rest()

        # Release the sensor GPIO pins and clear the stop signal so the next Mode
        # starts clean and any web-requested action can proceed once the lock
        # frees.
        self._close_nap_sensor()
        nap_signal.clear_stop()

    def _run_awake_loop(self, timeout_seconds):
        """Run random ambient Routines until interrupted; return the reason.

        Picks a random routine from ``_AWAKE_ROUTINE_POOL`` and runs it to
        completion, then checks the stop/sensor/timeout signals before starting
        another. Checking BETWEEN routines (never mid-performance) matches the
        nap loop's between-cycles semantics.

        Args:
            timeout_seconds: Seconds after which the timeout interrupt fires.

        Returns:
            One of AWAKE_INTERRUPT_TIMEOUT / AWAKE_INTERRUPT_SENSOR /
            AWAKE_INTERRUPT_STOP.
        """
        deadline = time.monotonic() + max(1, timeout_seconds)
        while True:
            # Check interrupts BEFORE each routine so a fresh stop/sensor/timeout
            # ends the mode promptly without starting another performance.
            if nap_signal.stop_requested():
                return self.AWAKE_INTERRUPT_STOP
            if self._poll_nap_sensor():
                return self.AWAKE_INTERRUPT_SENSOR
            if time.monotonic() >= deadline:
                return self.AWAKE_INTERRUPT_TIMEOUT

            routine_name = random.choice(self._AWAKE_ROUTINE_POOL)
            print(f"[awake] performing: {routine_name}")
            getattr(self, routine_name)()

    # ------------------------------------------------------------------ #
    # Private gesture coroutines (called by run_action_and_audio)         #
    # ------------------------------------------------------------------ #

    async def _do_wave(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave())

    async def _do_wave_and_swivel(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_swivel())

    async def _do_wave_and_swivel_smooth(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_swivel_smooth())

    async def _do_come_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.come_and_look())

    async def _do_reach_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.reach_and_look())

    async def _do_reach_and_look_smooth(self):
        # vincentPrice: smooth eased reach + flowing head look-around that runs
        # for the whole laugh, then returns to rest. Wait the standard idle so
        # the audio is playing before motion begins, then flow for the rest of
        # the clip.
        mv = Movements("Animatronic")
        duration = self._audio_duration_seconds(self.music[17])  # vincent-price-laugh.wav
        await asyncio.sleep(self.idle)
        await mv.reach_and_look_smooth(duration=max(0.0, duration - self.idle))

    async def _do_yawn(self):
        # Gesture starts at t=0; the yawn audio is gated 300ms in yawn() so the
        # arm is already rising when the yawn sound comes in.
        mv = Movements("Animatronic")
        await mv.yawn_cover()

    async def _do_patrol(self):
        mv = Movements("Animatronic")
        await self._run(mv.patrol())

    async def _do_neck_ellipse(self):
        mv = Movements("Animatronic")
        await self._run_quick(mv.neck_ellipse())

    async def _do_shake_no(self):
        mv = Movements("Animatronic")
        await self._run(mv.shake_no())

    async def _do_swivel_head(self):
        mv = Movements("Animatronic")
        await self._run(mv.swivel_head())

    async def _do_snuck_up(self):
        # Ungated: the gesture starts at t=0 alongside the audio (no lead-in
        # delay), so the head jerk/arm recoil fires the instant the gasp begins.
        mv = Movements("Animatronic")
        await mv.snuck_up()

    async def _do_awaken(self):
        # Ungated: motion + audio start together at t=0. The lazy head bob runs
        # for the awakened.wav duration so the head lolls around until the audio
        # finishes, then the arm lowers to rest.
        mv = Movements("Animatronic")
        duration = self._audio_duration_seconds(self.music[26])  # awakened.wav
        await mv.awaken(duration=duration)


def main(args):
    """Dispatch --action to the corresponding Animatronic routine.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    a = Animatronic()

    action_map = {
        # Wave routines
        'startParty':     a.start_party,
        # Beckon routines
        'waiting':        a.waiting,
        'exorcist':       a.exorcist,
        'vaderFather':    a.vader_father,
        'torture':        a.torture,
        'yodaFear':       a.yoda_fear,
        # Patrol / ambient
        'krusty':         a.krusty,
        'vaderBeaten':    a.vader_beaten,
        # Reaction
        'blah':           a.blah,
        # New routines
        'evilLaugh':      a.evil_laugh,
        'vincentPrice':   a.vincent_price,
        'yawn':           a.yawn,
        'snuckUp':        a.snuck_up,
        'awaken':         a.awaken,
        # Performance-framework routines
        'brains':         a.brains,
        'hypnotic':       a.hypnotic,
        'sleep':          a.snore,
        'moreCandy':      a.more_candy,
    }

    if args.action in action_map:
        # SAFETY: hold the system-wide servo lock for the whole routine so no
        # other process can drive the servos at the same time. Two concurrent
        # routines can stall a servo against a mechanical block, causing it to
        # overheat and burn out — a fire hazard. Fail fast if already running.
        try:
            with servo_lock():
                action_map[args.action]()
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action == 'napping':
        # Napping is a MODE: it drives servos (head drop/bob + arm rock), so it
        # holds the servo lock for its whole run just like a routine. It runs
        # until interrupted (timeout, sensor-TBD, or an external stop request
        # from the web app). Fail fast if the servos are already in use.
        try:
            with servo_lock():
                a.napping(timeout_seconds=args.nap_timeout)
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action == 'awake':
        # Awake is a MODE: it performs ambient Routines on a loop, so it holds
        # the servo lock for its whole run just like napping. It runs until
        # interrupted (timeout, sensor, or an external stop request from the web
        # app). Fail fast if the servos are already in use.
        try:
            with servo_lock():
                a.awake(timeout_seconds=args.awake_timeout)
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action == 'mic':
        # Mic mode is audio-only and does not move servos, so it does NOT take
        # the servo lock (that would needlessly block gesture routines).
        streamer = AudioStreamer()
        streamer.start()
        # TODO: run a complementary movement while mic mode is active.
        input("Mic streaming — press Enter to stop...\n")
        streamer.stop()
    else:
        print(f"Unknown action: {args.action}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Animatronic controller — run a named gesture + audio routine."
    )
    parser.add_argument('--action', default=None,
                        help='Action to perform (e.g. startParty, waiting, blah, napping).')
    parser.add_argument('--nap-timeout', dest='nap_timeout', type=int, default=60,
                        help='Napping mode: seconds before the timeout wake '
                             '(default: 60). Only used with --action=napping.')
    parser.add_argument('--awake-timeout', dest='awake_timeout', type=int,
                        default=300,
                        help='Awake mode: seconds before the timeout ends the '
                             'mode (default: 300). Only used with --action=awake.')
    args = parser.parse_args()
    print(args.action)
    main(args)
