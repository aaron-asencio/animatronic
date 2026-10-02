"""Detector — TFLite COCO SSD-MobileNet object detection inside Camera_Service.

This module wraps a TensorFlow Lite COCO SSD-MobileNet interpreter and turns one
captured frame into a list of :class:`vision_models.Detection` objects. It lives
inside the non-root Camera_Service (Req 3) and feeds both the Control_Panel
overlay and Tracking_Mode.

Scope of this module: the core ``Detector`` class, ``detect()``, the raw-output
-> ``Detection`` normalization, confidence filtering, label-file loading (task
6.1), and the optional Edge TPU delegate with CPU fallback (task 6.2). When
``use_edge_tpu`` is set, ``_load_interpreter`` attempts the ``libedgetpu``
delegate against the ``*_edgetpu`` model and, if the delegate or Coral device is
unavailable, falls back to a CPU ``Interpreter`` while printing a clear fallback
indication. ``Detector.edge_tpu_active`` records whether the accelerator is
actually in use so Camera_Service can report it in ``/status`` (task 6.3).

Design notes:

- The TFLite runtime is imported **lazily and guarded** (``_import_interpreter``
  / ``_import_load_delegate``) so this module imports for tests on a machine
  without ``tflite-runtime`` installed. The import uses the classic
  ``from tflite_runtime.interpreter import Interpreter`` (and ``load_delegate``)
  API the design references (see requirements.txt pin ``tflite-runtime==2.14.0``).
- The raw-output -> ``Detection`` conversion is factored into the pure, stateless
  :func:`normalize_detections` helper (no interpreter, no I/O) so the
  normalization + clamping + confidence filtering can be unit/property tested
  directly (Properties 3, 4, 5).
- Debug output uses ``print()`` to stay consistent with the rest of the
  codebase; there is no logging framework.

No servo code lives here, so there are no SAFE_LIMITS/collision concerns in this
module.
"""

from vision_models import Detection, PERSON_LABEL


# Default confidence threshold for reporting a Detection; configurable in
# [0.0, 1.0] via the constructor (Req 3.3).
DEFAULT_CONF_THRESHOLD = 0.5

# Shared libedgetpu runtime library name used by the Edge TPU delegate. Defined
# here so task 6.2 has a single place to reference when it wires the delegate;
# unused by the CPU-only path implemented in this task.
EDGETPU_SHARED_LIB = "libedgetpu.so.1"


def _import_interpreter():
    """Import the TFLite ``Interpreter`` lazily so the module imports anywhere.

    ``tflite-runtime`` is an architecture/Python-specific wheel that is only
    installed on the target Pi, so importing it at module load would break
    importing this file for hardware-free tests on a dev machine. Deferring the
    import to interpreter construction keeps the module importable everywhere.

    Returns:
        The ``Interpreter`` class from ``tflite_runtime.interpreter``.

    Raises:
        ImportError: If ``tflite_runtime`` is not importable in the current
            interpreter.
    """
    from tflite_runtime.interpreter import Interpreter
    return Interpreter


def _import_load_delegate():
    """Import the TFLite ``load_delegate`` helper lazily.

    Deferred for the same reason as :func:`_import_interpreter`: the
    ``tflite-runtime`` wheel is only installed on the target Pi, so importing it
    at module load would break importing this file for hardware-free tests on a
    dev machine. ``load_delegate`` is the entry point used to attach the Edge
    TPU (``libedgetpu``) delegate to an interpreter.

    Returns:
        The ``load_delegate`` callable from ``tflite_runtime.interpreter``.

    Raises:
        ImportError: If ``tflite_runtime`` is not importable in the current
            interpreter.
    """
    from tflite_runtime.interpreter import load_delegate
    return load_delegate


def edgetpu_model_path(model_path):
    """Derive the Edge-TPU-compiled model path from a CPU model path.

    Edge TPU inference requires a model compiled for the accelerator, named with
    an ``_edgetpu`` suffix by convention (e.g. ``ssd_mobilenet.tflite`` ->
    ``ssd_mobilenet_edgetpu.tflite``). If ``model_path`` already carries the
    ``_edgetpu`` suffix it is returned unchanged so passing an already-compiled
    model is idempotent.

    Args:
        model_path: Filesystem path to the CPU ``.tflite`` model.

    Returns:
        The corresponding ``*_edgetpu.tflite`` path, or ``model_path`` unchanged
        when it already ends in ``_edgetpu.tflite``.
    """
    if model_path.endswith("_edgetpu.tflite"):
        return model_path
    if model_path.endswith(".tflite"):
        return model_path[: -len(".tflite")] + "_edgetpu.tflite"
    # No recognized extension; append the suffix so the path is still distinct.
    return model_path + "_edgetpu"


def load_labels(labels_path):
    """Load COCO class labels from a labels file.

    Supports both plain ``label-per-line`` files and the indexed
    ``"<index> <label>"`` form some COCO label files use. Blank lines are
    skipped. When a line carries a leading integer index, that index is used as
    the label's position so sparse/non-contiguous index files map correctly;
    otherwise labels are assigned sequentially by line order.

    Args:
        labels_path: Filesystem path to the labels file.

    Returns:
        A dict mapping class index (int) to label string (e.g.
        ``{0: "person", 1: "bicycle", ...}``).
    """
    labels = {}
    with open(labels_path, "r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f):
            text = raw.strip()
            if not text:
                continue
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[0].isdigit():
                labels[int(parts[0])] = parts[1].strip()
            else:
                labels[line_no] = text
    return labels


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


def normalize_detections(raw_boxes, raw_classes, raw_scores,
                         frame_width, frame_height, labels,
                         conf_threshold=DEFAULT_CONF_THRESHOLD):
    """Convert raw SSD-MobileNet output into filtered, in-frame Detections.

    This is the pure, interpreter-free core of :meth:`Detector.detect` so it can
    be exercised directly in tests (Properties 3, 4, 5). It performs three jobs
    per raw detection:

    - **Score normalization** (Req 3.2): the raw confidence is clamped into
      ``[0.0, 1.0]`` so every reported Detection carries a valid score.
    - **Bounding-box normalization** (Req 3.2): SSD-MobileNet emits boxes as
      normalized ``[ymin, xmin, ymax, xmax]`` fractions in ``[0, 1]``; each is
      scaled to pixels and clamped into the frame so ``x`` stays in
      ``[0, frame_width]`` and ``y`` in ``[0, frame_height]``. Edges are ordered
      so ``x1 <= x2`` and ``y1 <= y2``.
    - **Confidence filtering** (Req 3.3): a detection is reported if and only if
      its normalized score is ``>= conf_threshold``.

    The ``person`` flag itself is not computed here: it is a property of
    :class:`vision_models.Detection` derived from the label (Req 3.7), so a
    Detection whose resolved label is ``person`` is automatically ``is_person``.

    Args:
        raw_boxes: Sequence of per-detection boxes, each a 4-tuple/sequence of
            normalized ``[ymin, xmin, ymax, xmax]`` fractions (SSD-MobileNet
            output order).
        raw_classes: Sequence of per-detection class indices (floats or ints
            aligned with the labels map).
        raw_scores: Sequence of per-detection confidence scores aligned with
            ``raw_boxes``.
        frame_width: Captured frame width in pixels; bbox x-coords are bounded
            by it (Req 3.2).
        frame_height: Captured frame height in pixels; bbox y-coords are bounded
            by it (Req 3.2).
        labels: Mapping of class index -> label string (from
            :func:`load_labels`). An unknown index resolves to
            ``"class_<index>"``.
        conf_threshold: Minimum score to report, in ``[0.0, 1.0]`` (Req 3.3).

    Returns:
        A list of :class:`vision_models.Detection`, one per raw detection whose
        normalized score is ``>= conf_threshold``, each with a valid score and
        an in-frame pixel bbox.
    """
    detections = []
    for box, cls_idx, raw_score in zip(raw_boxes, raw_classes, raw_scores):
        score = _clamp(float(raw_score), 0.0, 1.0)
        if score < conf_threshold:
            continue

        ymin, xmin, ymax, xmax = (float(box[0]), float(box[1]),
                                  float(box[2]), float(box[3]))

        # Scale normalized fractions to pixels, then clamp into the frame so the
        # bbox never reports coordinates outside the captured dimensions.
        x1 = _clamp(xmin * frame_width, 0.0, frame_width)
        x2 = _clamp(xmax * frame_width, 0.0, frame_width)
        y1 = _clamp(ymin * frame_height, 0.0, frame_height)
        y2 = _clamp(ymax * frame_height, 0.0, frame_height)

        # Order edges so x1<=x2 and y1<=y2 regardless of raw corner ordering.
        left, right = (x1, x2) if x1 <= x2 else (x2, x1)
        top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)

        label = labels.get(int(cls_idx), f"class_{int(cls_idx)}")

        detections.append(
            Detection(
                label=label,
                score=score,
                x1=int(round(left)),
                y1=int(round(top)),
                x2=int(round(right)),
                y2=int(round(bottom)),
            )
        )
    return detections


class Detector:
    """TFLite COCO SSD-MobileNet detector over one frame at a time.

    Loads a TFLite SSD-MobileNet model and its COCO labels, then produces a
    list of :class:`vision_models.Detection` per frame via :meth:`detect`, each
    with a score in ``[0.0, 1.0]`` and a pixel bbox bounded by the frame
    dimensions, filtered to detections at or above the configured confidence
    threshold (Req 3.1-3.3). ``person``-label detections are flagged
    ``is_person`` by the Detection model itself (Req 3.7).

    When ``use_edge_tpu`` is set, :meth:`_load_interpreter` attempts the
    ``libedgetpu`` delegate against the ``*_edgetpu`` model and, if the delegate
    or Coral device is unavailable, falls back to a CPU interpreter while
    printing a clear fallback indication (Req 3.5, 3.6). Whether the accelerator
    is actually in use is exposed on ``edge_tpu_active`` for ``/status`` (task
    6.3).
    """

    def __init__(self, model_path, labels_path, conf_threshold=DEFAULT_CONF_THRESHOLD,
                 use_edge_tpu=False, interpreter_factory=None):
        """Load the model and labels and prepare the interpreter.

        Args:
            model_path: Filesystem path to the ``.tflite`` SSD-MobileNet model.
            labels_path: Filesystem path to the COCO labels file (see
                :func:`load_labels`).
            conf_threshold: Confidence threshold in ``[0.0, 1.0]``; detections
                below it are excluded (Req 3.3). Defaults to
                :data:`DEFAULT_CONF_THRESHOLD` (0.5).
            use_edge_tpu: Whether to attempt the Edge TPU delegate. When set,
                :meth:`_load_interpreter` tries the ``libedgetpu`` delegate with
                the ``*_edgetpu`` model and falls back to CPU on a
                delegate-missing/device-absent error (Req 3.5, 3.6). The actual
                outcome is reflected in :attr:`edge_tpu_active`.
            interpreter_factory: Optional callable
                ``(model_path, use_edge_tpu) -> interpreter`` injected for
                hardware-free tests so ``detect`` can be exercised without a
                real TFLite runtime. When None, the delegate/CPU path is loaded
                lazily. A factory bypasses the delegate attempt, so
                ``edge_tpu_active`` reflects the requested ``use_edge_tpu`` value
                for an injected interpreter.
        """
        self.model_path = model_path
        self.labels_path = labels_path
        self.conf_threshold = _clamp(float(conf_threshold), 0.0, 1.0)
        self.use_edge_tpu = bool(use_edge_tpu)
        self._interpreter_factory = interpreter_factory

        # Whether the Edge TPU accelerator is actually driving inference. Set by
        # _load_interpreter: True only when the delegate loaded successfully,
        # False on CPU (including after a fallback). Reported in /status (6.3).
        self.edge_tpu_active = False

        self.labels = load_labels(labels_path)
        self.interpreter = self._load_interpreter()
        self.interpreter.allocate_tensors()
        self._input_details = self.interpreter.get_input_details()
        self._output_details = self.interpreter.get_output_details()

        # Model input height/width, used to letterbox/resize incoming frames.
        input_shape = self._input_details[0]["shape"]
        self._input_height = int(input_shape[1])
        self._input_width = int(input_shape[2])

    def _load_interpreter(self):
        """Build the TFLite interpreter, using the Edge TPU when available.

        Behaviour depends on ``use_edge_tpu`` (Req 3.5, 3.6):

        - When ``use_edge_tpu`` is False, a plain CPU ``Interpreter`` is built
          for ``model_path`` and :attr:`edge_tpu_active` stays False.
        - When ``use_edge_tpu`` is True, an ``Interpreter`` is constructed with
          the ``libedgetpu`` delegate (:data:`EDGETPU_SHARED_LIB`) against the
          ``*_edgetpu`` model (see :func:`edgetpu_model_path`). On success
          :attr:`edge_tpu_active` becomes True. If the delegate shared library
          is missing or no Coral device is present — surfaced as ``ValueError``
          or ``OSError`` — this falls back to a CPU ``Interpreter`` on the
          original ``model_path``, prints a clear
          "Edge TPU unavailable -> CPU fallback" indication, and leaves
          :attr:`edge_tpu_active` False.

        The ``interpreter_factory`` test seam short-circuits both paths: when a
        factory is injected it is called directly (no delegate attempt), and
        :attr:`edge_tpu_active` mirrors the requested ``use_edge_tpu`` so tests
        can assert the requested mode without a real TFLite runtime.

        Returns:
            A ready (not-yet-allocated) TFLite interpreter. For the Edge TPU path
            it is bound to the delegate; otherwise it is a CPU interpreter.
        """
        if self._interpreter_factory is not None:
            # Injected interpreter bypasses the real delegate load; reflect the
            # requested mode so /status reporting is still exercised in tests.
            self.edge_tpu_active = self.use_edge_tpu
            return self._interpreter_factory(self.model_path, self.use_edge_tpu)

        interpreter_cls = _import_interpreter()

        if not self.use_edge_tpu:
            self.edge_tpu_active = False
            return interpreter_cls(model_path=self.model_path)

        # Edge TPU requested: try the libedgetpu delegate against the compiled
        # *_edgetpu model. A missing delegate .so or absent Coral device raises
        # ValueError/OSError — catch exactly those and degrade to CPU.
        tpu_model_path = edgetpu_model_path(self.model_path)
        try:
            load_delegate = _import_load_delegate()
            delegate = load_delegate(EDGETPU_SHARED_LIB)
            interpreter = interpreter_cls(
                model_path=tpu_model_path,
                experimental_delegates=[delegate],
            )
            self.edge_tpu_active = True
            print(f"Detector: Edge TPU delegate loaded; using {tpu_model_path}")
            return interpreter
        except (ValueError, OSError) as exc:
            # Delegate shared lib missing or no Coral device attached. Fall back
            # to CPU on the original (non-edgetpu) model and make the degrade
            # explicit so the operator knows the accelerator is not in use.
            print(
                "Detector: Edge TPU unavailable -> CPU fallback "
                f"({type(exc).__name__}: {exc}); using {self.model_path}"
            )
            self.edge_tpu_active = False
            return interpreter_cls(model_path=self.model_path)

    def _prepare_input(self, frame):
        """Resize ``frame`` to the model input size and shape it for inference.

        Args:
            frame: An ``HxWx3`` frame array (e.g. from ``capture_array()``).

        Returns:
            A batched array of shape ``(1, input_height, input_width, 3)`` with
            the interpreter's expected dtype.
        """
        import numpy as np

        arr = np.asarray(frame)
        # Nearest-neighbour resize to the model's expected input dimensions,
        # avoiding an extra image library dependency in this leaf module.
        src_h, src_w = arr.shape[0], arr.shape[1]
        row_idx = (np.arange(self._input_height) * src_h // self._input_height)
        col_idx = (np.arange(self._input_width) * src_w // self._input_width)
        resized = arr[row_idx][:, col_idx]

        dtype = self._input_details[0]["dtype"]
        if dtype == np.float32:
            # Float models expect normalized [-1, 1] input (SSD-MobileNet).
            resized = (resized.astype(np.float32) - 127.5) / 127.5
        else:
            resized = resized.astype(dtype)
        return resized[np.newaxis, ...]

    def _read_outputs(self):
        """Read the four SSD-MobileNet output tensors from the interpreter.

        SSD-MobileNet post-processing emits four output tensors: boxes
        ``[1, N, 4]`` as normalized ``[ymin, xmin, ymax, xmax]``, class indices
        ``[1, N]``, scores ``[1, N]``, and the detection count ``[1]``. Output
        tensor ordering varies between model exports, so tensors are identified
        by shape rather than a fixed index.

        Returns:
            A tuple ``(boxes, classes, scores, count)`` of the per-detection
            arrays with the leading batch dimension removed.
        """
        import numpy as np

        # Collect each output tensor with its post-process op name so we can
        # tell the two same-shaped [N] tensors (classes vs scores) apart. The
        # standard TFLite_Detection_PostProcess op emits, by name suffix:
        #   ""  -> boxes [N,4], ":1" -> classes [N], ":2" -> scores [N],
        #   ":3" -> count [1]
        # NOTE: get_output_details() order is NOT guaranteed, and classes and
        # scores have identical shape [N], so they MUST be distinguished by name
        # (or value) — never by positional "first [N] seen". Assigning
        # positionally silently swapped classes<->scores on this model, which
        # made every detection read class index int(score)==0 -> "person".
        boxes = classes = scores = count = None
        candidates = []  # (name, squeezed array) for ambiguous [N] tensors
        for detail in self._output_details:
            tensor = self.interpreter.get_tensor(detail["index"])
            raw_shape = tuple(int(x) for x in tensor.shape)
            squeezed = np.squeeze(tensor, axis=0) if tensor.ndim > 1 else tensor
            name = detail.get("name", "")

            # Classify by the ORIGINAL (pre-squeeze) rank/shape and the op name,
            # NOT by the squeezed size — at N==1 the classes/scores tensors are
            # also size 1, so a size-based count test would misclaim them.
            # boxes:   original last dim == 4            (e.g. [1, N, 4])
            # count:   original rank 1                   (e.g. [1])
            # classes: op name ends ":1"                 (e.g. [1, N])
            # scores:  op name ends ":2"                 (e.g. [1, N])
            if raw_shape and raw_shape[-1] == 4 and len(raw_shape) >= 2:
                boxes = squeezed
            elif len(raw_shape) == 1 or name.endswith(":3"):
                count = squeezed
            elif name.endswith(":1"):
                classes = squeezed
            elif name.endswith(":2"):
                scores = squeezed
            else:
                candidates.append((name, np.atleast_1d(squeezed)))

        # If the names were not the canonical ":1"/":2" (some exports differ),
        # distinguish the remaining [N] tensors by VALUE: scores lie in [0, 1],
        # whereas class indices are integer-valued and routinely exceed 1.
        for name, arr in candidates:
            if scores is None and arr.size and float(np.max(arr)) <= 1.0 \
                    and float(np.min(arr)) >= 0.0:
                scores = arr
            elif classes is None:
                classes = arr
            elif scores is None:
                scores = arr

        # Keep classes/scores at least 1-D so a single detection (N==1, squeezed
        # to a scalar) is still iterable/sliceable downstream.
        if classes is not None:
            classes = np.atleast_1d(classes)
        if scores is not None:
            scores = np.atleast_1d(scores)
        if boxes is not None and boxes.ndim == 1:
            boxes = boxes.reshape(1, -1)

        return boxes, classes, scores, count

    def detect(self, frame):
        """Run inference on one frame and return filtered Detections.

        Prepares the frame, runs the interpreter, reads the raw SSD-MobileNet
        output, and normalizes it via :func:`normalize_detections` into a list
        of in-frame, confidence-filtered Detections (Req 3.1-3.3). The input
        frame's own dimensions bound the reported pixel bboxes (Req 3.2);
        ``person`` detections are flagged ``is_person`` by the Detection model
        (Req 3.7).

        Args:
            frame: An ``HxWx3`` frame array (e.g. from ``capture_array()``).

        Returns:
            A list of :class:`vision_models.Detection` with score in
            ``[0.0, 1.0]`` and a pixel bbox bounded by the frame dimensions,
            each at or above the configured confidence threshold.
        """
        import numpy as np

        arr = np.asarray(frame)
        frame_height, frame_width = int(arr.shape[0]), int(arr.shape[1])

        input_data = self._prepare_input(arr)
        self.interpreter.set_tensor(self._input_details[0]["index"], input_data)
        self.interpreter.invoke()

        boxes, classes, scores, count = self._read_outputs()

        # Honour the model's reported detection count when present so trailing
        # padding slots are not treated as real detections.
        if count is not None:
            try:
                n = int(np.asarray(count).reshape(-1)[0])
                boxes, classes, scores = boxes[:n], classes[:n], scores[:n]
            except (ValueError, IndexError):
                pass

        return normalize_detections(
            boxes, classes, scores,
            frame_width, frame_height,
            self.labels, self.conf_threshold,
        )
