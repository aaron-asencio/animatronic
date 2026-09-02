import RPi.GPIO as GPIO
import time

def ranger(self):
    for i in range(10, 20):
        # 16,18,22, 23,24
        LED_NUM = i
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(LED_NUM,GPIO.OUT)
        print "LED {} on".format(i)
        GPIO.output(LED_NUM,GPIO.HIGH)
        time.sleep(3)
        print "LED off"
        GPIO.output(LED_NUM,GPIO.LOW)

# verified channels
# 40,38,36,32,28,26,24,18,16,12,10
#broken channels 22

#invalid channels
#34(GND),30(GND),28(DNC),20(GND),14(GND)
LED_NUM = 24
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(LED_NUM,GPIO.OUT)
print "LED {} on".format(LED_NUM)
GPIO.output(LED_NUM,GPIO.HIGH)
time.sleep(20)
print "LED off"
GPIO.output(LED_NUM,GPIO.LOW)
