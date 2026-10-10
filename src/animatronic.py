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
    PlaybackController,
)
import nap_signal
import mode_exit
import constants
import config_store
from range_sensor import ApproachDetector
from vision_models import Detection, TrackingConfig
from tracking_controller import (
    select_target,
    compute_offset,
    next_neck_targets,
    smooth_neck_targets,
)
from detection_routine_map import (
    DetectionRoutineMap,
    ARM_ONLY_CHANNELS,
    SCAN_GESTURE_CHANNELS,
    SCAN_SAFE_ARM_ACTIONS,
    ScanActionKind,
    choose_scan_action,
    choose_scan_action_weighted,
    scan_rules,
    DEFAULT_DOG_LABEL,
)
import asyncio
import threading
import argparse
import functools
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
        None,                      # 5  (removed: vaderBeaten)
        None,                      # 6  (removed: vaderFather)
        'were-waiting.wav',        # 7
        'yoda-900.wav',            # 8
        None,                      # 9  (removed: yoda / yoda-agent-evil.wav)
        None,                      # 10 (removed: yodaFear)
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
        'elf_smell_ghost_burrito.wav',  # 34  (fartGhost: gated reaction after fan-nose arrives)
        'sneeze.wav',                   # 35  (sneeze: yawn-cover arm + sneeze.wav, snapHead after 5s)
        'elf_hh_get_candy.wav',       # 36  (comeGetCandy: random beckon/comeHere + candy call, gated 1.2s)
        'elf_nice_day_walk.wav',      # 37  (niceDay: wave + "nice day for a walk", arm leads audio by 0.5s)
        'maximus.wav',                # 38  (maximus: headFocus x3 + audio, gated until NECK_TILT settles)
    ]

    # Seconds to pause before movement begins, giving audio time to start.
    idle = 3

    # --- niceDay timing (arm LEADS audio) --------------------------------- #
    # Seconds the wave arm moves before audio for niceDay. The gesture's first
    # servo write is immediate (move_to drives set_angle on step 1 with no
    # preceding sleep), so 0.0 starts the raise at t=0. On hardware the operator
    # can nudge this up if the raise still looks a beat late.
    _NICE_DAY_ARM_LEAD = 0.0
    # Seconds to HOLD the audio after the arm starts, so motion LEADS the sound
    # by this much. This replaces the old 3s _run idle for niceDay — that idle
    # was the ~2-3s the operator saw before the arm moved, and it was designed
    # for the OPPOSITE goal (let audio start first). For niceDay the arm leads,
    # so audio_delay = _NICE_DAY_ARM_LEAD + _NICE_DAY_AUDIO_LEAD. Operator
    # fine-tunes on hardware.
    _NICE_DAY_AUDIO_LEAD = 0.5

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

    def nice_day(self):
        """Wave hello while saying it's a nice day for a walk.

        Pairs the existing ``wave`` gesture (via ``_do_wave``) with
        ``elf_nice_day_walk.wav``. The arm LEADS the audio: the wave starts
        moving ~0.5s before the clip. ``_do_wave`` drops the default 3s idle so
        the raise begins at t=0, and the audio is GATED by
        ``audio_delay = _NICE_DAY_ARM_LEAD + _NICE_DAY_AUDIO_LEAD`` (0.0 + 0.5 =
        0.5s) so the sound comes in 0.5s after the arm starts. The ~2-3s the
        operator previously saw before the arm moved was the old
        ``_run``/``self.idle`` pause, now replaced. The gesture returns to rest
        on its own. Operator fine-tunes the two constants on hardware.
        """
        self.run_action_and_audio(
            "_do_wave", self.music[37],
            # Arm leads audio by _NICE_DAY_AUDIO_LEAD: hold the clip until the
            # wave has been moving ~0.5s. = _NICE_DAY_ARM_LEAD (arm pre-move,
            # ~0s) + _NICE_DAY_AUDIO_LEAD (0.5s lead).
            audio_delay=self._NICE_DAY_ARM_LEAD + self._NICE_DAY_AUDIO_LEAD,
        )

    # --- Patrol / ambient routines ---

    def krusty(self):
        """Krusty laugh audio — neck ellipse."""
        self.run_action_and_audio("_do_neck_ellipse", self.music[2])

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

        BLAH = self._blah_performance(mv, scan=False)

        asyncio.run(PerformanceRunner(BLAH, mv, audio_dir).run())

    def _blah_performance(self, mv, scan=False):
        """Build the ``blah`` ``PerformanceDefinition`` (standalone or scan).

        The standalone routine (``scan=False``) runs the randomized head shake
        (neck channels 0-1) concurrently with the palm-present forearm bob (arm
        channels 4-7), looping both for ``blah.wav`` with audio gated ~250ms by
        the head shake — zero behaviour change from the pre-factored ``blah``.

        The scan variant (``scan=True``) is arm-only: it drops the neck
        ``head_shake`` ``MovementSpec`` so the group owns ONLY the arm channels
        {4,5,6,7}, leaving the neck free for the scan tracker. Because the
        dropped movement supplied the gate, the gate is set to ``None`` so no
        ``GateSpec`` references a movement that no longer exists and no remaining
        spec sets ``supplies_gate=True`` (``present_palm`` is already
        ``supplies_gate=False``). Audio therefore starts at t=0 for the scan
        variant — an intended, operator-visible timing change for scan only.
        Mirrors ``_hypnotic_performance(scan=True)``.

        Args:
            mv: The shared ``Movements`` instance backing every phase callable so
                the neck and arm adapters share one ``TrunkController``.
            scan: When ``True``, build the arm-only scan variant (neck
                ``MovementSpec`` dropped, ``gate=None``). Defaults to ``False``.

        Returns:
            The assembled ``PerformanceDefinition``.
        """
        head_shake_spec = MovementSpec(
            name="head_shake",
            owned_channels=frozenset({
                constants.NECK_PAN,
                constants.NECK_TILT,          # 0,1
            }),
            lead_in=mv.shake_no_lead_in,      # 250ms gate + center
            loop_body=mv.shake_no_loop_body,  # pan sweep + tilt centering (82-98)
            do_return=mv.shake_no_return,     # neck to center
            supplies_gate=True,               # opens the audio gate
        )
        present_palm_spec = MovementSpec(
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
        )

        # Scan variant: arm-only, so the neck stays free for the tracker. The
        # head_shake spec supplied the gate, so with it dropped the gate must be
        # None (no remaining spec supplies_gate=True) and audio starts at t=0.
        if scan:
            movements = (present_palm_spec,)
            gate = None
        else:
            movements = (head_shake_spec, present_palm_spec)
            # Head shake supplies the gate: its lead-in sleeps 250ms before
            # completing, so audio starts ~250ms after the routine begins.
            gate = GateSpec(movement_name="head_shake")

        return PerformanceDefinition(
            name="blah",
            audio_file=self.music[1],            # blah.wav
            gate=gate,
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=movements),
                ),
            ),
        )

    # --- New gesture routines ---

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

    def exorcist(self):
        """Exorcist — concurrent neckEllipse + talkingHandsII synced to audio.

        Runs two Gestures at the SAME time over disjoint channels for the whole
        ``beetel-exorcist.wav`` clip: ``neck_ellipse`` traces oval head arcs on
        the neck (channels 0-1) while ``talking_hands_ii`` oscillates the arm
        (channels 3-7). Both gestures loop to the audio DEADLINE so the motion
        keeps going for the full ~6.7s clip (neither finishes early), then each
        eases its own channels back to rest. Ungated: motion and audio start
        together at t=0.
        """
        self.run_action_and_audio("_do_exorcist", self.music[0])  # beetel-exorcist.wav

    def sneeze(self):
        """Sneeze reaction — cover-mouth arm held until sneeze.wav ends, head snap 5s in.

        Uses the nose_cover arm phases (``Movements.nose_cover_*``) -- a fork of
        the yawn cover-mouth family with RT_ELBOW_ROTATOR bumped +5 -- paired
        with ``sneeze.wav`` played UNGATED: the arm begins rising at t≈0
        the instant audio starts (no gate, no ``_YC_MOTION_DELAY``), the hand is
        HELD at the mouth for the full ``sneeze.wav`` duration, then lowers the
        moment the audio finishes. Concurrently, 5 seconds after audio start, the
        ``snapHead`` gesture (``Movements.snap_head``) fires on NECK_TILT — see
        ``_do_sneeze`` for the ordering.

        Driven by a ``PlaybackController`` (the project's real playback-
        completion signal: its ``is_active()`` is ``True`` until the WAV thread
        drains) rather than ``run_action_and_audio``, so the hold tracks actual
        audio completion instead of a hardcoded sleep. ``asyncio.run`` is called
        here at the top of the call stack (never inside a running loop).
        """
        mv = Movements("Animatronic")
        audio_path = os.path.join(self._resolve_audio_dir(), self.music[35])  # sneeze.wav
        playback = PlaybackController(audio_path)
        asyncio.run(self._do_sneeze(mv, playback))

    def come_get_candy(self):
        """Greet trick-or-treaters and call them to get candy. Randomly beckon or comeHere, with elf_hh_get_candy.wav gated 1.2s."""
        self.run_action_and_audio("_do_come_get_candy", self.music[36], audio_delay=1.2)

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

        BRAINS = self._brains_performance(mv, scan=False)

        asyncio.run(PerformanceRunner(BRAINS, mv, audio_dir).run())

    def _brains_performance(self, mv, scan=False):
        """Build the ``brains`` ``PerformanceDefinition`` (standalone or scan).

        The standalone routine (``scan=False``) runs ``menacing_reach`` (arm
        channels 4-7) concurrently with ``look_around_random`` (neck channels
        0-1), both looping for ``brains.wav`` with audio ungated at t=0 — zero
        behaviour change from the pre-factored ``brains``.

        The scan variant (``scan=True``) is arm-only: it drops the neck
        ``look_around_random`` ``MovementSpec`` so the group owns ONLY the arm
        channels {4,5,6,7}, leaving the neck free for the scan tracker to drive.
        Audio stays ungated (``gate=None``).

        Args:
            mv: The shared ``Movements`` instance backing every phase callable so
                the arm and neck adapters share one ``TrunkController``.
            scan: When ``True``, build the arm-only scan variant (neck
                ``MovementSpec`` omitted). Defaults to ``False`` (standalone).

        Returns:
            The assembled ``PerformanceDefinition``.
        """
        arm_spec = MovementSpec(
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
        )
        neck_spec = MovementSpec(
            name="look_around_random",
            owned_channels=frozenset({
                constants.NECK_PAN,
                constants.NECK_TILT,          # 0,1
            }),
            lead_in=None,                     # starts at t=0
            loop_body=mv.look_scan_loop_body, # one random glance
            do_return=mv.look_scan_return,    # neck to center
            supplies_gate=False,
        )

        # Scan variant: arm-only, so the neck stays free for the tracker. The
        # group owns ONLY {4,5,6,7}; the neck MovementSpec is dropped.
        movements = (arm_spec,) if scan else (arm_spec, neck_spec)

        return PerformanceDefinition(
            name="brains",
            audio_file=self.music[20],  # brains.wav — ~15.2 s
            gate=None,                  # ungated: audio starts at t=0
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=movements),
                ),
            ),
        )

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

        HYPNOTIC = self._hypnotic_performance(mv, scan=False)

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

    def _hypnotic_performance(self, mv, scan=False):
        """Build the ``hypnotic`` ``PerformanceDefinition`` (standalone or scan).

        The standalone routine (``scan=False``) runs ``hypnotic_arm`` (arm
        channels 4-7) concurrently with ``hyp_head_sway`` (neck channels 0-1).
        The head sway supplies the audio gate (``gate=GateSpec("hyp_head_sway")``)
        so playback starts ~100ms in — zero behaviour change from the
        pre-factored ``hypnotic``. The two-track audio chain, near-end cutoff,
        jaw/eyes-off player options and the ambient blink are unchanged.

        The scan variant (``scan=True``) is arm-only: it drops the neck
        ``hyp_head_sway`` ``MovementSpec`` so the group owns ONLY the arm
        channels {4,5,6,7}, leaving the neck free for the scan tracker. Because
        the dropped movement supplied the gate, the gate is set to ``None`` so
        no ``GateSpec`` references a movement that no longer exists and no
        remaining spec sets ``supplies_gate=True`` (``hypnotic_arm`` is already
        ``supplies_gate=False``). Audio therefore starts at t=0 for the scan
        variant — an intended, operator-visible timing change for scan only.
        ``followup_audio_files``, the arm's ``stop_loop_lead_seconds=1.5``, the
        jaw/eyes-off ``player_options`` and the ambient eye-blink are unchanged.

        Args:
            mv: The shared ``Movements`` instance backing every phase callable so
                the arm and neck adapters share one ``TrunkController``.
            scan: When ``True``, build the arm-only scan variant (neck
                ``MovementSpec`` dropped, ``gate=None``). Defaults to ``False``.

        Returns:
            The assembled ``PerformanceDefinition``.
        """
        arm_spec = MovementSpec(
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
        )
        neck_spec = MovementSpec(
            name="hyp_head_sway",
            owned_channels=frozenset({
                constants.NECK_PAN,
                constants.NECK_TILT,          # 0,1
            }),
            lead_in=mv.hyp_scan_lead_in,      # 100ms gate + center
            loop_body=mv.hyp_scan_loop_body,  # one gentle glance
            do_return=mv.hyp_scan_return,     # neck to center
            supplies_gate=True,               # opens the audio gate
        )

        # Scan variant: arm-only, so the neck stays free for the tracker. The
        # neck spec supplied the gate, so with it dropped the gate must be None
        # (no remaining spec supplies_gate=True) and audio starts at t=0.
        if scan:
            movements = (arm_spec,)
            gate = None
        else:
            movements = (arm_spec, neck_spec)
            # Head sway supplies the gate: its lead-in sleeps 100ms before
            # completing, so audio starts ~100ms after the routine begins.
            gate = GateSpec(movement_name="hyp_head_sway")

        return PerformanceDefinition(
            name="hypnotic",
            audio_file=self.music[21],  # hypnotic.wav — ~5.05 s
            # in_my_power.wav (~7.02 s) plays back-to-back immediately after
            # hypnotic.wav on the same audio thread, so the arm/head/eyes keep
            # going across BOTH tracks (~12.07 s total) and the arm's near-end
            # cutoff above fires against the end of in_my_power.wav, not hypnotic.
            # Per-track options: the jaw is ON for in_my_power.wav (so the mouth
            # articulates on this line) but eyes stay OFF -- the ambient blinker
            # owns EYE_LIGHT_PIN, so re-enabling envelope eyes here would clash.
            followup_audio_files=(
                (self.music[27], {"drive_jaw": True, "drive_eyes": False}),
            ),
            gate=gate,
            # Jaw silent; AudioPlayer does NOT claim the eye pin so the ambient
            # blinker can own EYE_LIGHT_PIN without a clash.
            player_options={"drive_jaw": False, "drive_eyes": False},
            steps=(
                PerformanceStep(
                    loop_for_audio=True,
                    group=ConcurrentGroup(movements=movements),
                ),
            ),
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

    def _cover_mouth_performance(self, mv, *, scan=False, kind):
        """Build a cover-mouth ``PerformanceDefinition`` (standalone or scan).

        The ONE parameterized builder behind every cover-mouth routine
        (``burp``, ``coughMedium``, ``coughLong``, ``fart`` phase 2,
        ``fartGhost`` phase 2), so there is a single definition rather than five
        near-duplicates. ``kind`` selects the per-routine audio clip, lead-in and
        near-end cutoff from a small internal table:

        * ``coughMedium`` / ``coughLong`` / ``fart`` / ``fartGhost`` — the SETTLED
          lead-in (``yawn_cover_lead_in_settled``, ``supplies_gate=True``): the
          clip is GATED until the hand reaches its final cover position; a single
          clip; ``stop_loop_lead_seconds=0.9``.
        * ``burp`` — the DELAYED lead-in (``yawn_cover_lead_in_delayed``, does NOT
          supply the gate), ``gate=None`` (``gurgle_burp.wav`` at t=0), a
          two-track follow-on (``excuseme_sb.wav``), ``stop_loop_lead_seconds``
          = the "excuse me" length (~3.45s).

        ``owned_channels`` is ``{0,1,4,5,6,7}`` when ``scan=False`` (the shared
        cover pose centers the head, so it lists the neck) and ``{4,5,6,7}`` when
        ``scan=True`` (the neck is dropped so the tracker keeps it). For
        ``scan=True`` the selected lead-in is bound (via ``functools.partial``)
        with ``center_head=False`` so it never writes NECK_PAN/NECK_TILT, and
        ``elbow_cover=mv._YC_ELBOW_COVER_SCAN`` (152) for burp/coughMedium/
        coughLong (the -10 that clears the off-center head) or ``elbow_cover=None``
        (162) for fart/fartGhost (operator verifies those on hardware). For
        ``scan=False`` the lead-in is used unbound (``center_head=True``,
        ``elbow_cover=None`` → 162), so the existing routines are byte-for-byte
        unchanged.

        Args:
            mv: The shared ``Movements`` instance backing every phase callable so
                they share one ``TrunkController``.
            scan: When ``True``, build the arm-only scan variant (neck dropped,
                head-centering suppressed, -10 for burp/cough*). Defaults to
                ``False`` (standalone).
            kind: One of ``"burp"``, ``"coughMedium"``, ``"coughLong"``,
                ``"fart"``, ``"fartGhost"`` — selects the audio clip, lead-in and
                near-end cutoff.

        Returns:
            The assembled ``PerformanceDefinition``.

        Raises:
            ValueError: If ``kind`` is not a known cover-mouth routine.
        """
        # Per-kind table: (lead_in_method, audio_file, followup_audio_files,
        # gate_name_or_None, stop_loop_lead_seconds, elbow_cover_scan).
        # gate_name is the GateSpec movement name (settled lead-ins supply the
        # gate) or None (burp is ungated). elbow_cover_scan is True when the scan
        # variant pulls the elbow back 10 degrees (burp/cough*), False otherwise.
        SETTLED = mv.yawn_cover_lead_in_settled
        DELAYED = mv.yawn_cover_lead_in_delayed
        table = {
            "coughMedium": (SETTLED, self.music[30], (), "coughMedium", 0.9, True),
            "coughLong":   (SETTLED, self.music[29], (), "coughLong", 0.9, True),
            "fart":        (SETTLED, self.music[32], (), "fart", 0.9, False),
            "fartGhost":   (SETTLED, self.music[34], (), "fartGhost", 0.9, False),
            "burp":        (DELAYED, self.music[31], (self.music[32],),
                            None, 3.45, True),
        }
        if kind not in table:
            raise ValueError(f"unknown cover-mouth kind: {kind!r}")

        lead_in, audio_file, followup, gate_name, stop_lead, elbow_scan = table[kind]

        # Scan binds the lead-in keywords so it never centers the head (neck
        # dropped) and, for burp/cough*, pulls the elbow back 10 degrees.
        if scan:
            elbow_cover = mv._YC_ELBOW_COVER_SCAN if elbow_scan else None
            lead_in = functools.partial(
                lead_in, elbow_cover=elbow_cover, center_head=False
            )
            owned_channels = frozenset({
                constants.RT_SHOULDER_ROTATOR,
                constants.RT_SHOULDER_TILT,
                constants.RT_ELBOW_TILT,
                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
            })
        else:
            owned_channels = frozenset({
                constants.NECK_PAN,
                constants.NECK_TILT,          # 0,1
                constants.RT_SHOULDER_ROTATOR,
                constants.RT_SHOULDER_TILT,
                constants.RT_ELBOW_TILT,
                constants.RT_ELBOW_ROTATOR,   # 7,6,5,4
            })

        # Settled lead-ins supply the gate (audio once the hand settles); burp is
        # ungated (gate_name None) with its arm motion delayed in the lead-in.
        supplies_gate = gate_name is not None
        gate = GateSpec(movement_name=gate_name) if gate_name is not None else None

        return PerformanceDefinition(
            name=kind,
            audio_file=audio_file,
            followup_audio_files=followup,
            gate=gate,
            steps=(
                PerformanceStep(
                    loop_for_audio=True,   # HOLD the hand until the clip ends
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name=kind,
                            owned_channels=owned_channels,
                            lead_in=lead_in,                   # fold (scan: no head-center)
                            loop_body=mv.yawn_cover_loop_body,  # hold while audio plays
                            do_return=mv.yawn_cover_return,     # lower hand at end
                            supplies_gate=supplies_gate,
                            stop_loop_lead_seconds=stop_lead,
                        ),
                    )),
                ),
            ),
        )

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

        Builds via the shared ``_cover_mouth_performance`` with ``scan=False`` so
        there is a single cover-mouth definition and the standalone cough
        behaviour is byte-for-byte unchanged. The single movement owns head
        channels {0,1} and arm channels {4,5,6,7}; with no concurrent movement
        the group is trivially channel-disjoint. A single ``Movements`` instance
        backs the phase callables so they share one ``TrunkController``; the
        runner is launched with ``asyncio.run`` at the top of the call stack.

        Args:
            audio_file: Filename of the cough clip (kept for signature parity;
                the clip is selected by ``name`` via the shared builder's table).
            name: Performance/gate name (``"coughMedium"`` or ``"coughLong"``).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        definition = self._cover_mouth_performance(mv, scan=False, kind=name)

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

    def maximus(self):
        """"Maximus" -- head focus (3 reps) with audio gated until the head settles.

        Pairs the existing ``head_focus`` gesture with ``maximus.wav`` via the
        Performance_Framework (gate-until-settled). The phased ``head_focus``
        adapters supply the gate: ``head_focus_lead_in`` lowers NECK_TILT to its
        hold angle and AWAITS it, so with ``supplies_gate=True`` the audio starts
        the instant NECK_TILT has REACHED its destination -- the head has settled
        before the clip begins.

        * ``head_focus_lead_in`` lowers NECK_TILT to the hold angle (the gate).
        * ``head_focus_loop_body`` runs the fixed ``_HF_PERF_REPS`` (3) concurrent
          pan+tilt centering reps. The step uses ``loop_for_audio=False`` so the
          body runs exactly ONCE: the user asked for a deterministic 3 reps, not
          audio-length looping, and the body itself iterates all 3 reps. (Using
          ``loop_for_audio=True`` would instead couple the rep count to
          ``maximus.wav``'s length, which is not what the routine wants.)
        * ``head_focus_return`` returns the head to REST (90/90).

        The single movement owns head channels {0,1}; with no concurrent movement
        the group is trivially channel-disjoint. The runner sweeps residual
        channels home on completion or failure. A single ``Movements`` instance
        backs the phase callables so they share one ``TrunkController``; the
        runner is launched with ``asyncio.run`` at the top of the call stack
        (never inside a running event loop).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        MAXIMUS = self._maximus_performance(mv, scan=False)

        asyncio.run(PerformanceRunner(MAXIMUS, mv, audio_dir).run())

    def _maximus_performance(self, mv, scan=False):
        """Build the ``maximus`` ``PerformanceDefinition`` (standalone or scan).

        The standalone routine (``scan=False``) runs ``head_focus`` (neck
        channels 0-1) for a fixed 3 reps, gated until NECK_TILT settles — zero
        behaviour change from the pre-factored ``maximus``.

        The scan variant (``scan=True``) is arm-only: because standalone
        maximus is purely head motion, there is no arm spec to keep, so the
        neck ``head_focus`` spec is REPLACED with a ``present_palm`` arm spec
        (owns {4,5,6,7}, ``supplies_gate=False``) to give maximus a visible arm
        presence while the tracker owns the head. ``head_focus`` supplied the
        gate, so with it dropped the gate is set to ``None`` and no remaining
        spec supplies it; audio starts at t=0. ``loop_for_audio=True`` so the
        present-palm arm bobs for the clip's duration (the standalone's fixed-3-
        reps semantics are specific to head_focus). The group owns exactly
        {4,5,6,7}; no -10 (not a cover-mouth pose). Mirrors
        ``_hypnotic_performance(scan=True)`` / ``_blah_performance(scan=True)``.

        Args:
            mv: The shared ``Movements`` instance backing every phase callable so
                the neck and arm adapters share one ``TrunkController``.
            scan: When ``True``, build the arm-only scan variant (head_focus
                replaced by present_palm, ``gate=None``, ``loop_for_audio=True``).
                Defaults to ``False`` (standalone).

        Returns:
            The assembled ``PerformanceDefinition``.
        """
        if scan:
            # Arm-only scan: swap the head_focus neck motion for a present_palm
            # arm bob so the neck stays free for the tracker. head_focus supplied
            # the gate, so with it gone the gate is None (audio at t=0) and no
            # remaining spec supplies_gate.
            movements = (
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
            )
            gate = None
            loop_for_audio = True  # bob for the whole clip
        else:
            movements = (
                MovementSpec(
                    name="head_focus",
                    owned_channels=frozenset({
                        constants.NECK_PAN,
                        constants.NECK_TILT,   # 0,1
                    }),
                    lead_in=mv.head_focus_lead_in,      # lower NECK_TILT to hold (gate)
                    loop_body=mv.head_focus_loop_body,   # 3 concurrent pan+tilt reps
                    do_return=mv.head_focus_return,      # head to REST 90/90
                    supplies_gate=True,                  # settled lead-in opens gate
                ),
            )
            # head_focus supplies the gate: audio starts the moment the lead-in
            # completes, i.e. once NECK_TILT has SETTLED at the hold angle.
            gate = GateSpec(movement_name="head_focus")
            loop_for_audio = False  # fixed 3 reps (see head_focus_loop_body)

        return PerformanceDefinition(
            name="maximus",
            audio_file=self.music[38],  # maximus.wav
            gate=gate,
            steps=(
                PerformanceStep(
                    loop_for_audio=loop_for_audio,
                    group=ConcurrentGroup(movements=movements),
                ),
            ),
        )

    def burp(self):
        """"Burp" — cover the mouth, burp, then an "excuse me" follow-on.

        Driven by the Performance_Framework with the phased ``yawn_cover``
        adapters (same operator-verified cover pose + override as ``clearThroat``
        and the coughs), but UNGATED (audio at t=0) with the ARM MOTION delayed
        ``_YC_MOTION_DELAY`` (0.5s) so the burp sound leads and the hand follows
        a beat later:

        * ``gate=None`` -- ``gurgle_burp.wav`` starts the instant the routine
          begins, before any movement runs.
        * ``yawn_cover_lead_in_delayed`` holds the arm at rest ``_YC_MOTION_DELAY``
          (0.5s) while the burp plays, THEN centers the head and folds the hand
          up to the mouth. It does NOT supply the gate.
        * Audio is a TWO-TRACK chain: ``gurgle_burp.wav`` then ``excuseme_sb.wav``
          played back-to-back with no gap (``followup_audio_files``). The
          ``PlaybackController`` stays ``is_active()`` across BOTH tracks and its
          duration is their SUM.
        * ``yawn_cover_loop_body`` HOLDS the hand at the mouth through the burp
          only: ``stop_loop_lead_seconds`` is set to the "excuse me" track length
          (~3.45s) so ``yawn_cover_return`` starts lowering the hand the instant
          ``gurgle_burp.wav`` completes, and the "excuse me" plays as the hand
          lowers. ``yawn_cover_return`` releases the override at the end.

        Builds via the shared ``_cover_mouth_performance`` with ``scan=False`` so
        the standalone burp behaviour is byte-for-byte unchanged. The single
        movement owns head channels {0,1} and arm channels {4,5,6,7} (trivially
        disjoint with no concurrent movement). A single ``Movements`` instance
        backs the phase callables; launched with ``asyncio.run`` at the top of
        the call stack.
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        BURP = self._cover_mouth_performance(mv, scan=False, kind="burp")

        asyncio.run(PerformanceRunner(BURP, mv, audio_dir).run())

    def fart(self):
        """"Fart" — fart first, THEN randomly cover the mouth OR fan the nose.

        A TWO-PHASE routine, because the fart happens BEFORE the reaction gesture
        (unlike the coughs/burp, where audio plays while the hand is already at
        the mouth):

        1. Play ``fart.wav`` to completion with NO movement as PURE audio -- a
           blocking ``AudioPlayer`` built with ``drive_jaw=False`` /
           ``drive_eyes=False`` so the jaw motor and eye LED never flash (a fart
           doesn't come out of the mouth). It claims neither GPIO pin, so both
           stay free for the Performance below; closed afterward for symmetry.
        2. RANDOMLY pick one of two reaction Gestures (50/50 via the shared
           ``random`` module, so a seeded run is reproducible):

           * cover-mouth -- the Cover-Mouth gesture via the Performance_Framework
             with the SETTLED lead-in, so ``excuseme_sb.wav`` is GATED until the
             hand reaches its final cover position (``_run_cover_mouth_settled``,
             same mechanics as the coughs); or
           * fan-nose -- the fan_nose gesture via the Performance_Framework
             UNGATED, so ``excuseme_sb.wav`` starts at t=0 the instant the arm
             begins moving (``_run_fan_nose``, the same mechanism ``fart_ghost``
             uses for its fan reaction).

           BOTH branches play ``excuseme_sb.wav`` (``self.music[32]``) as the
           gesture starts.

        The two audio phases never overlap, so there is no jaw-motor / audio
        contention. The whole routine runs inside the caller's servo lock; the
        Performance run does not take the lock itself. Launched with
        ``asyncio.run`` at the top of the call stack (phase 2's runner).
        """
        # Phase 1: fart.wav alone, no movement. Pure audio -- the jaw and eyes
        # are NOT driven (a fart doesn't come out of the mouth), so build the
        # player with drive_jaw/drive_eyes disabled: it claims neither GPIO pin,
        # leaving them free for phase 2's players. Blocking playback on this
        # thread; close() afterward for symmetry.
        fart_path = os.path.join(self._resolve_audio_dir(), self.music[33])  # fart.wav
        player = AudioPlayer(drive_jaw=False, drive_eyes=False)
        try:
            print(f"[fart] playing {fart_path} as pure audio (no jaw/eyes, no cover yet)")
            player.play_audio_file(fart_path)
        finally:
            player.close()

        # Phase 2: RANDOMLY react -- either cover the mouth (and, once the hand
        # settles, say "excuse me") or fan the nose (saying "excuse me" the
        # instant the arm begins moving). Both branches play excuseme_sb.wav
        # (self.music[32]) as the gesture starts; the choice uses the shared
        # ``random`` module so a seeded run is reproducible (see the
        # phased-vs-standalone equivalence tests). The two reaction branches run
        # the SAME phased adapters as the standalone cover-mouth / fan-nose
        # paths, so the motion is identical to those routines.
        if random.random() < 0.5:
            print("[fart] reaction: cover-mouth")
            self._run_cover_mouth_settled(self.music[32], name="fart")  # excuseme_sb.wav
        else:
            print("[fart] reaction: fan-nose")
            self._run_fan_nose(self.music[32], name="fart")  # excuseme_sb.wav

    def _run_fan_nose(self, audio_file, *, name):
        """Fan the nose; start the clip at t=0, hold the pose, then lower.

        Shared implementation for the fan-nose reaction used by ``fart`` (one of
        its two random branches) and ``fart_ghost``. It drives the phased
        ``fan_nose`` adapters through the Performance_Framework UNGATED
        (``gate=None``): the clip starts at t=0 -- the instant the fan phase
        begins moving -- so the reaction plays as soon as the arm MOVES, rather
        than waiting for it to reach the fan destination (shape #1 in
        audio-sequencing.md -- the same ungated start as ``brains`` /
        ``snuckUp``). ``fan_nose_lead_in`` runs the fan motion concurrently with
        the audio; the hand then HOLDS at the destination while the clip plays
        (``fan_nose_loop_body`` under ``loop_for_audio=True``) and
        ``fan_nose_return`` lowers the arm to rest at the end.

        DRY: the phased ``fan_nose_lead_in`` / ``fan_nose_loop_body`` /
        ``fan_nose_return`` adapters drive the SAME shared ``_fan_nose_*``
        primitives as the standalone ``fanNose`` gesture, so the fan motion is
        identical to the gesture-only CLI path. Every servo write still goes
        through ``move_to``/``set_angle`` (clamped to SAFE_LIMITS) and the runner
        sweeps residual channels home on completion or failure. A single
        ``Movements`` instance backs the phase callables so they share one
        ``TrunkController``; the runner is launched with ``asyncio.run`` at the
        top of the call stack.

        Args:
            audio_file: Filename of the reaction clip in the resolved audio dir.
            name: Performance name (also the single movement's name).
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()

        definition = PerformanceDefinition(
            name=name,
            audio_file=audio_file,
            # Ungated: audio starts at t=0, the instant the fan phase begins
            # moving -- the reaction plays as soon as the arm MOVES, not when it
            # arrives at the destination.
            gate=None,
            steps=(
                PerformanceStep(
                    loop_for_audio=True,   # HOLD at the destination until the clip ends
                    group=ConcurrentGroup(movements=(
                        MovementSpec(
                            name=name,
                            # fan_nose owns head channels 0,1 (set once in the
                            # start pose) and arm channels 3-7 (the fan joints
                            # plus the elbow/shoulder rotator lowered on return).
                            owned_channels=frozenset({
                                constants.NECK_PAN,
                                constants.NECK_TILT,          # 0,1
                                constants.RT_WRIST_TILT,      # 3
                                constants.RT_ELBOW_ROTATOR,
                                constants.RT_ELBOW_TILT,      # 4,5
                                constants.RT_SHOULDER_TILT,
                                constants.RT_SHOULDER_ROTATOR,  # 6,7
                            }),
                            lead_in=mv.fan_nose_lead_in,     # fan to destination (concurrent with audio)
                            loop_body=mv.fan_nose_loop_body,  # hold while audio plays
                            do_return=mv.fan_nose_return,     # lower arm at end
                            # Ungated performance (gate=None): no movement supplies a gate.
                        ),
                    )),
                ),
            ),
        )

        asyncio.run(PerformanceRunner(definition, mv, audio_dir).run())

    def fart_ghost(self):
        """"Fart Ghost" — fart first, THEN fan the nose and react to the smell.

        A TWO-PHASE routine, built from the SAME two shapes as ``fart`` (two
        sequential audio phases, no overlap) but with the fan-nose Gesture and a
        gate-until-settled second clip instead of the cover-mouth hold:

        1. Play ``fart.wav`` to completion with NO movement as PURE audio -- a
           blocking ``AudioPlayer`` built with ``drive_jaw=False`` /
           ``drive_eyes=False`` so the jaw motor and eye LED never flash (a fart
           doesn't come out of the mouth). This is EXACTLY ``fart``'s phase 1: it
           claims neither GPIO pin, so both stay free for the Performance below,
           and is ``close()``d afterward for symmetry.
        2. Run the ``fan_nose`` Gesture via the Performance_Framework UNGATED:
           ``gate=None`` starts ``elf_smell_ghost_burrito.wav`` at t=0 -- the
           instant the fan phase begins moving -- so the reaction clip plays as
           soon as the arm MOVES, rather than waiting for it to reach the fan
           destination (shape #1 in audio-sequencing.md -- the same ungated
           start as ``brains`` / ``snuckUp``). ``fan_nose_lead_in`` still runs
           the fan motion concurrently with the audio; it just no longer supplies
           a gate. The hand then HOLDS at the destination while the clip plays
           (``fan_nose_loop_body`` under ``loop_for_audio=True``) and
           ``fan_nose_return`` lowers the arm to rest at the end.

        The two audio phases never overlap (``fart.wav`` fully drains before the
        fan phase starts), so -- as in ``fart`` -- there is no jaw-motor /
        audio-device contention. The whole routine runs inside the caller's servo
        lock; the Performance run does not take the lock itself. Phase 2's runner
        is launched with ``asyncio.run`` at the top of the call stack.

        DRY: the phased ``fan_nose_lead_in`` / ``fan_nose_loop_body`` /
        ``fan_nose_return`` adapters drive the SAME shared ``_fan_nose_*``
        primitives as the standalone ``fanNose`` gesture, so the fan motion is
        identical to the gesture-only CLI path. Every servo write still goes
        through ``move_to``/``set_angle`` (clamped to SAFE_LIMITS) and the
        runner sweeps residual channels home on completion or failure.
        """
        # Phase 1: fart.wav alone, no movement -- identical to fart's phase 1.
        # Pure audio with the jaw and eyes NOT driven (a fart doesn't come out of
        # the mouth), so the player claims neither GPIO pin, leaving them free for
        # phase 2. Blocking playback on this thread; close() afterward for symmetry.
        fart_path = os.path.join(self._resolve_audio_dir(), self.music[33])  # fart.wav
        player = AudioPlayer(drive_jaw=False, drive_eyes=False)
        try:
            print(f"[fartGhost] playing {fart_path} as pure audio (no jaw/eyes, no fan yet)")
            player.play_audio_file(fart_path)
        finally:
            player.close()

        # Phase 2: fan the nose; the reaction clip starts at t=0 (as soon as the
        # arm begins moving), holds the pose while it plays, then lowers. Shared
        # with fart's fan-nose branch via ``_run_fan_nose``; this routine keeps
        # its own reaction clip (elf_smell_ghost_burrito.wav).
        self._run_fan_nose(self.music[34], name="fanNose")  # elf_smell_ghost_burrito.wav

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

    # Routines the napping Mode may run on a sensor wake (startle), chosen at
    # random. All own audio, own their event loop, and return to rest. Unlike
    # scan, napping holds the WHOLE-ROBOT lock and the head is level at wake
    # (sleep_snore_return leaves REST), so head-coupled reactions are safe here.
    # Names are hard-coded constants, so getattr(self, <constant>) in _startle
    # stays inside the allowlist boundary.
    _NAP_STARTLE_REACTIONS = ("awaken", "snuck_up", "brains")

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
            detect_mode: ``"approach"`` (getting-closer trend), ``"presence"``
                (object simply within the gate, used by awake), or ``"both"``
                (fire on either — used by napping so a person who walks in and
                stands still still wakes it). See
                ``range_sensor.ApproachDetector``.
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
            mode_desc = {
                "presence": "present within",
                "both": "approaching or present within",
            }.get(detect_mode, "approaching within")
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

        - **Timeout** (``timeout_seconds``; the CLI knob is in MINUTES,
          0..120 where 0 = no timeout / manual stop only): the nap
          ends and the head is RAISED exactly as at the end of the ``sleep``
          routine (``sleep_snore_return``). When 0/None there is NO timeout
          deadline — only the sensor and external stop can end the nap.
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
                raises the head. In SECONDS (the hardware-facing unit); the CLI
                knob that sets it is in MINUTES (``--nap-timeout-min``, 0..120
                where 0 = no timeout) and is converted at the dispatch site. A
                falsy value (0/None) runs with NO timeout — manual stop or
                sensor only.
        """
        # Clear any stale stop request from a previous run so we start clean.
        nap_signal.clear_stop()

        # Arm the proximity wake sensor for this nap (best-effort; the nap still
        # runs and simply won't sensor-wake if the sensor can't be opened).
        # "both": wake on EITHER a getting-closer approach OR someone simply
        # standing within the gate. Approach-only never fired for a person who
        # walks in and stands still (no sustained closing motion), so Sleep now
        # also honours presence.
        self._open_nap_sensor(detect_mode="both")

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
            # Fail safe: a crashed-then-recovered nap reports a non-chaining
            # reason so the webapp watcher never chains on an error path.
            return self.NAP_INTERRUPT_STOP

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

        # Report WHY the nap ended so the webapp chain watcher can decide whether
        # to chain to the opposite mode (timeout/sensor chain; stop does not).
        return reason

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
                A falsy value (0/None) means NO timeout deadline — only the
                sensor and external stop end the nap.

        Returns:
            One of NAP_INTERRUPT_TIMEOUT / NAP_INTERRUPT_SENSOR /
            NAP_INTERRUPT_STOP.
        """
        mv = Movements("Animatronic")
        audio_dir = self._resolve_audio_dir()
        # No timeout when timeout_seconds is 0/None: the deadline is simply never
        # applied, so ONLY the sensor and external stop can end the nap. The
        # max(1, ...) floor applies ONLY to real positive durations — a 0 must
        # NOT become a 1-second timeout.
        deadline = (
            time.monotonic() + max(1, timeout_seconds) if timeout_seconds else None
        )

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
                if deadline is not None and time.monotonic() >= deadline:
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
        """Wake response to a nap interruption: run ONE random startle Routine.

        Chosen at random from ``_NAP_STARTLE_REACTIONS`` (``awaken``,
        ``snuckUp``, ``brains``). ``awaken`` is a groggy "just woke up" (a gentle
        arm stir while the head lolls lazily); ``snuckUp`` is a sharper startle;
        ``brains`` a zombie reach. The nap loop's wake (``sleep_snore_return``)
        has already brought the figure to REST and released the sleep pose
        override before this runs, which is the start pose ``snuck_up`` /
        ``awaken`` expect, so each reaction can run from the post-wake pose
        without a jump.

        This runs INSIDE the napping mode, which already holds the servo lock,
        so it must NOT re-acquire it — ``run_action_and_audio`` / the Performance
        Framework do not take the lock. ``reaction`` is only ever one of the
        three hard-coded constant strings, so ``getattr(self, reaction)`` stays
        inside the allowlist boundary (no raw external input). Each reaction
        returns to REST on its own; the ``except`` drives everything back to rest
        on failure so the figure never ends energized against a jam.
        """
        reaction = random.choice(self._NAP_STARTLE_REACTIONS)
        print(f"[nap] startle: {reaction}")
        try:
            getattr(self, reaction)()  # name is a hard-coded constant -> allowlist-safe
        except Exception as e:
            print(f"[nap] startle reaction '{reaction}' failed: {e}")
            self._safe_rest()

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
    #   - lookAroundRandom  60%  (gesture, no audio) — stays dominant
    #   - handVisor         15%  (gesture, no audio)
    #   - fanNose            8%  (gesture, no audio) — occasional
    #   - tapSide            7%  (gesture, no audio) — occasional
    #   - yawn / clearThroat 10% (routines, with audio) — split evenly below
    # All of these return to rest cleanly on their own. Extend as new ambient
    # gestures/routines land, keeping the weights relative.
    _AWAKE_AMBIENT_POOL = (
        (60, "_do_look_around_random"),  # gesture — stays dominant
        (15, "_do_hand_visor"),          # gesture
        (8,  "_do_fan_nose"),            # gesture (new, occasional)
        (7,  "_do_tap_side"),            # gesture (new, occasional)
        (10, "_AWAKE_OCCASIONAL"),       # placeholder → picks yawn|clearThroat
    )
    # The 10% "occasional" bucket splits evenly between these two Routines.
    _AWAKE_OCCASIONAL_ROUTINES = ("yawn", "clear_throat")

    # camelCase gesture action name -> Movements method name, for the OPTIONAL
    # operator-configured Awake response pool (the Config-tab "Awake response
    # pool"). Unlike the ambient _AWAKE_AMBIENT_POOL above (which only names the
    # handful of _do_* gesture coroutines), the operator pool is seeded from the
    # FULL MOVEMENT_ACTIONS list, so an arbitrary gesture may be picked and the
    # runtime cannot rely on a _do_* coroutine existing for it. This map is a
    # VERIFIED-IDENTICAL copy of controller.py's gesture action_map (camelCase ->
    # Movements coroutine) — kept as a copy rather than imported because
    # controller.py has a CLI main() and importing it here would be heavyweight
    # and risk an import cycle. If controller.py's action_map changes, update
    # this to match. Driven via asyncio.run WITHOUT re-taking servo_lock() (Awake
    # already holds the whole-robot lock for its life). Routines are translated
    # separately via self.build_action_map() (camelCase -> bound Animatronic
    # method).
    _AWAKE_POOL_GESTURE_METHODS = {
        # --- ARM gestures ---
        'wave':             'wave',
        'beckon':           'beckon',
        'comeHere':         'come_here',
        'menacingReach':    'menacing_reach',
        'yawnCover':        'yawn_cover',
        'facePalm':         'face_palm',
        'fanButt':          'fan_butt',
        'fanNose':          'fan_nose',
        'tapSide':          'tap_side',
        'talkingWithHands': 'talking_with_hands',
        'talkingHandsII':   'talking_hands_ii',
        # --- HEAD gestures ---
        'yes':              'nod',
        'lookAroundSmall':  'look_around_small',
        'lookAroundRandom': 'look_around_random',
        'neckEllipse':      'neck_ellipse',
        'swivelHead':       'swivel_head',
        'shakeHead':        'shake_head',
        'snapHead':         'snap_head',
        'smno':             'small_shake_no',
        'snuckUp':          'snuck_up',
        'awaken':           'awaken',
        'headFocus':        'head_focus',
        # --- COMPOSITE gestures ---
        'waveAndSwivelSmooth': 'wave_and_swivel_smooth',
        'handVisor':        'hand_visor',
    }

    # On a confirmed sensor approach the mode reacts with ONE reaction, chosen at
    # random across BOTH typed sets below, before resuming (mirrors napping's
    # startle response). Split by kind so a GESTURE (no audio, driven via the
    # _do_* coroutine under asyncio.run) and a ROUTINE (owns audio + its own
    # event loop, called directly) can both be reactions — a single list +
    # getattr()() cannot drive both. Both lists are hard-coded module constants,
    # so getattr(self, <constant>) stays inside the allowlist boundary.
    #
    # Approach-reaction ROUTINES (own audio + their own event loop; called directly).
    _AWAKE_APPROACH_REACTION_ROUTINES = ("snuck_up", "brains", "hypnotic", "more_candy")
    # Approach-reaction GESTURES (no audio; gesture-only _do_* coroutine via asyncio.run).
    _AWAKE_APPROACH_REACTION_GESTURES = ("_do_face_palm",)

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

    def awake(self, timeout_seconds=300, chain_sensor_end=False):
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
          random reaction (gesture OR routine) from
          ``_AWAKE_APPROACH_REACTION_GESTURES`` (``facePalm``) /
          ``_AWAKE_APPROACH_REACTION_ROUTINES`` (``snuckUp``, ``brains``,
          ``hypnotic``, ``moreCandy``) via
          ``_awake_approach_reaction``, the detector latch is reset (so it fires
          once per detected presence), and the ambient loop RESUMES. The mode
          keeps running until the admin console stops it (or the timeout). A PIR
          sensor can later replace the presence source with the same contract.
        - **External stop** (``nap_signal``): the admin console sets this when
          the operator stops the mode, or asks to run a routine/gesture or
          toggle the mic in its place; the mode ends and exits so the servo lock
          frees for the requested action. This is what makes a web action button
          "arouse" the mode (see the animation-vocabulary Awake mode).
        - **Timeout** (``timeout_seconds``; the CLI knob is in MINUTES,
          0..120 where 0 = no timeout / manual stop only): the mode ends after
          the current action finishes. When 0/None there is NO timeout deadline
          — only an external stop ends the mode.

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
                mode. In SECONDS (the hardware-facing unit); the CLI knob that
                sets it is in MINUTES (``--awake-timeout-min``, 0..120 where
                0 = no timeout) and is converted at the dispatch site. A falsy
                value (0/None) runs with NO timeout — external stop only.
            chain_sensor_end: When True (set by ``main()`` only when auto-chaining
                is enabled), a confirmed PRESENCE event ENDS awake mode after one
                reaction so it can chain to napping. When False (the default and
                standalone behaviour), a presence event triggers one reaction and
                the ambient loop RESUMES (react-and-resume), exactly as before.
                This flag is snapshotted at launch and fixed for the subprocess's
                life, like ``timeout_seconds``.
        """
        # Clear any stale stop request from a previous Mode run so we start clean.
        nap_signal.clear_stop()

        # Load the OPTIONAL operator-configured Awake response pool ONCE at mode
        # entry (mirrors how scan() does `pools = config_store.load_scan_pools()`
        # before its loop). When BOTH pools are empty/unset the loop falls back
        # to the hard-coded ambient behaviour (_AWAKE_AMBIENT_POOL) unchanged.
        self._awake_pools = config_store.load_awake_pools()

        # Arm the proximity sensor in PRESENCE mode (best-effort): awake reacts
        # to someone simply standing in front of the sensor, not only to a
        # getting-closer trend. Publishes each reading (source "awake") for the
        # live dashboard range gauge. (A PIR sensor can later replace this with
        # the same present/absent contract.)
        self._open_nap_sensor(source="awake", detect_mode="presence")

        if timeout_seconds:
            print(f"[awake] entering awake mode (timeout {int(timeout_seconds)}s)")
        else:
            print("[awake] entering awake mode (no timeout)")
        try:
            # The loop reacts to sensor presence inline (react + resume) unless
            # chain_sensor_end is set, in which case it ends after one reaction
            # so the webapp can chain to napping. It otherwise returns only when
            # STOPPED by the admin console or the timeout.
            reason = self._run_awake_loop(timeout_seconds, chain_sensor_end)
        except Exception as e:
            print(f"[awake] error during awake loop: {e}")
            self._safe_rest()
            self._close_nap_sensor()
            nap_signal.clear_stop()
            # Fail safe: a crashed-then-recovered awake reports a non-chaining
            # reason so the webapp watcher never chains on an error path.
            return self.AWAKE_INTERRUPT_STOP

        print(f"[awake] {reason} interrupt -> winding down")
        # Ensure a clean, unloaded rest pose on exit regardless of how the last
        # routine ended.
        self._safe_rest()

        # Release the sensor GPIO pins and clear the stop signal so the next Mode
        # starts clean and any web-requested action can proceed once the lock
        # frees.
        self._close_nap_sensor()
        nap_signal.clear_stop()

        # Report WHY awake ended so the webapp chain watcher can decide whether
        # to chain to napping (timeout/sensor chain; stop does not).
        return reason

    def _check_awake_interrupt(self, deadline):
        """Return a loop-ENDING interrupt reason, or None to keep running.

        Non-blocking. Only two things END awake mode: an external stop request
        (``nap_signal`` — set by the admin console when it wants to stop the
        mode or run a routine/gesture/mic in its place) and the timeout
        deadline. A sensor approach does NOT end the mode; it is handled
        separately by ``_poll_nap_sensor`` in the loop, which reacts and then
        RESUMES the ambient loop.

        Args:
            deadline: ``time.monotonic()`` value at/after which the timeout
                fires, or ``None`` for no timeout (the timeout never fires;
                only an external stop ends the mode).

        Returns:
            AWAKE_INTERRUPT_STOP, AWAKE_INTERRUPT_TIMEOUT, or ``None``.
        """
        if nap_signal.stop_requested():
            return self.AWAKE_INTERRUPT_STOP
        if deadline is not None and time.monotonic() >= deadline:
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
            deadline: Monotonic timeout deadline (or ``None`` for no timeout),
                forwarded to the interrupt check so the pause also ends when the
                awake timeout elapses.

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
        60% / handVisor 15% / fanNose 8% / tapSide 7% / occasional 10%). The
        "occasional" bucket then splits evenly between the ``yawn`` and
        ``clearThroat`` Routines.

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

    def _pick_awake_pool_action(self):
        """Pick a ``(kind, camelCase_name)`` from the operator Awake pool.

        Builds a weighted list from ``self._awake_pools`` — each routine name
        repeated by its (int >= 1) weight tagged ``"routine"`` and each gesture
        name tagged ``"gesture"`` — then draws one via ``random.choice``. Modeled
        on ``detection_routine_map.choose_scan_action_weighted`` but WITHOUT the
        arm-only-safe ``allow`` intersection: Awake is a whole-robot mode, so any
        FULL-list routine/gesture is a legal pick. Uses the shared stdlib
        ``random`` so ``random.seed(x)`` stays reproducible.

        Returns:
            A tuple ``(kind, name)`` with ``kind`` ``"routine"`` or ``"gesture"``
            and ``name`` the camelCase action name, or ``None`` when the pool is
            empty (so the caller can fall back to the default ambient behaviour).
        """
        pools = getattr(self, "_awake_pools", None) or {}
        routine_pool = pools.get("routine_pool") or {}
        gesture_pool = pools.get("gesture_pool") or {}

        weighted = []
        for pool, kind in ((routine_pool, "routine"), (gesture_pool, "gesture")):
            if not isinstance(pool, dict):
                continue
            for name, raw_weight in pool.items():
                try:
                    weight = int(raw_weight)
                except (TypeError, ValueError):
                    continue
                if weight < 1:
                    continue
                weighted.extend([(kind, name)] * weight)

        if not weighted:
            return None
        return random.choice(weighted)

    def _run_awake_pool_action(self, kind, name):
        """Run one operator-pool action (camelCase) to completion.

        Translates the camelCase action name to the internal callable BEFORE
        dispatch (the camelCase name is the security boundary — never passed to
        ``getattr``/``eval``/a shell on a raw value): routines via
        ``self.build_action_map()`` (camelCase -> bound ``Animatronic`` method, called
        directly so the routine manages its own audio + event loop), gestures via
        ``_AWAKE_POOL_GESTURE_METHODS`` (camelCase -> ``Movements`` method name,
        driven with ``asyncio.run``). Neither re-takes the servo lock — Awake
        already holds the whole-robot lock for its whole life (exactly like
        ``_run_awake_action``).

        An unknown/untranslatable name is skipped with a warning rather than
        raising, so a stale persisted name can never crash the loop.

        Args:
            kind: ``"routine"`` or ``"gesture"`` (from ``_pick_awake_pool_action``).
            name: The camelCase action name to dispatch.
        """
        if kind == "routine":
            method = self.build_action_map().get(name)
            if method is None:
                print(f"[awake] skipping unknown pool routine: {name!r}")
                return
            method()
        else:
            method_name = self._AWAKE_POOL_GESTURE_METHODS.get(name)
            if method_name is None:
                print(f"[awake] skipping unknown pool gesture: {name!r}")
                return
            mv = Movements("Animatronic")
            asyncio.run(getattr(mv, method_name)())

    def _run_awake_loop(self, timeout_seconds, chain_sensor_end=False):
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
                A falsy value (0/None) means NO timeout deadline — only an
                external stop ends the mode.
            chain_sensor_end: When True, a confirmed PRESENCE event ends the mode
                (``AWAKE_INTERRUPT_SENSOR``) AFTER running exactly one reaction,
                so the webapp can chain to napping. When False (default), the
                figure reacts and RESUMES the loop (today's behaviour).

        Returns:
            AWAKE_INTERRUPT_STOP, AWAKE_INTERRUPT_TIMEOUT, or — only when
            ``chain_sensor_end`` is True — AWAKE_INTERRUPT_SENSOR.
        """
        # No timeout when timeout_seconds is 0/None: the deadline is never
        # applied, so ONLY an external stop ends the mode. max(1, ...) floors
        # real positive durations only — a 0 must NOT become a 1-second timeout.
        deadline = (
            time.monotonic() + max(1, timeout_seconds) if timeout_seconds else None
        )
        while True:
            # End only on stop/timeout, checked before each action.
            reason = self._check_awake_interrupt(deadline)
            if reason is not None:
                return reason

            # A pending sensor presence: react once. When chaining is enabled,
            # END after that one reaction so the webapp chains to napping;
            # otherwise reset the latch and RESUME the loop (today's behaviour).
            if self._poll_nap_sensor():
                self._awake_approach_reaction()
                if chain_sensor_end:
                    return self.AWAKE_INTERRUPT_SENSOR
                self._reset_nap_sensor()
                continue

            # Prefer the operator-configured response pool when one is saved;
            # otherwise fall back to today's hard-coded ambient behaviour
            # (_pick_ambient_action + _run_awake_action), unchanged. The pool
            # names are camelCase (FULL lists) so they dispatch through the
            # camelCase translation in _run_awake_pool_action; the ambient
            # fallback uses the internal _do_*/snake_case names as before.
            pool_pick = self._pick_awake_pool_action()
            if pool_pick is not None:
                kind, name = pool_pick
                print(f"[awake] performing {kind}: {name}")
                self._run_awake_pool_action(kind, name)
            else:
                kind, name = self._pick_ambient_action()
                print(f"[awake] performing {kind}: {name}")
                self._run_awake_action(kind, name)

            # Pause ~30s between actions, reacting promptly to any signal.
            reason = self._awake_pause(deadline)
            if reason == self.AWAKE_INTERRUPT_SENSOR:
                # Sensor fired mid-pause: react once. When chaining is enabled,
                # END after that one reaction so the webapp chains to napping;
                # otherwise reset the latch and RESUME the loop (today's
                # behaviour — do not end).
                self._awake_approach_reaction()
                if chain_sensor_end:
                    return self.AWAKE_INTERRUPT_SENSOR
                self._reset_nap_sensor()
                continue
            if reason is not None:
                return reason  # stop or timeout ends the mode

    def _awake_approach_reaction(self):
        """React to a sensor approach with ONE random reaction (gesture OR routine).

        Chosen uniformly at random across the UNION of
        ``_AWAKE_APPROACH_REACTION_GESTURES`` (``facePalm``) and
        ``_AWAKE_APPROACH_REACTION_ROUTINES`` (``snuckUp``, ``brains``,
        ``hypnotic``, ``moreCandy``) — the figure "notices" the approaching
        visitor and performs a reaction before the ambient loop resumes,
        mirroring how napping runs ``_startle`` on a sensor wake.

        A GESTURE is a gesture-only ``_do_*`` coroutine driven via
        ``asyncio.run`` (this loop is synchronous, not inside an event loop),
        identical to the ambient gesture path. A ROUTINE owns its own audio +
        event loop, so it is called directly. Both lists are hard-coded module
        constants, so ``getattr(self, <constant>)`` stays inside the allowlist
        boundary (no ``getattr``/``eval`` on raw external input).

        Runs INSIDE the awake mode, which already holds the servo lock, so it
        must NOT re-acquire it: the routines use ``run_action_and_audio`` /
        the Performance Framework and the gesture's ``_do_*`` coroutine, none of
        which takes the lock. Each returns to rest on its own; the ``except``
        below drives everything back to rest on failure so the arm/head is never
        left energized, and the caller's ``_safe_rest`` is a final backstop.
        """
        gestures = self._AWAKE_APPROACH_REACTION_GESTURES
        routines = self._AWAKE_APPROACH_REACTION_ROUTINES
        choice = random.choice(gestures + routines)  # uniform over the union
        print(f"[awake] approach detected -> reacting with: {choice}")
        try:
            if choice in gestures:
                # Gesture-only _do_* coroutine: drive it with asyncio.run like
                # the ambient gesture path (this loop is not inside an event loop).
                asyncio.run(getattr(self, choice)())
            else:
                # Routine: owns its audio + event loop; call the method directly.
                getattr(self, choice)()
        except Exception as e:
            print(f"[awake] approach reaction '{choice}' failed: {e}")
            self._safe_rest()

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

    # Scan_Sweep tuning. The sweep pans NECK_PAN across its SAFE_LIMITS range as
    # an eased, incremental pan — each step written through set_angle (so each is
    # clamped) with a short asyncio.sleep between steps. The per-step increment
    # is NOT constant: it follows a TRUE accel -> cruise -> decel VELOCITY profile
    # across each endpoint-to-endpoint traversal LEG, with the commanded velocity
    # reaching ~ZERO at BOTH ends of every leg (including the very first leg away
    # from the starting pan) so the head glides up from a standstill and settles
    # to a near-stop before each turnaround — no hard start, no hard reversal. A
    # brief dwell (_SCAN_REVERSAL_DWELL_S) at each endpoint lets the motion fully
    # settle before the opposite leg begins from zero velocity. The cadence stays
    # in the same gentle "surveillance" ballpark as the old fixed ~2 deg/0.05s
    # feel (a slow ~0.05s/deg surveillance pan) — we are smoothing the
    # accel/decel + adding a reversal dwell, NOT making the sweep faster — while
    # still polling /detections and nap_signal every tick (including during the
    # dwell) so a reacquire or stop is honored within ~1s.
    #
    # Instantaneous step = STEP_MAX * smoothstep(e), where e in [0,1] is the
    # normalized distance into the nearer end-ramp of the current leg (clamped to
    # 1 across the un-ramped middle). Unlike the earlier profile, the eased step
    # ramps toward ~0 at a leg boundary (NOT toward a non-zero STEP floor), so the
    # velocity truly decays to zero at the start and the endpoints. To avoid the
    # zero-velocity STALL that a hard floor used to guard against, progress is
    # guaranteed two other ways: (a) a tiny _SCAN_STEP_DEG_CREEP minimum applies
    # ONLY when NOT inside an end-ramp band (so the un-ramped middle never
    # crawls); and (b) inside the end-ramp approaching the target, once the
    # remaining distance drops below _SCAN_ENDPOINT_SNAP_DEG the pan SNAPS to the
    # endpoint and reverses promptly instead of crawling toward it forever. The
    # result: 0 -> accelerate -> cruise(STEP_MAX) -> decelerate -> ~0 at endpoint,
    # every leg.
    _SCAN_STEP_PERIOD_S = 0.05       # delay between sweep steps (seconds; the tick period)
    _SCAN_STEP_DEG_MAX = 3.0         # max pan increment per step (mid-leg cruise, full speed)
    _SCAN_STEP_DEG_CREEP = 0.05      # tiny anti-stall creep, applied ONLY outside an end-ramp band
    _SCAN_ENDPOINT_SNAP_DEG = 0.5    # within this of the leg target, snap to it and reverse (anti-crawl)
    _SCAN_RAMP_DEG = 35.0            # width (deg) of the ease ramp at EACH end of a leg (~100 deg cruise middle)
    _SCAN_REVERSAL_DWELL_S = 0.2     # settle pause at each endpoint before reversing (~4 ticks at 0.05s)

    # Puppeteer idle-recenter cadence. When a leave-frame Scan_Sweep times out
    # with no person, Puppeteer eases the Neck_Group back to rest (rather than
    # snapping) via a smoothstep move_to. Coming from a fully-panned sweep this
    # could be a ~85 deg pan swing; at these values a worst-case full sweep takes
    # ~2s (100 steps x 0.02s), a deliberately slow, gentle recenter so the head
    # does not jerk to center. Only the Neck_Group channels are driven.
    _PUPPETEER_RECENTER_STEPS = 100    # interpolation steps for the eased recenter
    _PUPPETEER_RECENTER_DELAY_S = 0.02  # delay between steps (seconds)

    # Tracking/Scan wind-down recenter cadence. On a stop request or any error,
    # the neck eases back to rest (rather than snapping) via a smoothstep
    # move_to on the Neck_Group only. Tuned to be smooth enough to kill the
    # one-shot snap jerk yet prompt enough not to stall an error unwind: at these
    # values a worst-case full pan recenter takes ~0.9s (60 steps x 0.015s).
    # Tune on hardware.
    _RECENTER_STEPS = 60       # interpolation steps for the eased wind-down recenter
    _RECENTER_DELAY_S = 0.015  # delay between steps (seconds)

    # Tracking/Puppeteer STARTUP-pose cadence. At the top of the loop the neck is
    # eased to the tracking start pose (pan=center, tilt=level gaze) from WHEREVER
    # it physically rests when the Mode launches — an unknown, possibly large
    # swing. This is the operator-visible "first movement when Puppeteer starts",
    # so it must ease from ~0 velocity via a smoothstep move_to rather than a
    # one-shot set_angle snap. Gentle like the Puppeteer recenter: a worst-case
    # ~85 deg pan swing takes ~2s (100 steps x 0.02s). Tune on hardware.
    _STARTUP_POSE_STEPS = 100     # interpolation steps for the eased startup move
    _STARTUP_POSE_DELAY_S = 0.02  # delay between steps (seconds)

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
        suppress_triggers=False,
        end_on_scan_timeout=True,
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
            suppress_triggers: Puppeteer Mode knob (FR3). When ``True`` BOTH
                ``routine_map`` and ``action_map`` are forced to ``None`` so the
                loop's existing "``None`` means disabled" contract fires: no
                detection can ever arm a pending Routine trigger, keeping neck
                aim as the mode's only effect so a detection never seizes the
                jaw/audio path from the operator's live mic Stream. Defaults to
                ``False`` so plain ``--action=tracking`` is unchanged (FR4): it
                still builds the default maps and arms triggers as before.
            end_on_scan_timeout: Scan_Sweep reacquire-timeout policy. Default
                ``True`` is plain ``--action=tracking`` behaviour (Req 6.10):
                when a leave-frame Scan_Sweep elapses ``cfg.scan_timeout_s`` with
                no person reacquired, the Mode winds down and yields the
                Neck_Group to the previously active Mode. Puppeteer passes
                ``False``: the operator is performing live, so a camera that
                momentarily sees no person must NOT end the Mode — a Scan_Sweep
                timeout instead recenters the neck to its rest pose and IDLES
                there (holding, polling detections, no re-sweep), resuming
                tracking the instant a person reappears. Puppeteer exits ONLY on
                an explicit stop (``nap_signal``) or being preempted by another
                Mode. Does not affect plain tracking (which never sets it
                ``False``).

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
        if suppress_triggers:
            # Puppeteer: neck aim only. Leave BOTH None so _evaluate_trigger
            # short-circuits every frame and no detection can ever dispatch a
            # Routine (FR3) — the operator's live mic owns the jaw/audio path.
            routine_map = None
            action_map = None
        else:
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
                self._run_tracking_loop(
                    client,
                    cfg,
                    routine_map,
                    action_map,
                    end_on_scan_timeout=end_on_scan_timeout,
                )
            )
            print(f"[tracking] {reason} interrupt -> wound down")
        except Exception as e:
            # Any loop error: recenter the neck and exit cleanly. The loop
            # helper already recenters in its own finally, but this is the final
            # backstop if asyncio.run itself raised before/after that path.
            print(f"[tracking] error during tracking loop: {e}")
            # Sync backstop: asyncio.run already unwound the loop, so this is the
            # top of the call stack — safe to asyncio.run the eased recenter.
            asyncio.run(self._recenter_neck(tilt_angle=cfg.tilt_center_deg))
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

    async def _run_tracking_loop(
        self,
        client,
        cfg,
        routine_map=None,
        action_map=None,
        end_on_scan_timeout=True,
    ):
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
            end_on_scan_timeout: When ``True`` (plain tracking, Req 6.10) a
                Scan_Sweep reacquire timeout — ``_run_scan_sweep`` returning
                ``(False, pan)`` — ends the loop with
                ``TRACKING_INTERRUPT_SCAN_TIMEOUT``. When ``False`` (Puppeteer) a
                Scan_Sweep timeout does NOT end the loop: it recenters the neck
                to rest and idles there (``_puppeteer_idle_hold``), polling for a
                reappearing person while honoring ``nap_signal``, so Puppeteer
                exits only on an explicit stop (``NAP_INTERRUPT_STOP``) or
                preemption. The ``(None, pan)`` stop and ``(True, pan)`` reacquire
                paths are identical for both policies.

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

        # Ease the neck to the tracking start pose up front so the first command
        # works from the real level-gaze center rather than wherever the neck
        # happened to rest. This is the operator-visible FIRST movement when the
        # Mode starts, and the neck may be anywhere physically (a large swing),
        # so drive it with a smoothstep move_to that ramps velocity from ~0
        # (NOT a one-shot set_angle snap, which jerks). move_to captures the
        # neck's current angle as the start and clamps every step to SAFE_LIMITS;
        # only the Neck_Group channels are touched. On any failure, fall back to
        # a direct set_angle so startup still reaches the pose.
        try:
            await trunk.move_to(
                {constants.NECK_PAN: cur_pan, constants.NECK_TILT: cur_tilt},
                steps=self._STARTUP_POSE_STEPS,
                delay=self._STARTUP_POSE_DELAY_S,
                ease=True,
            )
            # move_to clamps every step to SAFE_LIMITS internally; the start-pose
            # targets (pan=center, tilt=level gaze) are in-range, so local state
            # equals the commanded targets. Keep cur_pan/cur_tilt as set above.
        except Exception as e:
            print(f"[tracking] eased startup move failed, snapping to pose: {e}")
            # Fallback snap: set_angle clamps and returns the post-clamp angle, so
            # local state stays consistent with what the hardware was commanded.
            cur_pan = trunk.set_angle(constants.NECK_PAN, cur_pan)
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
                        if end_on_scan_timeout:
                            # Plain tracking (Req 6.10): scan timeout with no
                            # person ends the Mode so it yields to the previously
                            # active Mode. The finally block recenters the
                            # Neck_Group to REST_POSITIONS.
                            reason = self.TRACKING_INTERRUPT_SCAN_TIMEOUT
                            break
                        # Puppeteer (end_on_scan_timeout=False): the operator is
                        # performing live, so a camera that momentarily sees no
                        # person must NOT end the Mode. Instead of re-sweeping,
                        # recenter the neck to its rest pose and HOLD there,
                        # polling detections until a person reappears (then
                        # resume tracking). The idle hold still checks nap_signal
                        # each tick so an explicit stop is honored within ~1s;
                        # only an explicit stop or a preempting Mode ends
                        # Puppeteer. Returns the (possibly stop-interrupted) pan
                        # so cur_pan stays consistent with the last write.
                        stopped, cur_pan, cur_tilt = await self._puppeteer_idle_hold(
                            client, cfg
                        )
                        if stopped:
                            reason = self.NAP_INTERRUPT_STOP
                            break
                        # A person reappeared: resume normal tracking on the next
                        # iteration, re-reading /detections from the rest pose.
                        continue
                    # A person reappeared: resume normal tracking on the next
                    # iteration, which re-reads /detections and commands the neck.
                    continue

                offset = compute_offset(target, frame_w, frame_h, cfg)
                targets = next_neck_targets(
                    offset, cur_pan, cur_tilt, cfg, frame_w, frame_h
                )
                # Slew-limit + low-pass the per-frame targets so a large single
                # correction or a rapid reversal can't produce an instant jump
                # (R3). Layered ON TOP of next_neck_targets' proportional/
                # deadband damping, not a replacement. This shared helper covers
                # tracking + scan + puppeteer (all run through this loop).
                targets = smooth_neck_targets(targets, cur_pan, cur_tilt)

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
            # Eased (smoothstep) recenter so the head glides to rest, not snaps.
            await self._recenter_neck(tilt_angle=cfg.tilt_center_deg)

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

        The leave-frame Scan_Sweep (Req 6.7-6.10). A deliberate surveillance pan
        of NECK_PAN, driven here as
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
        configurable scan range).

        The pan does NOT advance at constant velocity. Each step's increment
        follows a TRUE accel -> cruise -> decel velocity profile across the
        current traversal LEG (from where the leg began to the endpoint it heads
        to): a ``smoothstep`` ramp over the nearer ``_SCAN_RAMP_DEG``-wide
        end-band scales the step from ~0 (at either leg boundary) up to
        ``_SCAN_STEP_DEG_MAX`` (across the ~100 deg middle), so the commanded
        velocity reaches ~ZERO at BOTH ends of every leg. The head therefore
        glides up from a standstill at the START of the sweep (wherever
        ``cur_pan`` is — the first ``leg_start``) and off each endpoint after a
        reversal, cruises the middle, and decelerates to a near-stop as it
        settles into the next endpoint instead of snapping. After a reversal the
        leg (and thus the ramp) restarts, so the head eases away from the
        endpoint it just hit rather than jumping to full speed.

        Because the eased step ramps toward ~0 (not toward a non-zero floor),
        progress is guaranteed WITHOUT a hard floor two ways: a tiny
        ``_SCAN_STEP_DEG_CREEP`` minimum applies ONLY outside an end-ramp band
        (the un-ramped middle never crawls), and inside the end-ramp, once the
        remaining distance to the leg target drops below
        ``_SCAN_ENDPOINT_SNAP_DEG`` the pan SNAPS to the endpoint and reverses
        promptly rather than crawling toward it forever — so no zero-velocity
        stall and no infinite crawl.

        At each endpoint, before reversing, the sweep DWELLS for
        ``_SCAN_REVERSAL_DWELL_S`` (implemented as a few normal loop ticks that
        hold the pan but keep polling), so the motion fully settles to a stop and
        the opposite leg begins from zero velocity rather than snapping through
        the turnaround. The sweep's final commanded velocity on timeout/stop is
        thus ~0 (if it ends at/near an endpoint), making the handoff into the
        caller's recenter smooth; an interruption caught mid-leg at cruise is an
        acceptable abrupt-ish case the caller's eased ``move_to`` recenter
        absorbs.

        A fixed ``_SCAN_STEP_PERIOD_S`` ``asyncio.sleep`` between steps keeps it
        async-friendly and keeps detection/``nap_signal`` polling responsive
        every tick (dwell ticks included); the overall sweep stays in the gentle
        surveillance-pan ballpark of the former fixed ~2 deg/0.05s cadence.


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
        # Pan angle at the start of the CURRENT traversal leg. The ease-in ramp
        # is measured from here (not just from the safe-range endpoints) so the
        # head glides up from a standstill wherever a leg begins — the very first
        # move away from ``cur_pan`` (often near center, far from any endpoint)
        # as well as each move away from an endpoint after a reversal. Reset on
        # every reversal below.
        leg_start = pan

        # Dwell countdown (in whole ticks) held at a reversal/endpoint before the
        # next leg advances. While > 0 the loop still polls detections and
        # nap_signal and checks the deadline every tick, but does NOT advance the
        # pan — so the motion settles to a true stop and the next leg starts from
        # zero velocity, yet a stop/reacquire during the dwell is still honored
        # within ~1s. Derived from _SCAN_REVERSAL_DWELL_S / the tick period
        # (>=1 tick). The sweep begins with no dwell (eases straight off cur_pan).
        dwell_ticks_total = max(
            1, round(self._SCAN_REVERSAL_DWELL_S / max(self._SCAN_STEP_PERIOD_S, 1e-6))
        )
        dwell_remaining = 0

        start = time.monotonic()
        while True:
            # Wind down promptly on an external stop request (Req 6.4, 6.5).
            # Checked every tick, dwell ticks included.
            if nap_signal.stop_requested():
                return None, pan

            # Timeout with no reacquire -> end the sweep (Req 6.10). Checked every
            # tick, dwell ticks included.
            if (time.monotonic() - start) >= cfg.scan_timeout_s:
                print("[tracking] Scan_Sweep timed out -> recenter + yield")
                return False, pan

            # Poll for a reappearing person; stop the sweep the instant one is
            # found (Req 6.9). Polled every tick, dwell ticks included.
            detections, frame_w, frame_h = client.get_detections()
            if frame_w > 0 and frame_h > 0:
                if select_target(detections, frame_w, frame_h) is not None:
                    print("[tracking] Scan_Sweep reacquired a person -> track")
                    return True, pan

            # Reversal dwell: hold the pan at the endpoint (no advance) for a few
            # ticks so the head fully settles before the opposite leg eases off
            # from zero velocity. nap_signal / timeout / detections were already
            # checked above this tick, so a stop or reacquire mid-dwell is still
            # honored within ~1s. Re-issue the (unchanged) endpoint angle each
            # dwell tick so the neck stays actively commanded at the hold pose and
            # the settle is explicit — still only NECK_PAN, still clamped.
            if dwell_remaining > 0:
                dwell_remaining -= 1
                pan = trunk.set_angle(constants.NECK_PAN, pan)
                await asyncio.sleep(self._SCAN_STEP_PERIOD_S)
                continue

            # Eased step size: ~0 at BOTH ends of the current traversal leg
            # (ease-in as it leaves ``leg_start``, ease-out as it nears the target
            # endpoint), full speed across the middle. ``edge`` is the distance
            # (deg) to the NEARER of the two leg boundaries — where the leg began
            # and where it is headed — so the head glides up from a standstill at
            # the start of every leg (including the first move away from
            # ``cur_pan``) and decelerates toward the turnaround. Normalizing
            # ``edge`` over _SCAN_RAMP_DEG through smoothstep gives a ramp that is
            # ~0 at a leg boundary and 1 once past the ramp band; scaling
            # STEP_MAX by it makes the commanded velocity truly reach ~0 at each
            # end. The step ramps toward zero (NOT a non-zero floor), so progress
            # is kept two other ways: a tiny creep applies only OUTSIDE the
            # end-ramp band (the middle never crawls), and the endpoint snap below
            # finishes the ease-out leg promptly instead of crawling forever.
            target = float(pan_max if increasing else pan_min)
            dist_from_start = abs(pan - leg_start)
            remaining = abs(target - pan)
            edge = min(dist_from_start, remaining)
            ramp = max(0.0, min(1.0, edge / self._SCAN_RAMP_DEG))
            smooth = ramp * ramp * (3.0 - 2.0 * ramp)  # smoothstep(ramp)
            step = self._SCAN_STEP_DEG_MAX * smooth
            # Anti-stall creep: a tiny universal floor so the step is never
            # exactly zero — otherwise the ease-IN step at a leg boundary (where
            # distance-from-start is 0, so smoothstep is 0) would be 0 forever and
            # the leg would never leave the endpoint. _SCAN_STEP_DEG_CREEP
            # (~0.05 deg/tick ~= 1 deg/s) is slow enough to read as "near zero"
            # velocity at the ends yet guarantees progress off a standstill. The
            # endpoint snap below absorbs the symmetric ease-OUT tail so the leg
            # still reaches its target promptly rather than creeping in forever.
            step = max(step, self._SCAN_STEP_DEG_CREEP)

            # Endpoint snap: on the ease-OUT side (nearer the target than the leg
            # start) once we are within _SCAN_ENDPOINT_SNAP_DEG of the target,
            # land exactly on it and reverse — finishing the near-zero decel
            # promptly instead of creeping the last fraction of a degree. The
            # ease-IN off the SAME endpoint next leg is unaffected (there
            # ``remaining`` is large, so this is false).
            snap_now = (
                remaining <= self._SCAN_ENDPOINT_SNAP_DEG and remaining <= dist_from_start
            )

            # Advance one eased step, reversing at either safe-range endpoint. On
            # a reversal, restart the leg and arm the dwell so the next traversal
            # settles, then eases away from the endpoint instead of snapping to
            # full speed.
            if increasing:
                if snap_now:
                    pan = float(pan_max)
                else:
                    pan = min(float(pan_max), pan + step)
                if pan >= pan_max:
                    pan = float(pan_max)
                    increasing = False
                    leg_start = pan
                    dwell_remaining = dwell_ticks_total
            else:
                if snap_now:
                    pan = float(pan_min)
                else:
                    pan = max(float(pan_min), pan - step)
                if pan <= pan_min:
                    pan = float(pan_min)
                    increasing = True
                    leg_start = pan
                    dwell_remaining = dwell_ticks_total

            # Write through set_angle (SAFE_LIMITS clamp, Req 6.8); keep the
            # local angle consistent with the actually written (post-clamp)
            # value. Only NECK_PAN is touched (Neck_Group, no tilt/arm/jaw).
            pan = trunk.set_angle(constants.NECK_PAN, pan)

            await asyncio.sleep(self._SCAN_STEP_PERIOD_S)

    async def _puppeteer_idle_hold(self, client, cfg):
        """Recenter the neck to rest and hold there until a person reappears.

        Puppeteer-only (``end_on_scan_timeout=False``) behaviour for when a
        leave-frame Scan_Sweep times out with no person in view. Unlike plain
        tracking — which ends the Mode on that timeout (Req 6.10) — Puppeteer is
        a live operator performance that must stay up. Rather than keep sweeping,
        this EASES the Neck_Group back to its rest pose (NECK_PAN to the global
        rest ~90, NECK_TILT to the tracking level-gaze center) with a slow,
        smoothstep ``move_to`` sweep (slower than the wind-down/error path's
        eased ``_recenter_neck``, since this is an idle recenter rather than an
        unwind) — so coming off a fully-panned
        Scan_Sweep the head does not jerk to center. Every ``move_to`` step
        writes through ``set_angle`` (SAFE_LIMITS clamp, Req 5.5) and only the
        Neck_Group channels are driven. It then IDLES there, polling
        ``/detections`` without moving the neck, and resumes tracking the instant
        a Target_Person reappears.

        The hold checks ``nap_signal`` every tick so an explicit stop (the web
        stop button / a preempting Mode) is honored within ~1s. The Mode never
        ends on the scan-sweep timeout itself — only on that explicit stop or a
        preemption (handled by the caller breaking out).

        Args:
            client: The ``CameraClient`` to poll ``GET /detections``.
            cfg: The ``TrackingConfig`` (its ``tilt_center_deg`` is the rest
                tilt the neck holds at).

        Returns:
            A ``(stopped, cur_pan, cur_tilt)`` tuple. ``stopped`` is ``True`` if
            an external stop (``nap_signal``) was requested while idling (caller
            winds down with ``NAP_INTERRUPT_STOP``); ``False`` if a person
            reappeared (caller resumes tracking). ``cur_pan``/``cur_tilt`` are the
            rest angles the neck is now holding, so the caller's local angle
            state stays consistent with the last write.
        """
        # Ease the Neck_Group back to the rest pose and hold there — do NOT
        # start another Scan_Sweep. Like the wind-down/error path (which now
        # also eases via _recenter_neck), the idle recenter sweeps smoothly via
        # move_to so coming off a fully-panned sweep the head does not JERK to
        # center; this path simply recenters more slowly (idle, not an unwind).
        # move_to interpolates each joint with smoothstep
        # easing and writes every step through set_angle (SAFE_LIMITS clamp,
        # Req 5.5). ONLY the Neck_Group channels are driven — never an arm
        # channel a concurrent operator Gesture may own (Req 5.8, 6.11).
        rest_pan = float(constants.REST_POSITIONS[constants.NECK_PAN])
        rest_tilt = float(cfg.tilt_center_deg)
        recenter_targets = {
            constants.NECK_PAN: rest_pan,
            constants.NECK_TILT: rest_tilt,
        }
        print("[tracking] puppeteer: no person -> ease recenter + idle (hold)")
        await Movements.trunkController.move_to(
            recenter_targets,
            steps=self._PUPPETEER_RECENTER_STEPS,
            delay=self._PUPPETEER_RECENTER_DELAY_S,
            ease=True,
        )
        cur_pan = rest_pan
        cur_tilt = rest_tilt

        # move_to itself does not poll nap_signal, so a stop requested DURING the
        # ~2s eased sweep would otherwise be ignored until the first idle tick.
        # Check it right after the sweep so an explicit stop is still honored
        # promptly (Req 6.4, 6.5).
        if nap_signal.stop_requested():
            return True, cur_pan, cur_tilt

        while True:
            # Honor an explicit stop within ~1s (Req 6.4, 6.5).
            if nap_signal.stop_requested():
                return True, cur_pan, cur_tilt

            detections, frame_w, frame_h = client.get_detections()
            if frame_w > 0 and frame_h > 0:
                if select_target(detections, frame_w, frame_h) is not None:
                    print("[tracking] puppeteer: person reappeared -> track")
                    return False, cur_pan, cur_tilt

            await asyncio.sleep(self._TRACKING_LOOP_PERIOD_S)

    @staticmethod
    async def _recenter_neck(tilt_angle=None):
        """Drive ONLY the Neck_Group channels to their resting pan/tilt, EASED.

        Used by Tracking_Mode on wind-down (stop request) and on any error so
        the neck returns to a known pose. Pan always returns to the global rest
        (``REST_POSITIONS[NECK_PAN]`` = 90, centered). Tilt returns to
        ``tilt_angle`` when given — Tracking_Mode passes its tracking tilt
        center (``cfg.tilt_center_deg``, the level-gaze angle) so the head winds
        down to head height rather than the global rest 90 (which is chin-up on
        this build). When ``tilt_angle`` is None it falls back to the global
        ``REST_POSITIONS[NECK_TILT]``.

        The recenter is a smoothstep ``move_to`` (ease-in/out, velocity ~0 at
        both ends) so the head glides to rest rather than snapping — the same
        shape ``_puppeteer_idle_hold`` uses. Writes go through ``move_to`` ->
        ``TrunkController.set_angle`` so each angle is clamped to the global
        ``SAFE_LIMITS`` (Req 5.5) — the hardware clamp is always the final
        authority — and ONLY the Neck_Group channels (``NECK_PAN``/``NECK_TILT``)
        are touched, never an arm channel a concurrent Gesture may own
        (Req 5.8, 6.11).

        NEVER RAISES (recovery path). Because ``move_to`` can raise, the eased
        recenter is wrapped in try/except; on ANY exception it falls back to the
        legacy one-shot ``set_angle`` snap-to-rest (each write individually
        guarded) and the original error is never propagated.

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
        # Drop any None target (no rest angle configured for that channel).
        move_targets = {
            channel: angle
            for channel, angle in rest_targets.items()
            if angle is not None
        }
        if not move_targets:
            return
        try:
            # Preferred path: eased, zero-velocity-at-both-ends recenter.
            await trunk.move_to(
                move_targets,
                steps=Animatronic._RECENTER_STEPS,
                delay=Animatronic._RECENTER_DELAY_S,
                ease=True,
            )
        except Exception as e:
            # Recovery contract: NEVER raise. Fall back to the one-shot snap so
            # the neck still reaches a known-safe rest even if the eased move
            # failed partway, and swallow the error.
            print(f"[tracking] eased recenter failed, snapping to rest: {e}")
            for channel, angle in move_targets.items():
                try:
                    trunk.set_angle(channel, angle)
                except Exception as e2:
                    name = constants.servos.get(channel, f"ch{channel}")
                    print(f"[tracking] could not recenter {name}: {e2}")

    # ------------------------------------------------------------------ #
    # Scan — a MODE (continuous neck tracker + concurrent arm-only responder) #
    # ------------------------------------------------------------------ #

    def scan(
        self,
        camera_url=DEFAULT_CAMERA_URL,
        timeout_seconds=None,
        max_step=None,
        deadband=None,
        conf=None,
        scan_timeout=None,
        aim_frac=None,
        tilt_center=None,
        tilt_min=None,
        tilt_max=None,
        settle_gain=None,
    ):
        """SCAN mode: continuously neck-track a person while running arm-only responses.

        A Mode (per the animation vocabulary) that runs until interrupted. Scan
        layers two behaviours on DISJOINT servo groups so they run concurrently:

        * **The neck tracker** owns the Neck_Group (channels
          ``NECK_PAN``/``NECK_TILT`` = 0/1). It reads Detections from
          Camera_Service and drives the neck to follow the Target_Person exactly
          like Tracking_Mode, running a leave-frame Scan_Sweep when no one is in
          frame. The tracker NEVER winds down to run a response — it keeps
          holding the head on the person the entire time.
        * **The responder** fires an arm-only Gesture or Routine (Arm_Group
          channels, see ``ARM_ONLY_CHANNELS``) whenever a detection rule allows,
          chosen by the weighted picker ``choose_scan_action`` (Gestures 5x more
          likely than Routines). Only ONE response is ever in flight at a time.

        Because the two behaviours own disjoint channels they can run at the same
        time without a servo conflict — the arm responds while the head keeps
        tracking. Scan carries audio (its Routine responses drive the jaw motor),
        so like any Routine it owns the jaw/audio path and interrupts a live mic
        Stream.

        Lock model (IMPORTANT): like napping/awake/tracking this method takes NO
        lock itself. ``main()`` wraps the call in a single ``with servo_lock():``
        (the responder may drive the whole arm + jaw/audio, so scan needs the
        whole-robot lock, unlike tracking's Neck_Group-only lock).

        Dog handling: the person+dog rule logs a ``walkYourDog`` placeholder line
        to the console (no dedicated gesture/routine yet) and otherwise dispatches
        the SAME weighted arm-only response as a plain person (per the operator's
        instruction to treat it the same but log it).

        Wind-down: the loop stops on a ``nap_signal`` stop request (a web action
        preempting the Mode) OR when ``timeout_seconds`` elapses. On wind-down it
        cancels and awaits any in-flight response, then recenters the neck. The
        same recenter happens on any loop error (the ``except`` backstop below).

        Args:
            camera_url: Base URL of Camera_Service (default
                ``http://localhost:8001``).
            timeout_seconds: Seconds before the timeout winds the Mode down.
                ``None`` means no timeout (run until an external stop).
            max_step, deadband, conf, scan_timeout, aim_frac, tilt_center,
            tilt_min, tilt_max, settle_gain: Neck-tracker tuning forwarded to
                ``TrackingConfig`` (same meaning and clamping as ``tracking()``).
                Each ``None`` uses the ``TrackingConfig`` default.
        """
        # Build the TrackingConfig exactly like tracking() (dataclass defaults
        # fill any None; __post_init__ clamps each value into its safe range).
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

        routine_map = DetectionRoutineMap(scan_rules())

        # One shared Movements instance backs every response callable so the arm
        # adapters share a single TrunkController (same pattern as the Routines).
        mv = Movements("Animatronic")

        # Map each ENABLED allowlist name to its arm-only callable. This stays in
        # lockstep with SCAN_SAFE_ARM_ACTIONS (same eleven routines + four
        # gestures enabled in both; fanNose/snuckUp/moreCandy/facePalm FLAGGED in
        # both). The Performance responses build their arm-only variant (neck
        # spec dropped) and rest ONLY the arm channels via rest_channels so neck
        # 0/1 are left for the tracker; the simple-runner/two-phase adapters do
        # the same via their own restrict_channels + try/finally arm rest.
        scan_responses = {
            'beckon':   lambda: mv.beckon(),
            'comeHere': lambda: mv.come_here(),
            'wave':     lambda: mv._wave_arm(include_neck=False),
            'tapSide':  lambda: mv.tap_side(),  # NEW — arm-only gesture {3,4,5,6,7}
            # Performance-definition Routines: build the arm-only (scan=True)
            # variant (neck spec dropped) and rest ONLY {3,4,5,6,7}.
            'brains':   lambda: self._run_scan_performance(
                self._brains_performance(mv, scan=True), mv
            ),
            'hypnotic': lambda: self._run_scan_performance(
                self._hypnotic_performance(mv, scan=True), mv
            ),
            'blah':     lambda: self._run_scan_performance(
                self._blah_performance(mv, scan=True), mv
            ),
            'maximus':  lambda: self._run_scan_performance(
                self._maximus_performance(mv, scan=True), mv
            ),
            'burp':     lambda: self._run_scan_performance(
                self._cover_mouth_performance(mv, scan=True, kind="burp"), mv
            ),
            'coughMedium': lambda: self._run_scan_performance(
                self._cover_mouth_performance(mv, scan=True, kind="coughMedium"), mv
            ),
            'coughLong': lambda: self._run_scan_performance(
                self._cover_mouth_performance(mv, scan=True, kind="coughLong"), mv
            ),
            # Simple-runner / two-phase adapters: arm-only motion + audio via a
            # PlaybackController, driven inside the scan loop's event loop.
            'awaken':       lambda: self._scan_awaken(mv),
            'comeGetCandy': lambda: self._scan_come_get_candy(mv),
            'yawn':         lambda: self._scan_yawn(mv),
            'niceDay':      lambda: self._scan_nice_day(mv),
            'fart':         lambda: self._scan_fart(mv),
            'fartGhost':    lambda: self._scan_fart_ghost(mv),
            # Still head-coupled with no arm-only scan builder, so omitted (see
            # the FLAGGED block in SCAN_SAFE_ARM_ACTIONS): fanNose, snuckUp,
            # moreCandy, facePalm.
        }

        # Operator-selected weighted pool (FEAT-001). Restrict the safe-action
        # map to the names actually wired into scan_responses so a
        # dispatchable-but-unsafe mismatch can't occur, read the persisted pools,
        # and pre-build a 0-arg picker bound to that safe set. The picker
        # intersects the operator pool with this safe set and falls back to the
        # default 5:1 behavior when no pool is saved.
        allow = {
            n: k for n, k in SCAN_SAFE_ARM_ACTIONS.items() if n in scan_responses
        }
        pools = config_store.load_scan_pools()
        picker = lambda: choose_scan_action_weighted(
            pools["routine_pool"], pools["gesture_pool"], allow
        )

        # Clear any stale stop request from a previous Mode run (mirrors
        # napping/awake/tracking) so we start clean.
        nap_signal.clear_stop()

        client = CameraClient(base_url=camera_url)
        deadline = (
            time.monotonic() + max(1, timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        print(
            f"[scan] entering scan mode (camera {client.base_url}, "
            f"timeout {timeout_seconds}s)"
        )

        try:
            asyncio.run(
                self._run_scan_loop(
                    client, cfg, routine_map, scan_responses, mv, deadline, picker
                )
            )
            print("[scan] wound down")
        except Exception as e:
            # Any loop error: recenter the neck and exit cleanly. The loop helper
            # already recenters in its own finally, but this is the final
            # backstop if asyncio.run itself raised before/after that path.
            print(f"[scan] error during scan loop: {e}")
            # Sync backstop: asyncio.run already unwound the loop, so this is the
            # top of the call stack — safe to asyncio.run the eased recenter.
            asyncio.run(self._recenter_neck(tilt_angle=cfg.tilt_center_deg))
        finally:
            # Clear the stop signal on exit so the next Mode starts clean and the
            # requesting web-app action can proceed once the servo lock frees.
            nap_signal.clear_stop()

    async def _run_scan_loop(
        self, client, cfg, routine_map, scan_responses, mv, deadline, picker
    ):
        """Neck-track continuously while running one arm-only response at a time.

        The neck tracker is identical to ``_run_tracking_loop`` — it owns ONLY
        channels 0/1, reuses ``get_detections``/``select_target``/
        ``compute_offset``/``next_neck_targets`` + ``set_angle`` on
        ``NECK_PAN``/``NECK_TILT``, and runs ``_run_scan_sweep`` verbatim when no
        target is present. Unlike tracking, the tracker NEVER winds down to run a
        response: the response runs CONCURRENTLY on the disjoint Arm_Group.

        Responder: ``self._active`` holds ``(task, rule)`` for the single
        in-flight response (``None`` when idle). Each iteration asks
        ``routine_map.select_action`` which rule — if any — may fire. When idle
        and a rule fired, the person+dog rule logs the ``walkYourDog``
        placeholder, then a weighted ``choose_scan_action()`` name is dispatched
        as a concurrent task. The loop only POLLS ``task.done()`` — it never
        awaits the response — so the neck keeps stepping across the whole
        response. When the task finishes its result is checked (errors logged)
        and the rule's cooldown starts via ``mark_completed``.

        Wind-down (strict order in ``finally``): (1) cancel + await any in-flight
        response task; (2) recenter the neck to ``cfg.tilt_center_deg``; (3)
        return. The loop breaks on a ``nap_signal`` stop request (checked at the
        top of each iteration) OR when the deadline elapses.

        Args:
            client: A ``CameraClient`` for ``GET /detections``.
            cfg: The ``TrackingConfig`` neck tuning.
            routine_map: The ``DetectionRoutineMap`` (built from ``scan_rules()``)
                evaluated each iteration to decide whether a response may fire.
            scan_responses: Dict mapping an allowlisted action name to a 0-arg
                callable returning the response coroutine.
            mv: The shared ``Movements`` instance (its ``trunkController`` is used
                for the fail-closed arm rest in ``_dispatch_scan_response``).
            deadline: Monotonic time at which the timeout fires, or ``None`` for
                no timeout.
            picker: A 0-arg callable returning ``(ScanActionKind, name)`` for the
                next response to dispatch (the operator-weighted picker bound to
                the dispatchable safe set; falls back to the default 5:1 draw
                when no pool is saved).
        """
        trunk = Movements.trunkController

        # Seed the local neck angles like the tracking loop: pan from global rest
        # (centered), tilt from the tracking tilt center (level gaze).
        cur_pan = float(constants.REST_POSITIONS[constants.NECK_PAN])
        cur_tilt = float(cfg.tilt_center_deg)

        # Drive the neck to the start pose up front (every write clamped).
        trunk.set_angle(constants.NECK_PAN, cur_pan)
        cur_tilt = trunk.set_angle(constants.NECK_TILT, cur_tilt)

        # The single in-flight arm-only response: (task, rule) or None.
        self._active = None
        try:
            while True:
                # Wind down on an external stop request (a web action preempting
                # the Mode) at the top of each iteration.
                if nap_signal.stop_requested():
                    print("[scan] stop requested -> wound down")
                    break
                # Wind down on timeout.
                if deadline is not None and time.monotonic() >= deadline:
                    print("[scan] timeout -> wound down")
                    break

                detections, frame_w, frame_h = client.get_detections()

                # Responder bookkeeping. Evaluate on the SAME detections used for
                # tracking so a response may fire whether we are actively
                # tracking or sweeping.
                rule = routine_map.select_action(detections, time.monotonic())
                if self._active is None and rule is not None:
                    if DEFAULT_DOG_LABEL in rule.required_classes:
                        # No walkYourDog gesture/routine yet: treat the same as a
                        # person but log the placeholder for testing (per spec).
                        print("[scan] person+dog detected (walkYourDog placeholder)")
                    _, name = picker()
                    self._active = (
                        asyncio.create_task(
                            self._dispatch_scan_response(name, scan_responses, mv)
                        ),
                        rule,
                    )
                elif self._active is not None and self._active[0].done():
                    task, fired_rule = self._active
                    self._active = None
                    try:
                        task.result()
                    except Exception as e:
                        print(f"[scan] response failed: {e}")
                    # Start the rule's cooldown measured from completion so the
                    # same condition cannot immediately re-fire.
                    routine_map.mark_completed(fired_rule, time.monotonic())

                # Neck tracker (owns 0/1 only). Hold the head on the person the
                # whole time — the response runs concurrently on the arm.
                if frame_w <= 0 or frame_h <= 0:
                    await asyncio.sleep(self._TRACKING_LOOP_PERIOD_S)
                    continue

                target = select_target(detections, frame_w, frame_h)

                if target is None:
                    # No Target_Person: run the leave-frame Scan_Sweep verbatim.
                    reacquired, cur_pan = await self._run_scan_sweep(
                        client, cfg, trunk, cur_pan
                    )
                    if reacquired is None:
                        # External stop mid-sweep — wind down.
                        print("[scan] stop requested -> wound down")
                        break
                    # Whether a person reappeared or the sweep timed out, scan
                    # keeps running (unlike tracking, scan never ends on a sweep
                    # timeout) — resume on the next iteration.
                    continue

                offset = compute_offset(target, frame_w, frame_h, cfg)
                targets = next_neck_targets(
                    offset, cur_pan, cur_tilt, cfg, frame_w, frame_h
                )
                # Shared per-frame neck smoothing (R3) — same helper as tracking
                # + puppeteer, so there is no variant fork. Layers slew/low-pass
                # on top of the proportional/deadband damping.
                targets = smooth_neck_targets(targets, cur_pan, cur_tilt)

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
            # Strict wind-down order: (1) cancel + await the in-flight response so
            # the arm task is fully stopped and its cleanup has run; (2) recenter
            # the neck to the tracking tilt center; (3) return.
            if self._active is not None:
                self._active[0].cancel()
                await asyncio.gather(self._active[0], return_exceptions=True)
                self._active = None
            # Eased (smoothstep) recenter so the head glides to rest, not snaps.
            await self._recenter_neck(tilt_angle=cfg.tilt_center_deg)

    async def _dispatch_scan_response(self, name, scan_responses, mv):
        """Run one allowlisted arm-only response, fail-closed on a Gesture.

        Membership-guard: ``name`` must be a key in ``scan_responses`` (and thus
        in ``SCAN_SAFE_ARM_ACTIONS``). An absent name is logged and ignored — the
        name is never passed to ``getattr``/``eval``/``exec``/shell.

        Gesture subset guard (fail-closed): for a Gesture
        (``SCAN_SAFE_ARM_ACTIONS[name] is ScanActionKind.GESTURE``) the gesture's
        declared channel footprint ``SCAN_GESTURE_CHANNELS[name]`` must be a
        subset of ``ARM_ONLY_CHANNELS`` so it can never command a neck channel
        the tracker owns. A missing or non-subset entry is logged and the
        response is skipped — it never crashes the loop.

        On an exception from a Gesture (which may have died before lowering the
        arm) a best-effort ``return_to_rest`` restricted to ``ARM_ONLY_CHANNELS``
        runs so the arm is not left energized, then the error is re-raised so the
        loop logs it. Performance (Routine) responses rest the arm themselves via
        the ``rest_channels=ARM_ONLY_CHANNELS`` cleanup, so they are not re-rested
        here.

        Args:
            name: The chosen allowlisted action name.
            scan_responses: The name -> 0-arg response-callable map.
            mv: The shared ``Movements`` instance (for the fail path's arm rest).
        """
        if name not in scan_responses:
            print(f"[scan] REJECTED response '{name}': not in scan_responses - skipped")
            return

        kind = SCAN_SAFE_ARM_ACTIONS.get(name)
        is_gesture = kind is ScanActionKind.GESTURE
        if is_gesture:
            channels = SCAN_GESTURE_CHANNELS.get(name)
            if channels is None or not channels <= ARM_ONLY_CHANNELS:
                print(
                    f"[scan] REJECTED gesture '{name}': channel set {channels} "
                    f"is not a subset of ARM_ONLY_CHANNELS {set(ARM_ONLY_CHANNELS)} "
                    f"- skipped (fail-closed)"
                )
                return

        try:
            await scan_responses[name]()
        except Exception:
            if is_gesture:
                # A Gesture may have died before lowering the arm; best-effort
                # rest ONLY the arm channels (never the neck the tracker owns).
                try:
                    await Movements.trunkController.return_to_rest(
                        channels=ARM_ONLY_CHANNELS
                    )
                except Exception as rest_err:
                    print(f"[scan] arm rest after '{name}' failed: {rest_err}")
            # Re-raise so the loop logs the failure and starts the cooldown.
            raise

    def _run_scan_performance(self, defn, mv):
        """Run an arm-only Performance response inside the scan loop's event loop.

        Returns the ``PerformanceRunner.run()`` COROUTINE (does NOT call
        ``asyncio.run`` — ``_run_scan_loop`` already runs under ``asyncio.run``,
        and the response is awaited there as a task). ``rest_channels`` is pinned
        to ``ARM_ONLY_CHANNELS`` (FEAT-001) so the runner's cleanup rests only the
        arm and never the neck channels the tracker owns. Uses the same shared
        ``Movements`` instance the ``scan_responses`` lambdas use so the arm
        adapters share one ``TrunkController``.

        For ``hypnotic`` the ambient eye-blink is kept (bound to the audio
        window) exactly as the standalone routine; ``brains`` has no ambient.

        Args:
            defn: The arm-only ``PerformanceDefinition`` (built with ``scan=True``).
            mv: The shared ``Movements`` instance.

        Returns:
            The ``PerformanceRunner.run()`` coroutine to be awaited by the caller.
        """
        ambient = None
        if defn.name == "hypnotic":
            ambient = lambda pb: self._blink_eyes(pb, 0.25, 0.25)
        return PerformanceRunner(
            defn,
            mv,
            self._resolve_audio_dir(),
            ambient=ambient,
            rest_channels=ARM_ONLY_CHANNELS,
        ).run()

    # ------------------------------------------------------------------ #
    # Scan simple-runner / two-phase adapters                             #
    # ------------------------------------------------------------------ #
    # The four "simple-runner" scan adapters below are the arm-only equivalents
    # of the standalone simple-runner routines (awaken / comeGetCandy / yawn /
    # niceDay) that use run_action_and_audio (its own asyncio.run cannot be
    # awaited inside the already-running scan loop). Each drives audio with a
    # PlaybackController (see sneeze()/_do_sneeze) so it integrates with the
    # loop's event loop, wraps its motion in restrict_channels(ARM_ONLY_CHANNELS)
    # (FEAT-001 fail-closed per-task neck guard — a stray 0/1 write raises
    # ChannelGuardError only inside this response task, never on the tracker's
    # task), and rests ONLY ARM_ONLY_CHANNELS in a try/finally so the arm is
    # driven home on completion, error and cancellation and the neck is never
    # touched.

    async def _scan_awaken(self, mv):
        """Scan awaken: arm stir + awakened.wav, no head bob (arm-only).

        Mirrors the standalone ``awaken`` (ungated: motion + audio start
        together) but drops the head bob — only ``awaken_arm_only`` (owns
        {4,5,6,7}) runs so the neck stays free for the tracker.

        Args:
            mv: The shared ``Movements`` instance (its ``trunkController`` is the
                class-level one the tracker also uses).
        """
        audio_path = os.path.join(self._resolve_audio_dir(), self.music[26])  # awakened.wav
        duration = self._audio_duration_seconds(self.music[26])
        playback = PlaybackController(audio_path)
        try:
            with Movements.trunkController.restrict_channels(ARM_ONLY_CHANNELS):
                playback.start()  # ungated: audio at t=0 alongside the stir
                await mv.awaken_arm_only(duration=duration)
            playback.wait_finished(timeout=2)
        finally:
            await Movements.trunkController.return_to_rest(channels=ARM_ONLY_CHANNELS)

    async def _scan_come_get_candy(self, mv):
        """Scan comeGetCandy: beckon/comeHere + candy call (arm-only).

        Replicates the standalone timing exactly — audio LEADS the gesture:
        ``elf_hh_get_candy.wav`` starts after a 1.2s audio gate (standalone
        ``audio_delay=1.2``) while the chosen arm gesture starts after a 3s
        gesture gate (standalone ``self.idle``). The two gated branches run
        concurrently so audio begins at t≈1.2s and the arm at t≈3.0s. The gesture
        is ``random.choice((mv.beckon, mv.come_here))`` via the shared ``random``
        module so a seeded run matches the standalone. Both gestures are arm-only
        ({3,4,5,6,7}).

        Args:
            mv: The shared ``Movements`` instance.
        """
        gesture = random.choice((mv.beckon, mv.come_here))
        audio_path = os.path.join(self._resolve_audio_dir(), self.music[36])  # elf_hh_get_candy.wav
        playback = PlaybackController(audio_path)

        async def audio_branch():
            await asyncio.sleep(1.2)   # standalone audio_delay=1.2
            playback.start()

        async def gesture_branch():
            await asyncio.sleep(self.idle)  # standalone self.idle=3
            with Movements.trunkController.restrict_channels(ARM_ONLY_CHANNELS):
                await gesture()

        try:
            await asyncio.gather(audio_branch(), gesture_branch())
            playback.wait_finished(timeout=2)
        finally:
            await Movements.trunkController.return_to_rest(channels=ARM_ONLY_CHANNELS)

    async def _scan_yawn(self, mv):
        """Scan yawn: cover-mouth yawn + yawn.wav gated 0.3s (arm-only, -10).

        Mirrors the standalone ``yawn`` (``audio_delay=0.3``): the arm begins the
        fold at t=0 and ``yawn.wav`` comes in 0.3s later. Runs the self-contained
        ``yawn_cover_arm_only`` (owns {4,5,6,7}, never touches the neck) with the
        -10 scan elbow angle (``_YC_ELBOW_COVER_SCAN`` = 152) so the hand clears
        the off-center head.

        Args:
            mv: The shared ``Movements`` instance.
        """
        audio_path = os.path.join(self._resolve_audio_dir(), self.music[19])  # yawn.wav
        playback = PlaybackController(audio_path)

        async def audio_branch():
            await asyncio.sleep(0.3)   # standalone audio_delay=0.3
            playback.start()

        async def motion_branch():
            with Movements.trunkController.restrict_channels(ARM_ONLY_CHANNELS):
                await mv.yawn_cover_arm_only(elbow_cover=mv._YC_ELBOW_COVER_SCAN)

        try:
            await asyncio.gather(audio_branch(), motion_branch())
            playback.wait_finished(timeout=2)
        finally:
            await Movements.trunkController.return_to_rest(channels=ARM_ONLY_CHANNELS)

    async def _scan_nice_day(self, mv):
        """Scan niceDay: arm wave + "nice day" clip, arm leads audio (arm-only).

        Mirrors the standalone ``nice_day`` (arm leads by
        ``_NICE_DAY_ARM_LEAD + _NICE_DAY_AUDIO_LEAD`` = 0.5s): the wave arm starts
        at t=0 and ``elf_nice_day_walk.wav`` comes in 0.5s later. Runs the
        arm-only ``_wave_arm(include_neck=False)`` (owns {3,4,5,6,7}, no neck).

        Args:
            mv: The shared ``Movements`` instance.
        """
        audio_path = os.path.join(self._resolve_audio_dir(), self.music[37])  # elf_nice_day_walk.wav
        playback = PlaybackController(audio_path)
        audio_lead = self._NICE_DAY_ARM_LEAD + self._NICE_DAY_AUDIO_LEAD  # 0.5s

        async def audio_branch():
            await asyncio.sleep(audio_lead)
            playback.start()

        async def motion_branch():
            with Movements.trunkController.restrict_channels(ARM_ONLY_CHANNELS):
                await mv._wave_arm(include_neck=False)

        try:
            await asyncio.gather(audio_branch(), motion_branch())
            playback.wait_finished(timeout=2)
        finally:
            await Movements.trunkController.return_to_rest(channels=ARM_ONLY_CHANNELS)

    async def _scan_fart(self, mv):
        """Scan fart: fart.wav (pure audio) THEN forced arm-only cover-mouth.

        Two-phase, mirroring standalone ``fart`` but FORCING the arm-only
        cover-mouth branch (the standalone's 50/50 fan-nose coin flip is removed,
        because fan-nose drives the neck). No -10 (fart's cover branch keeps 162;
        operator verifies on hardware).

        Phase 1 plays ``fart.wav`` as pure audio on an
        ``AudioPlayer(drive_jaw=False, drive_eyes=False)`` offloaded via
        ``asyncio.to_thread`` (the player's ``play_audio_file`` is blocking) with
        a ``try/finally: close()`` so the GPIO pins are released even on
        cancellation; it commands NO servo. Phase 2 runs the arm-only
        cover-mouth Performance (owns {4,5,6,7}). Because neither phase can write
        the neck, this adapter is intentionally NOT ``restrict_channels``-wrapped
        (phase 2's safety is the build-time ``ConcurrentGroup`` ownership of the
        Performance def).

        Args:
            mv: The shared ``Movements`` instance.
        """
        # Phase 1: fart.wav alone, no movement, jaw/eyes off (a fart doesn't come
        # out of the mouth). Offload the blocking player to a worker thread so
        # the scan loop keeps running; close() in finally releases the pins.
        fart_path = os.path.join(self._resolve_audio_dir(), self.music[33])  # fart.wav
        player = AudioPlayer(drive_jaw=False, drive_eyes=False)
        try:
            print(f"[scan] fart phase 1: {fart_path} as pure audio (no jaw/eyes)")
            await asyncio.to_thread(player.play_audio_file, fart_path)
        finally:
            player.close()

        # Phase 2: forced arm-only cover-mouth (no coin flip). excuseme_sb.wav,
        # settled lead-in, elbow_cover=None (162, no -10).
        await self._run_scan_performance(
            self._cover_mouth_performance(mv, scan=True, kind="fart"), mv
        )

    async def _scan_fart_ghost(self, mv):
        """Scan fartGhost: fart.wav (pure audio) THEN arm-only cover-mouth react.

        Two-phase like ``_scan_fart`` but the phase-2 reaction carries the
        ghost's distinct clip (``elf_smell_ghost_burrito.wav`` via
        ``kind="fartGhost"``). The standalone reacts via the head-coupled
        fan-nose; the scan variant reuses the arm-only cover-mouth reaction so no
        neck channel is driven (an intended, operator-visible ungated→gated
        timing shift). No -10 (elbow_cover=None → 162).

        Phase 1 is identical to ``_scan_fart`` (pure ``fart.wav`` via
        ``AudioPlayer(drive_jaw=False, drive_eyes=False)`` + ``asyncio.to_thread``
        + ``try/finally: close()``). Not ``restrict_channels``-wrapped for the
        same reason as ``_scan_fart``.

        Args:
            mv: The shared ``Movements`` instance.
        """
        # Phase 1: identical to _scan_fart — fart.wav alone, jaw/eyes off.
        fart_path = os.path.join(self._resolve_audio_dir(), self.music[33])  # fart.wav
        player = AudioPlayer(drive_jaw=False, drive_eyes=False)
        try:
            print(f"[scan] fartGhost phase 1: {fart_path} as pure audio (no jaw/eyes)")
            await asyncio.to_thread(player.play_audio_file, fart_path)
        finally:
            player.close()

        # Phase 2: arm-only cover-mouth reaction with the ghost's clip.
        await self._run_scan_performance(
            self._cover_mouth_performance(mv, scan=True, kind="fartGhost"), mv
        )

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
            'niceDay':        self.nice_day,
            # Patrol / ambient
            'krusty':         self.krusty,
            # Reaction
            'blah':           self.blah,
            # New routines
            'vincentPrice':   self.vincent_price,
            'yawn':           self.yawn,
            'snuckUp':        self.snuck_up,
            'awaken':         self.awaken,
            'exorcist':       self.exorcist,
            'sneeze':         self.sneeze,
            # Performance-framework routines
            'brains':         self.brains,
            'hypnotic':       self.hypnotic,
            'clearThroat':    self.clear_throat,
            'coughLong':      self.cough_long,
            'coughMedium':    self.cough_medium,
            'maximus':        self.maximus,
            'burp':           self.burp,
            'fart':           self.fart,
            'fartGhost':      self.fart_ghost,
            'sleep':          self.snore,
            'moreCandy':      self.more_candy,
            'comeGetCandy':   self.come_get_candy,
            # Tracking Mode — camelCase key kept in the allowlist for parity with
            # the webapp's dispatch, but dispatched by main()'s dedicated branch
            # (NOT the generic servo_lock() path) because it takes only the
            # Neck_Group lock. See the 'tracking' branch in main().
            'tracking':       self.tracking,
            # Scan Mode — camelCase key kept in the allowlist for parity with the
            # webapp's dispatch, but dispatched by main()'s dedicated branch
            # (placed BEFORE the generic servo_lock() path) so the timeout flag
            # and single whole-robot lock are applied. See the 'scan' branch.
            'scan':           self.scan,
            # Puppeteer Mode — camelCase key kept in the allowlist for parity
            # with the webapp's dispatch, but dispatched by main()'s dedicated
            # branch (Neck_Group lock, suppress_triggers=True, no trigger
            # dispatch). Maps to self.tracking because Puppeteer IS neck-only
            # tracking + an externally-owned mic Stream; the key only needs to
            # make membership validation pass. See the 'puppeteer' branch.
            'puppeteer':      self.tracking,
        }

    # ------------------------------------------------------------------ #
    # Private gesture coroutines (called by run_action_and_audio)         #
    # ------------------------------------------------------------------ #

    async def _do_wave(self):
        # niceDay: start the wave arm almost immediately (not after the 3s
        # self.idle) so motion LEADS the audio. nice_day() holds the clip
        # _NICE_DAY_AUDIO_LEAD seconds via audio_delay. Only nice_day calls
        # _do_wave, so this lead-in change affects no other routine, and
        # mv.wave() is reused verbatim (no gesture fork).
        mv = Movements("Animatronic")
        await self._run_lead(mv.wave(), self._NICE_DAY_ARM_LEAD)

    async def _do_wave_and_swivel_smooth(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_swivel_smooth())

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

    async def _do_exorcist(self):
        # Ungated: motion + audio start together at t=0. neck_ellipse (neck
        # channels 0-1) and talking_hands_ii (arm channels 3-7) own DISJOINT
        # channels, so they run CONCURRENTLY under one asyncio.gather (the
        # Movements.awaken pattern). Each gesture is shorter than the clip, so
        # each is looped to the audio DEADLINE: both keep moving for the whole
        # beetel-exorcist.wav duration, then each eases its own channels back to
        # REST_POSITIONS at the end of its final iteration.
        mv = Movements("Animatronic")
        duration = self._audio_duration_seconds(self.music[0])  # beetel-exorcist.wav
        loop = asyncio.get_event_loop()
        end = loop.time() + max(0.0, duration)

        async def neck_loop():
            """Trace neck ellipses (ch 0-1) until the audio deadline."""
            while loop.time() < end:
                await mv.neck_ellipse(loops=1)

        async def arm_loop():
            """Oscillate the talking-hands arm (ch 3-7) until the audio deadline."""
            while loop.time() < end:
                await mv.talking_hands_ii(reps=2)

        await asyncio.gather(
            asyncio.create_task(neck_loop()),
            asyncio.create_task(arm_loop()),
        )

    async def _do_come_get_candy(self):
        # Randomly pick ONE of the two "come toward me" arm gestures per
        # invocation (beckon or comeHere). Both are arm-only; elf_hh_get_candy.wav
        # is gated 1.2s in come_get_candy() so the chosen gesture leads.
        mv = Movements("Animatronic")
        gesture = random.choice((mv.beckon, mv.come_here))
        await self._run(gesture())

    async def _do_sneeze(self, mv, playback):
        """Sneeze gesture coroutine: cover-mouth hold until audio ends + snapHead 5s in.

        Ungated and audio-driven. ``playback.start()`` begins ``sneeze.wav`` and
        ``start`` is anchored immediately after, so t≈0 == audio start. Two
        sub-coroutines run CONCURRENTLY under one ``asyncio.gather`` over DISJOINT
        channels (the ``Movements.awaken`` pattern): the arm hold owns {4,5,6,7},
        ``snap_head`` owns NECK_TILT {1}.

        hold_cover():
            1. ``nose_cover_lead_in_settled`` raises the hand to the mouth. There
               is NO pre-motion delay — the ungated, non-delayed ``_settled``
               lead-in centers the head and folds the hand with no
               ``_YC_MOTION_DELAY`` hold and no gate-lead sleep, so the FIRST arm
               servo write happens at t≈0 the instant audio starts.
            2. HOLD at the mouth: ``while playback.is_active(): await
               nose_cover_loop_body()`` — the hand stays folded (no servo re-
               commanded) for the full ``sneeze.wav`` duration, driven by the
               real playback-completion signal (``is_active()`` is ``True`` until
               the WAV thread drains), NOT a hardcoded sleep.
            3. ``nose_cover_return`` lowers the hand to rest and releases the
               verified-pose override the instant audio finishes.

        head_snap():
            sleeps until the 5s mark measured from audio start, then runs
            ``snap_head`` (NECK_TILT only, ends at rest 90).

        Shorter-than-5s edge case (documented ordering): if ``sneeze.wav`` is
        shorter than 5s, the hold loop exits and the hand lowers when audio ends
        — we do NOT artificially hold to 5s. ``head_snap`` independently still
        sleeps to the 5s mark and fires ``snap_head`` then, on a now-idle neck.
        Because the two sub-coroutines own disjoint channels, even a marginal
        overlap of the arm return and the neck snap is safe, and the ``gather``
        waits for BOTH so the routine always runs through the 5s snap.

        Safety: every servo write still goes through ``move_to``/``set_angle``
        (clamped to ``SAFE_LIMITS`` / the scoped ``_NOSE_COVER_OVERRIDE``). On
        any exception, and always on completion, everything is swept to safe rest
        via ``return_to_rest`` so no servo is left energized; the audio thread is
        joined (``wait_finished``) so the daemon track is never killed mid-clip.
        ``main()`` already wraps the dispatch in ``servo_lock()``, so this
        routine does not take the lock itself.
        """
        playback.start()
        start = time.monotonic()
        print(f"Playing audio: {playback.audio_path} (ungated, arm raises at t~0)")

        async def hold_cover():
            # Non-delayed lead-in: first arm servo write at t≈0, no gate/motion
            # delay. Hold at the mouth while audio plays, lower when it ends.
            await mv.nose_cover_lead_in_settled()
            while playback.is_active():
                await mv.nose_cover_loop_body()
            await mv.nose_cover_return()

        async def head_snap():
            # Fire snapHead at ~5s after audio start, regardless of whether the
            # hand is still up (disjoint channel: NECK_TILT only).
            await asyncio.sleep(max(0.0, 5.0 - (time.monotonic() - start)))
            await mv.snap_head()

        try:
            await asyncio.gather(
                asyncio.create_task(hold_cover()),
                asyncio.create_task(head_snap()),
            )
            # Sweep any residual channel home on normal completion.
            await mv.trunkController.return_to_rest()
        except Exception:
            # SAFETY: a raised gesture must never leave a servo energized —
            # drive everything to safe rest before re-raising.
            await mv.trunkController.return_to_rest()
            raise
        finally:
            # Join the daemon audio thread so sneeze.wav is never killed mid-clip
            # when asyncio.run unwinds and the servo-lock context tears down.
            playback.wait_finished(timeout=2)

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

    async def _do_fan_nose(self):
        # Gesture-only (no audio): fan the hand in front of the nose, then rest.
        # No idle lead-in; kept short for responsiveness (see
        # _do_look_around_random). fan_nose takes no duration arg and ends at
        # REST_POSITIONS on its own.
        mv = Movements("Animatronic")
        await mv.fan_nose()

    async def _do_tap_side(self):
        # Gesture-only (no audio): idly tap the hand against the side, then rest.
        # No idle lead-in. tap_side(reps=None) self-randomizes 3-5 reps, takes no
        # duration arg, and ends at REST_POSITIONS on its own.
        mv = Movements("Animatronic")
        await mv.tap_side()

    async def _do_face_palm(self):
        # Gesture-only (no audio): head-into-hand dismay, then recover to rest.
        # No idle lead-in. face_palm takes no duration arg and ends at
        # REST_POSITIONS on its own.
        mv = Movements("Animatronic")
        await mv.face_palm()


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


# --------------------------------------------------------------------------- #
# MODE INTERRUPT / ALLOWLIST REFERENCE                                          #
# --------------------------------------------------------------------------- #
# DISPLAY-ONLY reference data for the web control panel (src/webapp.py renders
# it under each mode's panel). For each Mode this lists, besides the mode's
# timer, the sensor(s) that can interrupt it and the allowlist of actions it may
# run on interrupt, split into sounds (.wav), gestures (no audio), and routines
# (audio). It is NOT consumed by any mode logic and triggers no servo motion.
#
# Honesty rule (do NOT invent entries): every name below is sourced from the
# actual mode implementation in THIS file (and detection_routine_map.py) — see
# the inline citation on each entry. Where a module/class constant already holds
# the names, the list is BUILT from it so it tracks code changes; literals that
# have no constant (e.g. the yawn.wav played at nap start, routine->wav
# mappings) carry a comment citing the symbol/line they come from. An empty list
# renders as "(N/A)" in the template (the template owns that presentation).
#
# Display names use the project's camelCase action convention
# (clearThroat / snuckUp / moreCandy / lookAroundRandom / handVisor) so they
# match the ROUTINE_ACTIONS / MOVEMENT_ACTIONS allowlists in webapp.py.

# Map the internal snake_case ambient-pool / reaction names used by the awake
# mode to the camelCase action names shown in the UI (and in webapp allowlists).
_AWAKE_ROUTINE_DISPLAY = {
    "yawn": "yawn",                 # music[19] yawn.wav
    "clear_throat": "clearThroat",  # clearThroat routine, music[28] clear_throat.wav
    "snuck_up": "snuckUp",          # snuckUp routine, music[25] snuck_up.wav
    "brains": "brains",             # brains routine, music[20] brains.wav
    "hypnotic": "hypnotic",         # hypnotic routine, music[21] + music[27]
    "more_candy": "moreCandy",      # moreCandy routine, music[23] more_candy.wav
    "awaken": "awaken",             # awaken routine, music[26] awakened.wav (nap startle)
}
# Map the awake ambient-pool / approach-reaction gesture coroutine names (_do_*)
# to camelCase.
_AWAKE_GESTURE_DISPLAY = {
    "_do_look_around_random": "lookAroundRandom",  # MOVEMENT_ACTIONS lookAroundRandom
    "_do_hand_visor": "handVisor",                 # MOVEMENT_ACTIONS handVisor
    "_do_fan_nose": "fanNose",                     # MOVEMENT_ACTIONS fanNose
    "_do_tap_side": "tapSide",                     # MOVEMENT_ACTIONS tapSide
    "_do_face_palm": "facePalm",                   # MOVEMENT_ACTIONS facePalm (approach reaction)
}

MODE_INTERRUPT_REFERENCE = {
    # --- NAPPING: Animatronic.napping ---------------------------------------
    "Napping": {
        # napping() calls self._open_nap_sensor() with the default
        # detect_mode="approach" (Animatronic._open_nap_sensor); an approach
        # within NAP_WAKE_GATE_M wakes the nap.
        "sensors": ["HC-SR04 proximity (approach)"],
        "sounds": [
            "yawn.wav",      # nap start: run_action_and_audio("_do_yawn", music[19])
            *Animatronic._NAP_SNORE_TRACKS,  # snore.wav, sb_snore.wav (sleep loop)
            "awakened.wav",  # sensor-wake _startle -> _do_awaken, music[26]
        ],
        # napping fires no standalone gesture — all motion lives inside routines.
        "gestures": [],
        # The Routines napping dispatches on a sensor wake (_startle), derived
        # from the live _NAP_STARTLE_REACTIONS constant → awaken, snuckUp, brains.
        "routines": [
            _AWAKE_ROUTINE_DISPLAY[name]
            for name in Animatronic._NAP_STARTLE_REACTIONS  # awaken, snuck_up, brains
        ],
        # Planned-but-unwired wake sensor: animation-vocabulary.md (Sleep mode)
        # says the mode is "interrupted by a sensor (sensor TBD)".
        "planned_sensors": ["motion/proximity sensor (planned)"],
        # Ambient (looping) behavior: _run_nap_loop repeats the head-lowered
        # sleep bob/rock (Movements.sleep_snore_loop_body, animatronic.py:1522).
        "ambient_gestures": ["Sleep bob/rock (snore loop)"],
        # The nap loop drives audio directly via AudioPlayer, not a named Routine.
        "ambient_routines": [],
        # Each segment plays a track chosen from _NAP_SNORE_TRACKS (line 1543).
        "ambient_sounds": [*Animatronic._NAP_SNORE_TRACKS],  # snore.wav, sb_snore.wav
    },
    # --- AWAKE: Animatronic.awake -------------------------------------------
    "Awake": {
        # awake() calls self._open_nap_sensor(source="awake",
        # detect_mode="presence"); presence within the gate triggers one random
        # reaction Routine (does not end the mode).
        "sensors": ["HC-SR04 proximity (presence)"],
        "sounds": [
            # Reachable through awake's occasional ambient routines + approach
            # reactions (the ambient gestures carry no audio).
            "yawn.wav",          # occasional routine yawn, music[19]
            "clear_throat.wav",  # occasional routine clearThroat, music[28]
            "snuck_up.wav",      # approach reaction snuckUp, music[25]
            "brains.wav",        # approach reaction brains, music[20]
            "hypnotic.wav",      # approach reaction hypnotic, music[21]
            "in_my_power.wav",   # hypnotic follow-on, music[27]
            "more_candy.wav",    # approach reaction moreCandy, music[23]
        ],
        # INTERRUPT gestures: the approach-reaction GESTURES only (NOT the
        # ambient pool) → facePalm. lookAroundRandom/handVisor stay ambient.
        "gestures": [
            _AWAKE_GESTURE_DISPLAY[name]
            for name in Animatronic._AWAKE_APPROACH_REACTION_GESTURES  # _do_face_palm
        ],
        # INTERRUPT routines: the approach-reaction ROUTINES only (NOT the
        # occasional ambient routines) → snuckUp, brains, hypnotic, moreCandy.
        # yawn/clearThroat are ambient, not interrupt reactions.
        "routines": [
            _AWAKE_ROUTINE_DISPLAY[name]
            for name in Animatronic._AWAKE_APPROACH_REACTION_ROUTINES  # snuck_up, brains, hypnotic, more_candy
        ],
        # Planned-but-unwired wake sensor: animation-vocabulary.md (Awake mode)
        # says the mode is "interrupted by a sensor (sensor TBD)".
        "planned_sensors": ["motion/proximity sensor (planned)"],
        # Ambient (looping) behavior: _pick_ambient_action weight-picks the
        # gesture-only entries of _AWAKE_AMBIENT_POOL (line 1898) each tick.
        "ambient_gestures": [
            _AWAKE_GESTURE_DISPLAY[name]
            for _, name in Animatronic._AWAKE_AMBIENT_POOL
            if name in _AWAKE_GESTURE_DISPLAY
        ],
        # The 10% "occasional" bucket loops a Routine from _AWAKE_OCCASIONAL_ROUTINES
        # (line 1904) — these carry audio, unlike the ambient gestures.
        "ambient_routines": [
            _AWAKE_ROUTINE_DISPLAY[name]
            for name in Animatronic._AWAKE_OCCASIONAL_ROUTINES  # yawn, clear_throat
        ],
        # Audio owned by the occasional looping routines (yawn / clearThroat).
        "ambient_sounds": ["yawn.wav", "clear_throat.wav"],
    },
    # --- SCAN: Animatronic.scan ---------------------------------------------
    "Scan": {
        # scan polls CameraClient.get_detections() against Camera_Service and
        # fires responses via scan_rules() (person / person+dog). It never calls
        # _open_nap_sensor, so it uses NO HC-SR04 range sensor.
        "sensors": ["Camera person/dog detection (Camera_Service)"],
        # Sounds reachable through scan's ROUTINE responses (GESTURE responses
        # carry no audio). The only ScanActionKind.ROUTINE entries are brains and
        # hypnotic (burp is deliberately withheld — see SCAN_SAFE_ARM_ACTIONS).
        "sounds": [
            "brains.wav",        # scan response brains, music[20]
            "hypnotic.wav",      # scan response hypnotic, music[21]
            "in_my_power.wav",   # hypnotic follow-on, music[27]
        ],
        # SCAN_SAFE_ARM_ACTIONS entries that are ScanActionKind.GESTURE.
        "gestures": [
            name
            for name, kind in SCAN_SAFE_ARM_ACTIONS.items()
            if kind is ScanActionKind.GESTURE
        ],
        # SCAN_SAFE_ARM_ACTIONS entries that are ScanActionKind.ROUTINE.
        # (walkYourDog for the person+dog rule is a logged placeholder only — not
        # in any allowlist/dispatch — so it is intentionally NOT listed.)
        "routines": [
            name
            for name, kind in SCAN_SAFE_ARM_ACTIONS.items()
            if kind is ScanActionKind.ROUTINE
        ],
        # No planned sensor named for scan in steering/code (it already uses the
        # camera) → empty; the template renders this as "(none planned)".
        "planned_sensors": [],
        # Ambient (looping) behavior: when select_target finds no person the loop
        # runs _run_scan_sweep (animatronic.py:2631), a slow NECK_PAN sweep.
        "ambient_gestures": ["Scan_Sweep (NECK_PAN sweep)"],
        # The sweep carries no audio (audio only on detection responses, above).
        "ambient_routines": [],
        "ambient_sounds": [],
    },
    # --- PUPPETEER: Animatronic.tracking(suppress_triggers=True) + mic Stream -
    "Puppeteer": {
        # Puppeteer tracks via CameraClient.get_detections() (same feed as
        # tracking/scan). It uses NO HC-SR04 range sensor.
        # NOTE (intentional): this reads "Camera person detection", NOT Scan's
        # "Camera person/dog detection", because Puppeteer has no dog rule
        # (triggers are suppressed entirely). The wording difference from the
        # "Scan" entry is deliberate — do NOT "fix" it to match Scan.
        "sensors": ["Camera person detection (Camera_Service)"],
        # Audio is the OPERATOR's live mic Stream (micwebcontroller /
        # AudioStreamer), not a canned track. No .wav files are dispatched by
        # the mode itself.
        "sounds": ["Operator live mic Stream (voice)"],
        # Puppeteer fires NO automatic gesture — arm-only Gestures are chosen by
        # the OPERATOR from the control panel while the mode runs.
        "gestures": [],
        # Detection->Routine triggering is SUPPRESSED (FR3): no Routine is ever
        # auto-dispatched, so a detection never seizes the jaw/audio path from
        # the operator's live mic.
        "routines": [],
        # No planned-but-unwired sensor beyond the camera already in use.
        "planned_sensors": [],
        # Ambient (looping) behavior: the neck tracker aims at the detected
        # person; when no person is found it runs the same Scan_Sweep NECK_PAN
        # sweep the tracking loop uses, but — UNLIKE plain tracking — a
        # Scan_Sweep timeout does NOT end the Mode. Puppeteer recenters the neck
        # to rest and idles (holds + polls) until a person reappears, so a live
        # performance is never cut short by the camera losing the person; it
        # ends only on an explicit stop or preemption.
        "ambient_gestures": ["Neck tracking / Scan_Sweep -> recenter+idle (NECK_PAN)"],
        # The tracker carries no audio of its own; audio is the operator's
        # Stream.
        "ambient_routines": [],
        "ambient_sounds": [],
    },
}


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
    elif args.action == 'scan':
        # Scan is a MODE: a continuous neck tracker PLUS a concurrent arm-only
        # responder (which may drive the whole arm + jaw/audio), so unlike
        # tracking it holds the WHOLE-ROBOT servo_lock() for its whole run. This
        # dedicated branch is placed BEFORE the generic `args.action in
        # action_map` branch so it shadows the generic path (which would call
        # a.scan() with no timeout). The call is allowlist-gated: 'scan' is an
        # explicit key in action_map and this is an explicit branch — args.action
        # is never passed to getattr/eval/shell. The timeout minutes are clamped
        # to [0, 120] here, where 0 = NO timeout -> timeout_seconds=None (scan's
        # loop already guards `deadline is not None`); other values convert to
        # seconds. 0 is NOT floored to a 1-second timeout. Fail fast if the
        # servos are already in use.
        try:
            with servo_lock():
                scan_min = max(0, min(120, args.scan_timeout_min))
                a.scan(
                    camera_url=args.camera_url,
                    timeout_seconds=(None if scan_min == 0 else scan_min * 60),
                    max_step=args.max_step,
                    deadband=args.deadband,
                    conf=args.conf,
                    scan_timeout=args.scan_timeout,
                    aim_frac=args.aim_frac,
                    tilt_center=args.tilt_center,
                    tilt_min=args.tilt_min,
                    tilt_max=args.tilt_max,
                    settle_gain=args.settle_gain,
                )
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action == 'puppeteer':
        # Puppeteer is a MODE: live mic Stream + neck-only tracking + operator
        # arm Gestures. Like tracking it holds ONLY the Neck_Group lock (channels
        # 0-1) so an arm-only Gesture (channels 4-7) can run concurrently; it
        # must NOT take the whole-robot servo_lock(). The mic Stream is owned by
        # the web app (started on launch) and holds no servo lock, so
        # animatronic.py does not manage audio here. This dedicated branch is
        # placed BEFORE the generic `args.action in action_map` branch so it
        # shadows the generic path (which would call a.tracking() with triggers
        # armed and no lock). Allowlist-gated: 'puppeteer' is an explicit key in
        # action_map AND this is an explicit branch — args.action is never passed
        # to getattr/eval/shell. Triggers are suppressed so a detection can never
        # seize the jaw/audio path away from the operator's live mic (FR3).
        try:
            with group_lock(NECK_GROUP):
                a.tracking(
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
                    suppress_triggers=True,
                    # Puppeteer runs until the operator explicitly stops it: a
                    # camera Scan_Sweep timeout (no person in view) must NOT wind
                    # the Mode down the way plain tracking does (Req 6.10).
                    # Instead the neck recenters to rest and idles (holds +
                    # polls) until a person reappears; the operator is performing
                    # live, so the Mode stays up until an explicit stop
                    # (nap_signal) or a preempting Mode.
                    end_on_scan_timeout=False,
                )
            # No pending_trigger to dispatch: suppress_triggers=True always
            # returns None, so there is deliberately NO _dispatch_detection_trigger
            # call here (unlike the tracking branch).
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
        reason = None
        try:
            with servo_lock():
                # CLI knob is in MINUTES; clamp to the web app's bounds (0-120)
                # so a direct CLI call is bounded too, then convert to the
                # SECONDS the method/loop operate in (the method stays seconds).
                # 0 passes through as 0 = NO timeout (the loop guard reads a
                # falsy timeout_seconds as "no deadline"); it is NOT floored to
                # a 1-second timeout.
                nap_min = max(0, min(120, args.nap_timeout_min))
                reason = a.napping(timeout_seconds=nap_min * 60)
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
        # OUTSIDE the lock + busy handler: report WHY the nap ended via the exit
        # code (10 timeout / 11 sensor / 12 stop) so the webapp chain watcher can
        # decide whether to chain. A busy refusal already exited 3; an uncaught
        # non-busy crash still produces Python's default exit 1 (non-chaining).
        sys.exit(mode_exit.reason_to_exit_code(reason))
    elif args.action == 'awake':
        # Awake is a MODE: it performs ambient Routines on a loop, so it holds
        # the servo lock for its whole run just like napping. It runs until
        # interrupted (timeout, sensor, or an external stop request from the web
        # app). Fail fast if the servos are already in use.
        reason = None
        try:
            with servo_lock():
                # CLI knob is in MINUTES; clamp to the web app's bounds (0-120)
                # so a direct CLI call is bounded too, then convert to the
                # SECONDS the method/loop operate in (the method stays seconds).
                # 0 passes through as 0 = NO timeout (the loop guard reads a
                # falsy timeout_seconds as "no deadline"); it is NOT floored to
                # a 1-second timeout.
                awake_min = max(0, min(120, args.awake_timeout_min))
                # Awake NEVER ends on a sensor event: by design it only returns
                # to sleep on its TIMEOUT (a presence event just triggers one
                # react-and-resume reaction, since whoever woke the figure is
                # typically still standing in the gate). So chain_sensor_end is
                # always False — ending awake on sensor made it bounce straight
                # back to sleep the moment it woke. Awake still chains to napping
                # via its timeout (AWAKE_INTERRUPT_TIMEOUT is chainable); sleep
                # still wakes to awake on sensor OR timeout.
                reason = a.awake(timeout_seconds=awake_min * 60,
                                 chain_sensor_end=False)
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
        # OUTSIDE the lock + busy handler: report WHY awake ended via the exit
        # code (10 timeout / 11 sensor / 12 stop) so the webapp chain watcher can
        # decide whether to chain. A busy refusal already exited 3; an uncaught
        # non-busy crash still produces Python's default exit 1 (non-chaining).
        sys.exit(mode_exit.reason_to_exit_code(reason))
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
                        help='Action to perform (e.g. startParty, krusty, blah, napping).')
    parser.add_argument('--nap-timeout-min', dest='nap_timeout_min', type=int,
                        default=1,
                        help='Napping mode: minutes before the timeout wake, '
                             '0..120 where 0 = no timeout (manual stop only) '
                             '(default 1). Only used with --action=napping.')
    parser.add_argument('--awake-timeout-min', dest='awake_timeout_min',
                        type=int, default=5,
                        help='Awake mode: minutes before the timeout ends the '
                             'mode, 0..120 where 0 = no timeout (manual stop '
                             'only) (default 5). Only used with --action=awake.')
    # Tracking Mode flags (only used with --action=tracking). TrackingConfig
    # clamps every numeric value into its documented safe range, so argparse
    # only needs sensible types here; a None default means "use the
    # TrackingConfig default" (so the dataclass owns the real default).
    parser.add_argument('--scan-timeout', dest='scan_timeout', type=int,
                        default=10,
                        help='Tracking mode: Scan_Sweep reacquire timeout in '
                             'seconds, clamped to 1-120 (default: 10). Only '
                             'used with --action=tracking.')
    # Scan Mode timeout (only used with --action=scan). DISTINCT from
    # --scan-timeout above (the tracking Scan_Sweep reacquire timeout in
    # SECONDS): this is the whole-Mode wind-down timeout in MINUTES.
    parser.add_argument('--scan-timeout-min', dest='scan_timeout_min', type=int,
                        default=60,
                        help='Scan mode: minutes before the timeout winds the '
                             'mode down, 0..120 where 0 = no timeout (manual '
                             'stop only) (default 60). Only used with '
                             '--action=scan.')
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
