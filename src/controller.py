"""
controller.py

CLI entry point for testing individual gestures without audio.

Accepts --action and runs the matching Movements coroutine directly.
Useful for tuning angles and timing before wiring up audio routines.

Usage:
    python3 controller.py --action=<action_name>

ARM gestures (channels 4–7):
    wave, come, comein, reachOut, yawnCover

HEAD gestures (channels 0–1):
    nod, nodYes, lookUp, lookAround, lookAroundSmall, neckEllipse,
    swivelHead, scan, slowScan, shakeHead, shakeNo, smallShakeNo

COMPOSITE gestures (arm + head simultaneously):
    waveAndNod, waveAndLookAround, waveAndSwivel,
    comeAndLook, comeAndSwivel, reachAndLook, yawnAndLookUp, patrol
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
        'come':             mv.come,
        'comein':           mv.comein,
        'reachOut':         mv.reach_out,
        'yawnCover':        mv.yawn_cover,

        # --- HEAD gestures ---
        'nod':              mv.nod,
        'nodYes':           mv.nod_yes,
        'lookUp':           mv.look_up,
        'lookAround':       mv.look_around,
        'lookAroundSmall':  mv.look_around_small,
        'neckEllipse':      mv.neck_ellipse,
        'swivelHead':       mv.swivel_head,
        'scan':             mv.scan,
        'slowScan':         mv.slow_scan,
        'shakeHead':        mv.shake_head,
        'no':               mv.shake_no,
        'smno':             mv.small_shake_no,

        # --- COMPOSITE gestures ---
        'waveAndNod':       mv.wave_and_nod,
        'waveAndLookAround': mv.wave_and_look_around,
        'waveAndSwivel':    mv.wave_and_swivel,
        'comeAndLook':      mv.come_and_look,
        'comeAndSwivel':    mv.come_and_swivel,
        'reachAndLook':     mv.reach_and_look,
        'yawnAndLookUp':    mv.yawn_and_look_up,
        'patrol':           mv.patrol,
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
            print("Servos busy — another routine is already running. Aborting.")
            sys.exit(BUSY_EXIT_CODE)
    elif args.action is not None:
        print(f"Unknown action: {args.action}")
        print(f"Available: {', '.join(sorted(action_map))}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Test a single animatronic gesture (no audio)."
    )
    parser.add_argument('--action', default=None,
                        help='Gesture to perform (e.g. wave, comeAndLook, patrol).')
    args = parser.parse_args()
    main(args)
