"""
driver.py

Early prototype / sandbox for low-level TrunkController testing.

This file was used to explore direct TrunkController calls and experiment
with asyncio event-loop patterns before the Movements and Animatronic layers
were built.  It is not part of the normal runtime but is kept for reference.

To test a specific behaviour, uncomment the relevant call in main().
"""

from trunkcontroller import TrunkController
import asyncio
import subprocess


def look_around():
    """Run neck-pan, neck-tilt, and shoulder-rotate concurrently, then center.

    Demonstrates composing concurrent async tasks with asyncio.gather()
    and asyncio.run() at the top of the call stack.
    """
    async def _run():
        controller = TrunkController("Servo TrunkController")
        neck_tilt = asyncio.create_task(controller.neck_tilt())
        neck_pan  = asyncio.create_task(controller.neck_pan())
        shoulder  = asyncio.create_task(controller.shoulder_rotate())
        await asyncio.gather(neck_tilt, neck_pan, shoulder)
        await controller.neck_center()

    asyncio.run(_run())


def wave():
    """Run the arm wave sequence directly from TrunkController.

    Demonstrates calling a single async coroutine with asyncio.run().
    """
    controller = TrunkController("Servo TrunkController")
    asyncio.run(controller.arm())


def main():
    """Sandbox entry point — uncomment the call you want to test."""
    # look_around()
    # wave()

    # Example: trigger lightshowpi directly (legacy test).
    # subprocess.call(
    #     ['/home/pi/lightshowpi/py/synchronized_lights.py',
    #      '--file=/home/pi/Music/sb_party_switch.mp3'],
    #     shell=False
    # )
    pass


if __name__ == '__main__':
    main()
