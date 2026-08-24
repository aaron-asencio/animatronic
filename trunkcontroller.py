from time import sleep
import asyncio
import math
import model.constants as constants
from model.bodyparts import BodyParts
from adafruit_servokit import ServoKit

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
    servos[BodyParts.NECK_TILT] = "NECK_TILT"
    servos[BodyParts.NECK_PAN] = "NECK_PAN"
    servos[BodyParts.RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
    servos[BodyParts.RT_SHOULDER_TILT] = "RT_SHOULDER_TILT"
    servos[BodyParts.RT_ELBOW_TILT] = "RT_ELBOW_TILT"
    servos[BodyParts.RT_ELBOW_ROTATOR] = "RT_ELBOW_ROTATOR"
    
    NECK_CENTER = 90

    # neck servo pan
    async def neckPan(self, revert=True):
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
        await self.move(BodyParts.NECK_PAN.value, NECK_PAN_MIN, NECK_PAN_MAX, 1, revert, .1)
        await asyncio.sleep(.5)

    async def neckFullPan(self, revert=True):
        NECK_PAN_MIN = 0
        NECK_PAN_MAX = 180
        await self.move(BodyParts.NECK_PAN.value, NECK_PAN_MIN, NECK_PAN_MAX, 1, revert, .1)
        await asyncio.sleep(.5)    

    # neck servo tilt
    async def neckTilt(self, min=30, max=95, revert=True):
        await self.move(BodyParts.NECK_TILT.value, min, max,1, revert, .1)
       
    async def neckCenter(self, revert=True):
        await self.returnToStart(BodyParts.NECK_PAN.value, self.NECK_CENTER,delay=0.00005)
   
    async def neckTiltCenter(self):
        NECK_TILT_MIN = 19
        NECK_TILT_MAX = 21
        await self.move(BodyParts.NECK_TILT.value, NECK_TILT_MIN, NECK_TILT_MAX, 0.005, False, 1)

    # shoulder servo pan
    async def shoulderTilt(self, revert=True):
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 230
        await self.move(BodyParts.RT_SHOULDER_TILT.value, RT_SHOULDER_TILT_MIN,
                        RT_SHOULDER_TILT_MAX,   0.75, revert, 1)

    # elbow servo tilt
    async def elbowTilt(self, revert=True):
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 190
        await self.move(BodyParts.RT_ELBOW_TILT.value, RT_ELBOW_TILT_MIN,
                        RT_ELBOW_TILT_MAX, 0.075, revert, 1)
    # elbow servo pan   
    async def elbowRotate(self, revert=True):
        RT_ELBOW_ROTATE_MIN = 0
        RT_ELBOW_ROTATE_MAX = 150
        await self.move(BodyParts.RT_ELBOW_ROTATOR.value, RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.075, revert, .1)
        await asyncio.sleep(.5)
  
    # rt shoulder servo
    async def shoulderRotate(self, revert=True):
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 270
        await self.move(BodyParts.RT_SHOULDER_ROTATOR.value, RT_SHOULDER_ROTATOR_MIN,
                        RT_SHOULDER_ROTATOR_MAX, 0.75, revert, 1)
        #self.kit.servo[self.RT_SHOULDER_ROTATOR].angle = RT_SHOULDER_ROTATOR_MIN 

    

    # revert arg flips the start and stop and can make it increment/decrement
    async def move(self, servo_num=0, start=0, stop=180, duration=0.075, revert=False, revertDelay=0.05):
    
        print(f"move called with servo_num={servo_num}, start={start}, stop={stop}, duration={duration}, revert={revert}, revertDelay={revertDelay}")
        servo = self.kit.servo[servo_num]
        
        #await self.move_servo_sine(servo, start, stop, duration=2.0, steps=60)
        await self.move_servo_eased(servo, start, stop, duration, steps=50, reverse=False)
        #await self.move_servo_exponential(servo, start, stop, smoothing_factor=0.15, threshold=0.5)
        # for i in range(start, stop, 1):
        #     servo.angle = i
        #     currentPosition = round(servo.angle)
        #     print("Servo angle set " +str(i) + "; angle returned: " + str(currentPosition) )
        #     await asyncio.sleep(delay)

        if(revert):
            sleep(revertDelay)
            await self.move_servo_eased(servo, start, stop, duration, steps=50, reverse=True)
            # for i in range(stop, start, -1):
            #     servo.angle = i
            #     await asyncio.sleep(delay)
    
    # Method 1: Ease-In-Out (Smoothest)
    async def move_servo_eased(self, servo, start, stop, duration=1.0, steps=50, reverse=False):
        print(f"move_servo_eased called with servo={servo} start={start}, stop={stop}, duration={duration}, steps={steps}, reverse={reverse}")
        """
        Smooth movement using ease-in-out interpolation
        Starts slow, speeds up in middle, slows down at end
        
        Args:
            servo: The servo object to control
            start: Starting angle
            stop: Ending angle
            duration: Total time for movement in seconds
            steps: Number of steps for smooth motion
            reverse: If True, reverses the direction (swaps start and stop)
        """
        # Reverse direction if requested
        print(f"reverse is {reverse}")
        if reverse == True:
            print("*********reversing movement")
            start, stop = stop, start
        
        for i in range(steps + 1):
            t = i / steps  # Normalize to 0-1
            
            # Ease-in-out formula
            if t < 0.5:
                eased_t = 2 * t * t
            else:
                eased_t = 1 - pow(-2 * t + 2, 2) / 2
            
            # Calculate angle based on eased progress
            angle = start + (stop - start) * eased_t
            servo.angle = angle
            
            currentPosition = round(servo.angle)
            print(f"Servo angle set {angle:.1f}; angle returned: {currentPosition}")
            
            await asyncio.sleep(duration / steps)
        

            
    # Method 3: Exponential Smoothing (Natural feel)
    async def move_servo_exponential(self, servo, start, stop, smoothing_factor=0.15, threshold=0.5):
        """
        Exponential smoothing - approaches target naturally
        Higher smoothing_factor = faster movement (0.1 = smooth, 0.5 = fast)
        """
        current = start
        servo.angle = current
        
        while abs(current - stop) > threshold:
            # Move a fraction of the remaining distance
            current += (stop - current) * smoothing_factor
            servo.angle = current
            
            currentPosition = round(servo.angle)
            print(f"Servo angle set {current:.1f}; angle returned: {currentPosition}")
            
            await asyncio.sleep(0.02)  # 50Hz update rate
        
        # Ensure we reach exact target
        servo.angle = stop
        print(f"Servo angle set {stop}; angle returned: {round(servo.angle)}")
            
    # Method 4: Sine Wave Easing (Very smooth)
    async def move_servo_sine(self, servo, start, stop, duration=1.0, steps=50):
        """
        Sine wave easing - very smooth and natural
        """
        for i in range(steps + 1):
            t = i / steps
            
            # Sine easing: sin((t * π) / 2)
            eased_t = math.sin((t * math.pi) / 2)
            
            angle = start + (stop - start) * eased_t
            servo.angle = angle
            
            currentPosition = round(servo.angle)
            print(f"Servo angle set {angle:.1f}; angle returned: {currentPosition}")
            
            await asyncio.sleep(duration / steps)
     
    async def slowScan(self, revert=True):
        
        NECK_LEFT = 150
        NECK_RIGHT = 40
        print("slow scan ")
        # don't move if already at center
        revert = True
        await self.move(BodyParts.NECK_PAN.value, NECK_RIGHT, NECK_LEFT, 1.20, revert, .1)
        await self.move(BodyParts.NECK_PAN.value,  NECK_LEFT,self.NECK_CENTER, 2.0, revert = False)
    

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
#asyncio.run(trunkController.shoulderRotate())
#asyncio.run(trunkController.shoulderTilt())
#asyncio.run(trunkController.elbowTilt())
#asyncio.run(trunkController.elbowRotate())
#asyncio.run(trunkController.slowScan())
# RT_SHOULDER_ROTATOR_MIN = 0
# RT_SHOULDER_ROTATOR_MAX = 270
# RT_ELBOW_ROTATE_MIN = 0
# RT_ELBOW_ROTATE_MAX = 80
# RT_ELBOW_TILT_MIN = 0
# RT_ELBOW_TILT_MAX = 50
#asyncio.run(trunkController.move_servo_eased(BodyParts.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, .5, 50, reverse=False))
#asyncio.run(trunkController.move_servo_eased(BodyParts.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 1, 50, reverse=False))