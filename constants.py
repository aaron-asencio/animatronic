"""
constants.py

Servo channel assignments and default positions for the animatronic.

Hardware: Adafruit 16-channel PCA9685 PWM servo driver board.
All servos are configured for a 270-degree actuation range.

Channel layout
--------------
Channels 0-3 : Head / neck
Channels 4-7 : Right arm
"""

# --- Right arm (channels 4-7) ---
RT_SHOULDER_ROTATOR = 7  # Rotates the shoulder joint (raises/lowers the whole arm)
RT_SHOULDER_TILT    = 6  # Tilts the shoulder forward/back
RT_ELBOW_TILT       = 5  # Bends the elbow up/down
RT_ELBOW_ROTATOR    = 4  # Rotates the forearm (wrist/palm orientation)

# --- Head / neck (channels 0-1) ---
NECK_PAN  = 0  # Left-right head rotation
NECK_TILT = 1  # Up-down head tilt

# Human-readable name map keyed by channel number.
# Used for debug logging throughout the codebase.
servos = {}
servos[NECK_TILT]           = "NECK_TILT"
servos[NECK_PAN]            = "NECK_PAN"
servos[RT_SHOULDER_ROTATOR] = "RT_SHOULDER_ROTATOR"
servos[RT_SHOULDER_TILT]    = "RT_SHOULDER_TILT"
servos[RT_ELBOW_TILT]       = "RT_ELBOW_TILT"
servos[RT_ELBOW_ROTATOR]    = "RT_ELBOW_ROTATOR"

# Default center position (degrees) for the neck pan servo.
# Used as the resting/neutral angle between movements.
NECK_CENTER = 90
