"""
controller.py

CLI entry point for testing individual gestures without audio.

Accepts --action and runs the matching Movements coroutine directly.
Useful for tuning angles and timing before wiring up audio routines.

Usage:
    python3 controller.py --action=<action_name>

ARM gestures (channels 4–7):
    wave, beckon, comeHere, menacingReach, yawnCover, facePalm, fanButt

HEAD gestures (channels 0–1):
    yes, lookAroundSmall, lookAroundRandom, neckEllipse,
    swivelHead, shakeHead, smallShakeNo

COMPOSITE gestures (arm + head simultaneously):
    waveAndSwivelSmooth

Tracking Mode (NOT a controller gesture):
    Head tracking is a camera-fed Mode, not an audio-free one-shot gesture, so
    it is deliberately absent from this module's action_map. It needs the
    Camera_Service (src/camera_service.py) running and acquires only the
    Neck_Group lock. Run it from animatronic.py instead:
        sudo python3 src/animatronic.py --action=tracking
    (optional flags: --camera-url, --scan-timeout, --max-step, --deadband,
    --conf). It cannot be exercised standalone from controller.py because the
    gestures here are self-contained one-shots with no detection feed.
"""

from movements import Movements
from servo_lock import servo_lock, ServoBusyError, BUSY_EXIT_CODE
import asyncio
import argparse
import sys


def main(args):
    """Dispatch --action to the corresponding Movements coroutine.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    mv = Movements("Controller")

    action_map = {
        # --- ARM gestures ---
        'wave':             mv.wave,
        'beckon':           mv.beckon,
        'comeHere':         mv.come_here,
        'menacingReach':    mv.menacing_reach,
        'yawnCover':        mv.yawn_cover,
        'facePalm':         mv.face_palm,
        'fanButt':          mv.fan_butt,

        # --- HEAD gestures ---
        'yes':              mv.nod,
        'lookAroundSmall':  mv.look_around_small,
        'lookAroundRandom': mv.look_around_random,
        'neckEllipse':      mv.neck_ellipse,
        'swivelHead':       mv.swivel_head,
        'shakeHead':        mv.shake_head,
        'smno':             mv.small_shake_no,
        'snuckUp':          mv.snuck_up,
        'awaken':           mv.awaken,

        # --- COMPOSITE gestures ---
        'waveAndSwivelSmooth': mv.wave_and_swivel_smooth,
        'handVisor':        mv.hand_visor,
    }

    print(args.action)

    if args.action in action_map:
        # SAFETY: acquire the system-wide servo lock so this gesture cannot run
        # concurrently with another routine/movement. Concurrent servo commands
        # can stall the arm against a block and overheat the motor. Fail fast.
        try:
            with servo_lock():
                try:
                    asyncio.run(action_map[args.action]())
                except Exception as e:
                    print(f"Error during gesture '{args.action}': {e}")
                    # A stalled servo may be left energized against a jam —
                    # drive everything back to safe rest before exiting.
                    try:
                        asyncio.run(mv.trunkController.return_to_rest())
                    except Exception as rest_err:
                        print(f"return_to_rest failed: {rest_err}")
        except ServoBusyError:
            print("Servos busy - another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action is not None:
        print(f"Unknown action: {args.action}")
        print(f"Available: {', '.join(sorted(action_map))}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Test a single animatronic gesture (no audio)."
    )
    parser.add_argument('--action', default=None,
                        help='Gesture to perform (e.g. wave, yes, swivelHead).')
    args = parser.parse_args()
    main(args)
