"""Tracking_Controller — pure selection and Offset math (Req 4).

This module turns a frame's person Detections into the signed pixel Offset of
the chosen Target_Person from Frame_Center, and then maps that Offset into the
next pan/tilt neck target angles. It is pure logic: no servo writes, no
hardware, no network. The Tracking_Mode loop that applies
``TrunkController.set_angle`` (and its SAFE_LIMITS clamp) lives in a later
layer; only that touches hardware.

Three functions are exposed:

- ``select_target(detections, frame_w, frame_h)`` — pick exactly one
  Target_Person: the person Detection with the largest bounding-box area,
  tie-broken by closeness of the bbox center to Frame_Center (Req 4.1, 4.2).
  Deterministic for identical input.
- ``compute_offset(target, frame_w, frame_h, cfg)`` — the signed pixel Offset of
  the Target_Person bbox center vs Frame_Center (+x right, +y below), zeroed on
  both axes inside the Deadband (Req 4.3, 4.4); ``Offset(0, 0, False)`` when
  there is no target (Req 4.5).
- ``next_neck_targets(offset, cur_pan, cur_tilt, cfg)`` — map the Offset sign to
  the next ``NECK_PAN``/``NECK_TILT`` target angles, honoring the ``constants``
  axis directions and capping each per-update step at ``cfg.max_step_deg``
  (Req 5.1-5.4, 5.6). Returns ``{}`` when there is no target so the caller holds
  the current angle (Req 5.9); the returned channel set is a subset of
  ``{NECK_PAN, NECK_TILT}`` (Req 5.8).

Like the other leaf vision modules, this imports only the standard library and
``vision_models``; higher layers call into it, never the reverse. Debug output
uses ``print()`` to stay consistent with the rest of the codebase.
"""

from typing import Dict, List, Optional

import constants
from vision_models import Detection, Offset, TrackingConfig


def _frame_center(frame_w: int, frame_h: int):
    """The pixel coordinate at the center of the frame.

    Args:
        frame_w: Frame width in pixels.
        frame_h: Frame height in pixels.

    Returns:
        The ``(cx, cy)`` Frame_Center, each coordinate the integer midpoint of
        the matching frame dimension.
    """
    return (frame_w // 2, frame_h // 2)


def _center_distance_sq(detection: Detection, frame_w: int, frame_h: int) -> int:
    """Squared pixel distance from a Detection's bbox center to Frame_Center.

    Squared distance is used because only ordering matters for the tie-break
    (Req 4.2); squaring avoids a float ``sqrt`` and keeps the comparison exact
    and deterministic.

    Args:
        detection: The Detection whose bbox center is measured.
        frame_w: Frame width in pixels.
        frame_h: Frame height in pixels.

    Returns:
        ``(cx - fcx)**2 + (cy - fcy)**2`` where ``(cx, cy)`` is the bbox center
        and ``(fcx, fcy)`` is Frame_Center.
    """
    cx, cy = detection.center
    fcx, fcy = _frame_center(frame_w, frame_h)
    return (cx - fcx) ** 2 + (cy - fcy) ** 2


def select_target(
    detections: List[Detection], frame_w: int, frame_h: int
) -> Optional[Detection]:
    """Select the single Target_Person from a frame's Detections.

    Only person Detections are considered (``Detection.is_person``); any
    non-person Detection is ignored (Req 4.1). The chosen Target_Person is the
    person Detection with the largest bounding-box area. When two or more share
    the largest area, the tie is broken toward the one whose bbox center is
    closest to Frame_Center (Req 4.2). Selection is deterministic for identical
    input: the key sorts by area (descending) then by center distance
    (ascending), so the same input always yields the same Target_Person.

    Args:
        detections: All Detections for the current frame (any classes).
        frame_w: Frame width in pixels, used for the center tie-break.
        frame_h: Frame height in pixels, used for the center tie-break.

    Returns:
        The selected Target_Person Detection, or ``None`` when no person
        Detection is present.
    """
    persons = [d for d in detections if d.is_person]
    if not persons:
        return None

    # Largest area wins; ties resolved by smallest center distance. Negating
    # area makes the single min() both maximize area and minimize distance,
    # which is total and therefore deterministic for identical input.
    return min(
        persons,
        key=lambda d: (-d.area, _center_distance_sq(d, frame_w, frame_h)),
    )


def compute_offset(
    target: Optional[Detection],
    frame_w: int,
    frame_h: int,
    cfg: TrackingConfig,
) -> Offset:
    """Compute the signed pixel Offset of the Target_Person from Frame_Center.

    The horizontal Offset is positive when the bbox center is right of
    Frame_Center and negative when left; the vertical Offset is positive when
    the aim point is below Frame_Center and negative when above (Req 4.3).

    Aim point: the horizontal aim is the bbox center x, but the VERTICAL aim is
    ``y1 + cfg.aim_frac_h * (y2 - y1)`` — a fraction of the box height down from
    its top edge — NOT the geometric center. A full-body person box has its
    geometric center at the torso, so centering on it points the head below the
    face (the downward bias). ``aim_frac_h`` defaults to 0.35 (upper chest/head
    region), so the Offset drives the head up to the face without over-tilting
    toward the top of the box. ``aim_frac_h = 0.5`` reproduces the old
    center-of-box behavior.

    A Deadband centered on Frame_Center suppresses jitter: its half-widths are
    ``cfg.deadband_frac_w * frame_w`` horizontally and
    ``cfg.deadband_frac_h * frame_h`` vertically. The deadband is applied
    PER-AXIS and INDEPENDENTLY: whichever axis is within its half-width is zeroed
    on its own, without waiting for the other axis to also be centered (Req 4.4).

    (This is a deliberate change from the original paired-deadband spec, which
    only zeroed when BOTH axes were inside the band. Paired zeroing made a
    centered axis keep nudging while the other axis corrected, producing a
    left-right/up-down limit-cycle oscillation on the robot — the head never
    settled. Independent zeroing lets an already-centered axis stop and hold
    while the other finishes.)

    With no Target_Person, the Offset reports ``has_target=False`` and both axes
    zero (Req 4.5).

    Args:
        target: The selected Target_Person, or ``None`` when none is available.
        frame_w: Frame width in pixels.
        frame_h: Frame height in pixels.
        cfg: Tracking tuning supplying the Deadband fractions (already clamped
            to ``[0.0, 0.5]`` by ``TrackingConfig``) and ``aim_frac_h`` (clamped
            to ``[0.0, 1.0]``), the vertical aim fraction within the bbox.

    Returns:
        An ``Offset``: ``Offset(0, 0, has_target=False)`` when ``target`` is
        ``None``; otherwise the signed ``(dx, dy)`` with ``has_target=True``
        (``dy`` measured from the ``aim_frac_h`` aim point, not the box center),
        each axis independently zeroed when it is inside its deadband half-width.
    """
    if target is None:
        return Offset(dx=0, dy=0, has_target=False)

    cx, _cy = target.center
    fcx, fcy = _frame_center(frame_w, frame_h)

    # Horizontal aim = bbox center x. Vertical aim = a fraction of the box
    # height down from the TOP edge (y1), so for aim_frac_h<0.5 the head targets
    # the upper box (head/face) instead of the torso-level geometric center.
    aim_y = target.y1 + cfg.aim_frac_h * (target.y2 - target.y1)

    dx = cx - fcx
    dy = int(round(aim_y - fcy))

    deadband_half_w = cfg.deadband_frac_w * frame_w
    deadband_half_h = cfg.deadband_frac_h * frame_h

    # Zero EACH axis independently when it is within its own deadband half-width
    # (Req 4.4). A centered axis stops on its own without waiting for the other,
    # which is what lets the gaze settle instead of limit-cycling.
    if abs(dx) <= deadband_half_w:
        dx = 0
    if abs(dy) <= deadband_half_h:
        dy = 0

    return Offset(dx=dx, dy=dy, has_target=True)


def _capped_step(distance: float, max_step: float) -> float:
    """Clamp a per-update step magnitude to ``max_step`` (Req 5.6).

    The caller passes a signed step ``distance``; this preserves the sign while
    limiting the magnitude so no single update moves a joint by more than the
    configured maximum. ``max_step`` is already clamped to ``[1, 30]`` by
    ``TrackingConfig``.

    Args:
        distance: The signed, uncapped step in degrees (sign = direction).
        max_step: The maximum allowed step magnitude per update, in degrees.

    Returns:
        ``distance`` unchanged when its magnitude is within ``max_step``;
        otherwise ``max_step`` carrying the sign of ``distance``.
    """
    if distance > max_step:
        return max_step
    if distance < -max_step:
        return -max_step
    return distance


def _proportional_step(
    offset_px: float, half_frame_px: float, cfg: TrackingConfig
) -> float:
    """Proportional, capped step (degrees) toward reducing a 1-axis offset.

    The step magnitude is proportional to the FRACTIONAL offset of the person
    from frame center, so the head moves fast when far off-center and eases to a
    stop as it approaches center (instead of always taking the full ``max_step``
    and overshooting, which caused the left-right / up-down oscillation):

        step = settle_gain * (offset_px / half_frame_px) * max_step_deg

    then capped to ``[-max_step_deg, +max_step_deg]``. The returned step carries
    the SAME sign as ``offset_px``; the caller applies the correct direction per
    axis (pan uses the opposite sign of dx, tilt the same sign as dy).

    Args:
        offset_px: Signed pixel offset on this axis (0 means centered/deadbanded).
        half_frame_px: Half the frame dimension on this axis, in pixels; the
            offset at the frame edge. Guarded against zero.
        cfg: Tracking tuning supplying ``settle_gain`` and ``max_step_deg``
            (both already clamped by ``TrackingConfig``).

    Returns:
        The signed step in degrees, proportional to the fractional offset and
        capped at ``cfg.max_step_deg``. Zero when ``offset_px`` is zero.
    """
    if offset_px == 0 or half_frame_px <= 0:
        return 0.0
    frac = offset_px / half_frame_px
    step = cfg.settle_gain * frac * cfg.max_step_deg
    return _capped_step(step, cfg.max_step_deg)


def next_neck_targets(
    offset: Offset,
    cur_pan: float,
    cur_tilt: float,
    cfg: TrackingConfig,
    frame_w: int,
    frame_h: int,
) -> Dict[int, float]:
    """Map an Offset to the next NECK_PAN/NECK_TILT target angles.

    This is pure math: it returns the next target angle for each neck channel
    that needs to move, computed as the current angle plus a capped step in the
    direction that reduces the Offset. It does NOT call ``set_angle`` and does
    NOT clamp to ``constants.SAFE_LIMITS`` — the Tracking_Mode loop applies each
    returned target through ``TrunkController.set_angle``, which performs the
    final SAFE_LIMITS clamp (Req 5.5). The returned dict maps a servo channel to
    its target angle in degrees.

    Direction mapping honors the ``constants`` AXIS DIRECTION REFERENCE:

    - Person to the animatronic's LEFT (``offset.dx < 0``) -> INCREASE
      ``NECK_PAN`` (increasing pan turns the head to its left) (Req 5.1).
    - Person to the RIGHT (``offset.dx > 0``) -> DECREASE ``NECK_PAN`` (Req 5.2).
    - Person BELOW Frame_Center (``offset.dy > 0``) -> INCREASE ``NECK_TILT``
      (increasing tilt lowers the head toward the person) (Req 5.3).
    - Person ABOVE Frame_Center (``offset.dy < 0``) -> DECREASE ``NECK_TILT``
      (Req 5.4).

    Each per-update step is PROPORTIONAL to the fractional offset
    (``settle_gain * (offset / half_frame) * max_step_deg``) and capped at
    ``cfg.max_step_deg`` (Req 5.6). Proportional stepping means the head slows as
    it nears center and settles, rather than taking a full fixed step and
    overshooting (which oscillated). Only channels whose axis has a non-zero
    Offset are included, so the returned channel set is always a subset of
    ``{NECK_PAN, NECK_TILT}`` (0, 1) (Req 5.8). An axis with a zero Offset
    (inside the Deadband, or exactly centered) contributes no entry, so the
    caller holds that joint's current angle.

    When there is no Target_Person (``offset.has_target`` is False) or the Offset
    is zero on both axes, an empty dict is returned so the caller issues no neck
    command and the neck holds its current angle (Req 5.9).

    Args:
        offset: The signed pixel Offset of the Target_Person from Frame_Center,
            as produced by ``compute_offset``.
        cur_pan: The current ``NECK_PAN`` commanded angle in degrees.
        cur_tilt: The current ``NECK_TILT`` commanded angle in degrees.
        cfg: Tracking tuning supplying ``settle_gain`` and ``max_step_deg``
            (already clamped by ``TrackingConfig``).
        frame_w: Frame width in pixels, used to scale the proportional pan step
            by the fractional horizontal offset.
        frame_h: Frame height in pixels, used to scale the proportional tilt
            step by the fractional vertical offset.

    Returns:
        A dict mapping each neck channel that should move to its next target
        angle in degrees: ``{NECK_PAN: angle}`` and/or ``{NECK_TILT: angle}``.
        Empty when there is no target or the Offset is zero on both axes.
    """
    if not offset.has_target:
        return {}

    targets: Dict[int, float] = {}
    half_w = frame_w / 2.0
    half_h = frame_h / 2.0

    # Horizontal: person LEFT (dx < 0) -> increase pan; RIGHT (dx > 0) ->
    # decrease pan. The step toward the target has the OPPOSITE sign of dx, and
    # its magnitude is proportional to the fractional horizontal offset so the
    # pan eases to a stop at center instead of overshooting.
    if offset.dx != 0:
        pan_step = -_proportional_step(float(offset.dx), half_w, cfg)
        targets[constants.NECK_PAN] = cur_pan + pan_step

    # Vertical: person BELOW (dy > 0) -> increase tilt (lower head); ABOVE
    # (dy < 0) -> decrease tilt (raise head). The step toward the target has
    # the SAME sign as dy and is proportional to the fractional vertical offset.
    # The result is then clamped into the tracking tilt band
    # [cfg.tilt_min_deg, cfg.tilt_max_deg] so the head stays at head height and
    # the closed loop cannot pitch far enough to throw the person out of the
    # frame. (Tracking-only band, separate from the global SAFE_LIMITS;
    # TrunkController.set_angle still applies the hardware clamp.)
    if offset.dy != 0:
        tilt_step = _proportional_step(float(offset.dy), half_h, cfg)
        tilt_target = cur_tilt + tilt_step
        tilt_target = _clamp_tilt_band(tilt_target, cfg)
        targets[constants.NECK_TILT] = tilt_target

    return targets


def _clamp_tilt_band(angle: float, cfg: TrackingConfig) -> float:
    """Clamp a NECK_TILT target to the tracking tilt band.

    Confines the commanded tilt to ``[cfg.tilt_min_deg, cfg.tilt_max_deg]`` — a
    narrow band around the level-gaze center used ONLY by Tracking_Mode (not the
    global ``constants.SAFE_LIMITS`` that head gestures use). Keeping tracking
    tilt inside this band holds the head at head height and prevents the closed
    loop from pitching far enough to lose the person out of the frame.

    Args:
        angle: The proposed NECK_TILT target in degrees.
        cfg: Tracking tuning supplying ``tilt_min_deg`` / ``tilt_max_deg``
            (already validated into the servo envelope by ``TrackingConfig``).

    Returns:
        ``angle`` clamped into ``[cfg.tilt_min_deg, cfg.tilt_max_deg]``.
    """
    if angle < cfg.tilt_min_deg:
        return cfg.tilt_min_deg
    if angle > cfg.tilt_max_deg:
        return cfg.tilt_max_deg
    return angle
