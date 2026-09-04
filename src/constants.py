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
# AXIS DIRECTION REFERENCE — how commanded angle maps to physical motion       #
# --------------------------------------------------------------------------- #
# Use this when composing gestures so movement direction is unambiguous.
# Angles are degrees; "increase"/"decrease" mean a larger/smaller commanded
# angle. Directions are from the ANIMATRONIC's own point of view (its left/
# right), consistent with the RT_ (right-side) channel naming.
#
# NECK_PAN  (channel 0) — left/right head rotation
#     center  = 90  (head faces forward, neither left nor right)
#     increase -> head turns to its LEFT
#     decrease -> head turns to its RIGHT
#
# NECK_TILT (channel 1) — up/down head tilt
#     center  = 90  (head level)
#     increase -> head lowers (chin toward chest)
#     decrease -> head raises (chin up)
#
# RT_SHOULDER_ROTATOR / RT_SHOULDER_TILT / RT_ELBOW_TILT / RT_ELBOW_ROTATOR:
#     arm-axis directions NOT YET CALIBRATED — document here once verified.
# --------------------------------------------------------------------------- #

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
# CALIBRATION STATUS:
# NECK_PAN and NECK_TILT are CALIBRATED to the physical build (verified by
# nudging to the mechanical stops). The four RT_ arm channels are still
# CONSERVATIVE PLACEHOLDERS set to the angles existing gestures use — NOT yet
# verified against the build; treat any stall there as a sign to narrow them.
#
# Format: channel -> (min_deg, max_deg)
SAFE_LIMITS = {
    NECK_PAN:            (5, 175),    # left-right head rotation: 90=center, ~85 deg each way (natural neck range)
    NECK_TILT:           (30, 160),   # up-down: 90=level, higher=chin down (160=chin-to-chest stop), lower=head up
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
    NECK_TILT:           90,   # head level (new neutral after reseat)
    RT_SHOULDER_ROTATOR: 60,   # arm lowered
    RT_SHOULDER_TILT:    0,    # shoulder back/neutral
    RT_ELBOW_TILT:       0,    # elbow straight
    RT_ELBOW_ROTATOR:    90,   # forearm neutral
}
