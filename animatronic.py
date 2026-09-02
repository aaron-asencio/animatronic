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
import asyncio
import threading
import argparse
import sys
import os


class Animatronic:
    """Pairs named audio tracks with matching servo gesture routines."""

    # Audio directory — resolves to the invoking user's ~/Music so it works for
    # 'pi', 'aaron', etc. Under sudo, HOME/expanduser may resolve to /root, so
    # prefer the SUDO_USER's home. Override with ANIMATRONIC_AUDIO_DIR.
    @staticmethod
    def _resolve_audio_dir():
        override = os.environ.get('ANIMATRONIC_AUDIO_DIR')
        if override:
            return override
        sudo_user = os.environ.get('SUDO_USER')
        if sudo_user:
            return os.path.join('/home', sudo_user, 'Music')
        return os.path.join(os.path.expanduser('~'), 'Music')



    # Audio file list — indices referenced by the routine methods below.
    music = [
        'beetel-exorcist.wav',     # 0
        'blah.wav',                # 1
        'krusty-laugh.wav',        # 2
        'sb_party_switch.wav',     # 3
        'spongebob-torture.mp3',   # 4
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
        """Blah audio — emphatic head-shake no."""
        self.run_action_and_audio("_do_shake_no", self.music[1])

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
        """Yawn — cover mouth + look up (no audio, gesture test)."""
        asyncio.run(self._do_yawn())

    # ------------------------------------------------------------------ #
    # Private gesture coroutines (called by run_action_and_audio)         #
    # ------------------------------------------------------------------ #

    async def _do_wave(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave())

    async def _do_wave_and_swivel(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_swivel())

    async def _do_wave_and_nod(self):
        mv = Movements("Animatronic")
        await self._run(mv.wave_and_nod())

    async def _do_come_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.come_and_look())

    async def _do_come_and_swivel(self):
        mv = Movements("Animatronic")
        await self._run(mv.come_and_swivel())

    async def _do_reach_and_look(self):
        mv = Movements("Animatronic")
        await self._run(mv.reach_and_look())

    async def _do_yawn(self):
        mv = Movements("Animatronic")
        await self._run(mv.yawn_and_look_up())

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
