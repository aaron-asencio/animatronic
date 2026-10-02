"""Camera_Service — non-root camera capture loop and latest-frame buffer.

This module is the long-lived, **non-root** process that owns the head-mounted
Raspberry Pi Camera Module 3 Noir, captures frames continuously, and exposes the
single most recent frame to the rest of the system. It confines no privilege:
unlike the servo/GPIO Servo_Writers it never needs root, so it is launched with
the project venv directly (``.venv/bin/python3 src/camera_service.py``, no
``sudo``) — keeping root confined to the servo processes (Req 1.7, 9.6).

Scope now covers Build Step 1 (the capture loop + latest-frame buffer +
init/capture error handling), Build Step 2's Camera_Service HTTP interface
(task 5.1): a localhost-only Flask app serving ``GET /stream`` (MJPEG) and
``GET /status``, AND Build Step 3's Detector wiring (task 6.3): a background
detection thread, the ``GET /detections`` endpoint, real detection overlays on
``/stream?overlay=1``, and the real ``edge_tpu`` state in ``/status``. The
``ir`` field in ``/status`` remains a placeholder for task 13.1 to fill in.

Detector integration is **optional and graceful** (Req 3.4, 3.6): the Detector
is injectable for tests (via ``detector`` or ``detector_factory``), and if no
model is configured or Detector construction fails, Camera_Service keeps running
CPU-only with ``/detections`` returning an empty detection list and ``/status``
reporting ``edge_tpu: False``.

The HTTP app binds **only** to the loopback address ``127.0.0.1`` so camera
frames and detections never leave the device (Req 9.7).

Design notes:

- ``LatestFrame`` is a small, dependency-free abstraction (a single
  lock-guarded slot, depth-1, newest-wins) deliberately split out so it can be
  unit/property tested directly without a camera (Property 1 — newest-wins,
  depth 1). It never buffers more than one frame (Req 1.4): each ``push``
  overwrites the previous slot.
- picamera2 is imported **lazily and guarded** (``_import_picamera2``) because
  on bookworm it is provided by apt (``python3-picamera2``), not pip, and is not
  in the project venv. Importing it lazily lets this module be imported for
  tests on a machine without picamera2.
- cv2 (opencv-python-headless) is likewise imported **lazily and guarded**
  (``_import_cv2``) — it is only needed to JPEG-encode frames for MJPEG and to
  draw overlay boxes, so deferring the import keeps the module importable for
  hardware-free tests on a machine without opencv.
- Flask is imported lazily inside :func:`build_app` for the same reason, so the
  capture-loop classes above can be imported and unit/property tested without
  the web stack installed.
- The Detector is imported **lazily and guarded** (``_import_detector``) so
  Camera_Service still imports on a machine without ``tflite-runtime``. When the
  import or Detector construction fails, the service degrades gracefully to
  no-detection operation rather than crashing (Req 3.6).
- Debug output uses ``print()`` to stay consistent with the rest of the
  codebase; there is no logging framework.

No servo code lives here, so there are no SAFE_LIMITS/collision concerns in this
module.
"""

import sys
import time
import threading

import constants
from vision_models import TrackingConfig, DEFAULT_IR_AMBIENT_THRESHOLD


# Human-readable name for the camera device, used in init/capture error messages
# so the operator knows which device failed (Req 1.5, 1.6).
CAMERA_DEVICE_NAME = "Raspberry Pi Camera Module 3 Noir (picamera2)"

# How long, in seconds, initialization is allowed to take before it is treated
# as a failure and the process exits non-zero (Req 1.1, 1.5).
INIT_TIMEOUT_S = 10.0

# Non-zero exit status used when the camera cannot be initialized, so the
# supervising Control_Panel can tell init failed while it keeps running (Req 1.5).
INIT_FAILURE_EXIT_CODE = 1

# IR auto-switch hysteresis margin (luminance units on the 0-255 mean-luminance
# scale). The single configured ``ir_ambient_threshold`` is split into a LOW and
# a HIGH threshold ``threshold ± IR_HYSTERESIS_MARGIN`` so the illuminator turns
# ON when ambient falls below LOW and OFF when it rises above HIGH — two
# thresholds, never one, so the IR never flaps on/off around a single boundary
# (Req 10.4).
IR_HYSTERESIS_MARGIN = 5.0

# How often, in seconds, the IR auto-switch loop re-evaluates ambient light.
IR_AUTO_POLL_S = 1.0

# The fixed, allowlisted set of IR modes accepted by ``set_mode`` / ``POST /ir``
# (Req 10.3). Mirrors ``vision_models.IR_MODES`` but kept local so the IR owner
# validates against its own source of truth.
IR_MODE_ON = "on"
IR_MODE_OFF = "off"
IR_MODE_AUTO = "auto"
IR_MODES = (IR_MODE_ON, IR_MODE_OFF, IR_MODE_AUTO)


def ir_auto_decision(current_on, luminance, low, high):
    """Decide the IR_Illuminator on/off state under ambient-light hysteresis.

    Pure, deterministic hysteresis function for IR auto-switch (Property 22,
    Req 10.4). Two thresholds — never one — prevent flapping around a single
    boundary:

    - enable (return True) when ``luminance`` is strictly below ``low``;
    - disable (return False) when ``luminance`` is strictly above ``high``;
    - otherwise hold the current state (return ``current_on``) inside the
      ``[low, high]`` hysteresis band.

    Having no side effects and reading no hardware makes this directly testable
    across many inputs (the property test feeds it luminance sequences and
    asserts enable-below / disable-above / hold-between).

    Args:
        current_on: The IR illuminator's current on/off state (bool).
        luminance: The measured ambient light level (mean frame luminance on a
            0-255 scale, or a light-sensor reading on the same scale).
        low: The LOW threshold; ambient below this enables IR.
        high: The HIGH threshold; ambient above this disables IR. Expected
            ``>= low`` (a sane split of the configured threshold guarantees it).

    Returns:
        The new IR on/off state (bool): True to enable, False to disable, or
        ``current_on`` unchanged when ``luminance`` lies within ``[low, high]``.
    """
    if luminance < low:
        return True
    if luminance > high:
        return False
    return current_on


class IRController:
    """Owns the IR_Illuminator: mode, on/off state, and the GPIO output.

    The IR illuminator lets the NoIR camera capture a usable image in darkness
    (Req 10.1). This controller is owned by the non-root Camera_Service and
    exposes three modes (Req 10.3):

    - ``on``   — illuminator forced on;
    - ``off``  — illuminator forced off;
    - ``auto`` — illuminator switched by ambient light under hysteresis: on when
      ambient falls below the LOW threshold, off when it rises above the HIGH
      threshold (Req 10.4), using the pure :func:`ir_auto_decision`.

    Graceful degrade (Req 10.5): the gpiozero output is imported and constructed
    lazily and guarded. If gpiozero is missing or the pin cannot be claimed, the
    controller marks the hardware **unavailable** and keeps running — the camera
    continues to operate in available light and ``/status`` reports IR as
    unavailable rather than crashing.

    This is a GPIO LED, not a servo: there are no SAFE_LIMITS, no collision
    combinations, and nothing to simulate — just a binary output.
    """

    def __init__(self, mode=IR_MODE_AUTO, low=None, high=None,
                 ambient_threshold=DEFAULT_IR_AMBIENT_THRESHOLD,
                 pin=None, led_factory=None):
        """Initialise the IR controller and attempt to claim the GPIO output.

        The gpiozero output is constructed here, guarded: on any failure the
        controller degrades to unavailable and keeps running (Req 10.5).

        Args:
            mode: Initial IR mode, one of ``on``/``off``/``auto``. Any other
                value falls back to ``auto`` (validated like ``set_mode``,
                Req 10.3).
            low: Optional explicit LOW hysteresis threshold. When None it is
                derived as ``ambient_threshold - IR_HYSTERESIS_MARGIN``.
            high: Optional explicit HIGH hysteresis threshold. When None it is
                derived as ``ambient_threshold + IR_HYSTERESIS_MARGIN``.
            ambient_threshold: The configured auto-switch ambient threshold
                (mean luminance, 0-255) the LOW/HIGH band is derived around when
                ``low``/``high`` are not given explicitly (Req 10.4).
            pin: The BCM pin the illuminator is wired to. When None the shared
                ``constants.IR_ILLUMINATOR_PIN`` is used.
            led_factory: Optional one-arg callable ``factory(pin) -> led`` used
                instead of the real gpiozero ``LED``, injected for hardware-free
                tests. The returned object must provide ``on()``, ``off()`` and
                ``close()``.
        """
        self._lock = threading.Lock()
        self._mode = mode if mode in IR_MODES else IR_MODE_AUTO
        if mode not in IR_MODES:
            print(
                f"IRController: unknown mode '{mode}'; falling back to "
                f"'{IR_MODE_AUTO}'"
            )

        # Derive the two hysteresis thresholds from the configured ambient
        # threshold unless explicit values were supplied. Order is enforced so
        # low <= high even if the caller passes a crossed pair.
        derived_low = ambient_threshold - IR_HYSTERESIS_MARGIN
        derived_high = ambient_threshold + IR_HYSTERESIS_MARGIN
        self._low = derived_low if low is None else low
        self._high = derived_high if high is None else high
        if self._low > self._high:
            self._low, self._high = self._high, self._low

        self._on = False
        self._led = None
        self._available = False

        pin = constants.IR_ILLUMINATOR_PIN if pin is None else pin
        self._pin = pin
        self._init_led(pin, led_factory)

        # Apply the initial mode to the (possibly unavailable) hardware so a
        # forced on/off takes effect immediately.
        self._apply_mode_locked()

    def _init_led(self, pin, led_factory):
        """Construct the GPIO output, degrading to unavailable on any failure.

        Args:
            pin: The BCM pin to drive.
            led_factory: Optional ``factory(pin) -> led`` for tests; when None
                the real gpiozero ``LED`` is imported lazily and used.
        """
        try:
            if led_factory is not None:
                self._led = led_factory(pin)
            else:
                led_cls = _import_gpiozero_led()
                self._led = led_cls(pin)
            self._available = True
            print(f"IRController: IR illuminator ready on pin {pin}.")
        except Exception as e:
            # Graceful degrade: no IR hardware -> keep running in available
            # light and report unavailable (Req 10.5).
            self._led = None
            self._available = False
            print(
                f"IRController: IR illuminator unavailable on pin {pin} ({e}); "
                "continuing in available light."
            )

    @property
    def available(self):
        """Whether the IR illuminator hardware is present and usable.

        Returns:
            True when the GPIO output was claimed successfully; False when the
            hardware is absent/failed and the service is degraded (Req 10.5).
        """
        with self._lock:
            return self._available

    @property
    def mode(self):
        """The current IR mode (``on``/``off``/``auto``)."""
        with self._lock:
            return self._mode

    @property
    def is_on(self):
        """Whether the IR illuminator is currently commanded on."""
        with self._lock:
            return self._on

    def set_mode(self, mode):
        """Set the IR mode, validated against the fixed ``{on,off,auto}`` set.

        The mode is checked against the allowlist before anything is applied
        (Req 10.3); an invalid mode is rejected and the current mode is left
        unchanged. On a valid ``on``/``off`` the hardware is driven immediately;
        on ``auto`` the next auto evaluation drives it.

        Args:
            mode: The requested mode string.

        Returns:
            True when ``mode`` was accepted and applied; False when it was
            rejected as invalid (caller returns an "invalid value" response).
        """
        if mode not in IR_MODES:
            print(f"IRController: rejected invalid mode {mode!r}")
            return False
        with self._lock:
            self._mode = mode
            self._apply_mode_locked()
        return True

    def _apply_mode_locked(self):
        """Drive the hardware for forced modes; auto is left to the auto loop.

        Must be called with ``self._lock`` held. For ``on``/``off`` this sets
        the output directly; for ``auto`` the on/off state is decided later by
        :meth:`evaluate_auto`.
        """
        if self._mode == IR_MODE_ON:
            self._set_on_locked(True)
        elif self._mode == IR_MODE_OFF:
            self._set_on_locked(False)

    def _set_on_locked(self, want_on):
        """Set the illuminator on/off, writing the GPIO output if available.

        Must be called with ``self._lock`` held. Reports a state change via
        ``print()`` so transitions are visible in logs; ``/status`` reflects the
        new state for the Control_Panel to poll (Req 10.6). A hardware write
        failure degrades the controller to unavailable rather than raising.

        Args:
            want_on: The desired on/off state.
        """
        changed = want_on != self._on
        self._on = want_on
        if self._available and self._led is not None:
            try:
                if want_on:
                    self._led.on()
                else:
                    self._led.off()
            except Exception as e:
                # A write failure means the hardware is no longer usable; degrade.
                self._available = False
                print(f"IRController: IR write failed ({e}); marking unavailable.")
        if changed:
            print(
                f"IRController: IR illuminator -> "
                f"{'ON' if want_on else 'OFF'} (mode={self._mode}, "
                f"available={self._available})"
            )

    def evaluate_auto(self, luminance):
        """Apply the hysteresis decision in ``auto`` mode for a luminance value.

        A no-op unless the current mode is ``auto``: forced ``on``/``off`` modes
        ignore ambient light. In ``auto`` the pure :func:`ir_auto_decision`
        computes the new state from the current state and the LOW/HIGH band, and
        the result is driven to the hardware (Req 10.4).

        Args:
            luminance: The measured ambient light level (mean frame luminance on
                a 0-255 scale), or None when no frame/sensor reading is
                available yet (then the state is held).
        """
        if luminance is None:
            return
        with self._lock:
            if self._mode != IR_MODE_AUTO:
                return
            new_on = ir_auto_decision(self._on, luminance, self._low, self._high)
            if new_on != self._on:
                self._set_on_locked(new_on)

    def status(self):
        """Return a JSON-friendly IR status for ``/status`` (Req 10.5, 10.6).

        Returns:
            When the hardware is available, a dict ``{"mode", "on", "available"}``
            reflecting the current mode, on/off state, and availability so the
            Control_Panel can render and poll IR state changes (Req 10.6). When
            the hardware is unavailable, the string ``"unavailable"`` so the
            panel shows "IR unavailable" (Req 10.5).
        """
        with self._lock:
            if not self._available:
                return "unavailable"
            return {
                "mode": self._mode,
                "on": self._on,
                "available": True,
            }

    def close(self):
        """Turn the illuminator off and release the GPIO pin. Never raises."""
        with self._lock:
            if self._led is None:
                return
            led = self._led
            self._led = None
            self._available = False
            self._on = False
            for method in ("off", "close"):
                fn = getattr(led, method, None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception as e:
                    print(f"IRController: error during IR {method}() ({e})")


def _import_picamera2():
    """Import picamera2 lazily so the module imports without the camera stack.

    picamera2 is provided by apt (``python3-picamera2``) on bookworm and is not
    part of the project's pip venv, so importing it at module load would break
    importing this file for hardware-free tests. Deferring the import to camera
    initialization keeps the module importable everywhere.

    Returns:
        The ``Picamera2`` class from the ``picamera2`` package.

    Raises:
        ImportError: If picamera2 is not importable in the current interpreter
            (e.g. a dev machine, or the venv was not created with
            ``--system-site-packages``).
    """
    from picamera2 import Picamera2
    return Picamera2


def _import_cv2():
    """Import cv2 (opencv-python-headless) lazily so the module imports without it.

    cv2 is only needed by the HTTP layer to JPEG-encode frames for the MJPEG
    ``/stream`` and to draw overlay boxes/labels. Deferring the import to the
    point of use keeps this module importable for hardware-free tests on a
    machine without opencv installed.

    Returns:
        The imported ``cv2`` module.

    Raises:
        ImportError: If opencv is not importable in the current interpreter.
    """
    import cv2
    return cv2


def _import_gpiozero_led():
    """Import the gpiozero ``LED`` class lazily and guarded.

    The IR_Illuminator is driven as a simple on/off GPIO output (an LED, not a
    servo — there are no SAFE_LIMITS/collision concerns here). gpiozero is only
    importable on the Pi with a working pin factory, so importing it at module
    load would break importing this file for hardware-free tests and would
    prevent the service from running at all when the IR pin is unavailable.
    Deferring the import to IR construction lets the service import everywhere
    and gracefully degrade to "IR unavailable" when the hardware is absent or
    fails (Req 10.5).

    Returns:
        The ``LED`` class from the ``gpiozero`` package.

    Raises:
        ImportError: If gpiozero is not importable in the current interpreter.
    """
    from gpiozero import LED
    return LED


def _import_detector():
    """Import the :class:`detector.Detector` class lazily and guarded.

    The Detector pulls in ``tflite-runtime``, an architecture/Python-specific
    wheel installed only on the target Pi. Importing it at module load would
    break importing this file for hardware-free tests on a dev machine, so the
    import is deferred to the point where a real Detector is actually
    constructed. Callers wrap this in a ``try``/``except`` so a missing runtime
    degrades to no-detection operation rather than crashing (Req 3.6).

    Returns:
        The ``Detector`` class from the ``detector`` module.

    Raises:
        ImportError: If ``detector`` (or its ``tflite_runtime`` dependency) is
            not importable in the current interpreter.
    """
    from detector import Detector
    return Detector


class LatestFrame:
    """A single lock-guarded latest-frame slot: depth-1, newest-wins.

    This is the exposed-frame buffer for Camera_Service. It holds at most one
    frame; every ``push`` replaces the stored frame so a reader always gets the
    most recent one and no more than a single frame is ever buffered (Req 1.2,
    1.4). Access is guarded by a ``threading.Lock`` so the capture thread can
    push while readers get concurrently without tearing.

    The abstraction is intentionally tiny and camera-free so it can be tested
    directly (Property 1: newest-wins, depth 1).
    """

    def __init__(self):
        """Initialise an empty latest-frame slot."""
        self._lock = threading.Lock()
        self._frame = None
        self._count = 0
        # Monotonic timestamp of the most recent push, used by /status to
        # compute last_frame_age_s. None until the first frame is pushed.
        self._timestamp = None

    def push(self, frame):
        """Store ``frame`` as the newest frame, discarding any previous one.

        Depth-1, newest-wins: the previously stored frame (if any) is dropped,
        so the buffer never holds more than one frame (Req 1.4). A monotonic
        timestamp is recorded alongside each push so readers (e.g. ``/status``)
        can tell how stale the exposed frame is.

        Args:
            frame: The newly captured frame to expose (e.g. a numpy array from
                ``capture_array()``). May be any object; the buffer does not
                inspect or copy it.
        """
        with self._lock:
            self._frame = frame
            self._count += 1
            self._timestamp = time.monotonic()

    def get_with_timestamp(self):
        """Return ``(frame, timestamp)`` for the most recent push.

        Returns:
            A ``(frame, timestamp)`` tuple where ``timestamp`` is the
            ``time.monotonic()`` value captured when the frame was pushed, or
            ``(None, None)`` when no frame has been pushed yet.
        """
        with self._lock:
            return self._frame, self._timestamp

    @property
    def timestamp(self):
        """Monotonic timestamp of the most recent push, or None if never pushed.

        Returns:
            The ``time.monotonic()`` value recorded by the last ``push``, or
            ``None`` when no frame has been pushed.
        """
        with self._lock:
            return self._timestamp

    def get(self):
        """Return the most recently pushed frame, or None if none pushed yet.

        Returns:
            The newest frame previously passed to ``push``, or ``None`` when no
            frame has been pushed.
        """
        with self._lock:
            return self._frame

    @property
    def count(self):
        """Total number of frames ever pushed.

        Useful for liveness/age checks and tests. This is a monotonic counter,
        not the number of buffered frames (which is always at most one).

        Returns:
            The number of ``push`` calls made so far.
        """
        with self._lock:
            return self._count


class LatestDetections:
    """A single lock-guarded latest-detections slot, keyed to a frame id.

    Mirrors :class:`LatestFrame` for detection results: it holds the most recent
    detection pass keyed to the frame it was computed from, plus the frame
    dimensions and a wall-clock timestamp. Depth-1, newest-wins — every
    ``push`` replaces the stored result so a reader (``/detections``, the
    ``/stream`` overlay) always gets the latest.

    It starts in a well-defined empty state (``frame_id`` 0, empty detection
    list), so ``/detections`` returns a valid, empty payload even before the
    first detection pass runs or when no Detector is configured (Req 3.4, 3.6).
    """

    def __init__(self):
        """Initialise an empty detections slot."""
        self._lock = threading.Lock()
        self._detections = []
        self._frame_id = 0
        self._width = 0
        self._height = 0
        self._ts = None

    def push(self, frame_id, width, height, detections, ts=None):
        """Store a detection pass, discarding any previous one.

        Depth-1, newest-wins: the previously stored result (if any) is dropped
        so the buffer never holds more than the latest detection pass.

        Args:
            frame_id: The capture frame id these detections were computed from,
                tying the result to a specific frame for the overlay and
                Tracking_Mode.
            width: Width in pixels of the frame the detections were computed on.
            height: Height in pixels of the frame the detections were computed
                on.
            detections: The list of :class:`vision_models.Detection` for this
                frame. May be empty.
            ts: Optional wall-clock timestamp (``time.time()``) for the pass;
                defaults to ``time.time()`` when None.
        """
        with self._lock:
            self._frame_id = frame_id
            self._width = width
            self._height = height
            self._detections = list(detections)
            self._ts = time.time() if ts is None else ts

    def snapshot(self):
        """Return the latest detection pass as a plain dict.

        Returns:
            A dict with keys ``frame_id`` (int), ``width`` (int), ``height``
            (int), ``ts`` (float or None), and ``detections`` — the list of
            :class:`vision_models.Detection` objects for the latest pass.
        """
        with self._lock:
            return {
                "frame_id": self._frame_id,
                "width": self._width,
                "height": self._height,
                "ts": self._ts,
                "detections": list(self._detections),
            }

    def get_detections(self):
        """Return just the latest list of detections.

        Returns:
            A copy of the latest list of :class:`vision_models.Detection`
            objects (empty when no pass has run yet).
        """
        with self._lock:
            return list(self._detections)


class CameraService:
    """Owns the picamera2 stream and the continuous capture loop.

    Configures a picamera2 video stream at the configured resolution/fps,
    then runs a background daemon thread that continuously calls
    ``capture_array()`` and pushes each frame into a :class:`LatestFrame` slot
    (Req 1.1, 1.2, 1.3). The newest frame is exposed via :meth:`get_frame`.

    Optionally runs a Detector over captured frames on a second background
    thread, storing the latest detections (keyed to a frame id) for the
    ``/detections`` endpoint, the ``/stream`` overlay, and Tracking_Mode (task
    6.3, Req 3.4). The Detector is **optional and graceful**: when no model is
    configured or Detector construction fails, the service keeps running with an
    empty detection list and reports ``edge_tpu: False`` (Req 3.6).

    Error handling:

    - Initialization that fails or does not complete within ``INIT_TIMEOUT_S``
      is reported with the camera device name and causes a non-zero exit so the
      Control_Panel keeps running (Req 1.1, 1.5).
    - A capture failure *after* successful init is reported but does not crash
      the loop; the last successfully captured frame is retained as the exposed
      frame (Req 1.6).
    - A Detector construction or per-frame inference failure is reported but
      never crashes the service; detections simply fall back to empty (Req 3.6).
    """

    def __init__(self, config=None, picamera2_factory=None, detector=None,
                 detector_factory=None, ir_controller=None):
        """Initialise the service (does not start the camera or detector yet).

        Args:
            config: A :class:`vision_models.TrackingConfig` supplying the
                capture ``resolution`` and ``capture_fps``. Defaults to a fresh
                ``TrackingConfig`` (whose defaults sustain >= 5 fps, Req 1.3).
            picamera2_factory: Optional zero-arg callable returning a configured
                picamera2-like camera object, injected for hardware-free tests.
                When None, the real ``Picamera2`` class is imported lazily and
                used.
            detector: Optional pre-built Detector-like object with a
                ``detect(frame) -> list[Detection]`` method and an
                ``edge_tpu_active`` attribute, injected for tests. When
                provided, it takes precedence over ``detector_factory``.
            detector_factory: Optional zero-arg callable returning a
                Detector-like object, called once at :meth:`start`. A factory
                lets construction (which may import ``tflite-runtime``) be
                deferred and guarded; if it raises, the service degrades to
                no-detection operation (Req 3.6). When both ``detector`` and
                ``detector_factory`` are None, no detection runs and
                ``/detections`` returns an empty list.
            ir_controller: Optional pre-built :class:`IRController` injected for
                tests. When None, one is constructed from the config's
                ``ir_mode`` and ``ir_ambient_threshold``; its GPIO output is
                claimed lazily and guarded, degrading to "IR unavailable" when
                the hardware is absent/fails (Req 10.5).
        """
        self._config = config or TrackingConfig()
        self._picamera2_factory = picamera2_factory
        self._latest = LatestFrame()
        self._camera = None
        self._capture_thread = None
        self._stop_event = threading.Event()
        # Set once the first frame has been captured, so start() can confirm the
        # camera actually began producing frames within the init timeout.
        self._first_frame_event = threading.Event()
        # Retained most-recent good frame, so a capture failure can keep exposing
        # the last good frame rather than clobbering it (Req 1.6).
        self._last_good_frame = None
        # Set once the camera device has been opened/configured without error, so
        # /status can report camera_ok independently of whether frames have begun
        # flowing yet.
        self._camera_ok = False

        # Optional Detector integration (task 6.3). The detector is OPTIONAL: if
        # none is injected/built the service runs CPU-only with no detections.
        self._detector = detector
        self._detector_factory = detector_factory
        self._detections = LatestDetections()
        self._detect_thread = None
        # Monotonically increasing id assigned to each captured frame, so a
        # detection pass can be tied to the specific frame it ran on.
        self._frame_id = 0
        self._frame_id_lock = threading.Lock()

        # IR_Illuminator control (task 13.1). Owned here in the non-root
        # Camera_Service (Req 10). The initial mode comes from the config's
        # ir_mode, and the auto-switch hysteresis band is derived around the
        # config's ir_ambient_threshold. Hardware is claimed lazily/guarded and
        # degrades to "unavailable" on failure (Req 10.5).
        self._ir = ir_controller or IRController(
            mode=self._config.ir_mode,
            ambient_threshold=self._config.ir_ambient_threshold,
        )
        self._ir_thread = None

    def _create_camera(self):
        """Create and configure the picamera2 video stream.

        Builds a video configuration at the configured resolution and frame
        rate (frame duration derived from ``capture_fps``) and starts the
        camera. A test factory, when provided, is responsible for returning an
        already-usable camera object.

        Returns:
            The started camera object.
        """
        if self._picamera2_factory is not None:
            return self._picamera2_factory()

        picamera2_cls = _import_picamera2()
        camera = picamera2_cls()
        width, height = self._config.resolution
        # Frame duration (microseconds) corresponding to the configured fps,
        # applied as both the min and max so the sensor holds the target rate.
        frame_us = int(1_000_000 / self._config.capture_fps)
        video_config = camera.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"},
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        camera.configure(video_config)
        camera.start()
        return camera

    def start(self):
        """Initialize the camera and start the background capture loop.

        Attempts to create/configure/start the camera and spawn the capture
        thread, then waits up to ``INIT_TIMEOUT_S`` for the first frame to
        arrive so "capturing within 10 seconds" is actually confirmed, not just
        assumed (Req 1.1). On any initialization failure, or if no frame arrives
        within the timeout, an initialization error naming the camera device is
        printed and the process exits non-zero so the Control_Panel keeps
        running (Req 1.5).
        """
        start_t = time.monotonic()
        try:
            self._camera = self._create_camera()
        except Exception as e:
            # Init failure: name the device as the failure source and exit
            # non-zero (Req 1.5).
            self._fail_init(f"could not open/initialize camera ({e})")
            return

        # The device opened/configured cleanly; /status can report camera_ok
        # even before the first frame has been pushed.
        self._camera_ok = True

        # Capture runs on a daemon thread so the process can exit cleanly.
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="camera-capture", daemon=True
        )
        self._capture_thread.start()

        # Resolve the (optional) Detector up front so /status can report the
        # real Edge TPU state immediately, then run detection on its own daemon
        # thread. A missing/failed Detector degrades to no detection (Req 3.6).
        self._detector = self._build_detector()
        if self._detector is not None:
            self._detect_thread = threading.Thread(
                target=self._detect_loop, name="camera-detect", daemon=True
            )
            self._detect_thread.start()

        # Run the IR auto-switch loop on its own daemon thread so ambient-light
        # evaluation is independent of capture/detection cadence (task 13.1).
        # In forced on/off modes the loop is effectively idle (evaluate_auto is
        # a no-op); in auto mode it applies the hysteresis each tick (Req 10.4).
        self._ir_thread = threading.Thread(
            target=self._ir_loop, name="camera-ir", daemon=True
        )
        self._ir_thread.start()

        # Confirm the camera actually started producing frames within the init
        # budget (Req 1.1). Account for any time already spent opening it.
        remaining = INIT_TIMEOUT_S - (time.monotonic() - start_t)
        if remaining <= 0 or not self._first_frame_event.wait(timeout=remaining):
            self._fail_init(
                "camera opened but produced no frame within "
                f"{INIT_TIMEOUT_S:.0f}s"
            )
            return

        elapsed = time.monotonic() - start_t
        print(
            f"Camera_Service: {CAMERA_DEVICE_NAME} initialized and capturing "
            f"({self._config.resolution[0]}x{self._config.resolution[1]} @ "
            f"{self._config.capture_fps} fps) in {elapsed:.2f}s"
        )

    def _fail_init(self, reason):
        """Report an initialization failure and exit the process non-zero.

        The message names the camera device as the failure source so the
        operator can see which device failed, then exits with a non-zero status
        so the supervising Control_Panel can detect the failure while it keeps
        running (Req 1.5).

        Args:
            reason: A short description of what went wrong, appended to the
                device-named prefix.
        """
        print(
            f"Camera_Service INIT ERROR: {CAMERA_DEVICE_NAME}: {reason}. "
            f"Exiting; the Control_Panel keeps running.",
            file=sys.stderr,
        )
        self._stop_event.set()
        self._close_camera()
        sys.exit(INIT_FAILURE_EXIT_CODE)

    def _capture_loop(self):
        """Continuously capture frames into the latest-frame slot.

        Runs on the background thread until :meth:`stop` is called. Each
        iteration captures one frame via ``capture_array()`` and pushes it,
        newest-wins, into the latest-frame slot (Req 1.2, 1.4). A capture
        failure after successful init is reported but does not stop the loop or
        clobber the exposed frame — the last good frame is retained (Req 1.6).
        """
        while not self._stop_event.is_set():
            try:
                frame = self._camera.capture_array()
            except Exception as e:
                # Capture failure after init: report it, retain the last good
                # frame as the exposed frame, and keep trying (Req 1.6).
                print(
                    f"Camera_Service CAPTURE ERROR: {CAMERA_DEVICE_NAME}: "
                    f"frame capture failed ({e}); retaining last good frame.",
                    file=sys.stderr,
                )
                # Brief backoff so a persistent error does not spin the CPU.
                time.sleep(0.1)
                continue

            self._last_good_frame = frame
            with self._frame_id_lock:
                self._frame_id += 1
            self._latest.push(frame)
            if not self._first_frame_event.is_set():
                self._first_frame_event.set()

    def _build_detector(self):
        """Resolve the Detector to run, or None for no-detection operation.

        Prefers an injected ``detector`` instance; otherwise calls
        ``detector_factory`` once. Any failure building the detector (including
        a missing ``tflite-runtime`` surfacing as ``ImportError``) is reported
        and swallowed so the service keeps running with no detections (Req 3.6).

        Returns:
            A Detector-like object with ``detect(frame)`` and
            ``edge_tpu_active``, or ``None`` when no detector is configured or
            construction failed.
        """
        if self._detector is not None:
            return self._detector
        if self._detector_factory is None:
            print(
                "Camera_Service: no Detector configured; /detections will be "
                "empty (CPU-only, no detection)."
            )
            return None
        try:
            detector = self._detector_factory()
            edge_tpu = getattr(detector, "edge_tpu_active", False)
            print(
                "Camera_Service: Detector ready "
                f"(edge_tpu={'on' if edge_tpu else 'off'})."
            )
            return detector
        except Exception as e:
            # Graceful degrade: a missing model/runtime or construction error
            # must not take down the camera service (Req 3.6).
            print(
                f"Camera_Service: Detector unavailable ({e}); continuing with "
                "no detections (CPU-only)."
            )
            return None

    def _detect_loop(self):
        """Continuously run the Detector over the newest frame.

        Runs on a background daemon thread until :meth:`stop` is called. Each
        iteration grabs the newest frame and its frame id, runs
        ``detector.detect(frame)``, and stores the result keyed to that frame id
        for ``/detections`` and the overlay (Req 3.4). A per-frame inference
        error is reported and skipped so a transient failure never crashes the
        service (Req 3.6). The loop paces itself to the capture rate so it does
        not spin re-detecting the same frame.
        """
        detector = self._detector
        if detector is None:
            # No detector: leave the empty LatestDetections in place and exit
            # the thread. /detections stays valid-but-empty (Req 3.4, 3.6).
            return

        fps = self._config.capture_fps
        period = 1.0 / fps if fps > 0 else 0.1
        last_seen_id = None
        while not self._stop_event.is_set():
            frame = self._latest.get()
            frame_id = self._current_frame_id()
            if frame is None or frame_id == last_seen_id:
                # No frame yet, or no new frame since the last pass; wait.
                time.sleep(period)
                continue

            try:
                detections = detector.detect(frame)
            except Exception as e:
                # Per-frame inference failure: report, skip, keep running.
                print(
                    f"Camera_Service DETECT ERROR: inference failed ({e}); "
                    "skipping this frame."
                )
                time.sleep(period)
                continue

            import numpy as np

            arr = np.asarray(frame)
            height, width = int(arr.shape[0]), int(arr.shape[1])
            self._detections.push(frame_id, width, height, detections)
            last_seen_id = frame_id
            time.sleep(period)

    def _current_frame_id(self):
        """Return the id of the most recently captured frame.

        Returns:
            The monotonically increasing frame id assigned by the capture loop
            to the newest frame (0 before any frame is captured).
        """
        with self._frame_id_lock:
            return self._frame_id

    def _ambient_luminance(self):
        """Estimate ambient light as the mean luminance of the newest frame.

        Computes the mean over a **downscaled** copy of the latest frame so the
        estimate is cheap and insensitive to local detail — a coarse ambient
        proxy on the same 0-255 scale as ``ir_ambient_threshold`` (Req 10.4). A
        colour frame is reduced to luminance via a simple channel mean (a plain
        average is sufficient for an ambient estimate; no colour-accurate
        weighting is needed). Returns None when no frame has been captured yet
        so the IR auto loop holds its current state rather than acting on no
        data.

        Returns:
            The mean frame luminance as a float on a 0-255 scale, or ``None``
            when no frame is available yet.
        """
        frame = self._latest.get()
        if frame is None:
            return None
        import numpy as np

        arr = np.asarray(frame)
        if arr.size == 0:
            return None
        # Downscale by simple strided subsampling (no cv2 dependency): take at
        # most ~32x32 samples spread across the frame for a cheap mean.
        try:
            h = arr.shape[0]
            w = arr.shape[1]
            step_h = max(1, h // 32)
            step_w = max(1, w // 32)
            sample = arr[::step_h, ::step_w]
            return float(np.mean(sample))
        except Exception as e:
            print(f"Camera_Service: ambient luminance estimate failed ({e}).")
            return None

    def _ir_loop(self):
        """Drive the IR auto-switch hysteresis from ambient luminance.

        Runs on a background daemon thread until :meth:`stop` is called. Each
        tick estimates ambient light from the newest frame and hands it to
        :meth:`IRController.evaluate_auto`, which applies the pure hysteresis
        decision only when the IR mode is ``auto`` (forced on/off modes ignore
        it). The loop paces itself with :data:`IR_AUTO_POLL_S` (Req 10.4).
        """
        while not self._stop_event.is_set():
            luminance = self._ambient_luminance()
            try:
                self._ir.evaluate_auto(luminance)
            except Exception as e:
                # IR evaluation must never crash the service (Req 10.5).
                print(f"Camera_Service IR ERROR: auto-switch failed ({e}).")
            time.sleep(IR_AUTO_POLL_S)

    @property
    def ir(self):
        """The :class:`IRController` owned by this service.

        Exposed so the HTTP layer can read IR state for ``/status`` and route
        ``POST /ir`` mode changes to it.

        Returns:
            The :class:`IRController` instance.
        """
        return self._ir

    def get_frame(self):
        """Return the most recent exposed frame.

        Returns:
            The newest captured frame, or ``None`` if no frame has been captured
            yet. After a capture failure this is still the last good frame
            (Req 1.6).
        """
        return self._latest.get()

    @property
    def latest(self):
        """The underlying :class:`LatestFrame` buffer.

        Exposed so later tasks (the HTTP ``/stream`` and ``/status`` interface,
        task 5.1) can read the newest frame and its push count without reaching
        through the capture internals.

        Returns:
            The :class:`LatestFrame` instance backing this service.
        """
        return self._latest

    @property
    def config(self):
        """The :class:`vision_models.TrackingConfig` this service runs with.

        Exposed so the HTTP layer can read capture/stream fps and resolution for
        ``/status`` and the ``/stream`` throttle.

        Returns:
            The active ``TrackingConfig``.
        """
        return self._config

    @property
    def detections(self):
        """The underlying :class:`LatestDetections` buffer.

        Exposed so the HTTP layer can read the latest detection pass for the
        ``/detections`` endpoint and the ``/stream`` overlay.

        Returns:
            The :class:`LatestDetections` instance backing this service.
        """
        return self._detections

    @property
    def edge_tpu_active(self):
        """Whether the active Detector is running on the Edge TPU.

        Reads the resolved Detector's ``edge_tpu_active`` flag, defaulting to
        False when no Detector is configured (Req 3.6). Reported in ``/status``.

        Returns:
            True only when a Detector is active and the Edge TPU accelerator is
            actually in use; False otherwise.
        """
        if self._detector is None:
            return False
        return bool(getattr(self._detector, "edge_tpu_active", False))

    def status_snapshot(self):
        """Return a point-in-time status dict for the ``/status`` endpoint.

        Derives ``camera_ok`` and ``capturing`` from the capture-thread / first
        frame state, and ``last_frame_age_s`` from the latest-frame push
        timestamp (Req 2.6 — a stale feed is detectable from the age). The
        ``edge_tpu`` field reports the active Detector's real Edge TPU state
        (False when no Detector is configured, Req 3.6, task 6.3). The ``ir``
        field reports the real IR_Illuminator state from the
        :class:`IRController`: a ``{"mode","on","available"}`` dict when the
        hardware is present, or the string ``"unavailable"`` when it is
        absent/failed (Req 10.5, 10.6, task 13.1).

        Returns:
            A dict with keys ``camera_ok`` (bool), ``capturing`` (bool),
            ``fps`` (the configured stream fps), ``last_frame_age_s`` (float, or
            ``None`` when no frame has been captured yet), ``edge_tpu`` (bool),
            and ``ir`` (a ``{"mode","on","available"}`` dict, or the string
            ``"unavailable"``).
        """
        timestamp = self._latest.timestamp
        if timestamp is None:
            last_frame_age_s = None
        else:
            last_frame_age_s = max(0.0, time.monotonic() - timestamp)

        thread_alive = (
            self._capture_thread is not None and self._capture_thread.is_alive()
        )
        # "capturing" means the loop is alive AND at least one frame has been
        # produced; before the first frame arrives the feed is not yet live.
        capturing = bool(thread_alive and self._first_frame_event.is_set())

        return {
            "camera_ok": bool(self._camera_ok),
            "capturing": capturing,
            "fps": self._config.stream_fps,
            "last_frame_age_s": last_frame_age_s,
            # Real Detector Edge TPU state (task 6.3): True only when a Detector
            # is active and the accelerator is in use; False when no Detector is
            # configured or it fell back to CPU (Req 3.6).
            "edge_tpu": self.edge_tpu_active,
            # Real IR_Illuminator state (task 13.1): a {"mode","on","available"}
            # dict when the hardware is present, or the string "unavailable" when
            # it is absent/failed so the Control_Panel shows "IR unavailable"
            # (Req 10.5). Polling /status surfaces IR state changes (Req 10.6).
            "ir": self._ir.status(),
        }

    def stop(self):
        """Stop the capture loop and release the camera.

        Signals the capture thread to stop, waits briefly for it to finish, and
        closes the camera. Safe to call more than once.
        """
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None
        if self._detect_thread is not None:
            self._detect_thread.join(timeout=2.0)
            self._detect_thread = None
        if self._ir_thread is not None:
            self._ir_thread.join(timeout=2.0)
            self._ir_thread = None
        # Turn the illuminator off and release its GPIO pin (Req 10.5 cleanup).
        self._ir.close()
        self._close_camera()

    def _close_camera(self):
        """Best-effort stop/close of the camera device.

        Swallows errors during teardown so shutdown never raises; the process is
        going away regardless.
        """
        if self._camera is None:
            return
        camera = self._camera
        self._camera = None
        for method in ("stop", "close"):
            fn = getattr(camera, method, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception as e:
                print(f"Camera_Service: error during camera {method}() ({e})")


# --- HTTP interface (task 5.1) ----------------------------------------------
# The Camera_Service HTTP layer: a localhost-only Flask app serving the MJPEG
# Live_Feed and a status probe. Bound to loopback only so frames/detections
# never leave the device (Req 9.7).

# Loopback-only bind address and the Camera_Service port. Binding to 127.0.0.1
# (never 0.0.0.0) is what keeps camera frames and detections on-device (Req 9.7).
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8001

# MJPEG multipart boundary marker for the multipart/x-mixed-replace stream.
_MJPEG_BOUNDARY = "frame"

# Overlay drawing constants (BGR for cv2). Kept here so task 6.3 can reuse them
# when it draws real Detections.
_OVERLAY_BOX_COLOR = (0, 255, 0)       # green bounding box
_OVERLAY_TEXT_COLOR = (0, 255, 0)
_OVERLAY_BOX_THICKNESS = 2


def _draw_overlay(cv2, frame, detections):
    """Draw detection bounding boxes and labels onto a copy of ``frame``.

    Server-side overlay rendering for ``GET /stream?overlay=1`` (Req 2.3). With
    no detections the frame is returned unchanged, so an enabled overlay with no
    detections shows no boxes (Req 2.4).

    The real Detector is a separate task (6.3); until it is wired in, callers
    pass an empty detection list, so this is a no-op copy. It is written to
    accept the :class:`vision_models.Detection` shape now so 6.3 can feed real
    detections without changing this function.

    Args:
        cv2: The imported cv2 module (passed in so the import stays lazy).
        frame: The BGR frame (numpy array) to annotate.
        detections: An iterable of objects with ``x1/y1/x2/y2``, ``label`` and
            ``score`` attributes. May be empty.

    Returns:
        A new annotated frame. The input frame is not modified.
    """
    annotated = frame.copy()
    for det in detections or ():
        x1, y1, x2, y2 = int(det.x1), int(det.y1), int(det.x2), int(det.y2)
        cv2.rectangle(
            annotated, (x1, y1), (x2, y2),
            _OVERLAY_BOX_COLOR, _OVERLAY_BOX_THICKNESS,
        )
        label = f"{det.label} {det.score:.2f}"
        cv2.putText(
            annotated, label, (x1, max(0, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, _OVERLAY_TEXT_COLOR, 1,
            cv2.LINE_AA,
        )
    return annotated


def _encode_jpeg(cv2, frame):
    """JPEG-encode a frame to bytes, or return None if encoding fails.

    Args:
        cv2: The imported cv2 module (passed in so the import stays lazy).
        frame: The frame (numpy array) to encode.

    Returns:
        The encoded JPEG as ``bytes``, or ``None`` if cv2 reported an encoding
        failure.
    """
    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        return None
    return buffer.tobytes()


def _detection_to_dict(det):
    """Serialize a :class:`vision_models.Detection` to a JSON-friendly dict.

    Args:
        det: A :class:`vision_models.Detection` instance.

    Returns:
        A dict with keys ``label`` (str), ``score`` (float), ``x1``/``y1``/
        ``x2``/``y2`` (int) and ``is_person`` (bool) — the shape the
        Control_Panel overlay and Tracking_Mode consume (Req 3.4).
    """
    return {
        "label": det.label,
        "score": det.score,
        "x1": det.x1,
        "y1": det.y1,
        "x2": det.x2,
        "y2": det.y2,
        "is_person": det.is_person,
    }


def _detections_payload(service):
    """Build the ``/detections`` JSON payload from the service's latest pass.

    Args:
        service: The running :class:`CameraService` to read detections from.

    Returns:
        A dict ``{frame_id, width, height, ts, detections:[...]}`` where
        ``detections`` is a list of :func:`_detection_to_dict` dicts (empty when
        no Detector is configured or no pass has run, Req 3.4, 3.6).
    """
    snap = service.detections.snapshot()
    return {
        "frame_id": snap["frame_id"],
        "width": snap["width"],
        "height": snap["height"],
        "ts": snap["ts"],
        "detections": [_detection_to_dict(d) for d in snap["detections"]],
    }


def build_app(service):
    """Build the localhost-only Flask app for Camera_Service.

    Flask is imported here (not at module load) so the capture-loop classes
    above can be imported for hardware-free tests without the web stack. The
    returned app is bound to loopback only by :func:`run_http` so frames never
    leave the device (Req 9.7).

    Routes:
        ``GET /stream`` — ``multipart/x-mixed-replace`` MJPEG of the latest
            frames, throttled to ``service.config.stream_fps`` (1-30 fps,
            independent of capture fps, Req 2.1, 2.2). ``?overlay=1`` draws
            server-side detection boxes (Req 2.3); with no detections the feed
            shows no boxes (Req 2.4).
        ``GET /detections`` — JSON
            ``{frame_id, width, height, ts, detections:[...]}`` of the latest
            detection pass for the Control_Panel overlay and Tracking_Mode
            (Req 3.4). Returns an empty detection list when no Detector is
            configured (Req 3.6).
        ``GET /status`` — the :meth:`CameraService.status_snapshot` dict
            (Req 2.6), including the real ``edge_tpu`` state (Req 3.6) and the
            real ``ir`` state (Req 10.5, 10.6).
        ``GET /ir`` — the current IR status. ``POST /ir`` with
            ``{"mode": "<on|off|auto>"}`` sets the IR mode, validated against the
            fixed set (HTTP 400 "invalid value" on an unknown mode, Req 10.3).

    Args:
        service: The running :class:`CameraService` to read frames/status from.

    Returns:
        A configured Flask ``app`` instance.
    """
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)

    def _frame_generator(overlay):
        """Yield MJPEG multipart parts of the latest frame at the stream rate.

        Throttled to ``stream_fps`` (independent of capture fps) by sleeping
        between parts; always sends the newest exposed frame, so the stream rate
        and capture rate are decoupled (Req 2.2). cv2 is imported once here,
        lazily, since the stream is the only consumer that needs it.

        Args:
            overlay: When True, draw the current frame's detections. With no
                Detector configured (or no detections this pass) the detection
                list is empty, so an enabled overlay correctly shows no boxes
                (Req 2.4, 3.4).

        Yields:
            ``bytes`` parts of the ``multipart/x-mixed-replace`` response.
        """
        cv2 = _import_cv2()
        # Clamp defensively; TrackingConfig already clamps stream_fps to [1, 30].
        fps = service.config.stream_fps
        period = 1.0 / fps if fps > 0 else 0.1
        while True:
            frame, _ts = service.latest.get_with_timestamp()
            if frame is not None:
                if overlay:
                    # Draw the latest detection pass over the frame (task 6.3).
                    # Empty when no Detector is configured, so the overlay shows
                    # no boxes (Req 2.4, 3.4).
                    frame = _draw_overlay(
                        cv2, frame, service.detections.get_detections()
                    )
                jpeg = _encode_jpeg(cv2, frame)
                if jpeg is not None:
                    yield (
                        b"--" + _MJPEG_BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                        b"\r\n" + jpeg + b"\r\n"
                    )
            time.sleep(period)

    @app.route("/stream")
    def stream():
        """Serve the MJPEG Live_Feed, optionally with detection overlays."""
        overlay = request.args.get("overlay") == "1"
        content_type = (
            "multipart/x-mixed-replace; boundary=" + _MJPEG_BOUNDARY
        )
        return Response(
            _frame_generator(overlay), mimetype=content_type
        )

    @app.route("/detections")
    def detections():
        """Return the latest detection pass as JSON (Req 3.4, 3.6)."""
        return jsonify(_detections_payload(service))

    @app.route("/status")
    def status():
        """Return the Camera_Service status snapshot as JSON (Req 2.6)."""
        return jsonify(service.status_snapshot())

    @app.route("/ir", methods=["GET", "POST"])
    def ir():
        """Get or set the IR_Illuminator mode (Req 10.3).

        ``GET`` returns the current IR status (the same shape as the ``/status``
        ``ir`` field). ``POST`` with JSON ``{"mode": "<on|off|auto>"}`` sets the
        mode: the mode is validated against the fixed ``{on,off,auto}`` set
        before anything is applied, and an invalid mode is rejected with HTTP
        400 and an "invalid value" message, driving no hardware (Req 10.3). On
        success the new IR status is returned so the Control_Panel sees the
        change immediately (Req 10.6).
        """
        if request.method == "GET":
            return jsonify({"ir": service.ir.status()})

        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        if not service.ir.set_mode(mode):
            # Reject: drive nothing, change nothing (Req 10.3).
            print(f"Camera_Service: rejected invalid IR mode {mode!r}")
            return jsonify({"status": "error", "message": "invalid value"}), 400
        return jsonify({"status": "ok", "ir": service.ir.status()})

    return app


def run_http(service, host=HTTP_HOST, port=HTTP_PORT):
    """Run the Flask HTTP interface, bound to loopback only.

    Binds to ``127.0.0.1`` (never ``0.0.0.0``) so camera frames and detections
    never leave the device (Req 9.7). ``threaded=True`` so the long-lived MJPEG
    ``/stream`` response does not block ``/status`` requests.

    Args:
        service: The running :class:`CameraService` to serve from.
        host: Bind address; defaults to loopback and should stay loopback.
        port: TCP port; defaults to :data:`HTTP_PORT` (8001).
    """
    app = build_app(service)
    print(f"Camera_Service: serving HTTP on http://{host}:{port} (loopback only)")
    app.run(host=host, port=port, threaded=True)


# Environment variables naming the (optional) detector model/labels. Kept as
# env lookups so Camera_Service stays runnable with no model configured — the
# selectable-model webapp route and MODELS_DIR validation are task 11.2's job.
MODEL_PATH_ENV = "CAMERA_MODEL_PATH"
LABELS_PATH_ENV = "CAMERA_LABELS_PATH"
# Optional override for the detector confidence threshold. When unset, the
# TrackingConfig default (0.5) is used. Lowering it (e.g. 0.3) surfaces more /
# smaller objects (scissors, cup) at the cost of more false positives; raising
# it is stricter. Parsed as a float and clamped to [0.0, 1.0].
CONF_THRESHOLD_ENV = "CAMERA_CONF_THRESHOLD"


def _detector_factory_from_env(config):
    """Build an optional detector factory from environment configuration.

    Looks up the model/labels paths from the environment
    (:data:`MODEL_PATH_ENV`, :data:`LABELS_PATH_ENV`). When neither is set, no
    Detector is configured and the service runs CPU-only with no detections
    (Req 3.6). When they are set, returns a zero-arg factory that builds a
    :class:`detector.Detector`. The confidence threshold comes from the
    ``CAMERA_CONF_THRESHOLD`` env var when set (parsed + clamped to [0.0, 1.0]),
    otherwise the config default; the Edge TPU preference comes from the config,
    and the Detector itself degrades Edge TPU -> CPU as needed (Req 3.5, 3.6).

    Args:
        config: The :class:`vision_models.TrackingConfig` supplying
            ``conf_threshold`` and ``use_edge_tpu``.

    Returns:
        A zero-arg factory returning a Detector, or ``None`` when no model is
        configured in the environment.
    """
    import os

    model_path = os.environ.get(MODEL_PATH_ENV)
    labels_path = os.environ.get(LABELS_PATH_ENV)
    if not model_path or not labels_path:
        return None

    # Confidence threshold: env override wins over the TrackingConfig default,
    # parsed as a float and clamped to [0.0, 1.0]. A malformed value is ignored
    # (falls back to the config default) with a warning, so a bad env entry can
    # never crash the service or disable detection.
    conf_threshold = config.conf_threshold
    raw_conf = os.environ.get(CONF_THRESHOLD_ENV)
    if raw_conf is not None:
        try:
            conf_threshold = min(1.0, max(0.0, float(raw_conf)))
            print(
                f"Camera_Service: detector confidence threshold overridden to "
                f"{conf_threshold:.2f} via {CONF_THRESHOLD_ENV}"
            )
        except ValueError:
            print(
                f"Camera_Service: ignoring invalid {CONF_THRESHOLD_ENV}="
                f"{raw_conf!r}; using default {conf_threshold:.2f}"
            )

    def factory():
        """Construct the configured Detector (imported lazily and guarded)."""
        detector_cls = _import_detector()
        return detector_cls(
            model_path=model_path,
            labels_path=labels_path,
            conf_threshold=conf_threshold,
            use_edge_tpu=config.use_edge_tpu,
        )

    return factory


def main():
    """Run Camera_Service as a standalone non-root process.

    Starts the capture loop (exiting non-zero on init failure per Req 1.5) and,
    when a detector model is configured in the environment, a background
    detection thread, then runs the localhost-only Flask HTTP interface
    (``/stream``, ``/detections``, ``/status``) in the foreground so operators
    can view the Live_Feed and overlays in the Control_Panel (Build Steps 2-3,
    tasks 5.1 and 6.3). The capture/detection loops run on background daemon
    threads; ``run_http`` blocks here serving requests until interrupted. With
    no model configured the service runs CPU-only and ``/detections`` is empty
    (Req 3.6).
    """
    config = TrackingConfig()
    service = CameraService(
        config=config,
        detector_factory=_detector_factory_from_env(config),
    )
    service.start()
    print(
        "Camera_Service: capture loop running (non-root). "
        "Press Ctrl+C to stop."
    )
    try:
        run_http(service)
    except KeyboardInterrupt:
        print("Camera_Service: shutting down.")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
