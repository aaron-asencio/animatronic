"""Data models for the camera person-detection & tracking feature.

This leaf module defines the shared data shapes used across the vision feature:
``Detection`` (one recognised object), ``Offset`` (signed pixel distance of a
tracked person from frame center), ``DetectionRule`` (one Detection_Routine_Map
entry), and ``TrackingConfig`` (the persisted tuning for the camera/detector/
tracking pipeline).

Like ``config_store``, this module imports only the standard library and never
imports the camera, detector, servo, or web layers, keeping it a dependency-free
leaf that higher layers call into (never the reverse). Debug output uses
``print()`` to stay consistent with the rest of the codebase.

Validation posture: ``TrackingConfig`` clamps every bounded field into its
documented ``[min, max]`` range on construction, so an out-of-range tuning value
(from a config file or a web request) is coerced to a safe value rather than
rejected or allowed through. This is pure data validation and is independent of
the on-hardware servo-travel/collision checks the operator performs — nothing
here commands a servo.
"""

from dataclasses import dataclass
from typing import Tuple


# --- Field bounds (documented [min, max] ranges) ----------------------------
# Each constant pair is the inclusive clamp range for the matching TrackingConfig
# field, sourced from the design's Data Models section and the requirements.
RESOLUTION_W_MIN = 320       # Req 1.3
RESOLUTION_W_MAX = 1920
RESOLUTION_H_MIN = 240
RESOLUTION_H_MAX = 1080
CAPTURE_FPS_MIN = 5          # Req 1.3
CAPTURE_FPS_MAX = 30
STREAM_FPS_MIN = 1           # Req 2.2
STREAM_FPS_MAX = 30
CONF_THRESHOLD_MIN = 0.0     # Req 3.3
CONF_THRESHOLD_MAX = 1.0
MAX_STEP_DEG_MIN = 1.0       # Req 5.6
MAX_STEP_DEG_MAX = 30.0
SCAN_TIMEOUT_MIN = 1         # Req 6.10
SCAN_TIMEOUT_MAX = 120
COOLDOWN_MIN = 1             # Req 7.8
COOLDOWN_MAX = 600

# Deadband fractions are fractions of the frame dimension; clamp to a sane
# [0.0, 0.5] band (0 = no deadband, 0.5 = the whole half-frame).
DEADBAND_FRAC_MIN = 0.0      # Req 4.4
DEADBAND_FRAC_MAX = 0.5

# Vertical aim fraction: WHERE inside the target's bbox the tracker aims
# vertically, as a fraction of the box height from its TOP edge (y1). 0.0 aims
# at the very top of the box, 0.5 at its geometric center, 1.0 at the bottom.
# A full-body person box has its geometric center at the torso/waist, so aiming
# there makes the head point BELOW the face (the "looking down" bias). Aiming
# a bit above center (~0.35, upper-chest/head region) targets the face without
# over-tilting toward the very top of the box (we track people, not birds).
# Clamped to the whole box so the aim point always lies on the target.
AIM_FRAC_H_MIN = 0.0
AIM_FRAC_H_MAX = 1.0

# Tracking-only neck-tilt geometry (degrees). These are SEPARATE from the global
# constants.SAFE_LIMITS / REST_POSITIONS for NECK_TILT (which every head gesture
# uses, where 90=level, range 30-160). On THIS physical build 90 is slightly
# chin-up and ~105 is a level gaze at a standing person's face, so Tracking_Mode
# uses its own center and a tight band constrained to roughly head height — it
# tracks people, not the ceiling. Confining tilt to a narrow band around the
# level-gaze center also prevents the head from pitching far enough to throw the
# person out of the frame (closed-loop overshoot). Gestures are unaffected.
# tilt_center_deg: the level-gaze tilt angle; the loop seeds and recenters here.
# tilt_min_deg / tilt_max_deg: the band tracking clamps every tilt command into.
TILT_CENTER_DEG_DEFAULT = 105.0
TILT_MIN_DEG_DEFAULT = 100.0
TILT_MAX_DEG_DEFAULT = 110.0
# Hard safety envelope for these tracking tilt values: never allow a tracking
# tilt angle outside the servo's own global SAFE_LIMITS for NECK_TILT (30-160).
# The TrunkController.set_angle clamp is still the final authority on hardware;
# this just keeps the config itself sane.
TILT_ENVELOPE_MIN = 30.0
TILT_ENVELOPE_MAX = 160.0

# Proportional settle gain for the neck control loop. The per-update step is
# proportional to the FRACTIONAL offset of the person from frame center:
#   step_deg = settle_gain * (offset_px / half_frame_px) * max_step_deg
# then capped at max_step_deg. This replaces the old bang-bang behavior (always
# step the full max_step toward target), which overshot center and oscillated
# left-right / up-down without ever settling. With a proportional step the head
# moves fast when the person is far off-center and eases to a stop as it nears
# center, so the gaze settles. gain 1.0 reaches max_step at the frame edge;
# lower gain = gentler/slower approach (more damping, less overshoot). Clamped
# to a sane [0.05, 1.0] range. 0.7 keeps a responsive follow while staying below
# 1.0 so it still damps toward center instead of overshooting into a limit cycle.
SETTLE_GAIN_DEFAULT = 0.7
SETTLE_GAIN_MIN = 0.05
SETTLE_GAIN_MAX = 1.0

PERSON_LABEL = "person"
IR_MODES = ("on", "off", "auto")
DEFAULT_IR_MODE = "auto"

# Default IR auto-switch ambient threshold (mean frame luminance, 0-255 scale).
# Chosen as a mid-low default; auto-switch applies hysteresis around it (Req 10.4).
DEFAULT_IR_AMBIENT_THRESHOLD = 40.0


def _clamp(value, low, high):
    """Clamp a numeric value into the inclusive ``[low, high]`` range.

    Args:
        value: The value to constrain.
        low: The inclusive lower bound.
        high: The inclusive upper bound.

    Returns:
        ``low`` if ``value`` is below it, ``high`` if above it, otherwise
        ``value`` unchanged.
    """
    if value < low:
        return low
    if value > high:
        return high
    return value


@dataclass(frozen=True)
class Detection:
    """A single recognised object from the Detector.

    A Detection carries a COCO class label, a confidence score, and a bounding
    box in frame pixel coordinates. It is immutable (frozen) so detections can
    be shared freely between the overlay, Tracking_Controller, and the
    Detection_Routine_Map without any caller mutating another's copy.

    Attributes:
        label: The COCO class label, e.g. ``"person"`` or ``"dog"``.
        score: The confidence score, expected in ``[0.0, 1.0]`` (Req 3.2).
        x1: Bounding-box top-left x in pixels.
        y1: Bounding-box top-left y in pixels.
        x2: Bounding-box bottom-right x in pixels.
        y2: Bounding-box bottom-right y in pixels.
    """

    label: str
    score: float
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def is_person(self) -> bool:
        """Whether this Detection is a person.

        Returns:
            True if and only if the label is exactly ``"person"`` (Req 3.7).
        """
        return self.label == PERSON_LABEL

    @property
    def area(self) -> int:
        """The bounding-box area in square pixels, used for target selection.

        Returns:
            ``(x2 - x1) * (y2 - y1)`` (Req 4.1).
        """
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    @property
    def center(self) -> Tuple[int, int]:
        """The bounding-box center point in pixels.

        Returns:
            The ``(cx, cy)`` midpoint of the bounding box, each coordinate
            computed as the integer midpoint of the two edges.
        """
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)


@dataclass(frozen=True)
class Offset:
    """The signed pixel offset of a Target_Person from Frame_Center.

    Immutable (frozen) so it can be passed through the tracking math without
    accidental mutation.

    Attributes:
        dx: Horizontal offset in pixels; positive when the person's bbox center
            is right of Frame_Center, negative when left (Req 4.3).
        dy: Vertical offset in pixels; positive when below Frame_Center,
            negative when above (Req 4.3).
        has_target: False when no Target_Person is available (Req 4.5); dx and
            dy are both zero in that case.
    """

    dx: int
    dy: int
    has_target: bool


@dataclass
class DetectionRule:
    """One Detection_Routine_Map entry: a condition mapped to a Routine.

    Attributes:
        required_classes: The COCO classes that must all be present for this
            rule to match, e.g. ``("person",)`` or ``("person", "dog")``
            (Req 7.1).
        action: The Routine action name to dispatch; must be present in
            ``action_map`` before it is ever launched (Req 7.6).
        cooldown_s: Seconds this condition is blocked from re-firing its Routine
            after the Routine completes. Clamped to ``[1, 600]`` on construction,
            default 30 (Req 7.8).
    """

    required_classes: Tuple[str, ...]
    action: str
    cooldown_s: int = 30

    def __post_init__(self):
        """Clamp ``cooldown_s`` into its documented ``[1, 600]`` range."""
        self.cooldown_s = int(_clamp(self.cooldown_s, COOLDOWN_MIN, COOLDOWN_MAX))


@dataclass
class TrackingConfig:
    """Tuning for the camera, detector, and tracking pipeline.

    Persisted alongside the other project tuning (reusing the ``config_store``
    pattern). Every bounded field is clamped into its documented ``[min, max]``
    range on construction so an out-of-range value loaded from the config file
    or supplied by a web request is coerced to a safe value rather than used as
    is (Property 2).

    Attributes:
        resolution: Capture resolution ``(width, height)`` in pixels; width
            clamped to ``[320, 1920]`` and height to ``[240, 1080]`` (Req 1.3).
        capture_fps: Camera capture rate, clamped to ``[5, 30]`` (Req 1.3).
        stream_fps: Live_Feed throttle rate, clamped to ``[1, 30]`` (Req 2.2).
        conf_threshold: Detector confidence threshold, clamped to ``[0.0, 1.0]``
            (Req 3.3).
        deadband_frac_w: Horizontal deadband half-width as a fraction of frame
            width, clamped to ``[0.0, 0.5]`` (Req 4.4).
        deadband_frac_h: Vertical deadband half-width as a fraction of frame
            height, clamped to ``[0.0, 0.5]`` (Req 4.4).
        aim_frac_h: Where inside the target bbox the tracker aims vertically, as
            a fraction of box height from the top edge; clamped to ``[0.0, 1.0]``
            (0.0 = top, 0.5 = geometric center, 1.0 = bottom). Default 0.35 aims
            at the upper-chest/head region on a full-body person box so the head
            tilts up to the face without over-reaching toward the top of the box.
        tilt_center_deg: Tracking-only "level gaze" NECK_TILT angle; the loop
            seeds and recenters the neck here instead of the global rest (90).
            Default 105 (level on this build). Clamped to the servo envelope
            ``[30, 160]`` and then into ``[tilt_min_deg, tilt_max_deg]``.
        tilt_min_deg: Lower bound of the tracking tilt band (head highest).
            Default 100. Clamped to the servo envelope ``[30, 160]``.
        tilt_max_deg: Upper bound of the tracking tilt band (head lowest).
            Default 110. Clamped to the servo envelope and kept >= tilt_min_deg.
            Tracking clamps every tilt command to ``[tilt_min_deg, tilt_max_deg]``
            so the head stays at head height and cannot pitch the person out of
            frame. This band is independent of the global SAFE_LIMITS gestures
            use.
        settle_gain: Proportional control gain for the neck step. The per-update
            step is ``settle_gain * (offset / half_frame) * max_step_deg``,
            capped at ``max_step_deg``; clamped to ``[0.05, 1.0]`` (default 0.5).
            Lower = gentler, more damped approach (less overshoot); higher =
            snappier. Replaces fixed full-step motion so the gaze settles instead
            of oscillating.
        max_step_deg: Maximum neck angle change per update, clamped to
            ``[1, 30]`` degrees (Req 5.6).
        scan_timeout_s: Scan_Sweep reacquire timeout, clamped to ``[1, 120]``
            seconds (Req 6.10).
        ir_mode: IR illuminator mode, one of ``on``/``off``/``auto``; any other
            value falls back to ``auto`` (Req 10.3).
        ir_ambient_threshold: Ambient-light threshold for IR auto-switch
            (Req 10.4).
        use_edge_tpu: Whether to attempt the Edge TPU delegate (Req 3.5).
    """

    resolution: Tuple[int, int] = (640, 480)
    capture_fps: int = 15
    stream_fps: int = 10
    conf_threshold: float = 0.5
    deadband_frac_w: float = 0.08
    deadband_frac_h: float = 0.08
    aim_frac_h: float = 0.35
    tilt_center_deg: float = TILT_CENTER_DEG_DEFAULT
    tilt_min_deg: float = TILT_MIN_DEG_DEFAULT
    tilt_max_deg: float = TILT_MAX_DEG_DEFAULT
    settle_gain: float = SETTLE_GAIN_DEFAULT
    max_step_deg: float = 8.0
    scan_timeout_s: int = 10
    ir_mode: str = DEFAULT_IR_MODE
    ir_ambient_threshold: float = DEFAULT_IR_AMBIENT_THRESHOLD
    use_edge_tpu: bool = False

    def __post_init__(self):
        """Clamp every bounded field into its documented ``[min, max]`` range.

        Resolution is clamped per-axis; the fps, confidence, step, and timeout
        fields are each clamped to their documented bounds; ``ir_mode`` falls
        back to ``auto`` for any value outside the allowed set. This runs on
        construction so a ``TrackingConfig`` can never hold an out-of-range
        value regardless of how it was built (Property 2).
        """
        width, height = self.resolution
        clamped_w = int(_clamp(width, RESOLUTION_W_MIN, RESOLUTION_W_MAX))
        clamped_h = int(_clamp(height, RESOLUTION_H_MIN, RESOLUTION_H_MAX))
        self.resolution = (clamped_w, clamped_h)

        self.capture_fps = int(_clamp(self.capture_fps, CAPTURE_FPS_MIN, CAPTURE_FPS_MAX))
        self.stream_fps = int(_clamp(self.stream_fps, STREAM_FPS_MIN, STREAM_FPS_MAX))
        self.conf_threshold = float(
            _clamp(self.conf_threshold, CONF_THRESHOLD_MIN, CONF_THRESHOLD_MAX)
        )
        self.deadband_frac_w = float(
            _clamp(self.deadband_frac_w, DEADBAND_FRAC_MIN, DEADBAND_FRAC_MAX)
        )
        self.deadband_frac_h = float(
            _clamp(self.deadband_frac_h, DEADBAND_FRAC_MIN, DEADBAND_FRAC_MAX)
        )
        self.aim_frac_h = float(
            _clamp(self.aim_frac_h, AIM_FRAC_H_MIN, AIM_FRAC_H_MAX)
        )

        # Tracking tilt band: clamp each value into the servo envelope [30, 160],
        # ensure max >= min, then clamp the center into the resulting band so the
        # loop seeds/recenters inside the band it will also clamp commands to.
        tmin = _clamp(self.tilt_min_deg, TILT_ENVELOPE_MIN, TILT_ENVELOPE_MAX)
        tmax = _clamp(self.tilt_max_deg, TILT_ENVELOPE_MIN, TILT_ENVELOPE_MAX)
        if tmax < tmin:
            # Swap rather than silently collapse so a reversed pair still yields
            # a usable (if narrow) band instead of an empty one.
            tmin, tmax = tmax, tmin
        self.tilt_min_deg = float(tmin)
        self.tilt_max_deg = float(tmax)
        self.tilt_center_deg = float(
            _clamp(self.tilt_center_deg, self.tilt_min_deg, self.tilt_max_deg)
        )

        self.settle_gain = float(
            _clamp(self.settle_gain, SETTLE_GAIN_MIN, SETTLE_GAIN_MAX)
        )
        self.max_step_deg = float(
            _clamp(self.max_step_deg, MAX_STEP_DEG_MIN, MAX_STEP_DEG_MAX)
        )
        self.scan_timeout_s = int(
            _clamp(self.scan_timeout_s, SCAN_TIMEOUT_MIN, SCAN_TIMEOUT_MAX)
        )

        if self.ir_mode not in IR_MODES:
            print(
                f"Unknown ir_mode '{self.ir_mode}'; falling back to "
                f"'{DEFAULT_IR_MODE}'"
            )
            self.ir_mode = DEFAULT_IR_MODE

        self.ir_ambient_threshold = float(self.ir_ambient_threshold)
        self.use_edge_tpu = bool(self.use_edge_tpu)
