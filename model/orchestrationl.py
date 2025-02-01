from bodypart import BodyPart
# hold map of positions of servos thqt will run at the same time

#  array of postion objects. servos ojects have servo number, name, description, default pos , start position and end position, deay between each position,
#  could have action instead - move, wait, stop, etc

# create new bodypart object for each servo 

# could do oerchestration type like parallel or sequence

# orchestration needs to know the body annd where it is moving to and from
shoulderRotator = BodyPart("RT_SHOULDER_ROTATOR", 7, 90)
shoulderRotator.move(90, 180, 0.02)

shoulderTilt = BodyPart("RT_SHOULDER_TILT", 6, 90)
shoulderTilt.move(90, 180, 0.02)

# body_part_postion class that has bodypart and the position it is moving to and from