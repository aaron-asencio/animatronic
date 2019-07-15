import RPi.GPIO as GPIO
import time

#setup GPIO using Board numbering
GPIO.setmode(GPIO.BOARD)

GPIO_RT_TRIGGER = 3
GPIO_RT_ECHO = 5

GPIO_LT_TRIGGER = 8
GPIO_LT_ECHO = 40

GPIO_PIR = 7
# right ultrasonic sensor
GPIO.setup(GPIO_RT_TRIGGER, GPIO.OUT)
GPIO.setup(GPIO_RT_ECHO, GPIO.IN)
#left ultrasonic sensor
GPIO.setup(GPIO_LT_TRIGGER, GPIO.OUT)
GPIO.setup(GPIO_LT_ECHO, GPIO.IN)

# PIR sensor
GPIO.setup(GPIO_PIR, GPIO.IN)
# 3,5
# 8,10

def distance(trigger, echo):
    
    # set Trigger to HIGH
    GPIO.output(trigger, True)
 
    # set Trigger after 0.01ms to LOW
    time.sleep(0.00001)
    GPIO.output(trigger, False)
 
    StartTime = time.time()
    StopTime = time.time()
 
    # save StartTime
    while GPIO.input(echo) == 0:
        StartTime = time.time()
 
    # save time of arrival
    while GPIO.input(echo) == 1:
        StopTime = time.time()
 
    # time difference between start and arrival
    TimeElapsed = StopTime - StartTime
    # multiply with the sonic speed (34300 cm/s)
    # and divide by 2, because there and back
    distance = (TimeElapsed * 34300) / 2
 
    return distance

 
if __name__ == '__main__':
    try:
        while True:
            dist = distance(GPIO_RT_TRIGGER, GPIO_RT_ECHO)
            print ("Right Distance = %.1f cm" % dist)
            # speed of sound 343 m/s
            # functional distance of .02 m - 4 m
            # it takes 23.3 s to go 4 m out and echo 4 m back.
            # delay between each one for 33 ms to avoid cross talk
            time.sleep(0.033)
            dist = distance(GPIO_LT_TRIGGER, GPIO_LT_ECHO)
            print ("Left Distance = %.1f cm" % dist)
            time.sleep(1)
 
        # Reset by pressing CTRL + C
    except KeyboardInterrupt:
        print("Measurement stopped by User")
        GPIO.cleanup()
