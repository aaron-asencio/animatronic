"""Regression tests for Detector output-tensor identification.

Guards the specific bug where ``Detector._read_outputs`` matched the four
SSD-MobileNet post-process outputs by shape and assigned the two identically
shaped ``[N]`` tensors (classes and scores) POSITIONALLY — "first [N] seen is
scores, second is classes". The real ``ssd_mobilenet_v1_coco_quant_postprocess``
model emits classes BEFORE scores (``TFLite_Detection_PostProcess:1`` = classes,
``:2`` = scores), so the positional assignment SWAPPED them. The symptom was
every detection reading class index ``int(score) == 0`` -> ``"person"`` while
the real class index was mis-used as the confidence score.

These tests drive the detector with a fake interpreter (no tflite_runtime, no
camera) via the ``interpreter_factory`` seam, reproducing that exact output
layout, and assert classes/scores are identified correctly — i.e. NOT swapped.

Run with:

    pytest tests/test_detector_outputs.py -q --maxfail=1
"""

import os
import sys
import tempfile
import pathlib

import numpy as np
from hypothesis import given, settings, strategies as st

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from detector import Detector  # noqa: E402


# A minimal COCO-style label map written to a temp file so the test does not
# depend on models/ being present. Indices match the real file's layout for the
# labels this test asserts on: 0=person, 47=cup, 76=scissors.
_LABELS = {0: "person", 1: "bicycle", 17: "dog", 47: "cup", 76: "scissors"}


def _write_labels():
    """Write a sparse COCO-style labels file (``<index> <label>`` per line)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = tmp / "labels.txt"
    lines = [f"{idx} {name}" for idx, name in sorted(_LABELS.items())]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


class FakeSSDInterpreter:
    """A fake TFLite interpreter mimicking the real SSD post-process layout.

    Reproduces the four outputs of ``TFLite_Detection_PostProcess`` in the SAME
    order the real ``ssd_mobilenet_v1_coco_quant_postprocess.tflite`` reports
    them from ``get_output_details()``:

        pos 0  name ""   boxes   [1, N, 4]  (ymin, xmin, ymax, xmax, normalized)
        pos 1  name ":1" classes [1, N]     (float-encoded class indices)
        pos 2  name ":2" scores  [1, N]     (confidences in [0, 1])
        pos 3  name ":3" count   [1]

    The ``name`` fields are what the fix keys off; the ordering (classes before
    scores, identical shape) is what made the old positional code swap them.
    """

    _INPUT_SIZE = 300

    def __init__(self, boxes, classes, scores, omit_names=False):
        n = len(scores)
        self._boxes = np.asarray(boxes, dtype=np.float32).reshape(1, n, 4)
        self._classes = np.asarray(classes, dtype=np.float32).reshape(1, n)
        self._scores = np.asarray(scores, dtype=np.float32).reshape(1, n)
        self._count = np.asarray([float(n)], dtype=np.float32)
        c1, c2 = ("", "") if omit_names else (":1", ":2")
        self._tensors = {
            10: self._boxes,
            11: self._classes,
            12: self._scores,
            13: self._count,
        }
        self._outputs = [
            {"index": 10, "name": "TFLite_Detection_PostProcess", "shape": np.array([1, n, 4])},
            {"index": 11, "name": "TFLite_Detection_PostProcess" + c1, "shape": np.array([1, n])},
            {"index": 12, "name": "TFLite_Detection_PostProcess" + c2, "shape": np.array([1, n])},
            {"index": 13, "name": "TFLite_Detection_PostProcess:3", "shape": np.array([1])},
        ]

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": np.array([1, self._INPUT_SIZE, self._INPUT_SIZE, 3]),
                 "dtype": np.uint8}]

    def get_output_details(self):
        return self._outputs

    def set_tensor(self, index, value):
        pass

    def invoke(self):
        pass

    def get_tensor(self, index):
        return self._tensors[index]


def _make_detector(boxes, classes, scores, omit_names=False, conf_threshold=0.3):
    """Build a Detector wired to a FakeSSDInterpreter via the factory seam."""
    labels_path = _write_labels()

    def factory(model_path, use_edge_tpu):
        return FakeSSDInterpreter(boxes, classes, scores, omit_names=omit_names)

    return Detector(
        model_path="fake_model.tflite",
        labels_path=labels_path,
        conf_threshold=conf_threshold,
        use_edge_tpu=False,
        interpreter_factory=factory,
    )


def test_classes_and_scores_not_swapped():
    """A non-person class with a passing score must NOT come back as 'person'.

    classes at ``:1`` (scissors, idx 76) and scores at ``:2`` (0.80). If the two
    are swapped, the label becomes ``person`` (int(0.80)==0) and the score
    becomes 76.0 — the asserts below would then fail.
    """
    det = _make_detector(
        boxes=[[0.1, 0.1, 0.5, 0.5]],
        classes=[76.0],      # scissors
        scores=[0.80],
        conf_threshold=0.3,
    )
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    results = det.detect(frame)

    assert len(results) == 1
    d = results[0]
    assert d.label == "scissors", f"expected 'scissors', got {d.label!r} (classes/scores swapped?)"
    assert d.is_person is False
    assert 0.0 <= d.score <= 1.0, f"score {d.score} out of [0,1] — class index leaked into score"
    assert abs(d.score - 0.80) < 1e-3


def test_multiple_classes_each_label_correct():
    """Several distinct classes each resolve to their OWN label, not all person."""
    det = _make_detector(
        boxes=[[0.0, 0.0, 0.3, 0.3], [0.3, 0.3, 0.6, 0.6], [0.6, 0.6, 0.9, 0.9]],
        classes=[0.0, 47.0, 76.0],       # person, cup, scissors
        scores=[0.9, 0.7, 0.5],
        conf_threshold=0.3,
    )
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    labels = {d.label for d in det.detect(frame)}

    assert labels == {"person", "cup", "scissors"}, (
        f"expected distinct labels, got {labels} — everything collapsing to one "
        "label indicates a classes/scores swap"
    )


def test_value_fallback_when_names_missing():
    """When ``:1``/``:2`` names are absent, value-based ID still separates them.

    scores lie in [0,1]; class indices are integer-valued > 1 — so even without
    the canonical name suffixes the detector must not confuse them.
    """
    det = _make_detector(
        boxes=[[0.1, 0.1, 0.5, 0.5]],
        classes=[47.0],      # cup
        scores=[0.65],
        omit_names=True,
        conf_threshold=0.3,
    )
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    results = det.detect(frame)

    assert len(results) == 1
    assert results[0].label == "cup"
    assert abs(results[0].score - 0.65) < 1e-3


@settings(max_examples=100)
@given(
    class_idx=st.sampled_from(sorted(_LABELS)),
    score=st.floats(min_value=0.3, max_value=1.0,
                    allow_nan=False, allow_infinity=False),
)
def test_label_matches_class_index_property(class_idx, score):
    """Property: the reported label is ALWAYS the label of the emitted class
    index, and the reported score is ALWAYS the emitted score (never swapped),
    across every known class and any passing confidence."""
    det = _make_detector(
        boxes=[[0.1, 0.1, 0.5, 0.5]],
        classes=[float(class_idx)],
        scores=[score],
        conf_threshold=0.3,
    )
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    results = det.detect(frame)

    assert len(results) == 1
    d = results[0]
    assert d.label == _LABELS[class_idx]
    assert abs(d.score - score) < 1e-3
    assert d.is_person == (class_idx == 0)
