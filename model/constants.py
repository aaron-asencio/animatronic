#!/usr/bin/env python3
from model.bodyparts import BodyParts

servos = {}
# map user friendly names
servos[BodyParts.NECK_TILT.value] = "NECK_TILT"
servos[BodyParts.NECK_PAN.value] = "NECK_PAN"
servos[BodyParts.RT_SHOULDER_ROTATOR.value] = "RT_SHOULDER_ROTATOR"
servos[BodyParts.RT_SHOULDER_TILT.value] = "RT_SHOULDER_TILT"
servos[BodyParts.RT_ELBOW_TILT.value] = "RT_ELBOW_TILT"
servos[BodyParts.RT_ELBOW_ROTATOR.value] = "RT_ELBOW_ROTATOR"


# default center positions
NECK_CENTER = 90

EYE_LIGHT_PIN = 6
MOUTH_MOTOR_PIN = 18