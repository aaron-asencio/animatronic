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
    below Frame_Center and negative when above (Req 4.3).

    A Deadband centered on Frame_Center suppresses jitter: its half-widths are
    ``cfg.deadband_frac_w * frame_w`` horizontally and
    ``cfg.deadband_frac_h * frame_h`` vertically. WHEN both the horizontal and
    vertical distances from the bbox center to Frame_Center are within their
    respective half-widths, BOTH Offsets are reported as zero (Req 4.4). The
    deadband is only applied as a pair: if either axis is outside its
    half-width, both raw signed Offsets are reported.

    With no Target_Person, the Offset reports ``has_target=False`` and both axes
    zero (Req 4.5).

    Args:
        target: The selected Target_Person, or ``None`` when none is available.
        frame_w: Frame width in pixels.
        frame_h: Frame height in pixels.
        cfg: Tracking tuning supplying the Deadband fractions (already clamped
            to ``[0.0, 0.5]`` by ``TrackingConfig``).

    Returns:
        An ``Offset``: ``Offset(0, 0, has_target=False)`` when ``target`` is
        ``None``; otherwise the signed ``(dx, dy)`` with ``has_target=True``,
        zeroed on both axes inside the Deadband.
    """
    if target is None:
        return Offset(dx=0, dy=0, has_target=False)

    cx, cy = target.center
    fcx, fcy = _frame_center(frame_w, frame_h)
    dx = cx - fcx
    dy = cy - fcy

    deadband_half_w = cfg.deadband_frac_w * frame_w
    deadband_half_h = cfg.deadband_frac_h * frame_h

    # Zero both axes only when both distances are within the deadband (Req 4.4).
    if abs(dx) <= deadband_half_w and abs(dy) <= deadband_half_h:
        return Offset(dx=0, dy=0, has_target=True)

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


def next_neck_targets(
    offset: Offset,
    cur_pan: float,
    cur_tilt: float,
    cfg: TrackingConfig,
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

    Each per-update step magnitude is capped at ``cfg.max_step_deg`` so the head
    moves smoothly rather than snapping to target (Req 5.6). Only channels whose
    axis has a non-zero Offset are included, so the returned channel set is
    always a subset of ``{NECK_PAN, NECK_TILT}`` (0, 1) (Req 5.8). An axis with a
    zero Offset (inside the Deadband, or exactly centered) contributes no entry,
    so the caller holds that joint's current angle.

    When there is no Target_Person (``offset.has_target`` is False) or the Offset
    is zero on both axes, an empty dict is returned so the caller issues no neck
    command and the neck holds its current angle (Req 5.9).

    Args:
        offset: The signed pixel Offset of the Target_Person from Frame_Center,
            as produced by ``compute_offset``.
        cur_pan: The current ``NECK_PAN`` commanded angle in degrees.
        cur_tilt: The current ``NECK_TILT`` commanded angle in degrees.
        cfg: Tracking tuning supplying ``max_step_deg`` (already clamped to
            ``[1, 30]`` by ``TrackingConfig``).

    Returns:
        A dict mapping each neck channel that should move to its next target
        angle in degrees: ``{NECK_PAN: angle}`` and/or ``{NECK_TILT: angle}``.
        Empty when there is no target or the Offset is zero on both axes.
    """
    if not offset.has_target:
        return {}

    targets: Dict[int, float] = {}
    max_step = cfg.max_step_deg

    # Horizontal: person LEFT (dx < 0) -> increase pan; RIGHT (dx > 0) ->
    # decrease pan. The step toward the target has the OPPOSITE sign of dx.
    if offset.dx != 0:
        pan_step = _capped_step(-float(offset.dx), max_step)
        targets[constants.NECK_PAN] = cur_pan + pan_step

    # Vertical: person BELOW (dy > 0) -> increase tilt (lower head); ABOVE
    # (dy < 0) -> decrease tilt (raise head). The step toward the target has
    # the SAME sign as dy.
    if offset.dy != 0:
        tilt_step = _capped_step(float(offset.dy), max_step)
        targets[constants.NECK_TILT] = cur_tilt + tilt_step

    return targets
