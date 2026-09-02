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


# --------------------------------------------------------------------------- #
# SAFETY LIMITS  — hard per-channel angle bounds enforced on EVERY servo write #
# --------------------------------------------------------------------------- #
#
# These are the mechanism's SAFE travel range, not the servo's electrical range
# (which is 0-270). A servo driven past the mechanical limit stalls against a
# physical stop, draws locked-rotor current, overheats, and can burn out — a
# fire hazard. TrunkController.move() and friends clamp every commanded angle
# to these bounds so a bad gesture value can never drive into a jam.
#
# !!! CALIBRATION REQUIRED !!!
# The values below are CONSERVATIVE PLACEHOLDERS set to the angles the existing
# gestures already use. They are NOT yet verified against the physical build.
# We will tighten them per servo (neck first, then arm, then arm-with-neck).
# Until calibrated, treat any stall as a sign these need narrowing.
#
# Format: channel -> (min_deg, max_deg)
SAFE_LIMITS = {
    NECK_PAN:            (30, 150),   # left-right head rotation
    NECK_TILT:           (15, 90),    # up-down; JAMMED at low angles — calibrate!
    RT_SHOULDER_ROTATOR: (60, 270),   # raise/lower whole arm
    RT_SHOULDER_TILT:    (0, 230),    # shoulder forward/back
    RT_ELBOW_TILT:       (0, 120),    # elbow bend
    RT_ELBOW_ROTATOR:    (30, 150),   # forearm rotate
}

# Resting/neutral angle per channel. return_to_rest() drives every servo here
# — used between routines and, critically, after any error so servos are never
# left energized against a jam. Each rest angle MUST lie within SAFE_LIMITS.
REST_POSITIONS = {
    NECK_PAN:            90,   # centered
    NECK_TILT:           45,   # level-ish head; mid of safe range, away from jam
    RT_SHOULDER_ROTATOR: 60,   # arm lowered
    RT_SHOULDER_TILT:    0,    # shoulder back/neutral
    RT_ELBOW_TILT:       0,    # elbow straight
    RT_ELBOW_ROTATOR:    90,   # forearm neutral
}
