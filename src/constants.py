"""
constants.py

Servo channel assignments and default positions for the animatronic.

Hardware: Adafruit 16-channel PCA9685 PWM servo driver board.
All servos are configured for a 270-degree actuation range.

Channel layout
--------------
Channels 0-1 : Head / neck
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

# --- GPIO pins (not PCA9685 servo channels) ---
EYE_LIGHT_PIN = 6      # gpiozero LED — eye lights
MOUTH_MOTOR_PIN = 15   # gpiozero DigitalOutputDevice — jaw motor (pin 18 no longer working)

# HC-SR04 ultrasonic range sensor (gpiozero DistanceSensor).
# TRIG is an output (fires the ping); ECHO is an input (times the return pulse).
# NOTE: ECHO idles at 5V but the Pi GPIO is 3.3V — use a voltage divider on the
# ECHO line (or a level shifter) to avoid damaging the input.
RANGE_TRIG_PIN = 23    # HC-SR04 trigger (output)
RANGE_ECHO_PIN = 24    # HC-SR04 echo (input, via voltage divider)


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
#     rest = 0 (arm at the side of the body)
#     increase -> moves the arm UP (0=at side, 270=~170deg up/nearly straight up)
#     decrease -> moves the arm DOWN toward the side
#     NOTE: electrical range 0-270 maps to a ~170deg physical arc (gearing).
#     COLLISION: interacts with RT_SHOULDER_TILT — the tilt+rotator
#     combination is the primary body-collision pair for the 3D model, though
#     the limited physical arc mitigates most of the risk.
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
    RT_SHOULDER_ROTATOR: (0, 270),    # raise/lower whole arm: 0=arm at side, 270=arm ~170deg up (nearly straight up); increase=arm up. Electrical 0-270 maps to a ~170deg physical arc (gearing), which limits over-rotation and mitigates most shoulder tilt+rotator collision risk.
    RT_SHOULDER_TILT:    (45, 270),   # shoulder raise/lower: increase=raise arm from side (abduction), decrease=toward body (adduction); 135=arm straight out. Below ~45 risks body collision (depends on RT_SHOULDER_ROTATOR) — min 55 stays clear.
    RT_ELBOW_TILT:       (0, 160),     # elbow bend — TEMPORARILY widened to 0-90 for calibration/collision testing (was locked at 5=straight). Landmarks: 5=straight, 145=right angle, 210=full flexion. NOTE: elbow flexion is only collision-safe in certain shoulder positions — keep the arm clear of the body while testing this range.
    RT_ELBOW_ROTATOR:    (0, 270),    # forearm rotate (twist): 150=hand parallel to side, 270=palm up, 0=palm down. Full range — low collision risk.
}

# Resting/neutral angle per channel. return_to_rest() drives every servo here
# — used between routines and, critically, after any error so servos are never
# left energized against a jam. Each rest angle MUST lie within SAFE_LIMITS.
REST_POSITIONS = {
    NECK_PAN:            90,   # centered
    NECK_TILT:           90,   # head level (new neutral after reseat)
    RT_SHOULDER_ROTATOR: 0,    # arm at side (within (0,270))
    RT_SHOULDER_TILT:    55,   # arm lowered toward side (within (55,245))
    RT_ELBOW_TILT:       5,    # elbow straight (within locked (5,5) range)
    RT_ELBOW_ROTATOR:    150,  # forearm neutral — hand parallel to side
}


# --------------------------------------------------------------------------- #
# FORBIDDEN JOINT COMBINATIONS — multi-axis collisions the 3D model can't see  #
# --------------------------------------------------------------------------- #
#
# The kinematic collision model is a DECOUPLED per-joint approximation, so it
# loses accuracy at the far extremes of the coupled shoulder (tilt + rotator).
# The one empirically-confirmed collision it under-predicts there is the
# HAND-TO-FACE fold: at full elbow flexion with the shoulder rotated up, the
# curled hand reaches the head. These rules are a conservative hard guard for
# exactly those measured danger zones — the CollisionModel flags any pose that
# matches a rule as unsafe, in addition to its geometric detection.
#
# Each rule: a human-readable reason + a dict of {channel: (min_deg, max_deg)}.
# A pose MATCHES (is unsafe) when EVERY listed channel is within its inclusive
# range. Tune the bounds as you discover more of the contact envelope.
FORBIDDEN_COMBINATIONS = [
    {
        "reason": "hand-to-face: full elbow flexion + shoulder rotated up brings the curled hand into the head",
        "ranges": {
            RT_ELBOW_TILT:       (150, 270),  # near/at full flexion
            RT_SHOULDER_ROTATOR: (210, 270),  # arm rotated up toward the head
        },
    },
]


# --------------------------------------------------------------------------- #
# ARM DESTINATION POSES — peak/hold arm pose each gesture reaches before        #
# retracting to REST_POSITIONS. Reference for authoring new arm gestures.       #
# --------------------------------------------------------------------------- #
#
# Values are calibrated 0-270 servo degrees, hardware-measured from the targets
# in src/movements.py. Keys are the camelCase action names (as in action_map).
# Only the four right-arm channels (4-7) are listed; head channels and any
# per-gesture head motion are NOT captured here.
#
# WARNING: several of these dip below the global SAFE_LIMITS shoulder-tilt floor
# (45) or above the elbow ceiling, and are only collision-safe in THIS specific
# pose. The gesture that owns the pose widens the clamp with
# TrunkController.verified_pose_override(...) — see the "override" column below.
# If you reuse a pose in a new gesture, carry the SAME override and re-verify on
# the physical robot before trusting it.
#
#   Action          override (channel -> widened range)
#   ------------     ---------------------------------------------
#   facePalm         RT_SHOULDER_TILT: (40, 270)      (also NECK_TILT=140)
#   menacingReach    RT_SHOULDER_TILT: (25, 270)
#   beckon           none
#   comeHere         RT_SHOULDER_TILT: (14, 270)
#   yawnCover        RT_SHOULDER_TILT: (35, 270), RT_ELBOW_TILT: (0, 170)
#                                                    (also NECK_PAN=90, NECK_TILT=90)
#
# Format: action_name -> {channel: destination_angle_deg}
ARM_DESTINATION_POSES = {
    "facePalm": {
        RT_SHOULDER_ROTATOR: 200,
        RT_SHOULDER_TILT:    40,
        RT_ELBOW_TILT:       145,
        RT_ELBOW_ROTATOR:    200,
    },
    "menacingReach": {
        RT_SHOULDER_ROTATOR: 209,
        RT_SHOULDER_TILT:    43,   # center/reach value; swing oscillates tilt in [26, 60]
        RT_ELBOW_TILT:       0,
        RT_ELBOW_ROTATOR:    0,
    },
    "beckon": {
        RT_SHOULDER_ROTATOR: 90,
        RT_SHOULDER_TILT:    55,
        RT_ELBOW_TILT:       105,  # curl arc swings 105<->135
        RT_ELBOW_ROTATOR:    270,
    },
    "comeHere": {
        RT_SHOULDER_ROTATOR: 129,
        RT_SHOULDER_TILT:    14,
        RT_ELBOW_TILT:       140,
        RT_ELBOW_ROTATOR:    189,
    },
    "yawnCover": {
        RT_SHOULDER_ROTATOR: 200,
        RT_SHOULDER_TILT:    35,
        RT_ELBOW_TILT:       165,
        RT_ELBOW_ROTATOR:    185,
    },
}
