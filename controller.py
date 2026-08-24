"""
controller.py

CLI entry point for triggering individual movement gestures.

Accepts a single --action argument and runs the matching async coroutine from
the Movements class.  Useful for testing individual gestures without audio or
the full Animatronic routine stack.

Usage:
    python controller.py --action=<action_name>

Available actions:
    wave, yes, no, smno, lookAround, scan, slowScan,
    swivelHead, come, comein, neckEllipse, lookAroundSmall
"""

from movements import Movements
import asyncio
import argparse


def main(args):
    """Dispatch the requested --action to the corresponding Movements coroutine.

    Args:
        args: Parsed argparse Namespace with an 'action' attribute.
    """
    mv = Movements("Orchestrate Movements")

    action_map = {
        'wave':           mv.wave,
        'yes':            mv.nod_yes,
        'no':             mv.shake_no,
        'smno':           mv.small_shake_no,
        'lookAround':     mv.look_around,
        'scan':           mv.scan,
        'slowScan':       mv.slow_scan,
        'swivelHead':     mv.swivel_head,
        'come':           mv.come,
        'comein':         mv.comein,
        'neckEllipse':    mv.neck_ellipse,
        'lookAroundSmall': mv.look_around_small,
    }

    print(args.action)

    if args.action in action_map:
        asyncio.run(action_map[args.action]())
    elif args.action is not None:
        print(f"Unknown action: {args.action}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Run a single animatronic movement gesture."
    )
    parser.add_argument('--action', default=None, help='Gesture to perform.')
    args = parser.parse_args()
    main(args)
