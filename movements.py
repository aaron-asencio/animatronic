from time import sleep
import asyncio
from trunkcontroller import TrunkController
import concurrent.futures

"""
Macro movements built from trunkcontroller functions
"""

"""
Class for orchestrated movements that make expressions and gestures
"""
class Movements:
    def __init__(self, name):
        self.name = name

    trunkController = TrunkController("Servo TrunkController")
    # should be in an include
    servos = {}
    NECK_TILT = 5
    NECK_PAN = 4
    RT_SHOULDER_ROTATOR = 3
    RT_ELBOW_PAN = 2
    RT_ELBOW_TILT = 1

    # map user friendly names
    servos[NECK_TILT] = "NECK_TILT"
    servos[NECK_PAN] = "NECK_PAN"
    servos[RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
    servos[RT_ELBOW_PAN] = "RT_ELBOW_PAN"
    servos[RT_ELBOW_TILT] = "RT_ELBOW_TILT"

    def lookAround(self):
        loop = asyncio.get_event_loop()
        neckTilt = loop.create_task(self.trunkController.neckTilt())
        neckPan = loop.create_task(self.trunkController.neckPan())
        #shoulder = loop.create_task(controller.shoulder())
        loop.run_until_complete(asyncio.gather(neckTilt, neckPan))
        asyncio.run(self.trunkController.neckCenter())

    def scan(self):
        for i in range(0, 2):
            asyncio.run(self.trunkController.neckPan())

    async def nodYes(self, revert=True):
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 30
        for x in range(0, 2):
            await self.trunkController.move(self.NECK_TILT, NECK_TILT_MIN, NECK_TILT_MAX, 0.015, revert, .05)

    async def shakeHead(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        for x in range(0, 3):
            await self.trunkController.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, 1)

        await asyncio.sleep(1)

    async def shakeNo(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        for x in range(0, 2):
            await self.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.005, revert, 0.01)
        await self.neckCenter()

    async def wave(self):
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 220
        RT_ELBOW_PAN_MIN = 0
        RT_ELBOW_PAN_MAX = 80
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 15

        #raise arm
        increasing = True
        await self.trunkController.moveByDirection(self.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.002, increasing)
        await self.trunkController.moveByDirection(self.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        await self.trunkController.moveByDirection(self.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.002, increasing)
        sleep(1)
        # rotate elbow to wave
        for i in range(0, 3, 1):
            revert = i % 2 == 0
            await self.trunkController.move(self.RT_ELBOW_PAN,  RT_ELBOW_PAN_MIN, RT_ELBOW_PAN_MAX, 0.005, revert, 0.05)
        # lower arm
        increasing = False
        await self.trunkController.moveByDirection(self.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.0025, increasing)
        await self.trunkController.moveByDirection(self.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.002, False)

