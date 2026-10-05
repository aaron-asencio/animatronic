"""Tests for camera-feed stall detection & self-recovery (hardware-free).

Guards the confirmed field incident where ``picamera2.capture_array()`` blocked
FOREVER inside the capture thread: no new frame was pushed, yet the thread
stayed alive and ``/status`` kept reporting ``capturing: true`` /
``camera_ok: true`` on a 37-hour-old frame (and ``journalctl`` showed zero
``CAPTURE ERROR`` lines because the call never raised).

Two behaviours are covered:

- Defect 1 — truthful ``/status``: ``capturing`` (and ``camera_ok`` once frames
  have flowed) are derived from frame FRESHNESS, so a stalled feed reports
  ``capturing: false`` / ``camera_ok: false`` while ``last_frame_age_s`` keeps
  growing honestly. Before the first frame, ``capturing`` is ``False`` and
  ``last_frame_age_s`` is ``None``.
- Defect 2 — watchdog self-recovery: a SEPARATE watchdog thread detects the
  stall and recovers the camera (``_close_camera()`` + ``_create_camera()``),
  frame flow and ``capturing: true`` resume on the new device, and a recovery
  that raises never kills the watchdog thread.

Everything runs WITHOUT real hardware: the camera is injected through the
existing ``picamera2_factory`` seam with a ``FakeCamera`` whose
``capture_array()`` can be made to block, and the IR controller is injected as a
no-op fake so no gpiozero/pin access occurs.

Run with:

    .venv/bin/python -m pytest tests/test_camera_service_stall.py -q
"""

import os
import sys
import time
import threading

# Ensure the src/ directory is importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from camera_service import CameraService  # noqa: E402
from vision_models import TrackingConfig  # noqa: E402


# A trivial sentinel "frame". _capture_loop does not inspect frames and /status
# only reads timestamps, so any non-None object works. Using a 1x1 list avoids
# pulling in numpy at import.
_SENTINEL_FRAME = [[0]]


class FakeIR:
    """No-op IR controller injected so no gpiozero/GPIO access happens."""

    def status(self):
        return "unavailable"

    def evaluate_auto(self, luminance):
        pass

    def close(self):
        pass


class FakeCamera:
    """A fake picamera2-like device whose capture can be made to stall.

    ``capture_array()`` normally returns a sentinel frame immediately. When the
    ``stall`` event is set it blocks on ``stall.wait()`` to simulate the wedged,
    never-returning real call. ``stop()``/``close()`` record the call and SET
    the stall event, so a wedged ``capture_array()`` unblocks when the device is
    torn down — mirroring how tearing down the real device unblocks the call.
    """

    def __init__(self):
        self.stall = threading.Event()
        self.stopped = False
        self.closed = False

    def start(self):
        pass

    def capture_array(self):
        if self.stall.is_set():
            # Block until teardown releases us (device torn down => call returns).
            self.stall.wait()
            # After release, raise so the capture loop's existing except path
            # backs off and re-reads the (new) camera, like a stopped device.
            raise RuntimeError("camera stopped")
        return _SENTINEL_FRAME

    def stop(self):
        self.stopped = True
        self.stall.set()

    def close(self):
        self.closed = True
        self.stall.set()


class RecordingFactory:
    """Zero-arg factory that builds a fresh FakeCamera each call and records them.

    An optional ``fail_builds`` set names 0-based build indices that should
    raise instead of returning a camera, to exercise watchdog survival on a
    failing recovery.
    """

    def __init__(self, fail_builds=None):
        self.cameras = []
        self._fail_builds = set(fail_builds or ())
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            idx = len(self.cameras)
            # Record even failed attempts so the build count reflects tries.
            self.cameras.append(None)
            if idx in self._fail_builds:
                raise RuntimeError(f"simulated build failure #{idx}")
            cam = FakeCamera()
            self.cameras[idx] = cam
            return cam

    @property
    def build_count(self):
        with self._lock:
            return len(self.cameras)

    def camera(self, idx):
        with self._lock:
            return self.cameras[idx]


def _make_service(factory, **kwargs):
    """Build a CameraService wired to the fake factory and a no-op IR controller.

    Short watchdog/staleness timeouts keep the tests fast. A no-detector service
    keeps the detect thread from running.
    """
    defaults = dict(
        stall_timeout_s=0.2,
        watchdog_poll_s=0.05,
        staleness_timeout_s=0.1,
    )
    defaults.update(kwargs)
    return CameraService(
        config=TrackingConfig(),
        picamera2_factory=factory,
        ir_controller=FakeIR(),
        **defaults,
    )


def _wait_until(predicate, timeout=3.0, interval=0.01):
    """Poll ``predicate`` until true or ``timeout`` elapses; return its result."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def test_status_reports_not_capturing_when_feed_is_stale():
    """/status flips capturing+camera_ok to False once the latest frame is stale.

    Also asserts the pre-first-frame contract: capturing False, age None.
    """
    factory = RecordingFactory()
    service = _make_service(factory)

    # Pre-first-frame contract, checked before start() produces any frame.
    snap0 = service.status_snapshot()
    assert snap0["capturing"] is False
    assert snap0["last_frame_age_s"] is None

    try:
        service.start()
        # A frame flowed during start(): feed is live.
        assert _wait_until(lambda: service.status_snapshot()["capturing"] is True)
        assert service.status_snapshot()["camera_ok"] is True

        # Stall the device so no new frames push. Suppress the watchdog's
        # recovery for this test by giving it a huge stall timeout, so only the
        # (small) staleness threshold governs /status.
        service._stall_timeout_s = 10_000.0
        factory.camera(0).stall.set()

        # Once the latest frame ages past staleness_timeout_s, /status must tell
        # the truth: not capturing, not ok, with an honest growing age.
        assert _wait_until(
            lambda: service.status_snapshot()["capturing"] is False
        )
        snap = service.status_snapshot()
        assert snap["capturing"] is False
        assert snap["camera_ok"] is False
        assert isinstance(snap["last_frame_age_s"], float)
        assert snap["last_frame_age_s"] > service._staleness_timeout_s
    finally:
        service.stop()


def test_watchdog_recovers_camera_after_stall_timeout():
    """The watchdog tears down + recreates the camera after the stall timeout."""
    factory = RecordingFactory()
    service = _make_service(factory)
    try:
        service.start()
        assert _wait_until(lambda: service.status_snapshot()["capturing"] is True)
        assert factory.build_count == 1
        first = factory.camera(0)

        # Stall the feed; the watchdog should recover within a bounded wait.
        first.stall.set()
        assert _wait_until(lambda: service._recovery_count >= 1)

        # Teardown happened on the old device and a new one was built.
        assert first.stopped or first.closed
        assert factory.build_count == 2
        assert service._recovery_count == 1
    finally:
        service.stop()


def test_frame_flow_and_capturing_resume_after_recovery():
    """After recovery the new device produces frames and capturing goes True."""
    factory = RecordingFactory()
    service = _make_service(factory)
    try:
        service.start()
        assert _wait_until(lambda: service.status_snapshot()["capturing"] is True)
        before = service.latest.count

        # Stall -> watchdog recovers to a fresh (non-stalled) FakeCamera.
        factory.camera(0).stall.set()
        assert _wait_until(lambda: service._recovery_count >= 1)

        # Frame count climbs again on the new device and status is healthy.
        assert _wait_until(lambda: service.latest.count > before)
        assert _wait_until(lambda: service.status_snapshot()["capturing"] is True)
        snap = service.status_snapshot()
        assert snap["capturing"] is True
        assert snap["camera_ok"] is True
        assert snap["last_frame_age_s"] < service._staleness_timeout_s
    finally:
        service.stop()


def test_recovery_that_raises_does_not_kill_watchdog(capsys):
    """A recovery whose camera build raises must not kill the watchdog thread.

    The first recovery attempt (second build, index 1) raises; the watchdog must
    survive, keep retrying on cadence, and eventually recover once the factory
    is yielding healthy cameras again.
    """
    factory = RecordingFactory(fail_builds={1})
    service = _make_service(factory)
    try:
        service.start()
        assert _wait_until(lambda: service.status_snapshot()["capturing"] is True)

        # Stall -> the first recovery attempt (build #1) raises.
        factory.camera(0).stall.set()

        # The failing build (#1) was attempted and the watchdog is STILL alive
        # immediately after the raised recovery.
        assert _wait_until(lambda: factory.build_count >= 2)
        assert _wait_until(
            lambda: service._watchdog_thread.is_alive(), timeout=1.0
        )

        # The watchdog kept retrying on cadence: a later build succeeds and the
        # recovery count advances, proving the loop never stopped after the
        # exception. The thread must still be alive after the successful retry.
        assert _wait_until(lambda: service._recovery_count >= 1, timeout=3.0)
        assert service._watchdog_thread.is_alive()

        # The failed attempt printed a guarded failure line (print() only, no
        # logging framework) — the exception was caught, not propagated.
        out = capsys.readouterr().out
        assert "recovery failed" in out
    finally:
        service.stop()
