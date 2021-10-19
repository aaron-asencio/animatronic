from adafruit_servokit import ServoKit
from time import sleep
import asyncio

"""
Class for basic movements of the trunk
"""

class TrunkController:

    def __init__(self, name):
        self.name = name

    kit = ServoKit(channels=16)
    for i in range(0, 4):
        kit.servo[i].actuation_range = 270

    # set up servos with 270 range
    print("servo setup")

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

    # neck servo pan
    async def neckPan(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)

    async def neckFullPan(self, revert=True):
        NECK_PAN_MIN = 0
        NECK_PAN_MAX = 180
        await self.move(self.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)    

    # neck servo tilt
    async def neckTilt(self, min=30, max=95, revert=True):
        await self.move(self.NECK_TILT, min, max, 0.025, revert, .1)
       

    async def neckCenter(self, revert=True):
        NECK_CENTER = 90
        currentPosition = round(self.kit.servo[self.NECK_PAN].angle)
        increase = currentPosition < NECK_CENTER
        print("Current pos: " + str(increase) + ". Should increase? "+ str(increase))
        # don't move if already at center
        if currentPosition != NECK_CENTER:
            # determine increasing or decreasing then use moveByDirection 
            await self.moveByDirection(self.NECK_PAN, currentPosition, NECK_CENTER, 0.05, increase)
          

    async def neckTiltCenter(self):
        NECK_TILT_MIN = 19
        NECK_TILT_MAX = 21
        await self.move(self.NECK_TILT, NECK_TILT_MIN, NECK_TILT_MAX, 0.1, False, 1)

    # elbow servo pan
    async def elbowPan(self, revert=True):
        RT_ELBOW_PAN_MIN = 0
        RT_ELBOW_PAN_MAX = 180
        await self.move(self.RT_ELBOW_PAN, RT_ELBOW_PAN_MIN,
                        RT_ELBOW_PAN_MAX, 0.01, revert, 1)

    # elbow servo tilt
    async def elbowTilt(self, revert=True):
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 180
        await self.move(self.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN,
                        RT_ELBOW_TILT_MAX, 0.01, revert, 1)

    # rt shoulder servo
    async def shoulder(self, revert=True):
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 200
        await self.move(self.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN,
                        RT_SHOULDER_ROTATOR_MAX, 0.005, revert, 1)

    

    # TODO: make a method that can take multiple servos
    # revert arg flips the start and stop and can make it increment/decrement
    async def move(self, servo_num=0, start=0, stop=180, delay=0.1, revert=True, revertDelay=0.5):
        print("moving " + self.servos[servo_num])
        servo = self.kit.servo[servo_num]
        for i in range(start, stop, 1):
            servo.angle = i
            currentPosition = round(servo.angle)
            print("Servo angle set " +str(i) + "; angle returned: " + str(currentPosition) )
            await asyncio.sleep(delay)

        if(revert):
            sleep(revertDelay)
            for i in range(stop, start, -1):
                servo.angle = i
                await asyncio.sleep(delay)

    async def moveByDirection(self, servo_num, start, stop, delay=0.1, increasing=True):
        print("moving " + self.servos[servo_num] +
              "; increasing:" + str(increasing))

        if(increasing):
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)
          
        if(not increasing):
            print("not increasing " + self.servos[servo_num])
            print("start " + str(start) + "; stop:" + str(stop))
            for i in range(stop, start, -1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)

    def displayPosition(self):
        # for servo_num, servo_name in enumerate(servos, start=1):
        for servo_num in range(0, len(self.servos), 1):
            print(str(round(self.kit.servo[servo_num].angle)))

    async def test(self):
        # await self.neckTiltCenter()
        # await self.neckPan()
        await self.neckCenter()
        # await self.neckTiltCenter()
        # await self.neckTilt()
        # await self.neckTiltCenter()
        # await self.shoulder()
        # await self.elbowPan()
        # await self.elbowTilt()
        #await self.displayPosition()
        #await self.neckFullPan()


trunkController = TrunkController("Servo TrunkContoller")
asyncio.run(trunkController.test())
#asyncio.run(trunkController.wave())
