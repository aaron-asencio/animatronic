"""audioplaytest.py

Play an audio file through AudioPlayer to test the audio -> jaw + eyes pipeline
WITHOUT touching the servos.

AudioPlayer only drives the jaw motor (MOUTH_MOTOR_PIN) and the eye LED
(EYE_LIGHT_PIN) from audio amplitude — it never moves the arm/neck servos. So
this test works even when the servo board is unavailable (which makes the full
routines fail). Use it to verify jaw movement and eye flashing track the audio.

Runs on the Raspberry Pi with GPIO + audio access; may need root:

    sudo ../.venv/bin/python3 audioplaytest.py --file krusty-laugh.wav
    sudo ../.venv/bin/python3 audioplaytest.py --file blah.wav --dir /home/aaron/Music
"""

import argparse
import os
import sys

from audio_player import AudioPlayer


def main(args):
    """Play one audio file through AudioPlayer (jaw + eyes, no servos).

    Args:
        args: Parsed argparse Namespace with file, dir, and output_device.
    """
    audio_path = args.file
    if not os.path.isabs(audio_path):
        audio_path = os.path.join(args.dir, audio_path)

    if not os.path.exists(audio_path):
        print(f"Audio file not found: {audio_path}")
        sys.exit(1)

    player = AudioPlayer()
    print(f"Playing {audio_path} (jaw + eyes; no servos)")
    try:
        player.play_audio_file(audio_path, output_device_index=args.output_device)
    finally:
        # Leave the hardware in a safe idle state.
        player.jaw_motor.off()
        player.led_eye_light.off()
        print("Playback complete; jaw closed, eyes off.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Test file playback with jaw + eye sync (no servos)."
    )
    parser.add_argument('--file', required=True,
                        help='Audio file name (or absolute path) to play.')
    parser.add_argument('--dir', default='/home/aaron/Music',
                        help='Directory to resolve --file against when not absolute '
                             '(default: /home/aaron/Music).')
    parser.add_argument('--output-device', dest='output_device', type=int, default=2,
                        help='PyAudio output device index (default: 2).')
    args = parser.parse_args()
    main(args)
