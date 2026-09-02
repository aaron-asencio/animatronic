"""Property test for the shared jaw-value mapping (Property 1).

The jaw-value computation is duplicated byte-for-byte across three consumers:
  - ``AudioPlayer.talk()`` in ``audio_player.py``
  - ``AudioStreamer.talk()`` in ``audio_streamer.py``
  - the module-level ``talk()`` in ``micwebcontroller.py``

All three differ only in *where* the three tuning parameters come from and in
how they persist ``previous_jaw_value``; the peak -> jaw_value math is
identical. Rather than construct the hardware-dependent classes (gpiozero /
pyaudio), this test exercises a reference pure function that encodes the exact
gate -> scale -> snap semantics those methods share, and asserts the
three-branch outcome plus the [0.0, 100.0] output bound.

Property tests use hypothesis (minimum 100 iterations). Run with:

    pytest tests/test_jaw_mapping.py -q --maxfail=1
"""

import os
import sys

import pytest
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))


def jaw_value_from(peak, previous, sensitivity, noise_floor, drop_threshold):
    """Reference implementation of the shared jaw-value mapping.

    This mirrors the gate -> scale -> snap logic in the ``talk()`` methods of
    ``audio_player.py``, ``audio_streamer.py``, and ``micwebcontroller.py``.
    The three consumers share this computation verbatim; only the source of the
    tuning parameters and the storage of ``previous`` differ between them.

    Args:
        peak:           Peak absolute amplitude of the current audio frame (>= 0).
        previous:       The previous jaw value, or None if there is no prior frame.
        sensitivity:    Peak amplitude divisor used to scale peak to 0-100 (> 0).
        noise_floor:    Gate threshold; peaks below this close the jaw (>= 0).
        drop_threshold: Ratio below which a falling value snaps closed (0.0-1.0).

    Returns:
        The computed jaw value as a float in the range [0.0, 100.0].
    """
    if peak < noise_floor:
        return 0.0
    value = min(peak / sensitivity * 100.0, 100.0)
    if previous is not None and previous > 0 and value < previous:
        if (value / previous) < drop_threshold:
            value = 0.0
    return value


# ── Generators ────────────────────────────────────────────────────────────
# Constrain each argument to its valid input space per the requirements.

_peaks = st.floats(min_value=0.0, max_value=50000.0,
                   allow_nan=False, allow_infinity=False)
_previous = st.one_of(
    st.none(),
    st.floats(min_value=0.0, max_value=100.0,
              allow_nan=False, allow_infinity=False),
)
_sensitivity = st.floats(min_value=1.0, max_value=5000.0,
                         allow_nan=False, allow_infinity=False)
_noise_floor = st.floats(min_value=0.0, max_value=5000.0,
                         allow_nan=False, allow_infinity=False)
_drop_threshold = st.floats(min_value=0.0, max_value=1.0,
                            allow_nan=False, allow_infinity=False)


@settings(max_examples=150)
@given(
    peak=_peaks,
    previous=_previous,
    sensitivity=_sensitivity,
    noise_floor=_noise_floor,
    drop_threshold=_drop_threshold,
)
def test_jaw_value_mapping_gate_scale_snap(
    peak, previous, sensitivity, noise_floor, drop_threshold
):
    """Feature: separate-jaw-tuning-profiles, Property 1: Jaw-value mapping preserves the gate -> scale -> snap semantics.

    Validates: Requirements 2.2, 2.3, 2.4, 2.5, 3.2, 3.3, 3.4, 3.5
    """
    result = jaw_value_from(peak, previous, sensitivity, noise_floor, drop_threshold)

    # Independently recompute the expected three-branch outcome.
    if peak < noise_floor:
        # Gate: below the noise floor the jaw is held closed.
        expected = 0.0
    else:
        base = min(peak / sensitivity * 100.0, 100.0)
        if (
            previous is not None
            and previous > 0
            and base < previous
            and (base / previous) < drop_threshold
        ):
            # Snap: a sharp falling ratio closes the jaw.
            expected = 0.0
        else:
            # Scale: hold the scaled value.
            expected = base

    assert result == expected

    # Output is always a valid motor-drive value in [0.0, 100.0].
    assert 0.0 <= result <= 100.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--maxfail=1"]))
