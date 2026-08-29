"""
animatronic.py

Top-level named routines that combine servo gestures with audio playback.

Each public method on Animatronic pairs a movement choreography (from
movements.py) with a sound file played via lightshowpi's synchronized_lights
script.  Audio and movement run concurrently: the lightshowpi subprocess
starts first, the movement coroutine runs to completion, then the subprocess
is terminated.

Usage (run as root so lightshowpi can access GPIO / audio hardware):
    sudo /usr/bin/python3 animatronic.py --action=<action_name>

Available actions:
    startParty, yoda, torture, blah, vaderBeaten, exorcist, waiting,
    vaderFather, krusty, hello, happyHalloween, niceDay, howYallDoin,
    cantHear, mic

lightshowpi mic mode (live audio input):
    Reads from microphone instead of a file; config must live inside the
    lightshowpi/config directory (not an absolute path or LSP will ignore it).
    sudo /usr/bin/python3 /home/pi/workspace/lightshowpi/py/synchronized_lights.py \
         --config="overrides-mic.cfg"

Note on asyncio:
    asyncio.run() is called inside run_action_and_audio() rather than at the
    module level to avoid "cannot be called from a running event loop" errors
    that arise when other event loops are already active in the process.
"""

from movements import Movements
import asyncio
import shlex
import subprocess
import argparse
#from audio_player import AudioPlayer as player


class Animatronic:
    """Pairs named audio tracks with matching servo gesture routines."""

    # --- lightshowpi configuration ---
    # All Pi-specific paths are isolated here so they have a single place to update.
    lightshow_python     = '/usr/bin/python3'
    lightshow_script     = '/home/pi/workspace/lightshowpi/py/synchronized_lights.py'
    lightshow_dir        = '/home/pi/Music/'
    lightshow_mic_config = '/home/pi/workspace/lightshowpi/config/overrides-mic.cfg'

    # Ordered list of audio filenames.  Indices are used by the action methods
    # below — see each method for which index it references.
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
    ]

    # Seconds to wait before starting the movement (lets audio begin first).
    idle = 3

    # ------------------------------------------------------------------ #
    # Movement-only coroutines                                             #
    # ------------------------------------------------------------------ #

    async def patrol(self):
        """Idle patrol: neck ellipse arc followed by a small look-around."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        await mv.neck_ellipse()
        await asyncio.sleep(1)
        await mv.look_around_small()

    async def swivel_head_and_wave(self):
        """Wave the arm while swivelling the head concurrently."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        wave       = asyncio.create_task(mv.wave())
        swivel_head = asyncio.create_task(mv.swivel_head())
        await asyncio.gather(wave, swivel_head)

    async def no(self):
        """Shake the head "no"."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        await mv.shake_no()

    async def wave(self):
        """Wave the arm."""
        mv = Movements("Orchestrate Movements")
        # await asyncio.sleep(self.idle)
        await mv.wave()

    async def look_around_small(self):
        """Small curious look-around (shorter idle delay)."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(1)
        await mv.look_around_small()

    async def neck_ellipse(self):
        """Trace an elliptical arc with the head (shorter idle delay)."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(1)
        await mv.neck_ellipse()

    async def come_and_swivel_head(self):
        """Beckon gesture while swivelling the head concurrently."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        come        = asyncio.create_task(mv.come())
        swivel_head = asyncio.create_task(mv.swivel_head())
        await asyncio.gather(come, swivel_head)

    async def come_and_look(self):
        """Beckon gesture while looking around concurrently."""
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        come       = asyncio.create_task(mv.come())
        look_around = asyncio.create_task(mv.look_around())
        await asyncio.gather(come, look_around)

    # ------------------------------------------------------------------ #
    # Core audio + movement runner                                         #
    # ------------------------------------------------------------------ #

    def run_action_and_audio(self, method_name, audio_file):
        """Run a gesture coroutine and a lightshowpi audio process together.

        Starts lightshowpi as a subprocess, runs the named async movement
        method to completion, then terminates the audio subprocess.
        The subprocess is always terminated even if the gesture raises.

        Args:
            method_name: Name of an async method on this class (e.g. 'wave').
            audio_file:  Filename (not full path) of the audio file to play.
        """
        audio_path = self.lightshow_dir + audio_file
        cmd = [
            'sudo',
            self.lightshow_python,
            self.lightshow_script,
            f'--file={shlex.quote(audio_path)}',
        ]
        print(' '.join(cmd))
        proc = subprocess.Popen(cmd)
        try:
            asyncio.run(getattr(self, method_name)())
        except Exception as e:
            print(f"Error during gesture '{method_name}': {e}")
        finally:
            proc.terminate()

    # ------------------------------------------------------------------ #
    # Named routines (action → gesture + audio pairings)                  #
    # ------------------------------------------------------------------ #

    def start_party(self):
        """Party mode: wave + swivel head to the SpongeBob party switch track."""
        self.run_action_and_audio("swivel_head_and_wave", self.music[3])

    def ripped_pants(self):
        """Ripped-pants routine (uses torture audio as placeholder)."""
        self.run_action_and_audio("come_and_look", self.music[4])

    def torture(self):
        """SpongeBob torture audio with come-and-look gesture."""
        self.run_action_and_audio("come_and_look", self.music[4])

    def exorcist(self):
        """Exorcist theme audio with come-and-look gesture."""
        self.run_action_and_audio("come_and_look", self.music[0])

    def yoda900(self):
        """Yoda 900 years audio with patrol gesture."""
        self.run_action_and_audio("patrol", self.music[9])

    def vader_beaten(self):
        """Vader beaten audio with patrol gesture."""
        self.run_action_and_audio("patrol", self.music[5])

    def waiting(self):
        """"We're waiting" audio with come-and-look gesture."""
        self.run_action_and_audio("come_and_look", self.music[7])

    def vader_father(self):
        """"I am your father" audio with come-and-look gesture."""
        self.run_action_and_audio("come_and_look", self.music[6])

    def krusty(self):
        """Krusty laugh audio with neck ellipse gesture."""
        self.run_action_and_audio("neck_ellipse", self.music[2])

    def yoda_fear(self):
        """Yoda fear audio with come-and-look gesture."""
        self.run_action_and_audio("come_and_look", self.music[10])

    def blah(self):
        """Blah audio with head-shake "no" gesture."""
        self.run_action_and_audio("no", self.music[1])

    def hello(self):
        """Hello audio with wave gesture."""
        self.run_action_and_audio("wave", self.music[11])

    def happy_halloween(self):
        """Happy Halloween audio with wave gesture."""
        self.run_action_and_audio("wave", self.music[12])

    def nice_day(self):
        """Walk/nice day audio with wave gesture."""
        self.run_action_and_audio("wave", self.music[13])

    def how_yall_doin(self):
        """How y'all doing audio with wave gesture."""
        self.run_action_and_audio("wave", self.music[14])

    def cant_hear(self):
        """Can't hear audio with wave gesture."""
        self.run_action_and_audio("wave", self.music[15])


def main(args):
    """Dispatch the requested --action to the corresponding Animatronic method.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    a = Animatronic()

    action_map = {
        'startParty':     a.start_party,
        'yoda':           a.yoda900,
        'torture':        a.torture,
        'blah':           a.blah,
        'vaderBeaten':    a.vader_beaten,
        'exorcist':       a.exorcist,
        'waiting':        a.waiting,
        'vaderFather':    a.vader_father,
        'krusty':         a.krusty,
        'hello':          a.hello,
        'happyHalloween': a.happy_halloween,
        'niceDay':        a.nice_day,
        'howYallDoin':    a.how_yall_doin,
        'cantHear':       a.cant_hear,
    }

    if args.action in action_map:
        action_map[args.action]()
    elif args.action == 'mic':
        # Microphone/live-input mode — pass audio directly to lightshowpi.
        cmd = [
            'sudo',
            a.lightshow_python,
            a.lightshow_script,
            f'--config={shlex.quote(a.lightshow_mic_config)}',
        ]
        print(' '.join(cmd))
        proc = subprocess.Popen(cmd)
        # TODO: run a complementary movement while mic mode is active.
        proc.terminate()
    else:
        print(f"Unknown action: {args.action}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Animatronic controller — run a named gesture + audio routine."
    )
    parser.add_argument('--action', default=None,
                        help='Action to perform (e.g. startParty, wave, hello).')
    args = parser.parse_args()
    print(args.action)
    main(args)
