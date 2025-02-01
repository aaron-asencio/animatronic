#!/usr/bin/env python3
from concurrent.futures import ThreadPoolExecutor


from time import sleep, perf_counter
from model.bodyparts import BodyParts
from model.bodypart import BodyPart
import model.constants

class face_palm:
    def do(self):
        RT_SHOULDER_TILT_MIN = 0
        RT_SHOULDER_TILT_MAX = 90
        RT_SHOULDER_ROTATOR_MIN = 0
        RT_SHOULDER_ROTATOR_MAX = 240 # cover mouth at 200, 230 eyes, 260 head
    
        rtShoulderRotator = BodyPart(BodyParts.RT_SHOULDER_ROTATOR.name, BodyParts.RT_SHOULDER_ROTATOR.value, 0)
       
        RT_ELBOW_ROTATE_MIN = 0
        RT_ELBOW_ROTATE_MAX = 140 # cover mouth at 140
        rtElbowRotator = BodyPart(BodyParts.RT_ELBOW_ROTATOR.name, BodyParts.RT_ELBOW_ROTATOR.value,0)
        
        RT_ELBOW_TILT_MIN = 0
        RT_ELBOW_TILT_MAX = 145 # cover mouth at 170, 150 for eyes, 140 for head
        NECK_TILT_MIN = 20
        NECK_TILT_MAX = 45
        increasing = True
       
        start = perf_counter()
        with ThreadPoolExecutor(max_workers=5) as exe:
            exe.submit(rtElbowRotator.move, RT_SHOULDER_ROTATOR_MIN, RT_SHOULDER_ROTATOR_MAX, 0.002, increasing)
            exe.submit(self.moveByDir, BodyParts.RT_ELBOW_ROTATOR,  RT_ELBOW_ROTATE_MIN, RT_ELBOW_ROTATE_MAX, 0.005, increasing)

        #sleep(.25)
        # with ThreadPoolExecutor(max_workers=5) as exe:
        #     exe.submit(self.returnToStart,BodyParts.RT_ELBOW_TILT, RT_ELBOW_TILT_MIN,delay=0.005)
        #     exe.submit(self.returnToStart,BodyParts.RT_ELBOW_ROTATOR, RT_ELBOW_ROTATE_MIN,delay=0.003)
        #     exe.submit(self.returnToStart,BodyParts.NECK_TILT, NECK_TILT_MIN,delay=0.01)
        #     exe.submit(self.returnToStart,BodyParts.RT_SHOULDER_ROTATOR, RT_SHOULDER_ROTATOR_MIN,delay=0.005)
        
        finish = perf_counter()
        print(f"It took {finish-start} second(s) to finish.")

def main():
    # motions should not be completely linear but quickly increase at the beginning and quickly decrease at the end
    facePalm = face_palm();
    facePalm.do()


if __name__ == '__main__':
   main()