from adafruit_servokit import ServoKit
from time import sleep
import asyncio
import constants

"""
Class for basic movements of the trunk
"""

class TrunkController:

    def __init__(self, name):
        self.name = name

    kit = ServoKit(channels=16)
    for i in range(0, 15):
        kit.servo[i].actuation_range = 270

    # set up servos with 270 range
    print("servo setup")

    servos = {}
    # map user friendly names
    servos[constants.NECK_TILT] = "NECK_TILT"
    servos[constants.NECK_PAN] = "NECK_PAN"
    servos[constants.RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
    servos[constants.RT_SHOULDER_TILT] = "RT_SHOULDER_TILT"
    servos[constants.RT_ELBOW_TILT] = "RT_ELBOW_TILT"
    servos[constants.RT_ELBOW_ROTATOR] = "RT_ELBOW_ROTATOR"
    
    NECK_CENTER = 90

    # neck servo pan
    async def neckPan(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)

    async def neckFullPan(self, revert=True):
        NECK_PAN_MIN = 0
        NECK_PAN_MAX = 180
        await self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)    

    # neck servo tilt
    async def neckTilt(self, min=30, max=95, revert=True):
        await self.move(constants.NECK_TILT, min, max, 0.025, revert, .1)
       
    async def neckCenter(self, revert=True):
        await self.returnToStart(constants.NECK_PAN, self.NECK_CENTER,delay=0.04)
   
    async def neckTiltCenter(self):
        NECK_TILT_MIN = 19
        NECK_TILT_MAX = 21
        await self.move(constants.NECK_TILT, NECK_TILT_MIN, NECK_TILT_MAX, 0.1, False, 1)

    # shoulder servo pan
    async def shoulderTilt(self, revert=True):
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 230
        await self.move(constants.self.RT_SHOULDER_TILT, RT_SHOULDER_TILT_MIN,
                        RT_SHOULDER_TILT_MAX, 0.01, revert, 1)

    # elbow servo tilt
    async def elbowTilt(self, revert=True):
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 120
        await self.move(constants.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN,
                        RT_ELBOW_TILT_MAX, 0.01, revert, 1)
    # elbow servo pan   
    async def elbowRotate(self, revert=True):
        RT_ELBOW_ROTATE_MIN = 30
        RT_ELBOW_ROTATE_MAX = 150
        await self.move(constants.RT_ELBOW_ROTATOR, RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.01, revert, .1)
        await asyncio.sleep(.5)
  
    # rt shoulder servo
    async def shoulderRotate(self, revert=True):
        RT_SHOULDER_ROTATOR_MIN = 60
        RT_SHOULDER_ROTATOR_MAX = 270
        await self.move(constants.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN,
                        RT_SHOULDER_ROTATOR_MAX, 0.01, revert, 1)
        #self.kit.servo[self.RT_SHOULDER_ROTATOR].angle = RT_SHOULDER_ROTATOR_MIN 

    

    # TODO: make a method that can take multiple servos
    # revert arg flips the start and stop and can make it increment/decrement
    async def move(self, servo_num=0, start=0, stop=180, delay=0.1, revert=True, revertDelay=0.5):
        print("moving " + constants.servos[servo_num])
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
     
    async def slowScan(self, revert=True):
        
        NECK_LEFT = 110
        NECK_RIGHT = 70
        print("slow scan ")
        # don't move if already at center
        increase = True
        await self.moveByDir(constants.NECK_PAN, self.NECK_CENTER, NECK_LEFT, 0.05, increase)
        increase = False
        await self.moveByDir(constants.NECK_PAN, self.NECK_CENTER, NECK_RIGHT, 0.05, increase)
        
    async def returnToStart(self, servo_num, start, delay=0.1):
       
        # if current pos not start, send back to their gently
        # or just start their
        print(f"testing servo num: {servo_num}");
        print(f"number of servo channels: {self.kit._channels}");
        print( self.kit.servo[servo_num])
        print(f"angle: {self.kit.servo[servo_num].angle}")
        

        # check if value is None - it should have been setup with starting value no? Need to move it slowly to start if it isn't
        if self.kit.servo[servo_num].angle == None:
            self.kit.servo[servo_num].angle = start

        currentPosition = round(self.kit.servo[servo_num].angle)
       

        print("return to start " + constants.servos[servo_num] + " which is at " + str(currentPosition))
        if(currentPosition != start and currentPosition <= 270):
            if(currentPosition > start):
                # if decrementing the lower number is second
                for i in range(currentPosition, start, -1):
                    print("return to start now " + str(i));
                    self.kit.servo[servo_num].angle = i
                    await asyncio.sleep(delay)
            else:
                for i in range(currentPosition, start, 1):
                    self.kit.servo[servo_num].angle = i
                    await asyncio.sleep(delay)
                    
    async def moveByDir(self, servo_num, start, stop, delay=0.1, increasing=True):
        print("moving " + constants.servos[servo_num] +
              "; increasing:" + str(increasing))
        # TODO: if current pos not start, send back to their gently
        # or just start their?
        currentPosition = round(self.kit.servo[servo_num].angle)
        
        await self.returnToStart(servo_num, start,delay=0.1)
        
        if(increasing):
            print("increasing " + constants.servos[servo_num] + "; start " + str(start) + "; stop:" + str(stop))
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                print(i)
                await asyncio.sleep(delay)
        else:
            print("decreasing " + constants.servos[servo_num] + "; start " + str(start) + "; stop:" + str(stop))
            for i in range(start, stop,-1):
                self.kit.servo[servo_num].angle = i
                print(i)
                await asyncio.sleep(delay)
                
        await self.returnToStart(servo_num, start,delay=0.1)

    async def moveByDirection(self, servo_num, start, stop, delay=0.1, increasing=True):
        print("moving " + constants.servos[servo_num] +
              "; increasing:" + str(increasing))

        if(increasing):
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)
          
        if(not increasing):
            print("not increasing " + constants.servos[servo_num])
            print("start " + str(start) + "; stop:" + str(stop))
            for i in range(stop, start, -1):
                self.kit.servo[servo_num].angle = i
                await asyncio.sleep(delay)

    def displayPosition(self):
        # for servo_num, servo_name in enumerate(servos, start=1):
        for servo_num in range(0, len(constants.servos), 1):
            #print(str(round(self.kit.servo[servo_num].angle)))
            print(str(self.kit.servo[servo_num].angle))
    
    async def arm(self):
        await self.shoulderRotate() # shoulder up
        await self.shoulderTilt() 
        await self.elbowTilt() 
        await self.elbowRotate() 

    async def neck(self):
        #await self.neckTiltCenter()
        #await self.neckCenter() 
        #await self.neckPan() # jerks to right
        #await self.slowScan()
        #await self.neckFullPan()  # jerks to right
        #await self.neckTiltCenter() # barely moves it 
        await self.neckCenter() 
        await self.neckTilt() # must call neck center first!
        #await self.neckCenter()
        # await self.displayPosition() 

    async def test(self):
        await self.arm()
        await self.neck()
        
# uncomment lines below to test
trunkController = TrunkController("Servo TrunkContoller")
#asyncio.run(trunkController.neck())

