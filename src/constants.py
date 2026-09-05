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
# RT_ELBOW_TILT (channel 5) — elbow bend
#     5 = straight (arm extended). LOCKED at 5 for now (see SAFE_LIMITS).
#     increase -> flexion (elbow bends; 145=right angle, 210=full flexion)
#     decrease -> extension (elbow straightens toward 5)
#
# RT_ELBOW_ROTATOR (channel 4) — forearm twist (wrist/palm orientation)
#     center = 150 (hand parallel to the side)
#     increase -> rotates toward palm UP (270 = palm up)
#     decrease -> rotates toward palm DOWN (0 = palm down)
#
# RT_SHOULDER_TILT (channel 6) — raise/lower the whole arm at the shoulder
#     rest = 55 (arm down toward side)
#     increase -> raises the arm up/away from the side (abduction; 135=arm straight out horizontally)
#     decrease -> lowers the arm toward the body (adduction)
#     COLLISION: below ~45 the arm can hit the body depending on
#     RT_SHOULDER_ROTATOR; the (55,245) min stays clear, but the 3D model
#     must enforce this combination.
#
# RT_SHOULDER_ROTATOR (channel 7) — raise/lower the whole arm
#     rest = 15 (arm low)
#     increase -> moves the arm UP (160 = arm straight out)
#     decrease -> moves the arm DOWN
#     COLLISION: interacts with RT_SHOULDER_TILT — the tilt+rotator
#     combination is the primary body-collision pair for the 3D model.
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
# All six channels are CALIBRATED to the physical build (nudged to
# mechanical stops): NECK_PAN, NECK_TILT, RT_SHOULDER_TILT,
# RT_SHOULDER_ROTATOR, RT_ELBOW_ROTATOR, and RT_ELBOW_TILT (locked straight
# pending the 3D collision model). NOTE: per-axis limits do NOT prevent
# multi-axis collisions (e.g. shoulder tilt+rotator, or elbow flexion with
# shoulder position); those combinatorial constraints are deferred to the
# planned 3D collision model.
#
# Format: channel -> (min_deg, max_deg)
SAFE_LIMITS = {
    NECK_PAN:            (5, 175),    # left-right head rotation: 90=center, ~85 deg each way (natural neck range)
    NECK_TILT:           (30, 160),   # up-down: 90=level, higher=chin down (160=chin-to-chest stop), lower=head up
    RT_SHOULDER_ROTATOR: (0, 270),    # raise/lower whole arm: increase=arm up, decrease=arm down; 160=arm straight out. Interacts with RT_SHOULDER_TILT for body collision (see tilt note).
    RT_SHOULDER_TILT:    (55, 245),   # shoulder raise/lower: increase=raise arm from side (abduction), decrease=toward body (adduction); 135=arm straight out. Below ~45 risks body collision (depends on RT_SHOULDER_ROTATOR) — min 55 stays clear.
    RT_ELBOW_TILT:       (5, 5),      # elbow bend — LOCKED at 5 (straight). Landmarks: 5=straight, 145=right angle, 210=full flexion. Range clamped to straight until the 3D collision model exists (flexion is only collision-safe near straight, given shoulder positions).
    RT_ELBOW_ROTATOR:    (0, 270),    # forearm rotate (twist): 150=hand parallel to side, 270=palm up, 0=palm down. Full range — low collision risk.
}

# Resting/neutral angle per channel. return_to_rest() drives every servo here
# — used between routines and, critically, after any error so servos are never
# left energized against a jam. Each rest angle MUST lie within SAFE_LIMITS.
REST_POSITIONS = {
    NECK_PAN:            90,   # centered
    NECK_TILT:           90,   # head level (new neutral after reseat)
    RT_SHOULDER_ROTATOR: 15,   # arm low (within (0,270))
    RT_SHOULDER_TILT:    55,   # arm lowered toward side (within (55,245))
    RT_ELBOW_TILT:       5,    # elbow straight (within locked (5,5) range)
    RT_ELBOW_ROTATOR:    150,  # forearm neutral — hand parallel to side
}
