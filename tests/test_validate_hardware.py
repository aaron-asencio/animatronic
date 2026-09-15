"""Tests for the hardware validation driver ``src/validate_hardware.py``.

Covers the pure, hardware-free helper functions of the Requirement 9 hardware
validation workflow:
  - ``position_difference`` (per-link Euclidean distance metric)
  - ``boundary_probe_report`` (flagged-before-contact reporting)
  - ``compare_verdict`` (model vs. measured agreement)
  - ``log_disagreement`` (append-one-JSON-line disagreement log)
  - ``measured_scale`` (empirical scale from a commanded sweep)

Property tests use hypothesis (``max_examples=150`` each) and are tagged with
the design's Correctness Properties by number. Example-based unit tests use
pytest. Run with:

    .venv/bin/python -m pytest tests/test_validate_hardware.py -q
"""

import json
import math
import os
import sys

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import validate_hardware as vh  # noqa: E402


# A finite 3-vector strategy for link world positions.
_FINITE_COORD = st.floats(
    min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False
)


@st.composite
def _predicted_and_measured(draw):
    """Build two link-position dicts over a shared set of link names.

    Draws ``N`` link names ``link0``..``link(N-1)`` (N in 1..6). ``predicted``
    maps each to a finite length-3 numpy vector. ``measured`` maps the SAME
    link names either to the identical vector (zero-difference case) or to a
    perturbed vector (non-zero-difference case), chosen per link so both
    branches of Property 13 are exercised.

    Args:
        draw: The hypothesis draw callable.

    Returns:
        A ``(predicted, measured)`` tuple of ``dict[str, np.ndarray]`` over a
        shared key set.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    link_names = [f"link{i}" for i in range(n)]

    predicted: dict[str, np.ndarray] = {}
    measured: dict[str, np.ndarray] = {}
    for link_name in link_names:
        pred_vec = np.array(
            [draw(_FINITE_COORD), draw(_FINITE_COORD), draw(_FINITE_COORD)],
            dtype=float,
        )
        predicted[link_name] = pred_vec

        if draw(st.booleans()):
            # Identical -> zero difference for this link.
            measured[link_name] = np.array(pred_vec, dtype=float)
        else:
            # Perturb by a non-zero offset -> positive difference for this link.
            offset = np.array(
                [
                    draw(st.floats(min_value=0.5, max_value=5.0, allow_nan=False)),
                    draw(st.floats(min_value=-5.0, max_value=5.0, allow_nan=False)),
                    draw(st.floats(min_value=-5.0, max_value=5.0, allow_nan=False)),
                ],
                dtype=float,
            )
            measured[link_name] = pred_vec + offset

    return predicted, measured


# ---------------------------------------------------------------------------
# Task 11.2 — Property 13: Position difference is a well-formed metric
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(pair=_predicted_and_measured())
def test_property13_position_difference_is_well_formed_metric(pair):
    """Feature: kinematic-collision-model, Property 13: Position difference is a well-formed metric.

    For two link-position dicts over a shared set of link names, the per-link
    Euclidean difference is (a) exactly zero when a link's positions are
    identical and strictly positive when they differ, and (b) symmetric:
    ``position_difference(pred, meas)[link]`` equals
    ``position_difference(meas, pred)[link]`` for every shared link.

    Validates: Requirements 9.1
    """
    predicted, measured = pair

    # Identity: comparing a mapping to itself is zero for every link.
    self_diff = vh.position_difference(predicted, predicted)
    for link_name, distance in self_diff.items():
        assert distance == 0.0

    forward = vh.position_difference(predicted, measured)
    backward = vh.position_difference(measured, predicted)

    for link_name in predicted:
        distance = forward[link_name]
        identical = np.array_equal(
            np.asarray(predicted[link_name], dtype=float),
            np.asarray(measured[link_name], dtype=float),
        )
        if identical:
            assert distance == 0.0
        else:
            assert distance > 0.0

        # Symmetry: swapping the arguments yields the same distance.
        assert math.isclose(distance, backward[link_name], abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Task 11.3 — Property 14: Boundary probe reports flagged-before-contact
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    contact_index=st.integers(min_value=0, max_value=50),
    first_collision_index=st.one_of(
        st.none(), st.integers(min_value=0, max_value=50)
    ),
)
def test_property14_boundary_probe_reports_flagged_before_contact(
    contact_index, first_collision_index
):
    """Feature: kinematic-collision-model, Property 14: Boundary probe reports flagged-before-contact correctly.

    ``boundary_probe_report`` returns True exactly when the model flagged a
    collision (index is not None) at or before physical contact
    (``index <= contact_index``), and False otherwise.

    Validates: Requirements 9.3
    """
    expected = (
        first_collision_index is not None and first_collision_index <= contact_index
    )
    assert (
        vh.boundary_probe_report(first_collision_index, contact_index) == expected
    )


# ---------------------------------------------------------------------------
# Task 11.4 — Unit tests for verdict comparison, logging, and scale
# ---------------------------------------------------------------------------


def test_compare_verdict_agrees_and_disagrees():
    """Requirement 9.2: verdict comparison flags model/reality (dis)agreement.

    The model and reality agree iff ``model_ok == (not measured_collision)``:
    safe-and-no-collision or unsafe-and-collision agree; the mixed cases
    disagree.
    """
    # Disagreements: model says safe but a collision was measured, and vice versa.
    assert vh.compare_verdict(True, True) == "disagree"
    assert vh.compare_verdict(False, False) == "disagree"

    # Agreements: safe + no collision, unsafe + collision.
    assert vh.compare_verdict(True, False) == "agree"
    assert vh.compare_verdict(False, True) == "agree"


def test_log_disagreement_appends_angles_and_verdicts(tmp_path):
    """Requirement 9.4: each disagreement is appended as one parseable JSON line.

    Two calls to a fresh log file produce exactly two lines, each a JSON object
    recording the pose's servo angles and both verdicts. Servo angle values
    round-trip; channel keys may be stringified, so assert on values (and
    handle string-vs-int keys).
    """
    log_path = tmp_path / "disagreements.log"

    first_angles = {0: 90.0, 7: 12.5}
    second_angles = {1: 45.0, 3: -30.0, 5: 200.0}

    vh.log_disagreement(str(log_path), first_angles, model_ok=True, measured_collision=True)
    vh.log_disagreement(
        str(log_path), second_angles, model_ok=False, measured_collision=False
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    expected = [
        (first_angles, True, True),
        (second_angles, False, False),
    ]
    for line, (angles, model_ok, measured_collision) in zip(lines, expected):
        record = json.loads(line)  # parseable JSON object
        assert record["model_ok"] == model_ok
        assert record["measured_collision"] == measured_collision

        # Channel keys may be stringified in the JSON; compare on int channels.
        recorded = {
            int(channel): value for channel, value in record["servo_angles"].items()
        }
        assert recorded == angles


def test_measured_scale_computes_and_rejects_zero_width():
    """Requirement 9.6: empirical scale = arc / |commanded span|; zero-width raises.

    A ``0 -> 270`` commanded sweep producing a 170 deg physical arc yields
    ~0.6296; a zero-width commanded range raises ``ValueError``.
    """
    assert math.isclose(vh.measured_scale((0, 270), 170), 0.6296, abs_tol=1e-3)

    with pytest.raises(ValueError):
        vh.measured_scale((90, 90), 5)
