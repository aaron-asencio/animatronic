from concurrent.futures import ThreadPoolExecutor
from trunkcontroller import TrunkController
from movements import Movements
from time import sleep, perf_counter
from adafruit_servokit import ServoKit

import constants

class ConcurrentMovements:

    def __init__(self, name):
        self.name = name

    trunk = TrunkController("Servo TrunkController")
    mv = Movements("Servo Movements")

    kit = ServoKit(channels=16)
    for i in range(0, 15):
        kit.servo[i].actuation_range = 270

    
    def move(self, servo_num=0, start=0, stop=180, delay=0.1, revert=True, revertDelay=0.5):
        """
        Moves a servo in a positive direction and if revert is true, will return back to origin.
        servo_num -- number identifying server
        start -- start angle of servo
        stop -- stop angle of servo
        delay -- delay between each degree of turn in the servo motor. 
        Lower number increases speed.
        revert -- if true, servo reverts back to start position. If false, do nothing.
     
        """
        print("moving " + constants.servos[servo_num])
        servo = self.kit.servo[servo_num]
        for i in range(start, stop, 1):
            servo.angle = i
            currentPosition = round(servo.angle)
            print("Servo angle set " +str(i) + "; angle returned: " + str(currentPosition) )
            sleep(delay)

        if(revert):
            sleep(revertDelay)
            for i in range(stop, start, -1):
                servo.angle = i
                sleep(delay)

    def shakeNo(self, revert=True):
        print("shake head no ")
        NECK_PAN_MIN = 30
        NECK_PAN_MAX = 150
    
        NECK_LEFT = 140
        NECK_RIGHT = 40
        
        # don't move if already at center
        increase = True
   
        self.moveByDir(constants.NECK_PAN, constants.NECK_CENTER, NECK_LEFT, 0.05, increase)
        #self.move(constants.NECK_PAN, self.NECK_CENTER, NECK_LEFT, 0.05, True, .02 )
        increase = False
        self.moveByDir( constants.NECK_PAN, constants.NECK_CENTER, NECK_RIGHT, 0.05, increase)
        
        sleep(1)            
        self.returnToStart(constants.NECK_PAN, constants.NECK_CENTER,delay=0.04)


    def shakeHead(self, revert=True):
        NECK_PAN_MIN = 70
        NECK_PAN_MAX = 95
        self.returnToStart(constants.NECK_PAN, constants.NECK_CENTER,delay=0.04)
        for _ in range(2):
            self.move(constants.NECK_PAN, NECK_PAN_MIN, NECK_PAN_MAX, 0.03, revert, .1)
          
        self.returnToStart(constants.NECK_PAN, constants.NECK_CENTER,delay=0.04)

    def moveByDir(self, servo_num, start, stop, delay=0.1, increasing=True):
        """
        Moves a servo in a positive or negative direction. Useful for reverting 
        servo back to original position as you can use can call it twice with same args except 
        set increasing=false to return to origin.
        servo_num -- number identifying server
        start -- start angle of servo
        stop -- stop angle of servo
        delay -- delay between each degree of turn in the servo motor. 
        Lower number increases speed.
        increasing -- if true, servo turns from start to stop. 
        If false, turns from stop to start.
        """
        print("moving " + constants.servos[servo_num] +
                "; increasing:" + str(increasing))

        # currentPosition = round(self.kit.servo[servo_num].angle)
        
        #self.returnToStart(servo_num, start,delay=0.1)
        
        if(increasing):
            print("increasing " + constants.servos[servo_num] + "; start " + str(start) + "; stop:" + str(stop))
            for i in range(start, stop, 1):
                self.kit.servo[servo_num].angle = i
                print(i)
                sleep(delay)
        else:
            print("decreasing " + constants.servos[servo_num] + "; start " + str(start) + "; stop:" + str(stop))
            for i in range(start, stop,-1):
                self.kit.servo[servo_num].angle = i
                print(i)
                sleep(delay)
                
        #self.returnToStart(servo_num, start,delay=0.01)

    def graduatedDelay(delay, current, start, stop):
        """
        Increase the delay near the stop and start of a motion to slow it down.
        """
        # change should be gradual and based on a small fraction of the total movement
        if(abs(start - stop) >= 20):
            iteration = abs()
            denominator = 4

            if(current < start + denominator or current > stop -denominator):
                graduateDelay = delay * (2 - iteration/denominator)
                if(graduateDelay >= delay):
                    return graduateDelay
        
        return delay

        
    def returnToStart(self, servo_num, start = 0, delay=0.1):
    
        # if current pos not start, send back to their gently
        # or just start their
        print(f"returning servo num: {servo_num} to start: {start}");
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
                    sleep(delay)
            else:
                for i in range(currentPosition, start, 1):
                    self.kit.servo[servo_num].angle = i
                    sleep(delay)
    
       
    def facePalm(self):
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 90
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 240 # cover mouth at 200, 230 eyes, 260 head
        RT_ELBOW_ROTATE_MIN = 0
        RT_ELBOW_ROTATE_MAX = 140 # cover mouth at 140
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 145 # cover mouth at 170, 150 for eyes, 140 for head
        NECK_TILT_MIN = 30
        NECK_TILT_MAX = 45
        increasing = True
        self.returnToStart(constants.NECK_TILT, NECK_TILT_MIN,delay=0.005)
        start = perf_counter()
        with ThreadPoolExecutor(max_workers=5) as exe:
            future1 = exe.submit(self.moveByDir, constants.RT_SHOULDER_ROTATOR,  RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.004, increasing)
            exe.submit(self.moveByDir, constants.RT_ELBOW_ROTATOR,  RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.005, increasing)
            exe.submit(self.moveByDir, constants.RT_ELBOW_TILT,  RT_ELBOW_TILT_MIN, RT_ELBOW_TILT_MAX, 0.005, increasing)
            # need to set elbow from moving?
            sleep(1.25)
            exe.submit(self.moveByDir, constants.NECK_TILT,  NECK_TILT_MIN, NECK_TILT_MAX, 0.03, increasing)
            exe.submit(self.shakeHead) # returns to start

            # Maps the method 'cube' with a list of values.
            #result = exe.map(ConcurrentMovements.moveByDir,values)
        
        #print(future1.result())

        with ThreadPoolExecutor(max_workers=5) as exe:
            exe.submit(self.returnToStart,constants.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN,delay=0.005)
            exe.submit(self.returnToStart,constants.RT_ELBOW_ROTATOR, RT_ELBOW_ROTATE_MIN,delay=0.005)
            exe.submit(self.returnToStart,constants.NECK_TILT, NECK_TILT_MIN,delay=0.005)
            exe.submit(self.returnToStart,constants.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN,delay=0.005)
        
        finish = perf_counter()
        print(f"It took {finish-start} second(s) to finish.")

def main():
    # motions should not be completely linear but quickly increase at the beginning and quickly decrease at the end
    mv = ConcurrentMovements("ConcurrentMovements");
    mv.facePalm()


if __name__ == '__main__':
   main()