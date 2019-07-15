from adafruit_servokit import ServoKit
import time

kit = ServoKit(channels=16)
for i in range(0, 3):
    kit.servo[i].actuation_range = 270

NECK_TILT = 4
NECK_PAN = 5
RT_ELBOW_PAN = 2
RT_ELBOW_TILT = 3
RT_SHOULDER_ROTATOR = 6
# set up servos with 270 range
print("servo setup")

# Neck servos
print("move neck pan")
NECK_PAN_MIN = 0
NECK_PAN_MAX = 90
kit.servo[NECK_PAN].angle = NECK_PAN_MIN
time.sleep(1)
kit.servo[NECK_PAN].angle = NECK_PAN_MAX
time.sleep(1)
kit.servo[NECK_PAN].angle = NECK_PAN_MIN
time.sleep(1)

print("move neck tilt")
NECK_TILT_MIN = 0
NECK_TILT_MAX = 45
kit.servo[NECK_TILT].angle = NECK_TILT_MIN
time.sleep(1)
kit.servo[NECK_TILT].angle = NECK_TILT_MAX
time.sleep(1)
kit.servo[NECK_TILT].angle = NECK_TILT_MIN
time.sleep(1)

# elbow servos
print("move elbow pan")
RT_ELBOW_PAN_MIN = 0
RT_ELBOW_PAN_MAX = 180
kit.servo[RT_ELBOW_PAN].angle = RT_ELBOW_PAN_MAX
time.sleep(1)
kit.servo[RT_ELBOW_PAN].angle = RT_ELBOW_PAN_MAX
time.sleep(1)
kit.servo[RT_ELBOW_PAN].angle = RT_ELBOW_PAN_MAX
time.sleep(1)

#print("move elbow tilt")
RT_ELBOW_TILT_MIN = 0
RT_ELBOW_TILT_MAX = 180
kit.servo[RT_ELBOW_TILT].angle = RT_ELBOW_TILT_MIN
time.sleep(1)
kit.servo[RT_ELBOW_TILT].angle = RT_ELBOW_TILT_MAX
time.sleep(1)
kit.servo[RT_ELBOW_TILT].angle = RT_ELBOW_TILT_MIN
time.sleep(1)

# shoulder servo
print("move shoulder rotate")
RT_SHOULDER_ROTATOR_MIN = 0
RT_SHOULDER_ROTATOR_MAX = 180

kit.servo[RT_SHOULDER_ROTATOR].angle = RT_SHOULDER_ROTATOR_MIN
time.sleep(1)
kit.servo[RT_SHOULDER_ROTATOR].angle = RT_SHOULDER_ROTATOR_MAX
time.sleep(1)
kit.servo[RT_SHOULDER_ROTATOR].angle = RT_SHOULDER_ROTATOR_MIN
time.sleep(1)


#if __name__ == "__main__":
#    import sys
#   print(sys.argv[1])

