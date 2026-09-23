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
import constants
import asyncio
import threading
import argparse
import sys
import os


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
        'yoda-agent-evil.wav',     # 9
        'yoda-fear.wav',           # 10
        'hello-everyone.wav',      # 11
        'happy-halloween.wav',     # 12
        'walk.wav',                # 13
        'how-yall.wav',            # 14
        'cant-hear.wav',           # 15
        'evil-laugh.wav',          # 16
        'vincent-price-laugh.wav', # 17
        'owl.wav',                 # 18
        'yawn.wav',                # 19
        'brains.wav',              # 20
        'hypnotic.wav',            # 21
        'snore.wav',               # 22
        'more_candy.wav',          # 23
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

    def run_action_and_audio(self, method_name, audio_file):
        """Play an audio file via AudioPlayer while running a gesture coroutine.

        AudioPlayer runs in a background thread so the gesture coroutine can
        start immediately after the idle delay.  The thread is joined after
        the coroutine completes so resources are always cleaned up.

        Args:
            method_name: Name of an async method on this class (e.g. '_do_wave').
            audio_file:  Filename (not full path) of the audio file in audio_dir.
        """
        audio_path = os.path.join(self._resolve_audio_dir(), audio_file)
        player = AudioPlayer()

        audio_thread = threading.Thread(
            target=player.play_audio_file,
            args=(audio_path,),
            daemon=True,
        )
        audio_thread.start()
        print(f"Playing audio: {audio_path}")
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

    def hello(self):
        """Hello audio — wave."""
        self.run_action_and_audio("_do_wave", self.music[11])

    def happy_halloween(self):
        """Happy Halloween audio — wave."""
        self.run_action_and_audio("_do_wave", self.music[12])

    def nice_day(self):
        """Walk/nice day audio — wave."""
        self.run_action_and_audio("_do_wave", self.music[13])

    def how_yall_doin(self):
        """How y'all doing audio — wave."""
        self.run_action_and_audio("_do_wave", self.music[14])

    def cant_hear(self):
        """Can't hear audio — wave."""
        self.run_action_and_audio("_do_wave", self.music[15])

    def start_party(self):
        """Party switch audio — wave + swivel head."""
        self.run_action_and_audio("_do_wave_and_swivel", self.music[3])

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

    def yoda900(self):
        """Yoda 900 years audio — patrol."""
        self.run_action_and_audio("_do_patrol", self.music[9])

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
                            loop_body=mv.shake_no_loop_body,  # one randomized sweep
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
        """Vincent Price laugh audio — reach out + look around."""
        self.run_action_and_audio("_do_reach_and_look", self.music[17])

    def owl(self):
        """Owl audio — swivel head (head-only)."""
        self.run_action_and_audio("_do_swivel_head", self.music[18])

    def yawn(self):
        """Yawn audio + cover-mouth gesture (jaw syncs to the yawn.wav)."""
        self.run_action_and_audio("_do_yawn", self.music[19])

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
    # Private gesture coroutines (called by run_action_and_audio)         #
    # ------------------------------------------------------------------ #

    async def _do_wave(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave())

    async def _do_wave_and_swivel(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_swivel())

    async def _do_come_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.come_and_look())

    async def _do_reach_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.reach_and_look())

    async def _do_yawn(self):
        # yawn.wav is only ~2.5s, so use a short lead-in: the arm rises WITH the
        # yawn sound (and the jaw motion). Lead-in tuned to 0.05s so the hand
        # reaches the mouth on time (was arriving slightly late at 0.3s).
        mv = Movements("Animatronic")
        await self._run_lead(mv.yawn_cover(), 0.05)

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


def main(args):
    """Dispatch --action to the corresponding Animatronic routine.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    a = Animatronic()

    action_map = {
        # Wave routines
        'hello':          a.hello,
        'happyHalloween': a.happy_halloween,
        'niceDay':        a.nice_day,
        'howYallDoin':    a.how_yall_doin,
        'cantHear':       a.cant_hear,
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
        'yoda':           a.yoda900,
        # Reaction
        'blah':           a.blah,
        # New routines
        'evilLaugh':      a.evil_laugh,
        'vincentPrice':   a.vincent_price,
        'owl':            a.owl,
        'yawn':           a.yawn,
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
            print("Servos busy — another routine is already running. Aborting.")
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
                        help='Action to perform (e.g. startParty, waiting, blah).')
    args = parser.parse_args()
    print(args.action)
    main(args)
