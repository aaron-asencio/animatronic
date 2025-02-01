#!/usr/bin/env python3
from model.bodyparts import BodyParts

servos = {}
# map user friendly names
servos[BodyParts.NECK_TILT] = "NECK_TILT"
servos[BodyParts.NECK_PAN] = "NECK_PAN"
servos[BodyParts.RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
servos[BodyParts.RT_SHOULDER_TILT] = "RT_SHOULDER_TILT"
servos[BodyParts.RT_ELBOW_TILT] = "RT_ELBOW_TILT"
servos[BodyParts.RT_ELBOW_ROTATOR] = "RT_ELBOW_ROTATOR"


# default center positions
NECK_CENTER = 90