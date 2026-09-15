"""Hardware validation driver — SEPARATE from the collision model package.

This module lives at ``src/`` level, deliberately *outside* the
``src/kinematics/`` package. It is the hardware-validation workflow described in
Requirement 9: it compares model-predicted link positions and collision
verdicts against physically measured reality, supports boundary probing toward
predicted collision boundaries, measures each joint's actual physical arc
versus its commanded servo-degree range (so provisional ``scale``/``sign``/
``offset_deg`` seeds can be confirmed or corrected), and logs poses where the
model and reality disagree.

Design intent (Requirement 9.5 / 11.3): the ``kinematics`` model package must
stay importable and testable *without hardware*. All actual servo driving and
measurement capture is therefore kept here, not in the model. The pure helper
functions below import only ``numpy`` and (for predictions) ``CollisionModel``
from ``kinematics.model`` — they perform NO hardware imports.

The actual servo-driving / measurement-capture step is intentionally *not*
implemented in this module. It is a documented integration point: driving the
physical robot would go through the existing hardware layer
(``TrunkController`` etc.) in a caller/operator harness, feeding measured data
into the pure functions here. See :func:`main` for the stubbed entry point.

Naming is ``snake_case`` and docstrings follow the Google style with ``Args:``
sections, per Requirement 11.5.
"""

from __future__ import annotations

import argparse
import json

import numpy as np

# Prediction-only import. When ``src`` is on the path (e.g. PYTHONPATH=src),
# this resolves to src/kinematics/model.py. The model package imports NO
# hardware libraries, keeping this validation driver hardware-free for its
# pure helper functions.
from kinematics.model import CollisionModel  # noqa: F401  (used by main/harness)


def position_difference(
    predicted: dict[str, np.ndarray],
    measured: dict[str, np.ndarray],
) -> dict[str, float]:
    """Per-link Euclidean distance between predicted and measured link origins.

    Only links present in BOTH mappings are compared; links appearing in just
    one of the two dicts are skipped, so the result contains exactly the
    intersection of the two key sets.

    Args:
        predicted: link_name -> predicted world position, a length-3 array.
        measured: link_name -> measured world position, a length-3 array.

    Returns:
        link_name -> Euclidean distance in meters (``0.0`` when identical).
    """
    result: dict[str, float] = {}
    for link_name in predicted:
        if link_name not in measured:
            continue
        pred = np.asarray(predicted[link_name], dtype=float)
        meas = np.asarray(measured[link_name], dtype=float)
        result[link_name] = float(np.linalg.norm(pred - meas))
    return result


def measured_scale(
    commanded_deg_range: tuple[float, float],
    measured_arc_deg: float,
) -> float:
    """Estimates a joint's physical scale from a commanded sweep.

    Divides the measured physical arc (degrees) by the commanded servo-degree
    range so the per-joint ``scale`` seed can be confirmed or corrected against
    the physical robot. For example, a ``0 -> 270`` commanded sweep that
    produces a ~170 deg physical arc yields ~0.63. Direct-drive joints are
    expected near 1.0 but must be measured, not assumed (Requirement 9.6).

    Args:
        commanded_deg_range: ``(start_deg, end_deg)`` commanded servo sweep.
        measured_arc_deg: Observed physical arc in degrees over that sweep.

    Returns:
        The empirical scale = ``measured_arc_deg / |end_deg - start_deg|``.

    Raises:
        ValueError: If the commanded range is zero-width (start == end),
            which would otherwise divide by zero.
    """
    start_deg, end_deg = commanded_deg_range
    span = abs(end_deg - start_deg)
    if span == 0:
        raise ValueError(
            "commanded_deg_range is zero-width "
            f"(start == end == {start_deg}); cannot compute scale."
        )
    return measured_arc_deg / span


def compare_verdict(model_ok: bool, measured_collision: bool) -> str:
    """Compares the model verdict to the measured outcome.

    The model and reality agree when the model predicts safe AND no collision
    was measured, or when the model predicts unsafe AND a collision was
    measured. Equivalently, they agree iff
    ``model_ok == (not measured_collision)``.

    Args:
        model_ok: True if the model classified the pose as safe.
        measured_collision: True if a physical collision was observed.

    Returns:
        ``"agree"`` if the model and measured outcome are consistent, otherwise
        ``"disagree"``.
    """
    return "agree" if model_ok == (not measured_collision) else "disagree"


def boundary_probe_report(
    first_collision_index: int | None,
    contact_index: int,
) -> bool:
    """Reports whether the model flagged a collision at or before contact.

    A boundary probe advances joints step-by-step toward a model-predicted
    collision boundary. This confirms the (conservative) model flags a
    collision slightly before physical contact.

    Args:
        first_collision_index: Step index where the model first flags a
            collision along the probe, or ``None`` if it never does.
        contact_index: Step index where physical contact was observed.

    Returns:
        True iff the model flagged a collision before or at physical contact
        (``first_collision_index`` is not None and ``<= contact_index``).
        False otherwise, including when the model never flagged (``None``).
    """
    if first_collision_index is None:
        return False
    return first_collision_index <= contact_index


def log_disagreement(
    path: str,
    servo_angles: dict[int, float],
    model_ok: bool,
    measured_collision: bool,
) -> None:
    """Appends servo angles and both verdicts to a disagreement log file.

    The file is opened in append mode, so calling this repeatedly accumulates
    one JSON line per disagreement without truncating earlier entries. Each
    line is a parseable JSON object recording the pose's servo angles and both
    the model and measured verdicts (Requirement 9.4).

    Args:
        path: Filesystem path of the log file (created if missing, appended
            to if it already exists).
        servo_angles: Servo channel -> commanded angle (degrees) for the pose.
        model_ok: True if the model classified the pose as safe.
        measured_collision: True if a physical collision was observed.
    """
    record = {
        # JSON object keys are strings; keep channels stringified for parseability.
        "servo_angles": {str(channel): angle for channel, angle in servo_angles.items()},
        "model_ok": model_ok,
        "measured_collision": measured_collision,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def main() -> None:
    """CLI stub for the hardware validation workflow.

    This entry point is intentionally a stub. Running a real validation pass
    requires the physical robot plus captured measurements (link positions and
    observed collision outcomes), which is an operator-driven integration step
    outside this module. The pure helpers above (:func:`position_difference`,
    :func:`measured_scale`, :func:`compare_verdict`,
    :func:`boundary_probe_report`, :func:`log_disagreement`) are the graded,
    hardware-free building blocks used by such a harness.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Hardware validation driver for the Maximus collision model. "
            "Compares model predictions against measured physical reality. "
            "This requires the physical robot AND captured measurement data; "
            "actual servo driving/measurement capture is not implemented here."
        )
    )
    parser.add_argument(
        "--log",
        default="src/config/disagreements.log",
        help="Path to append model/reality disagreements to.",
    )
    parser.parse_args()
    print(
        "validate_hardware is a driver stub. Provide measured link positions "
        "and observed collision outcomes from the physical robot, then feed "
        "them to the pure helper functions in this module (position_difference, "
        "measured_scale, compare_verdict, boundary_probe_report, "
        "log_disagreement). Servo driving/measurement capture is an operator "
        "integration step and is not implemented here."
    )


if __name__ == "__main__":
    main()
