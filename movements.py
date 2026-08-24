from time import sleep
import asyncio
from trunkcontroller import TrunkController
import concurrent.futures
import model.constants as constants
from model.bodyparts import BodyParts

"""
Macro movements built from trunkcontroller functions
"""

"""
Class for orchestrated movements that make expressions and gestures
"""


class Movements:
    def __init__(self, name):
        self.name = name
        self.DEFAULT_DELAY = 0.05

    trunkController = TrunkController("Servo TrunkController")

    # TODO: gesture to come towards
    async def come(self):
        print("come here")
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 110

        # elbow rotate
        RT_ELBOW_ROTATE_MIN = 10
        RT_ELBOW_ROTATE_MAX = 120
        
        # elbow bend
        RT_ELBOW_TILT_MIN = 30
        RT_ELBOW_TILT_MAX = 100

        revert = True
            
        increasing = True
        # raise arm
         
        await self.trunkController.move(BodyParts.RT_SHOULDER_ROTATOR.value, RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.175, revert=False,revertDelay= 1)
        # palm up
        await self.trunkController.move(BodyParts.RT_ELBOW_ROTATOR.value, RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.1, revert=False, revertDelay=1)

        print("come arm up")
   
        for x in range(4):
            await self.trunkController.move(BodyParts.RT_ELBOW_TILT.value,  RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.095, revert=True, revertDelay=self.DEFAULT_DELAY)

       
       
        # increasing = False
        # palm down
        await self.trunkController.move(BodyParts.RT_ELBOW_ROTATOR.value,  RT_ELBOW_ROTATE_MAX, RT_ELBOW_ROTATE_MIN, 0.3, revert=False, revertDelay=.5)
    
        # # lower arm
        await self.trunkController.move(BodyParts.RT_SHOULDER_ROTATOR.value,  RT_SHOULDER_ROTATOR_MAX, RT_SHOULDER_ROTATOR_MIN, 0.5, revert=False, revertDelay=.5)
        print("come here done")

    async def comein(self):
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 40

        # elbow rotate
        RT_ELBOW_ROTATE_MIN = 10
        RT_ELBOW_ROTATE_MAX = 130
        
        # elbow bend
        RT_ELBOW_TILT_MIN = 25
        RT_ELBOW_TILT_MAX = 160

        revert = True
            
        increasing = True
        # raise arm
        await self.trunkController.move_servo_eased(BodyParts.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 1.0,reverse=False)
        # palm up
        await self.trunkController.move_servo_eased(BodyParts.RT_ELBOW_ROTATOR,  RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 1.0, reverse=False)

        for x in range(3):
            await self.trunkController.move(BodyParts.RT_ELBOW_TILT.value,  RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 1.0, revert=False)
        
        # sleep(.2)
       
        increasing = False
        # lower arm
        await self.trunkController.move_servo_eased(BodyParts.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 1.0, reverse=True)
        # # palm down
        await self.trunkController.move_servo_eased(BodyParts.RT_ELBOW_ROTATOR,  RT_ELBOW_TILT_MIN, RT_ELBOW_ROTATE_MAX, 1.0, reverse=True)


    async def lookAround(self):
        loop = asyncio.get_event_loop()
        await self.trunkController.neckCenter()
        neckTilt = loop.create_task(self.trunkController.neckTilt(10, 50))
        neckPan = loop.create_task(self.trunkController.neckPan())
        await asyncio.gather(neckTilt, neckPan)
        await self.trunkController.neckCenter()

    async def lookAroundSmall(self):
        await self.trunkController.neckCenter()
        await asyncio.sleep(.25)
        for x in range(2):
            loop = asyncio.get_event_loop()
            neckTilt = loop.create_task(self.trunkController.neckTilt(10, 30))
            neckPan = loop.create_task(self.trunkController.neckPan())
            await asyncio.gather(neckTilt, neckPan)
            await asyncio.sleep(.5)
        
        await self.trunkController.neckCenter()


    async def neckEllipse(self):
        await self.trunkController.neckCenter()
        loop = asyncio.get_event_loop()
        neckTilt = loop.create_task(self.trunkController.neckTilt(0, 45))
        neckPan = loop.create_task(self.trunkController.neckPan())
        await asyncio.gather(neckTilt, neckPan)
        await asyncio.sleep(1) 
        await self.trunkController.neckCenter()

    async def swivelHead(self):
        await self.trunkController.neckCenter()
        await self.neckEllipse()
        await self.neckEllipse()
        await asyncio.sleep(1) 
        await self.trunkController.neckCenter()
    
    
    async def scan(self):
        await self.trunkController.neckCenter()
        for _ in range(2):
            await self.trunkController.neckPan()
        
        await asyncio.sleep(1) 
        await self.trunkController.neckCenter()

    async def slowScan(self):
        await self.trunkController.neckCenter()
        await self.trunkController.slowScan();
        
    
    async def nodYes(self, revert=True):
        NECK_TILT_MIN = 0
        NECK_TILT_MAX = 45
        for _ in range(2):
            await self.trunkController.move(BodyParts.NECK_TILT.value, NECK_TILT_MIN, NECK_TILT_MAX, 0.075, revert, .05)

    async def shakeHead(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 120
        await self.trunkController.neckCenter()
        for _ in range(3):
            await self.trunkController.move(BodyParts.NECK_PAN.value , NECK_PAN_MIN, NECK_PAN_MAX, 0.075, revert, 1)
        
        await asyncio.sleep(1)
        await self.trunkController.neckCenter()
       

    async def shakeNo(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.trunkController.neckCenter()
        for _ in range(2):
            await self.trunkController.move(BodyParts.NECK_PAN.value, NECK_PAN_MIN, NECK_PAN_MAX, 0.25, revert, 0.01)
        
        await asyncio.sleep(.5)            
        await self.trunkController.neckCenter()


    async def smallShakeNo(self, revert=True):
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 110
        await self.trunkController.neckCenter()
        for _ in range(2):
            await self.trunkController.move(BodyParts.NECK_PAN.value, NECK_PAN_MIN, NECK_PAN_MAX, 1.0, revert, 0.01)
        
        await asyncio.sleep(1)            
        await self.trunkController.neckCenter()        

    async def wave(self):
       
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 20
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 270
        RT_ELBOW_ROTATE_MIN = 0
        RT_ELBOW_ROTATE_MAX = 45
        # elbow is reversed here but works for come() so we can't flip
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 0

        # raise arm at shoulder
        increasing = True
        await self.trunkController.move(BodyParts.RT_SHOULDER_ROTATOR.value,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, .175, revert=False)
         # raise forearm at elbow
        await self.trunkController.move(BodyParts.RT_ELBOW_TILT.value, RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.1, revert=False)
        #sleep(.25)
        # rotate elbow to wave
        # for i in range(0, 3, 1):
        #     revert = i % 2 == 0
        for x in range(4):
            sleep(0.04)
            await self.trunkController.move(BodyParts.RT_ELBOW_ROTATOR.value,  RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.095, revert=True, revertDelay=self.DEFAULT_DELAY)
        for x in range(4):
            await self.trunkController.move(BodyParts.RT_SHOULDER_TILT.value,  RT_SHOULDER_TILT_MIN, RT_SHOULDER_TILT_MAX, 0.095, revert=True, revertDelay=self.DEFAULT_DELAY)
          
        # lower arm
        increasing = False
        await self.trunkController.move(BodyParts.RT_SHOULDER_ROTATOR.value,  RT_SHOULDER_ROTATOR_MAX, RT_SHOULDER_ROTATOR_MIN, 1, revert=False)
        await self.trunkController.move(BodyParts.RT_ELBOW_TILT.value, RT_ELBOW_TILT_MAX, RT_ELBOW_TILT_MIN, 0.5, revert=True)

mv = Movements("Servo Movements")
#asyncio.run(mv.wave())
#asyncio.run(mv.come())
#asyncio.run(mv.shakeHead())
#asyncio.run(mv.shakeNo())
#asyncio.run(mv.lookAround())
#asyncio.run(mv.neckEllipse())
#asyncio.run(mv.swivelHead())
# method to cover mouth like yawn
