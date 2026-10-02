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
from servo_lock import servo_lock, group_lock, NECK_GROUP, ServoBusyError, BUSY_EXIT_CODE
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
from vision_models import Detection, TrackingConfig
from tracking_controller import select_target, compute_offset, next_neck_targets
from detection_routine_map import DetectionRoutineMap
import asyncio
import threading
import argparse
import random
import sys
import os
import time
import json
import urllib.request
import urllib.error
import wave


# Default Camera_Service base URL. Camera_Service (src/camera_service.py) is a
# separate non-root process that owns the camera/detector and exposes detections
# over localhost-only HTTP (never leaves the device, Req 9.7). Tracking_Mode is
# only a READER of that service — it issues no camera command.
DEFAULT_CAMERA_URL = "http://localhost:8001"


class CameraClient:
    """Thin read-only HTTP client for Camera_Service ``GET /detections``.

    Tracking_Mode polls this to get the latest frame's Detections. It uses only
    the stdlib ``urllib`` (no new dependency on the Pi), mirroring the webapp's
    ``_proxy`` pattern, and parses the Camera_Service JSON
    ``{frame_id, width, height, ts, detections:[{label, score, x1, y1, x2, y2,
    is_person}, ...]}`` into ``vision_models.Detection`` objects plus the frame
    dimensions the Tracking_Controller math needs.

    The client is a pure consumer: it reads detections and never issues any
    servo or camera command. A short timeout keeps the Tracking_Mode loop
    responsive (so it can meet the 500 ms update budget, Req 5.7) even when
    Camera_Service is slow or unreachable.

    Attributes:
        base_url: Camera_Service base URL (default ``http://localhost:8001``).
        timeout: Per-request timeout in seconds.
    """

    def __init__(self, base_url=DEFAULT_CAMERA_URL, timeout=0.4):
        """Build a Camera_Service client.

        Args:
            base_url: Base URL of Camera_Service. Defaults to
                ``http://localhost:8001`` (configurable so a non-default host/
                port can be passed from the CLI in task 10.3).
            timeout: Per-request HTTP timeout in seconds. Kept well under the
                500 ms tracking-update budget so a slow/unreachable service does
                not stall the neck loop.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_detections(self):
        """Fetch the latest frame's Detections from Camera_Service.

        Issues ``GET {base_url}/detections`` and parses the JSON payload into
        ``Detection`` objects. On any failure (service unreachable, HTTP error,
        malformed/non-JSON body) it fails soft: it ``print()``s the problem and
        returns an empty detection list with zero frame dimensions, so the
        Tracking_Mode loop treats the frame as "no person seen" (which triggers
        the Scan_Sweep / hold path) rather than crashing.

        Returns:
            A tuple ``(detections, frame_w, frame_h)`` where ``detections`` is a
            list of ``vision_models.Detection`` and ``frame_w`` / ``frame_h`` are
            the reported frame dimensions in pixels (0 when unavailable).
        """
        url = f"{self.base_url}/detections"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
            payload = json.loads(body)
        except urllib.error.URLError as e:
            print(f"[tracking] camera unreachable at {self.base_url}: {e.reason}")
            return [], 0, 0
        except (ValueError, UnicodeDecodeError) as e:
            print(f"[tracking] camera returned non-JSON detections: {e}")
            return [], 0, 0

        return self._parse_payload(payload)

    @staticmethod
    def _parse_payload(payload):
        """Parse a ``/detections`` JSON payload into Detections + frame size.

        Tolerant of missing/odd fields so a single bad entry can't crash the
        loop: non-dict payloads yield no detections, and any detection entry
        that can't be coerced to the ``Detection`` shape is skipped with a
        ``print()``. ``is_person`` is derived from the ``Detection`` label (its
        ``is_person`` property), so a mislabeled JSON ``is_person`` flag can't
        make a non-``person`` box drive tracking.

        Args:
            payload: The decoded JSON object from Camera_Service.

        Returns:
            A tuple ``(detections, frame_w, frame_h)``.
        """
        if not isinstance(payload, dict):
            print(f"[tracking] unexpected detections payload type: {type(payload)}")
            return [], 0, 0

        frame_w = int(payload.get("width", 0) or 0)
        frame_h = int(payload.get("height", 0) or 0)

        detections = []
        for raw in payload.get("detections", []) or []:
            try:
                detections.append(
                    Detection(
                        label=str(raw["label"]),
                        score=float(raw["score"]),
                        x1=int(raw["x1"]),
                        y1=int(raw["y1"]),
                        x2=int(raw["x2"]),
                        y2=int(raw["y2"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                print(f"[tracking] skipping malformed detection {raw!r}: {e}")

        return detections, frame_w, frame_h


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
        'in_my_power.wav',         # 27  (hypnotic follow-on: "in my power")
        'clear_throat.wav',        # 28  (clearThroat: hand-to-mouth throat clear)
        'cough_long.wav',          # 29  (coughLong: cover-mouth cough, long)
        'cough_medium.wav',        # 30  (coughMedium: cover-mouth cough, medium)
        'gurgle_burp.wav',         # 31  (burp: cover-mouth burp lead track)
        'excuseme_sb.wav',         # 32  (burp/fart follow-on: "excuse me")
        'fart.wav',                # 33  (fart: lead track, no cover)
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
        LEAD track (``hypnotic.wav``) plays with the jaw motor silent and does
        NOT claim ``EYE_LIGHT_PIN``. The FOLLOW-ON track (``in_my_power.wav``)
        overrides this with ``{"drive_jaw": True, "drive_eyes": False}`` so the
        jaw articulates on that line while the eyes stay off (the ambient blinker
        keeps owning the eye pin across both tracks). Instead the eyes are driven
        by the runner's
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

        Audio is a TWO-TRACK chain: ``hypnotic.wav`` (~5.05s) then
        ``in_my_power.wav`` (~7.02s) played back-to-back on the same audio
        thread with no gap (``followup_audio_files``), for ~12.07s total. The
        ``PlaybackController`` stays ``is_active()`` across both tracks and its
        duration is the SUM, so the looping arm and head keep going through the
        whole chain rather than stopping when ``hypnotic.wav`` ends.

        The arm sets ``stop_loop_lead_seconds=1.5`` so it stops starting new
        sways once the COMBINED audio is within ~1.5s of ending and its last
        sway (~1s) plus the ~40%-faster retract (~0.8s) finish before the audio
        does. The head sway has no cutoff, so it keeps looping until the audio
        fully ends. Both then return to rest — the arm retracts, the neck centers
        — with the runner sweeping any residual channels home on completion or
        failure.

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
            # in_my_power.wav (~7.02 s) plays back-to-back immediately after
            # hypnotic.wav on the same audio thread, so the arm/head/eyes keep
            # going across BOTH tracks (~12.07 s total) and the arm's near-end
            # cutoff below fires against the end of in_my_power.wav, not hypnotic.
            # Per-track options: the jaw is ON for in_my_power.wav (so the mouth
            # articulates on this line) but eyes stay OFF -- the ambient blinker
            # owns EYE_LIGHT_PIN, so re-enabling envelope eyes here would clash.
            followup_audio_files=(
                (self.music[27], {"drive_jaw": True, "drive_eyes": False}),
            ),
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

    def clear_throat(self):
        """"Clear throat" — bring the hand to the mouth, clear throat, lower.

        Driven by the Performance_Framework so the AUDIO owns the hold timing
        (unlike the standalone ``yawn_cover``, which holds a fixed 1.3s). A
        single-step ``PerformanceDefinition`` runs ONE movement -- the phased
        ``yawn_cover`` adapters -- which reuse yawn_cover's exact
        operator-verified hand-to-mouth pose and its verified_pose_override:

        * ``yawn_cover_lead_in`` centers the head and folds the hand up in front
          of the mouth. It ``supplies_gate=True`` (``gate=GateSpec("clear_throat")``),
          and opens the gate ~0.5s BEFORE the hand fully settles, so
          ``clear_throat.wav`` (~3.5s) starts a touch early -- the sound leads
          the final settle rather than waiting for it (the fold's last ~0.5s
          finishes at the top of the first hold).
        * ``yawn_cover_loop_body`` HOLDS the hand at the mouth. With
          ``loop_for_audio=True`` the framework repeats the (no-op) hold while
          playback is active. ``stop_loop_lead_seconds=0.9`` stops holding ~0.9s
          before the clip ends so the hand starts lowering that much sooner (the
          ~1.1s lower overlaps the audio tail).
        * ``yawn_cover_return`` lowers the hand back to rest, then releases the
          pose override.

        The single movement owns head channels {0,1} and arm channels {4,5,6,7};
        with no concurrent movement the group is trivially channel-disjoint. The
        runner sweeps residual channels home on completion or failure. Default
        player (jaw + envelope-driven eyes) articulates the throat-clear.

        A single ``Movements`` instance backs the ``MovementSpec`` phase
        callables so they share one ``TrunkController``. The runner is a
        coroutine, launched with ``asyncio.run`` here at the top of the call
        stack (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        CLEAR_THROAT = PerformanceDefinition(
            name="clearThroat",
            audio_file=self.music[28],  # clear_throat.wav — ~3.5 s
            # The hand-to-mouth reach supplies the gate: audio starts the moment
            # the lead-in completes (the hand has reached the mouth).
            gate=GateSpec(movement_name="clear_throat"),
            steps=(
                PerformanceStep(
                    loop_for_audio=True,   # HOLD the hand until the clip ends
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="clear_throat",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.yawn_cover_lead_in,     # raise hand to mouth (gate)
                            loop_body=mv.yawn_cover_loop_body,  # hold while audio plays
                            do_return=mv.yawn_cover_return,     # lower hand at end
                            supplies_gate=True,                 # opens the audio gate
                            # Stop holding ~0.9s before the clip ends so the hand
                            # starts lowering that much sooner (the ~1.1s lower
                            # then overlaps the tail of the audio).
                            stop_loop_lead_seconds=0.9,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(CLEAR_THROAT, mv, audio_dir).run())

    # --- Cover-mouth bodily-noise routines ------------------------------- #

    def _run_cover_mouth_settled(self, audio_file, *, name):
        """Cover the mouth, gate audio until the hand settles, then play a clip.

        Shared implementation for the ``coughLong`` / ``coughMedium`` routines.
        Like ``clearThroat`` it drives the phased ``yawn_cover`` adapters through
        the Performance_Framework, reusing yawn_cover's exact operator-verified
        hand-to-mouth pose and its verified_pose_override -- but it uses the
        SETTLED lead-in (``yawn_cover_lead_in_settled``) so the audio gate opens
        only once the hand has reached its final cover position ("gate audio
        until the hand reaches final position"), rather than ~0.5s early.

        * ``yawn_cover_lead_in_settled`` centers the head and folds the hand
          fully up BEFORE opening the gate (``supplies_gate=True``), so the clip
          starts the instant the hand covers the mouth.
        * ``yawn_cover_loop_body`` HOLDS the hand at the mouth while the clip
          plays (``loop_for_audio=True``); ``stop_loop_lead_seconds=0.9`` starts
          the lower ~0.9s before the clip ends so the ~1.1s lower overlaps the
          tail.
        * ``yawn_cover_return`` lowers the hand to rest and releases the override.

        The single movement owns head channels {0,1} and arm channels {4,5,6,7};
        with no concurrent movement the group is trivially channel-disjoint. A
        single ``Movements`` instance backs the phase callables so they share one
        ``TrunkController``; the runner is launched with ``asyncio.run`` at the
        top of the call stack.

        Args:
            audio_file: Filename of the cough clip in the resolved audio dir.
            name: Performance/gate name (also the single movement's name).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        definition = PerformanceDefinition(
            name=name,
            audio_file=audio_file,
            # The hand-to-mouth reach supplies the gate: audio starts the moment
            # the lead-in completes, i.e. once the hand has SETTLED at the mouth.
            gate=GateSpec(movement_name=name),
            steps=(
                PerformanceStep(
                    loop_for_audio=True,   # HOLD the hand until the clip ends
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name=name,
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.yawn_cover_lead_in_settled,  # fold fully, THEN gate
                            loop_body=mv.yawn_cover_loop_body,       # hold while audio plays
                            do_return=mv.yawn_cover_return,          # lower hand at end
                            supplies_gate=True,                      # opens the audio gate
                            # Stop holding ~0.9s before the clip ends so the hand
                            # starts lowering that much sooner (the ~1.1s lower
                            # then overlaps the tail of the audio).
                            stop_loop_lead_seconds=0.9,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(definition, mv, audio_dir).run())

    def cough_long(self):
        """"Cough (long)" — cover the mouth, then cough once the hand arrives.

        Runs the Cover-Mouth gesture; the audio is GATED until the hand reaches
        its final cover position, then ``cough_long.wav`` plays while the hand is
        held at the mouth. See ``_run_cover_mouth_settled`` for the mechanics.
        """
        self._run_cover_mouth_settled(self.music[29], name="coughLong")  # cough_long.wav

    def cough_medium(self):
        """"Cough (medium)" — cover the mouth, then cough once the hand arrives.

        Runs the Cover-Mouth gesture; the audio is GATED until the hand reaches
        its final cover position, then ``cough_medium.wav`` plays while the hand
        is held at the mouth. See ``_run_cover_mouth_settled`` for the mechanics.
        """
        self._run_cover_mouth_settled(self.music[30], name="coughMedium")  # cough_medium.wav

    def burp(self):
        """"Burp" — cover the mouth, burp, then an "excuse me" follow-on.

        Driven by the Performance_Framework with the phased ``yawn_cover``
        adapters (same operator-verified cover pose + override as ``clearThroat``
        and the coughs), but with a FIXED 250ms audio gate rather than
        gating until the hand settles:

        * ``yawn_cover_lead_in_gated`` centers the head and starts the up-fold,
          then opens the gate 250ms in (``supplies_gate=True``) while the fold
          finishes underneath -- so ``gurgle_burp.wav`` starts promptly and the
          hand reaches the mouth as the burp plays.
        * Audio is a TWO-TRACK chain: ``gurgle_burp.wav`` then ``excuseme_sb.wav``
          played back-to-back with no gap (``followup_audio_files``). The
          ``PlaybackController`` stays ``is_active()`` across BOTH tracks and its
          duration is their SUM, so the hand stays at the mouth through the burp
          AND the "excuse me", then lowers.
        * ``yawn_cover_loop_body`` HOLDS the hand at the mouth across the chain;
          ``yawn_cover_return`` lowers the hand and releases the override.

        The single movement owns head channels {0,1} and arm channels {4,5,6,7}
        (trivially disjoint with no concurrent movement). A single ``Movements``
        instance backs the phase callables; launched with ``asyncio.run`` at the
        top of the call stack.
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        BURP = PerformanceDefinition(
            name="burp",
            audio_file=self.music[31],  # gurgle_burp.wav
            # "excuse me" plays back-to-back immediately after the burp on the
            # same audio thread, so the hand stays at the mouth across both and
            # the near-end cutoff fires against the end of excuseme_sb.wav.
            followup_audio_files=(self.music[32],),  # excuseme_sb.wav
            # Cover-mouth movement supplies a fixed 250ms gate (not hand-arrival).
            gate=GateSpec(movement_name="burp"),
            steps=(
                PerformanceStep(
                    loop_for_audio=True,   # HOLD the hand across both tracks
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name="burp",
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                                constants.RT_SHOULDER_ROTATOR,
                                constants.RT_SHOULDER_TILT,
                                constants.RT_ELBOW_TILT,
                                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
                            }),
                            lead_in=mv.yawn_cover_lead_in_gated,  # 250ms gate + fold
                            loop_body=mv.yawn_cover_loop_body,     # hold while audio plays
                            do_return=mv.yawn_cover_return,        # lower hand at end
                            supplies_gate=True,                    # opens the audio gate
                            stop_loop_lead_seconds=0.9,
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(BURP, mv, audio_dir).run())

    def fart(self):
        """"Fart" — fart first, THEN cover the mouth and say "excuse me".

        A TWO-PHASE routine, because the fart happens BEFORE the cover-mouth
        gesture (unlike the coughs/burp, where audio plays while the hand is
        already at the mouth):

        1. Play ``fart.wav`` to completion with NO movement -- a blocking
           ``AudioPlayer`` on this thread, closed afterward so its jaw/eye pins
           are released before the Performance below claims them.
        2. Run the Cover-Mouth gesture via the Performance_Framework with the
           SETTLED lead-in, so ``excuseme_sb.wav`` is GATED until the hand reaches
           its final cover position -- same mechanics as the coughs
           (``yawn_cover_lead_in_settled`` -> hold -> ``yawn_cover_return``).

        The two audio phases never overlap, so there is no jaw-motor / audio
        contention. The whole routine runs inside the caller's servo lock; the
        Performance run does not take the lock itself. Launched with
        ``asyncio.run`` at the top of the call stack (phase 2's runner).
        """
        # Phase 1: fart.wav alone, no movement. Blocking playback on this thread;
        # close() releases the jaw/eye pins before phase 2's players claim them.
        fart_path = os.path.join(self._resolve_audio_dir(), self.music[33])  # fart.wav
        player = AudioPlayer()
        try:
            print(f"[fart] playing {fart_path} (no cover yet)")
            player.play_audio_file(fart_path)
        finally:
            player.close()

        # Phase 2: cover the mouth and, once the hand settles, say "excuse me".
        self._run_cover_mouth_settled(self.music[32], name="fart")  # excuseme_sb.wav

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

    def _open_nap_sensor(self, source="nap", detect_mode="approach"):
        """Arm approach/presence detection for a Mode run (best-effort).

        The Mode does NOT open the HC-SR04 GPIO itself. The web app is the SOLE
        owner of the sensor and publishes every reading via ``range_publish``;
        this detector consumes that published distance through a
        ``PublishedReadingSensor`` shim and runs the normal ``ApproachDetector``
        approach/presence logic on it. One process owns the pins, so there is no
        GPIO contention — which is what previously left the gauge blank and the
        Mode unable to trigger when it fought the web app for the sensor.

        The shim reports "far away" whenever there is no fresh published reading
        (web app not publishing yet, or a no-echo read), so a missing feed reads
        as "nothing there" rather than a false trigger. ``publish_source`` is
        NOT set here (the web app already publishes for the gauge), so the Mode
        never writes the shared file.

        Args:
            source: Retained for logging/back-compat (the web app is the actual
                gauge publisher now).
            detect_mode: ``"approach"`` (default — getting-closer trend, used by
                napping) or ``"presence"`` (object simply within the gate, used
                by awake). See ``range_sensor.ApproachDetector``.
        """
        self._nap_detector = None
        try:
            import range_publish
            from range_publish import PublishedReadingSensor
            self._nap_detector = ApproachDetector(
                sensor=PublishedReadingSensor(),
                gate_m=self.NAP_WAKE_GATE_M,
                detect_mode=detect_mode,
                # Read the operator's live sensitivity setting every sample so a
                # dashboard slider change takes effect without restarting the
                # mode (falls back to NAP_WAKE_GATE_M if unset).
                gate_provider=range_publish.get_gate_m)
            # Poll the PUBLISHED reading in the background (cheap file read); the
            # loop just checks the latched flag between cycles.
            self._nap_detector.start_polling()
            mode_desc = ("present within" if detect_mode == "presence"
                         else "approaches within")
            print(f"[nap] sensor detection armed ({detect_mode}: trigger if an "
                  f"object is {mode_desc} {self.NAP_WAKE_GATE_M} m; reading from "
                  f"the web app's published feed)")
        except Exception as e:
            print(f"[nap] sensor detection unavailable, no sensor-trigger: {e}")
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

    def _reset_nap_sensor(self):
        """Clear the approach detector's latched flag + streak (best-effort).

        Called after reacting to an approach so a SINGLE detected approach does
        not keep re-firing — the next reaction requires a fresh, newly-confirmed
        approach. No-op when no sensor is armed.
        """
        detector = getattr(self, "_nap_detector", None)
        if detector is not None:
            try:
                detector.reset()
            except Exception as e:
                print(f"[awake] could not reset approach sensor: {e}")

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

    # Weighted "idle-but-alive" ambient pool the awake loop draws from each
    # iteration. Each entry is (weight, method_name); weights are relative and
    # need not sum to 100. The robot spends MOST of its time just looking around
    # and only occasionally does something bigger:
    #   - lookAroundRandom  70%  (gesture, no audio)
    #   - handVisor         20%  (gesture, no audio)
    #   - yawn / clearThroat 10% (routines, with audio) — split evenly below
    # All of these return to rest cleanly on their own. Extend as new ambient
    # gestures/routines land, keeping the weights relative.
    _AWAKE_AMBIENT_POOL = (
        (70, "_do_look_around_random"),  # gesture
        (20, "_do_hand_visor"),          # gesture
        (10, "_AWAKE_OCCASIONAL"),       # placeholder → picks yawn|clearThroat
    )
    # The 10% "occasional" bucket splits evenly between these two Routines.
    _AWAKE_OCCASIONAL_ROUTINES = ("yawn", "clear_throat")

    # On a confirmed sensor approach the mode reacts with ONE of these Routines,
    # chosen at random, before winding down (mirrors napping's startle response).
    _AWAKE_APPROACH_REACTIONS = ("snuck_up", "brains", "hypnotic", "more_candy")

    # Seconds to pause between ambient actions ("run ... separated by 30s
    # pauses"). The pause is INTERRUPTIBLE: it is slept in small slices so a
    # stop request or sensor approach ends it (and the mode) promptly rather
    # than after a full 30s.
    _AWAKE_PAUSE_SECONDS = 30
    _AWAKE_PAUSE_POLL_S = 0.1

    # Duration (seconds) of each ambient GESTURE in awake mode. An action runs
    # to completion before the loop checks the sensor/stop again (motion is
    # never cut mid-move), so keeping the ambient scans short bounds how long a
    # sensor approach can wait: worst case ≈ this duration + poll interval,
    # versus the gestures' ~10-15s defaults. Kept brief so an approach is
    # noticed promptly while the figure still reads as idly looking around.
    _AWAKE_GESTURE_DURATION_S = 6.0

    def awake(self, timeout_seconds=300):
        """AWAKE mode: weighted ambient gestures/routines with 30s pauses.

        A Mode (per the animation vocabulary) is a continuous background
        behaviour that runs until interrupted. Awake mode is the active
        counterpart to napping: instead of resting, the figure performs a random
        ambient "filler" action, pauses ~30s, picks another, and so on, filling
        the time until a more deliberate action is wanted.

        Each action is picked by WEIGHT from ``_AWAKE_AMBIENT_POOL`` so the
        figure mostly just looks around and only occasionally does something
        bigger:

        - ``lookAroundRandom`` 70% — gesture, idle room scan (no audio).
        - ``handVisor``        20% — gesture, hand-as-visor look-around (no audio).
        - ``yawn`` / ``clearThroat`` 10% — the "occasional" bucket, an audio
          Routine, split evenly between the two.

        Gestures run via ``asyncio.run`` on their gesture-only ``_do_*``
        coroutines; routines call their ``Animatronic`` method (which owns its
        own audio + event loop). Consecutive actions are separated by an
        INTERRUPTIBLE ~30s pause (``_awake_pause``).

        Signals (checked BETWEEN whole actions and DURING the pause, never
        mid-action, so a gesture/routine is never cut off part-way):

        - **Sensor** (HC-SR04 in PRESENCE mode via ``_open_nap_sensor`` /
          ``_poll_nap_sensor``): an object simply STANDING within the gate — no
          approach motion required — does NOT end the mode; the figure runs ONE
          random reaction Routine from ``_AWAKE_APPROACH_REACTIONS`` (``snuckUp``,
          ``brains``, ``hypnotic``, ``moreCandy``) via
          ``_awake_approach_reaction``, the detector latch is reset (so it fires
          once per detected presence), and the ambient loop RESUMES. The mode
          keeps running until the admin console stops it (or the timeout). A PIR
          sensor can later replace the presence source with the same contract.
        - **External stop** (``nap_signal``): the admin console sets this when
          the operator stops the mode, or asks to run a routine/gesture or
          toggle the mic in its place; the mode ends and exits so the servo lock
          frees for the requested action. This is what makes a web action button
          "arouse" the mode (see the animation-vocabulary Awake mode).
        - **Timeout** (``timeout_seconds``, default 300): the mode ends after the
          current action finishes.

        This loop is SYNCHRONOUS: routines go through ``run_action_and_audio`` /
        the Performance_Framework (which call ``asyncio.run`` internally) and
        gestures are driven with ``asyncio.run`` here, so the loop itself must
        not be inside an event loop. All three interrupt checks are non-blocking
        (the sensor poll reads a latched flag; the timeout is a monotonic
        deadline).

        Runs INSIDE ``servo_lock()`` (taken by the CLI ``awake`` branch) for its
        whole life, exactly like napping — the actions it runs do NOT re-take the
        lock.

        Args:
            timeout_seconds: How long to stay awake before the timeout ends the
                mode. Default 300s; configurable via the CLI.
        """
        # Clear any stale stop request from a previous Mode run so we start clean.
        nap_signal.clear_stop()

        # Arm the proximity sensor in PRESENCE mode (best-effort): awake reacts
        # to someone simply standing in front of the sensor, not only to a
        # getting-closer trend. Publishes each reading (source "awake") for the
        # live dashboard range gauge. (A PIR sensor can later replace this with
        # the same present/absent contract.)
        self._open_nap_sensor(source="awake", detect_mode="presence")

        print(f"[awake] entering awake mode (timeout {int(timeout_seconds)}s)")
        try:
            # The loop reacts to sensor approaches inline (react + resume) and
            # returns only when STOPPED by the admin console or the timeout.
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

    def _check_awake_interrupt(self, deadline):
        """Return a loop-ENDING interrupt reason, or None to keep running.

        Non-blocking. Only two things END awake mode: an external stop request
        (``nap_signal`` — set by the admin console when it wants to stop the
        mode or run a routine/gesture/mic in its place) and the timeout
        deadline. A sensor approach does NOT end the mode; it is handled
        separately by ``_poll_nap_sensor`` in the loop, which reacts and then
        RESUMES the ambient loop.

        Args:
            deadline: ``time.monotonic()`` value at/after which the timeout fires.

        Returns:
            AWAKE_INTERRUPT_STOP, AWAKE_INTERRUPT_TIMEOUT, or ``None``.
        """
        if nap_signal.stop_requested():
            return self.AWAKE_INTERRUPT_STOP
        if time.monotonic() >= deadline:
            return self.AWAKE_INTERRUPT_TIMEOUT
        return None

    def _awake_pause(self, deadline):
        """Sleep the inter-action pause, returning early on any signal.

        Implements the "separated by 30s pauses" gap between ambient actions,
        but stays responsive: it sleeps in ``_AWAKE_PAUSE_POLL_S`` slices and
        returns as soon as a signal is seen (~0.1s), rather than sitting out the
        full 30s. Distinguishes the two kinds of signal for the caller:

        - A loop-ENDING interrupt (stop/timeout) returns that reason so the loop
          exits.
        - A SENSOR approach returns ``AWAKE_INTERRUPT_SENSOR`` so the loop can
          react (run a reaction routine) and then RESUME the pause/loop rather
          than exit.

        Args:
            deadline: Monotonic timeout deadline, forwarded to the interrupt
                check so the pause also ends when the awake timeout elapses.

        Returns:
            AWAKE_INTERRUPT_STOP / AWAKE_INTERRUPT_TIMEOUT (loop should exit),
            AWAKE_INTERRUPT_SENSOR (react and resume), or ``None`` when the full
            pause elapsed with no signal.
        """
        pause_until = time.monotonic() + self._AWAKE_PAUSE_SECONDS
        while time.monotonic() < pause_until:
            reason = self._check_awake_interrupt(deadline)
            if reason is not None:
                return reason
            if self._poll_nap_sensor():
                return self.AWAKE_INTERRUPT_SENSOR
            time.sleep(self._AWAKE_PAUSE_POLL_S)
        return None

    def _pick_ambient_action(self):
        """Pick the next ambient action by weight; return (kind, method_name).

        Draws from ``_AWAKE_AMBIENT_POOL`` by relative weight (lookAroundRandom
        70% / handVisor 20% / occasional 10%). The 10% "occasional" bucket then
        splits evenly between the ``yawn`` and ``clearThroat`` Routines.

        Returns:
            A tuple ``(kind, method_name)`` where ``kind`` is ``"gesture"`` (a
            gesture-only ``_do_*`` coroutine to drive via ``asyncio.run``) or
            ``"routine"`` (an ``Animatronic`` routine method to call directly).
        """
        weights = [w for w, _ in self._AWAKE_AMBIENT_POOL]
        names = [n for _, n in self._AWAKE_AMBIENT_POOL]
        choice = random.choices(names, weights=weights, k=1)[0]
        if choice == "_AWAKE_OCCASIONAL":
            # Occasional bucket: an audio Routine (yawn or clearThroat).
            return "routine", random.choice(self._AWAKE_OCCASIONAL_ROUTINES)
        # The two ambient _do_* entries are gesture-only coroutines.
        return "gesture", choice

    def _run_awake_action(self, kind, name):
        """Run one picked ambient action to completion.

        Gestures are gesture-only ``_do_*`` coroutines driven via
        ``asyncio.run`` (this loop is synchronous and not inside an event loop);
        routines are ``Animatronic`` methods that manage their own audio + event
        loop internally. Neither re-takes the servo lock — the awake mode
        already holds it for its whole life.

        Args:
            kind: ``"gesture"`` or ``"routine"`` (from ``_pick_ambient_action``).
            name: The coroutine name (gesture) or routine method name (routine).
        """
        if kind == "gesture":
            asyncio.run(getattr(self, name)())
        else:
            getattr(self, name)()

    def _run_awake_loop(self, timeout_seconds):
        """Run weighted ambient actions with 30s pauses until stopped/timeout.

        Each iteration: check the loop-ending interrupts, handle any pending
        sensor approach, pick a weighted ambient action (mostly
        ``lookAroundRandom``, occasionally ``handVisor`` or a
        ``yawn``/``clearThroat`` routine — see ``_pick_ambient_action``), run it
        to completion, then pause ~30s.

        The mode ENDS only on an external stop (``nap_signal`` — the admin
        console) or the timeout, checked BETWEEN whole actions and DURING the
        pause (never mid-action, so a gesture/routine is never cut off).

        A SENSOR approach does NOT end the mode: whenever one is confirmed
        (before an action or during the pause) the figure runs ONE random
        reaction routine (``_awake_approach_reaction``), the detector latch is
        reset so a single approach fires once, and the ambient loop RESUMES.
        Only the admin console (or the timeout) stops it.

        Args:
            timeout_seconds: Seconds after which the timeout interrupt fires.

        Returns:
            AWAKE_INTERRUPT_STOP or AWAKE_INTERRUPT_TIMEOUT (the only two ways
            the mode ends).
        """
        deadline = time.monotonic() + max(1, timeout_seconds)
        while True:
            # End only on stop/timeout, checked before each action.
            reason = self._check_awake_interrupt(deadline)
            if reason is not None:
                return reason

            # A pending sensor approach: react, reset the latch, then resume.
            if self._poll_nap_sensor():
                self._awake_approach_reaction()
                self._reset_nap_sensor()
                continue

            kind, name = self._pick_ambient_action()
            print(f"[awake] performing {kind}: {name}")
            self._run_awake_action(kind, name)

            # Pause ~30s between actions, reacting promptly to any signal.
            reason = self._awake_pause(deadline)
            if reason == self.AWAKE_INTERRUPT_SENSOR:
                # Sensor fired mid-pause: react and RESUME the loop (do not end).
                self._awake_approach_reaction()
                self._reset_nap_sensor()
                continue
            if reason is not None:
                return reason  # stop or timeout ends the mode

    def _awake_approach_reaction(self):
        """React to a sensor approach with ONE random reaction Routine.

        Chosen at random from ``_AWAKE_APPROACH_REACTIONS`` (``snuckUp``,
        ``brains``, ``hypnotic``, ``moreCandy``) — the figure "notices" the
        approaching visitor and performs a reaction before the mode winds down,
        mirroring how napping runs ``_startle`` on a sensor wake.

        Runs INSIDE the awake mode, which already holds the servo lock, so it
        must NOT re-acquire it: these routines use ``run_action_and_audio`` /
        the Performance_Framework, neither of which takes the lock. Each returns
        to rest on its own, and the caller's ``_safe_rest`` is a final backstop.
        """
        reaction = random.choice(self._AWAKE_APPROACH_REACTIONS)
        print(f"[awake] approach detected -> reacting with routine: {reaction}")
        getattr(self, reaction)()

    # ------------------------------------------------------------------ #
    # Tracking — a MODE (continuous head-tracking of a person until stopped) #
    # ------------------------------------------------------------------ #

    # Target cadence for the tracking loop: a neck update is issued within
    # 500 ms of a new detection (Req 5.7). We poll + command well under that
    # budget so the end-to-end detect->command latency stays inside 500 ms.
    _TRACKING_LOOP_PERIOD_S = 0.1

    # Tracking_Mode exit reason for the leave-frame Scan_Sweep timing out
    # (Req 6.10): no person reacquired within cfg.scan_timeout_s, so the Mode
    # recenters and yields to the previously active Mode.
    TRACKING_INTERRUPT_SCAN_TIMEOUT = "scan-timeout"

    # Tracking_Mode exit reason for a detection-triggered Routine (Req 7.2, 7.7):
    # a Detection_Routine_Map rule fired and its action is in the action_map
    # allowlist. The Mode winds down (recenters neck, releases the Neck_Group)
    # BEFORE the triggered Routine drives jaw/audio, so the Routine and
    # Tracking_Mode never own the Neck_Group simultaneously. The chosen rule is
    # carried back to main() as a pending trigger, dispatched only AFTER the
    # Neck_Group lock is released.
    TRACKING_INTERRUPT_TRIGGER = "trigger"

    # Scan_Sweep tuning. The sweep pans NECK_PAN across its SAFE_LIMITS range in
    # fixed-size incremental steps, each written through set_angle (so each is
    # clamped) with a short asyncio.sleep between steps for smooth motion. The
    # cadence is deliberately slower than the tracking loop (a deliberate
    # "surveillance" sweep, cf. TrunkController.slow_scan's 0.05s/deg) while
    # still polling /detections and nap_signal often enough to reacquire a
    # person or wind down on a stop request within ~1s.
    _SCAN_STEP_DEG = 2.0        # pan increment per sweep step (degrees)
    _SCAN_STEP_PERIOD_S = 0.05  # delay between sweep steps (seconds)

    def tracking(
        self,
        camera_url=DEFAULT_CAMERA_URL,
        max_step=None,
        deadband=None,
        conf=None,
        scan_timeout=None,
        aim_frac=None,
        tilt_center=None,
        tilt_min=None,
        tilt_max=None,
        settle_gain=None,
        routine_map=None,
        action_map=None,
    ):
        """TRACKING mode: pan/tilt the neck to follow a detected person.

        A Mode (per the animation vocabulary) is a continuous background
        behaviour that runs until interrupted. Tracking_Mode reads Detections
        from Camera_Service, selects the Target_Person, and drives the Neck_Group
        (channels ``NECK_PAN``/``NECK_TILT``) to reduce the person's Offset from
        Frame_Center — a closed feedback loop (Req 6.1). It carries NO audio and
        never drives the jaw motor (Req 6.2), so toward a live mic Stream it
        behaves like a Gesture: it touches only neck channels and does not
        interrupt the Stream (Req 6.3).

        Lock model (IMPORTANT — differs from napping/awake): napping and awake
        hold the WHOLE-ROBOT ``servo_lock()`` (every group), acquired by their
        ``main()`` dispatch branch. Tracking must instead hold ONLY the
        ``NECK_GROUP`` lock so an arm-only Gesture (disjoint Arm_Group channels
        4-7) can run concurrently (Req 6.6). Consistent with how napping()/awake()
        rely on ``main()``'s ``servo_lock()`` wrapper, this method does NOT take
        any lock itself — ``main()`` is responsible for wrapping the call in
        ``with group_lock(NECK_GROUP):`` (added in task 10.3). Keeping the lock
        in ``main()`` mirrors the existing Modes and lets a hardware-free caller
        (tests) run the loop without touching the lock files.

        Wind-down (Req 6.4, 6.5): like napping/awake the loop watches the
        cross-process ``nap_signal``. When the web app requests a Routine/Act (or
        presses any action button) it sets ``nap_signal``; the loop sees the stop
        request, recenters the Neck_Group to ``REST_POSITIONS`` and exits within
        1 second so the Neck_Group lock frees for the Routine. The same recenter+
        exit happens on ANY loop error, so the neck is never left energized at an
        offset.

        Leave-frame Scan_Sweep (Req 6.7-6.10): when ``select_target`` returns no
        Target_Person the loop runs ``_run_scan_sweep`` — a slow pan of
        ``NECK_PAN`` across its ``SAFE_LIMITS`` range (every command through
        ``set_angle``) that keeps polling ``/detections``. If a person reappears
        mid-sweep the sweep stops immediately and normal tracking resumes; if
        ``cfg.scan_timeout_s`` elapses with no reacquire the loop recenters to
        ``REST_POSITIONS`` and exits, yielding to the previously active Mode.

        Args:
            camera_url: Base URL of Camera_Service. Default
                ``http://localhost:8001`` (CLI flag ``--camera-url`` in task
                10.3).
            max_step: Max neck angle change per update in degrees; forwarded to
                ``TrackingConfig.max_step_deg`` (clamped to [1, 30]). ``None``
                uses the ``TrackingConfig`` default (5 deg).
            deadband: Center Deadband half-width as a fraction of the frame on
                both axes; forwarded to ``TrackingConfig`` deadband fracs
                (clamped to [0.0, 0.5]). ``None`` uses the default (0.05).
            conf: Detector confidence threshold; forwarded to
                ``TrackingConfig.conf_threshold`` (clamped to [0.0, 1.0]).
                ``None`` uses the default (0.5). (Camera_Service already filters
                by its own threshold; carried here so 10.3's CLI flag has a home
                and future client-side filtering can use it.)
            scan_timeout: Scan_Sweep reacquire timeout in seconds; forwarded to
                ``TrackingConfig.scan_timeout_s`` (clamped to [1, 120]) for use
                by task 10.2. ``None`` uses the default (10 s).
            aim_frac: Vertical aim point within the target bbox as a fraction of
                its height from the top edge; forwarded to
                ``TrackingConfig.aim_frac_h`` (clamped to [0.0, 1.0]). ``None``
                uses the default (0.35 = upper chest/head region), which
                corrects the downward bias of aiming at a full-body box's
                torso-level geometric center without over-tilting toward the top
                of the box. Use 0.5 for the old center-of-box behavior.
            tilt_center: Tracking-only level-gaze NECK_TILT angle; forwarded to
                ``TrackingConfig.tilt_center_deg``. ``None`` uses the default
                (105, level on this build). The loop seeds and winds down the
                neck here instead of the global rest 90.
            tilt_min: Lower bound (head highest) of the tracking tilt band;
                forwarded to ``TrackingConfig.tilt_min_deg``. ``None`` uses the
                default (100).
            tilt_max: Upper bound (head lowest) of the tracking tilt band;
                forwarded to ``TrackingConfig.tilt_max_deg``. ``None`` uses the
                default (110). Tracking clamps every tilt command into
                ``[tilt_min, tilt_max]`` so the head stays at head height and
                cannot pitch the person out of frame.
            settle_gain: Proportional control gain; forwarded to
                ``TrackingConfig.settle_gain`` (clamped to [0.05, 1.0]). ``None``
                uses the default (0.5). Lower = gentler/more damped approach
                (less overshoot/oscillation); higher = snappier.
            routine_map: The ``DetectionRoutineMap`` evaluated each loop
                iteration to decide whether a detection condition should trigger
                a Routine (Req 7.2, 7.5, 7.8). ``None`` builds the seed-default
                map (``person -> wave``, ``person+dog -> walkYourDog``).
            action_map: The dispatch allowlist (``camelCase`` action name ->
                method) used to VALIDATE a triggered action before it is ever
                dispatched (Req 7.6, 9.1-9.3). ``None`` builds the default via
                ``build_action_map``. A triggered action absent from this map is
                rejected (printed, no dispatch).

        Returns:
            An optional "pending trigger" ``dict`` when a ``Detection_Routine_Map``
            rule fired AND its action is in ``action_map``: ``{"action": <name>,
            "rule": <DetectionRule>, "routine_map": <DetectionRoutineMap>}``. The
            caller (``main()``) must dispatch ``action`` ONLY AFTER releasing the
            Neck_Group lock, so the triggered Routine and Tracking_Mode never own
            the Neck_Group simultaneously (Req 7.7), then call
            ``routine_map.mark_completed(rule, time.monotonic())`` to start the
            rule's cooldown. Returns ``None`` for every other wind-down (external
            stop, Scan_Sweep timeout, error): nothing further to dispatch.
        """
        # Build the TrackingConfig from the provided params, letting the
        # dataclass defaults fill any that are None and its __post_init__ clamp
        # every value into its documented safe range.
        cfg_kwargs = {}
        if max_step is not None:
            cfg_kwargs["max_step_deg"] = max_step
        if deadband is not None:
            cfg_kwargs["deadband_frac_w"] = deadband
            cfg_kwargs["deadband_frac_h"] = deadband
        if conf is not None:
            cfg_kwargs["conf_threshold"] = conf
        if scan_timeout is not None:
            cfg_kwargs["scan_timeout_s"] = scan_timeout
        if aim_frac is not None:
            cfg_kwargs["aim_frac_h"] = aim_frac
        if tilt_center is not None:
            cfg_kwargs["tilt_center_deg"] = tilt_center
        if tilt_min is not None:
            cfg_kwargs["tilt_min_deg"] = tilt_min
        if tilt_max is not None:
            cfg_kwargs["tilt_max_deg"] = tilt_max
        if settle_gain is not None:
            cfg_kwargs["settle_gain"] = settle_gain
        cfg = TrackingConfig(**cfg_kwargs)

        # Build the trigger allowlist + Detection_Routine_Map. These default to
        # the single-source-of-truth action_map and the seed rules so a plain
        # `--action=tracking` run still arbitrates/validates triggers; callers
        # (tests) may inject their own. The action_map is ONLY used to validate
        # a triggered name — tracking() never dispatches a Routine itself (that
        # happens in main() after the Neck_Group lock is released, Req 7.7).
        if action_map is None:
            action_map = self.build_action_map()
        if routine_map is None:
            routine_map = DetectionRoutineMap()

        # Clear any stale stop request from a previous Mode run so we start clean
        # (mirrors napping/awake).
        nap_signal.clear_stop()

        client = CameraClient(base_url=camera_url)
        print(f"[tracking] entering tracking mode (camera {client.base_url})")

        pending_trigger = None

        # Run the async loop here at the TOP of the call stack (like
        # _run_nap_loop) — never inside a running event loop, no watchdog.
        try:
            reason, pending_trigger = asyncio.run(
                self._run_tracking_loop(client, cfg, routine_map, action_map)
            )
            print(f"[tracking] {reason} interrupt -> wound down")
        except Exception as e:
            # Any loop error: recenter the neck and exit cleanly. The loop
            # helper already recenters in its own finally, but this is the final
            # backstop if asyncio.run itself raised before/after that path.
            print(f"[tracking] error during tracking loop: {e}")
            self._recenter_neck(tilt_angle=cfg.tilt_center_deg)
            pending_trigger = None
        finally:
            # Clear the stop signal on exit so the next Mode starts clean and the
            # requesting web-app action can proceed once the Neck_Group lock frees.
            nap_signal.clear_stop()

        # Hand any fired-and-validated trigger back to the caller (main()) to
        # dispatch AFTER the Neck_Group lock is released (Req 7.7). The neck has
        # already been recentered + will be released by main()'s context manager
        # before the Routine runs.
        return pending_trigger

    async def _run_tracking_loop(self, client, cfg, routine_map=None, action_map=None):
        """Drive the neck to track a person until interrupted; return the reason.

        The loop, each iteration (Req 6.1, 5.7):

        1. ``client.get_detections()`` — GET the latest frame's Detections +
           frame size from Camera_Service.
        2. ``select_target`` — pick the single Target_Person (largest bbox,
           center tie-break).
        3. ``compute_offset`` — signed pixel Offset of the Target_Person from
           Frame_Center, zeroed inside the Deadband.
        4. ``next_neck_targets`` — map the Offset to the next ``NECK_PAN`` /
           ``NECK_TILT`` target angles (direction + per-update step cap).
        5. ``TrunkController.set_angle`` on channels 0,1 — the ONLY hardware
           write, which clamps each commanded angle to ``SAFE_LIMITS`` (Req 5.5).
           Writes are restricted to the Neck_Group channels (Req 5.8, 6.11).

        It carries no audio and never touches the jaw motor (Req 6.2). When
        ``select_target`` returns no Target_Person the loop begins a leave-frame
        Scan_Sweep via ``_run_scan_sweep`` (Req 6.7): a slow ``NECK_PAN`` pan
        that reacquires a reappearing person (Req 6.9) or, after
        ``cfg.scan_timeout_s`` with no reacquire, recenters and ends the Mode so
        it yields to the previously active Mode (Req 6.10).

        Detection-triggered Routines (Req 7.2, 7.6, 7.7): each iteration also
        feeds the frame's detections to ``routine_map.select_action(...)``. When
        a rule fires, its action is validated against ``action_map`` (the
        allowlist) BEFORE anything else happens:

        * **In the allowlist:** the loop records the chosen action+rule as a
          pending trigger and breaks, so the ``finally`` recenters the
          Neck_Group and main() can release the Neck_Group lock BEFORE
          dispatching the Routine — Tracking and the Routine never co-own the
          Neck_Group (Req 7.7). Tracking_Mode itself issues NO subprocess/servo
          command for the Routine; it only yields.
        * **Not in the allowlist:** the name is rejected — it is ``print()``ed,
          NO subprocess/servo command runs, and tracking simply continues
          (Req 7.6, 9.1-9.3). The name is never passed to ``getattr``/``eval``/
          shell; validation is a plain ``in`` membership test on the dict.

        Current neck angles are tracked LOCALLY (starting from
        ``REST_POSITIONS``) to feed ``next_neck_targets``' ``cur_pan`` /
        ``cur_tilt`` rather than reading them back from the servo, so the math is
        deterministic and independent of hardware read-back. Each applied target
        updates the local state, and the actual written (post-clamp) angle from
        ``set_angle`` is used so the local state tracks what the hardware was
        actually commanded.

        Interruption is checked at the TOP of each iteration via ``nap_signal``
        (Req 6.4, 6.5). On a stop request OR any error, the ``finally`` block
        recenters the Neck_Group to ``REST_POSITIONS`` so the loop winds down and
        releases cleanly within 1 second.

        Args:
            client: A ``CameraClient`` for ``GET /detections``.
            cfg: The ``TrackingConfig`` tuning (deadband, max step, etc.).
            routine_map: The ``DetectionRoutineMap`` evaluated each iteration to
                decide whether a detection condition triggers a Routine. ``None``
                disables triggering (plain tracking only).
            action_map: The dispatch allowlist used to validate a triggered
                action name (Req 7.6). ``None`` disables triggering.

        Returns:
            A ``(reason, pending_trigger)`` tuple. ``reason`` is one of
            ``NAP_INTERRUPT_STOP`` (``nap_signal`` stop),
            ``TRACKING_INTERRUPT_SCAN_TIMEOUT`` (Scan_Sweep timeout, Req 6.10),
            or ``TRACKING_INTERRUPT_TRIGGER`` (a Detection_Routine_Map rule fired
            with an allowlisted action, Req 7.2/7.7). ``pending_trigger`` is
            ``None`` for stop/timeout, and for a trigger is a ``dict``
            ``{"action", "rule", "routine_map"}`` the caller dispatches AFTER the
            Neck_Group lock is released. Every reason winds the Mode down (the
            ``finally`` recenters the Neck_Group) so it yields the Neck_Group.
        """
        trunk = Movements.trunkController

        # Track the commanded neck angles locally so next_neck_targets gets a
        # stable cur_pan/cur_tilt without a hardware read-back. Pan seeds from
        # the global rest (90 = centered). Tilt seeds from the TRACKING tilt
        # center (cfg.tilt_center_deg, the level-gaze angle for a standing
        # person's face on this build — NOT the global rest 90), so tracking
        # starts aimed at head height and stays within its tilt band.
        cur_pan = float(constants.REST_POSITIONS[constants.NECK_PAN])
        cur_tilt = float(cfg.tilt_center_deg)

        # Drive the neck to the tracking start pose up front so the first
        # command works from the real level-gaze center rather than wherever the
        # neck happened to rest (every write clamped by set_angle).
        trunk.set_angle(constants.NECK_PAN, cur_pan)
        cur_tilt = trunk.set_angle(constants.NECK_TILT, cur_tilt)

        reason = self.NAP_INTERRUPT_STOP
        pending_trigger = None
        try:
            while True:
                # Check the external stop signal at the top of each iteration so
                # a Routine/Act request winds us down within ~1s (Req 6.4, 6.5).
                if nap_signal.stop_requested():
                    reason = self.NAP_INTERRUPT_STOP
                    break

                detections, frame_w, frame_h = client.get_detections()

                # Without valid frame dimensions the offset math is undefined;
                # treat as "no person" and hold (the 10.2 Scan_Sweep hook).
                if frame_w <= 0 or frame_h <= 0:
                    await asyncio.sleep(self._TRACKING_LOOP_PERIOD_S)
                    continue

                # Detection-triggered Routines (Req 7.2, 7.6, 7.7). Evaluate the
                # map on the SAME detections used for tracking, BEFORE the
                # tracking/scan branch, so a condition (e.g. person+dog) fires
                # whether we are actively tracking or about to scan. A fired rule
                # whose action is allowlisted winds the Mode down so main() can
                # dispatch the Routine only after releasing the Neck_Group.
                pending_trigger = self._evaluate_trigger(
                    detections, routine_map, action_map
                )
                if pending_trigger is not None:
                    reason = self.TRACKING_INTERRUPT_TRIGGER
                    break

                target = select_target(detections, frame_w, frame_h)

                if target is None:
                    # No Target_Person: begin the leave-frame Scan_Sweep
                    # (Req 6.7). It pans NECK_PAN across the safe range looking
                    # for a person, stopping the instant one reappears (Req 6.9)
                    # or when cfg.scan_timeout_s elapses with no reacquire
                    # (Req 6.10). It keeps NECK_PAN's local angle consistent and
                    # returns it so cur_pan tracks the last written angle.
                    reacquired, cur_pan = await self._run_scan_sweep(
                        client, cfg, trunk, cur_pan
                    )
                    if reacquired is None:
                        # External stop requested mid-sweep (nap_signal) — wind
                        # down like the top-of-loop check (Req 6.4, 6.5).
                        reason = self.NAP_INTERRUPT_STOP
                        break
                    if not reacquired:
                        # Scan timeout with no person: end the Mode so it yields
                        # to the previously active Mode (Req 6.10). The finally
                        # block recenters the Neck_Group to REST_POSITIONS.
                        reason = self.TRACKING_INTERRUPT_SCAN_TIMEOUT
                        break
                    # A person reappeared: resume normal tracking on the next
                    # iteration, which re-reads /detections and commands the neck.
                    continue

                offset = compute_offset(target, frame_w, frame_h, cfg)
                targets = next_neck_targets(
                    offset, cur_pan, cur_tilt, cfg, frame_w, frame_h
                )

                # Apply each target through set_angle (SAFE_LIMITS clamp, Req
                # 5.5) — the only hardware write, scoped to Neck_Group channels
                # only (Req 5.8, 6.11). Update the local angle to the actually
                # written (post-clamp) value.
                if constants.NECK_PAN in targets:
                    cur_pan = trunk.set_angle(
                        constants.NECK_PAN, targets[constants.NECK_PAN]
                    )
                if constants.NECK_TILT in targets:
                    cur_tilt = trunk.set_angle(
                        constants.NECK_TILT, targets[constants.NECK_TILT]
                    )

                await asyncio.sleep(self._TRACKING_LOOP_PERIOD_S)
        finally:
            # Recenter the Neck_Group to REST_POSITIONS on stop OR error so the
            # neck is never left energized at an offset, and the lock frees to a
            # known-safe pose within 1s (Req 6.4, 6.11, 5.5 clamp preserved).
            # For a trigger this is the "release the Neck_Group BEFORE the
            # Routine drives jaw/audio" wind-down (Req 7.7): the recenter happens
            # here, and main() releases the lock before dispatching the Routine.
            # Tilt winds down to the tracking level-gaze center, not global rest.
            self._recenter_neck(tilt_angle=cfg.tilt_center_deg)

        return reason, pending_trigger

    def _evaluate_trigger(self, detections, routine_map, action_map):
        """Evaluate the Detection_Routine_Map and allowlist-gate the result.

        Pure glue between the (logic-only) ``DetectionRoutineMap`` and the
        ``action_map`` allowlist (Req 7.2, 7.6, 9.1-9.3). It asks the map which
        rule — if any — should fire for this frame's detections, then validates
        the chosen rule's action against ``action_map`` with a plain ``in``
        membership test. It performs NO dispatch and issues NO subprocess/servo
        command; it only decides whether a trigger is pending.

        Allowlist enforcement (the security boundary):

        * A chosen action present in ``action_map`` becomes a pending trigger the
          caller later dispatches (after releasing the Neck_Group, Req 7.7).
        * A chosen action ABSENT from ``action_map`` is REJECTED: the rejected
          name is ``print()``ed, nothing is dispatched, and ``None`` is returned
          so tracking continues (Req 7.6). The name is never passed to
          ``getattr``/``eval``/``exec``/shell — only membership-tested.

        Args:
            detections: The current frame's Detections.
            routine_map: The ``DetectionRoutineMap`` to evaluate. ``None``
                disables triggering (returns ``None``).
            action_map: The dispatch allowlist to validate against. ``None``
                disables triggering (returns ``None``).

        Returns:
            A ``{"action", "rule", "routine_map"}`` dict when a rule fired and
            its action is allowlisted, else ``None``.
        """
        if routine_map is None or action_map is None:
            return None

        rule = routine_map.select_action(detections, time.monotonic())
        if rule is None:
            return None

        # Allowlist gate (Req 7.6, 9.1-9.3): membership test ONLY — never
        # getattr/eval/exec/shell on the externally-derived action name.
        if rule.action not in action_map:
            print(
                f"[tracking] REJECTED detection-triggered action "
                f"'{rule.action}': not in action_map allowlist - no Routine "
                f"dispatched, no servo/subprocess command run (Req 7.6)"
            )
            return None

        print(
            f"[tracking] detection trigger -> '{rule.action}' is allowlisted; "
            f"winding down Tracking_Mode to release the Neck_Group before the "
            f"Routine runs (Req 7.7)"
        )
        return {"action": rule.action, "rule": rule, "routine_map": routine_map}

    async def _run_scan_sweep(self, client, cfg, trunk, cur_pan):
        """Pan NECK_PAN across its safe range to reacquire a lost person.

        The leave-frame Scan_Sweep (Req 6.7-6.10). Builds on the deliberate
        surveillance pan of ``TrunkController.slow_scan`` but is driven here as
        an incremental ``set_angle`` sweep so it can interleave detection polling
        and ``nap_signal`` checks between every step. Starting from the current
        pan angle it steps toward one ``NECK_PAN`` safe-range endpoint, then
        reverses to the other, bouncing between the endpoints until one of three
        things happens:

        * **Reacquire (Req 6.9):** ``select_target`` finds a person on a polled
          frame — the sweep stops immediately and the caller resumes tracking
          that person as the Target_Person.
        * **Timeout (Req 6.10):** ``cfg.scan_timeout_s`` elapses since the sweep
          began with no reacquire — the sweep ends and the caller recenters the
          Neck_Group to ``REST_POSITIONS`` and yields to the previously active
          Mode.
        * **Stop:** ``nap_signal`` requests a stop — the sweep ends so the Mode
          winds down within ~1 s (Req 6.4, 6.5).

        Every neck write goes through ``TrunkController.set_angle`` so each
        commanded angle is clamped to ``constants.SAFE_LIMITS`` (Req 6.8), and
        ONLY ``NECK_PAN`` is driven — no tilt, no arm, no jaw/audio. The sweep is
        bounded strictly within the ``NECK_PAN`` ``SAFE_LIMITS`` range (the
        configurable scan range), stepping ``_SCAN_STEP_DEG`` degrees per
        ``_SCAN_STEP_PERIOD_S`` with an ``asyncio.sleep`` between steps so it
        stays async-friendly.

        Args:
            client: A ``CameraClient`` for ``GET /detections``.
            cfg: The ``TrackingConfig`` tuning; ``cfg.scan_timeout_s`` bounds the
                sweep (clamped to [1, 120], default 10).
            trunk: The shared ``TrunkController`` (writes via ``set_angle``).
            cur_pan: The current ``NECK_PAN`` angle (degrees) to sweep from.

        Returns:
            A ``(reacquired, pan_angle)`` tuple:

            * ``(True, pan_angle)`` — a person reappeared; ``pan_angle`` is the
              last written ``NECK_PAN`` angle (resume tracking).
            * ``(False, pan_angle)`` — the scan timed out with no reacquire;
              ``pan_angle`` is the last written angle (caller recenters + yields).
            * ``(None, pan_angle)`` — an external stop was requested mid-sweep;
              caller winds down.
        """
        pan_min, pan_max = constants.SAFE_LIMITS[constants.NECK_PAN]
        print(
            f"[tracking] no person -> Scan_Sweep across NECK_PAN "
            f"[{pan_min}, {pan_max}] (timeout {cfg.scan_timeout_s}s)"
        )

        # Clamp the starting angle into the safe band so the first step is
        # well-defined, and pick an initial direction that heads toward the
        # nearer endpoint's opposite so we cover the range (bounce at the ends).
        pan = max(pan_min, min(pan_max, float(cur_pan)))
        # Head toward the farther endpoint first for the widest initial sweep.
        increasing = (pan - pan_min) <= (pan_max - pan)

        start = time.monotonic()
        while True:
            # Wind down promptly on an external stop request (Req 6.4, 6.5).
            if nap_signal.stop_requested():
                return None, pan

            # Timeout with no reacquire -> end the sweep (Req 6.10).
            if (time.monotonic() - start) >= cfg.scan_timeout_s:
                print("[tracking] Scan_Sweep timed out -> recenter + yield")
                return False, pan

            # Poll for a reappearing person; stop the sweep the instant one is
            # found (Req 6.9).
            detections, frame_w, frame_h = client.get_detections()
            if frame_w > 0 and frame_h > 0:
                if select_target(detections, frame_w, frame_h) is not None:
                    print("[tracking] Scan_Sweep reacquired a person -> track")
                    return True, pan

            # Advance one bounded step, reversing at either safe-range endpoint.
            if increasing:
                pan += self._SCAN_STEP_DEG
                if pan >= pan_max:
                    pan = float(pan_max)
                    increasing = False
            else:
                pan -= self._SCAN_STEP_DEG
                if pan <= pan_min:
                    pan = float(pan_min)
                    increasing = True

            # Write through set_angle (SAFE_LIMITS clamp, Req 6.8); keep the
            # local angle consistent with the actually written (post-clamp)
            # value. Only NECK_PAN is touched (Neck_Group, no tilt/arm/jaw).
            pan = trunk.set_angle(constants.NECK_PAN, pan)

            await asyncio.sleep(self._SCAN_STEP_PERIOD_S)

    @staticmethod
    def _recenter_neck(tilt_angle=None):
        """Drive ONLY the Neck_Group channels to their resting pan/tilt.

        Used by Tracking_Mode on wind-down (stop request) and on any error so
        the neck returns to a known pose. Pan always returns to the global rest
        (``REST_POSITIONS[NECK_PAN]`` = 90, centered). Tilt returns to
        ``tilt_angle`` when given — Tracking_Mode passes its tracking tilt
        center (``cfg.tilt_center_deg``, the level-gaze angle) so the head winds
        down to head height rather than the global rest 90 (which is chin-up on
        this build). When ``tilt_angle`` is None it falls back to the global
        ``REST_POSITIONS[NECK_TILT]``.

        Writes go through ``TrunkController.set_angle`` so each angle is clamped
        to the global ``SAFE_LIMITS`` (Req 5.5) — the hardware clamp is always
        the final authority — and ONLY the Neck_Group channels
        (``NECK_PAN``/``NECK_TILT``) are touched, never an arm channel a
        concurrent Gesture may own (Req 5.8, 6.11). Never raises (recovery path).

        Args:
            tilt_angle: NECK_TILT angle to return to; defaults to the global
                ``REST_POSITIONS[NECK_TILT]`` when None.
        """
        trunk = Movements.trunkController
        rest_targets = {
            constants.NECK_PAN: constants.REST_POSITIONS.get(constants.NECK_PAN),
            constants.NECK_TILT: (
                tilt_angle if tilt_angle is not None
                else constants.REST_POSITIONS.get(constants.NECK_TILT)
            ),
        }
        for channel, angle in rest_targets.items():
            if angle is None:
                continue
            try:
                trunk.set_angle(channel, angle)
            except Exception as e:
                name = constants.servos.get(channel, f"ch{channel}")
                print(f"[tracking] could not recenter {name}: {e}")

    def build_action_map(self):
        """Build the dispatch allowlist of ``camelCase`` action name -> method.

        This is the SINGLE SOURCE OF TRUTH for which Routine/Mode action names
        are dispatchable. ``main()`` uses it to route ``--action``, and
        Tracking_Mode's detection trigger uses the SAME map to validate a
        ``Detection_Routine_Map`` action before dispatching it (Req 7.1, 7.6,
        9.1-9.3). Keeping one builder means a name is dispatchable from the
        detection trigger if and only if it is dispatchable from the CLI — there
        is exactly one allowlist, never two that can drift apart.

        The map is the security boundary: only names present as keys here are
        ever dispatched, and dispatch is always ``action_map[name]()`` (a direct
        dict lookup of a bound method) — an externally supplied name is NEVER
        passed to ``getattr``/``eval``/``exec`` or a shell (Req 9.3).

        Note that an action a ``DetectionRule`` references (e.g. the seed
        ``wave`` / ``walkYourDog``) is NOT guaranteed to be a key here; the
        trigger path treats any name missing from this map as rejected (Req 7.6).

        Returns:
            A dict mapping each ``camelCase`` action name to the bound
            ``Animatronic`` method that performs it.
        """
        return {
            # Wave routines
            'startParty':     self.start_party,
            # Beckon routines
            'waiting':        self.waiting,
            'exorcist':       self.exorcist,
            'vaderFather':    self.vader_father,
            'torture':        self.torture,
            'yodaFear':       self.yoda_fear,
            # Patrol / ambient
            'krusty':         self.krusty,
            'vaderBeaten':    self.vader_beaten,
            # Reaction
            'blah':           self.blah,
            # New routines
            'evilLaugh':      self.evil_laugh,
            'vincentPrice':   self.vincent_price,
            'yawn':           self.yawn,
            'snuckUp':        self.snuck_up,
            'awaken':         self.awaken,
            # Performance-framework routines
            'brains':         self.brains,
            'hypnotic':       self.hypnotic,
            'clearThroat':    self.clear_throat,
            'coughLong':      self.cough_long,
            'coughMedium':    self.cough_medium,
            'burp':           self.burp,
            'fart':           self.fart,
            'sleep':          self.snore,
            'moreCandy':      self.more_candy,
            # Tracking Mode — camelCase key kept in the allowlist for parity with
            # the webapp's dispatch, but dispatched by main()'s dedicated branch
            # (NOT the generic servo_lock() path) because it takes only the
            # Neck_Group lock. See the 'tracking' branch in main().
            'tracking':       self.tracking,
        }

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

    async def _do_look_around_random(self):
        # Gesture-only (no audio): idly scan the room, then return to rest. Run
        # with no idle lead-in so awake mode's ambient loop starts it promptly.
        # Kept SHORT (see _AWAKE_GESTURE_DURATION_S) so the loop returns to a
        # sensor/stop check point frequently — a long single action can't be
        # interrupted mid-move, so shorter actions = snappier reactions.
        mv = Movements("Animatronic")
        await mv.look_around_random(duration=self._AWAKE_GESTURE_DURATION_S)

    async def _do_hand_visor(self):
        # Gesture-only (no audio): raise a hand as a visor and look around, then
        # return to rest. No idle lead-in; kept short for responsiveness (see
        # _do_look_around_random).
        mv = Movements("Animatronic")
        await mv.hand_visor(duration=self._AWAKE_GESTURE_DURATION_S)


def _dispatch_detection_trigger(action_map, pending_trigger):
    """Dispatch a detection-triggered Routine AFTER the Neck_Group is released.

    Called by ``main()`` only once the ``group_lock(NECK_GROUP)`` context has
    exited, so Tracking_Mode no longer owns the Neck_Group when the Routine runs
    (Req 7.7). The Routine (e.g. ``wave``, ``walkYourDog``) needs the whole robot
    — neck + arm + jaw/audio — so it is run under the whole-robot
    ``servo_lock()`` exactly like a CLI-dispatched Routine.

    The action name was already allowlist-validated inside Tracking_Mode
    (``_evaluate_trigger``), but it is re-validated here with a plain ``in``
    membership test before dispatch as a defensive second gate — dispatch is
    always a direct ``action_map[name]()`` lookup, never ``getattr``/``eval``/
    shell (Req 7.6, 9.1-9.3). After the Routine completes, the rule's cooldown is
    started via ``routine_map.mark_completed`` so the same condition cannot
    immediately re-fire (Req 7.8).

    Args:
        action_map: The dispatch allowlist (single source of truth).
        pending_trigger: The ``{"action", "rule", "routine_map"}`` dict returned
            by ``Animatronic.tracking``.
    """
    action = pending_trigger["action"]
    rule = pending_trigger["rule"]
    routine_map = pending_trigger["routine_map"]

    # Defensive re-check at the dispatch boundary (Req 7.6, 9.1-9.3). Should
    # never fail (tracking() already gated it) but we never dispatch a name that
    # isn't an explicit allowlist key.
    if action not in action_map:
        print(
            f"[tracking] REJECTED detection-triggered action '{action}' at "
            f"dispatch: not in action_map allowlist - nothing run (Req 7.6)"
        )
        return

    print(f"[tracking] dispatching detection-triggered Routine '{action}' "
          f"(Neck_Group already released, Req 7.7)")
    try:
        # Whole-robot lock for the Routine (neck + arm + jaw/audio). The
        # Neck_Group lock Tracking held is already released, so this acquires
        # cleanly. Fail fast if some other process grabbed the servos in the gap.
        with servo_lock():
            action_map[action]()
    except ServoBusyError:
        print("Servos busy - could not run detection-triggered Routine. "
              "Skipping.")
        return
    finally:
        # Start the rule's cooldown measured from Routine completion (Req 7.8),
        # whether the Routine ran or was skipped busy, so a busy miss doesn't
        # hammer the servos every frame.
        routine_map.mark_completed(rule, time.monotonic())


def main(args):
    """Dispatch --action to the corresponding Animatronic routine.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    a = Animatronic()

    # Single source of truth for the dispatch allowlist (Req 7.1, 9.1-9.3). Both
    # CLI dispatch below AND the Tracking_Mode detection trigger validate against
    # THIS map, so a name is dispatchable from a detection trigger iff it is
    # dispatchable from the CLI — there is exactly one allowlist.
    action_map = a.build_action_map()

    if args.action == 'tracking':
        # Tracking is a MODE, but UNLIKE napping/awake it must NOT hold the
        # whole-robot servo_lock(). It writes only the Neck_Group (channels 0-1),
        # so it acquires ONLY the Neck_Group lock via group_lock(NECK_GROUP).
        # This lets an arm-only Gesture (disjoint Arm_Group channels 4-7) run
        # concurrently (Req 6.6). The call is still allowlist-gated: 'tracking'
        # is an explicit key in action_map above and this is an explicit branch —
        # args.action is never passed to getattr/eval/shell. It runs until
        # interrupted (nap_signal stop request or a Scan_Sweep timeout). Fail
        # fast if the Neck_Group is already in use.
        pending_trigger = None
        try:
            with group_lock(NECK_GROUP):
                # Pass the SAME action_map allowlist in so the detection trigger
                # validates against it (Req 7.6). tracking() returns a pending
                # trigger (or None); it never dispatches the Routine itself.
                pending_trigger = a.tracking(
                    camera_url=args.camera_url,
                    max_step=args.max_step,
                    deadband=args.deadband,
                    conf=args.conf,
                    scan_timeout=args.scan_timeout,
                    aim_frac=args.aim_frac,
                    tilt_center=args.tilt_center,
                    tilt_min=args.tilt_min,
                    tilt_max=args.tilt_max,
                    settle_gain=args.settle_gain,
                    action_map=action_map,
                )
            # The `with` block has now exited: the Neck_Group lock is RELEASED
            # and the neck was recentered inside tracking()'s wind-down. Only
            # NOW — with Tracking no longer owning the Neck_Group — do we dispatch
            # a detection-triggered Routine, so the Routine and Tracking_Mode
            # never own the Neck_Group simultaneously (Req 7.7).
            if pending_trigger is not None:
                _dispatch_detection_trigger(action_map, pending_trigger)
        except ServoBusyError:
            print("Neck group busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action in action_map:
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
    # Tracking Mode flags (only used with --action=tracking). TrackingConfig
    # clamps every numeric value into its documented safe range, so argparse
    # only needs sensible types here; a None default means "use the
    # TrackingConfig default" (so the dataclass owns the real default).
    parser.add_argument('--scan-timeout', dest='scan_timeout', type=int,
                        default=10,
                        help='Tracking mode: Scan_Sweep reacquire timeout in '
                             'seconds, clamped to 1-120 (default: 10). Only '
                             'used with --action=tracking.')
    parser.add_argument('--max-step', dest='max_step', type=int, default=None,
                        help='Tracking mode: max neck angle change per update '
                             'in degrees, clamped to 1-30 (default: 5). Only '
                             'used with --action=tracking.')
    parser.add_argument('--deadband', dest='deadband', type=float, default=None,
                        help='Tracking mode: center deadband half-width as a '
                             'fraction of the frame on both axes, clamped to '
                             '0.0-0.5 (default: 0.05). Only used with '
                             '--action=tracking.')
    parser.add_argument('--conf', dest='conf', type=float, default=None,
                        help='Tracking mode: detector confidence threshold, '
                             'clamped to 0.0-1.0 (default: 0.5). Only used with '
                             '--action=tracking.')
    parser.add_argument('--aim-frac', dest='aim_frac', type=float, default=None,
                        help='Tracking mode: vertical aim point within the '
                             'target box as a fraction of its height from the '
                             'top edge, clamped to 0.0-1.0 (default: 0.35 = aim '
                             'at the upper chest/head; 0.5 = box center). Lower '
                             'values raise the head; raise toward 0.5 to lower '
                             'it. Only used with --action=tracking.')
    parser.add_argument('--tilt-center', dest='tilt_center', type=float,
                        default=None,
                        help='Tracking mode: level-gaze NECK_TILT angle the '
                             'head seeds/winds down to, within [30,160] and the '
                             'tilt band (default: 105 = level on this build; '
                             'global rest 90 is chin-up). Only used with '
                             '--action=tracking.')
    parser.add_argument('--tilt-min', dest='tilt_min', type=float, default=None,
                        help='Tracking mode: lowest NECK_TILT value (head '
                             'highest) tracking may command, within [30,160] '
                             '(default: 100). Only used with --action=tracking.')
    parser.add_argument('--tilt-max', dest='tilt_max', type=float, default=None,
                        help='Tracking mode: highest NECK_TILT value (head '
                             'lowest) tracking may command, within [30,160] '
                             '(default: 110). Tracking clamps tilt to '
                             '[tilt-min, tilt-max] so the head stays at head '
                             'height. Only used with --action=tracking.')
    parser.add_argument('--settle-gain', dest='settle_gain', type=float,
                        default=None,
                        help='Tracking mode: proportional control gain, clamped '
                             'to 0.05-1.0 (default: 0.5). Lower = gentler, more '
                             'damped approach that settles without bopping back '
                             'and forth; higher = snappier. Only used with '
                             '--action=tracking.')
    parser.add_argument('--camera-url', dest='camera_url', default=DEFAULT_CAMERA_URL,
                        help='Tracking mode: base URL of Camera_Service '
                             f'(default: {DEFAULT_CAMERA_URL}). Only used with '
                             '--action=tracking.')
    args = parser.parse_args()
    print(args.action)
    main(args)
