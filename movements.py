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

    # TODO: gesture to come towards
    async def come(self):
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 40

        # elbow rotate
        RT_ELBOW_PAN_MIN = 10
        RT_ELBOW_PAN_MAX = 260
        
        # elbow bend
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 70

        revert = True
            
        increasing = True
        # raise arm
        await self.trunkController.moveByDirection(self.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
        # palm up
        await self.trunkController.moveByDirection(self.RT_ELBOW_PAN,  RT_ELBOW_PAN_MIN, RT_ELBOW_PAN_MAX, 0.0025, increasing)
        
        for x in range(3):
            await self.trunkController.move(self.RT_ELBOW_TILT,  RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, revert, 0.05)
        
        sleep(.2)
       
        increasing = False
        # lower arm
        await self.trunkController.moveByDirection(self.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.005, increasing)
        # palm down
        await self.trunkController.moveByDirection(self.RT_ELBOW_PAN,  RT_ELBOW_PAN_MIN, RT_ELBOW_PAN_MAX, 0.0025, increasing)
      
        
        
    async def lookAround(self):
        loop = asyncio.get_event_loop()
        await self.trunkController.neckCenter()
        neckTilt = loop.create_task(self.trunkController.neckTilt(10, 50))
        neckPan = loop.create_task(self.trunkController.neckPan())
        await asyncio.gather(neckTilt, neckPan)
        await self.trunkController.neckCenter()

    async def neckEllipse(self):
        loop = asyncio.get_event_loop()
        neckTilt = loop.create_task(self.trunkController.neckTilt(0, 80))
        neckPan = loop.create_task(self.trunkController.neckPan())
        await asyncio.gather(neckTilt, neckPan)

    async def swivelHead(self):
        await self.trunkController.neckCenter()
        await self.neckEllipse()
        await self.neckEllipse()
        # would like to sleep here but have to make this async and then have to change controller.py...
        await asyncio.sleep(.2) 
        await self.trunkController.neckCenter()
    
    def swivelHeadWorks(self):
        # this method cannot be async and even if it was you cannot call asyncio.run() on it because it uses asyncio.run in it
        asyncio.run(self.trunkController.neckCenter())
        asyncio.run(self.neckEllipse())
        asyncio.run(self.neckEllipse())
        # would like to sleep here but have to make this async and then have to change controller.py...
        asyncio.run(self.trunkController.neckCenter())   
   
    def scan(self):
        asyncio.run(self.trunkController.neckCenter())
        for _ in range(2):
            asyncio.run(self.trunkController.neckPan())
        asyncio.run(self.trunkController.neckCenter())

    async def nodYes(self, revert=True):
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 30
        for _ in range(2):
            await self.trunkController.move(self.NECK_TILT, NECK_TILT_MIN, NECK_TILT_MAX, 0.015, revert, .05)

    async def shakeHead(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        await self.trunkController.neckCenter()
        for _ in range(3):
            await self.trunkController.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, 1)
        await self.trunkController.neckCenter()
        await asyncio.sleep(1)

    async def shakeNo(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.trunkController.neckCenter()
        for _ in range(2):
            await self.trunkController.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.005, revert, 0.01)
        await self.trunkController.neckCenter()

    async def wave(self):
       
        RT_SHOULDER_ROTATOR_MIN = 10
        RT_SHOULDER_ROTATOR_MAX = 190
        RT_ELBOW_PAN_MIN = 0
        RT_ELBOW_PAN_MAX = 80
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 30

        # raise arm
        increasing = True
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
        print("Waved to the fans")

