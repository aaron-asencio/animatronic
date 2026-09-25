"""range_sensor.py

Reusable HC-SR04 ultrasonic range sensor helper.

Wraps gpiozero's ``DistanceSensor`` for the sensor wired to
``constants.RANGE_TRIG_PIN`` / ``constants.RANGE_ECHO_PIN`` and exposes the two
things the rest of the system needs:

- ``distance_m()`` / ``distance_cm()`` — a single reading.
- ``object_within(threshold_m)`` — presence test against a threshold, the
  primitive a sensor-interrupted Sleep mode polls to decide when to wake (see
  the animation vocabulary: Sleep mode is interrupted by a sensor).

``ApproachDetector`` builds on the sensor to detect an object *approaching*
(getting closer across several consecutive readings, within a range gate) —
the wake trigger for the napping Mode.

The class is a context manager so callers can guarantee the GPIO pins are
released::

    from range_sensor import RangeSensor

    with RangeSensor() as sensor:
        while not sensor.object_within(0.5):
            time.sleep(0.1)
        # something approached within 0.5 m — trigger a startle/wake response

Wiring note: the HC-SR04 ECHO pin idles at 5V but the Pi GPIO tolerates only
3.3V. A voltage divider (or level shifter) on the ECHO line is required (and is
in place on this build).

Runs on the Raspberry Pi with GPIO access; hardware scripts using it may need
root.
"""

import threading
import time

from gpiozero import DistanceSensor

from constants import RANGE_ECHO_PIN, RANGE_TRIG_PIN

# gpiozero DistanceSensor caps out around 4 m for the HC-SR04; readings saturate
# at max_distance. This default matches rangetest.py.
DEFAULT_MAX_DISTANCE_M = 4.0

# --- Approach-detection defaults (used by ApproachDetector) ---
# Ignore anything farther than this: an object beyond the gate never counts as
# approaching, so a distant wall / passer-by can't trigger a wake.
DEFAULT_APPROACH_GATE_M = 3.0
# Require this many consecutive closer readings to confirm a real approach
# (filters out one-off noise / a single flinch).
DEFAULT_APPROACH_CONSECUTIVE = 3
# A reading must be at least this much closer than the previous one to count as
# "getting closer". Guards against the HC-SR04's ~1 cm jitter registering as
# approach when the object is actually still.
DEFAULT_APPROACH_MIN_STEP_M = 0.05
# How often the background poller samples the sensor when watching for an
# approach. The HC-SR04 read itself takes tens of ms; ~0.1 s keeps the wake
# responsive without hammering the sensor (or its echo timeout).
DEFAULT_APPROACH_POLL_INTERVAL_S = 0.1
# GLITCH REJECTION. The HC-SR04 intermittently returns spurious readings —
# most importantly gpiozero reports 0.0 m on a MISSED ECHO, and electrical
# noise/crosstalk can yield other garbage-short values. Untreated, a single
# such spike looks like a huge instantaneous "approach" (anchor -> ~0) and
# false-fires the wake. Two guards reject these:
#   - Readings at/below MIN_VALID_M are treated as no-echo glitches and ignored
#     (a real target essentially never sits this close to the sensor).
#   - A single sample that moves more than MAX_STEP_M closer than the anchor is
#     physically implausible for one poll interval (a fast human walk ~1.5 m/s
#     over 0.1 s is ~15 cm), so it is treated as a glitch and ignored rather
#     than counted as many closer-steps at once.
DEFAULT_APPROACH_MIN_VALID_M = 0.03   # <= this reads as a no-echo glitch
DEFAULT_APPROACH_MAX_STEP_M = 0.40    # a bigger single jump closer = glitch


class RangeSensor:
    """HC-SR04 ultrasonic range sensor on the configured TRIG/ECHO pins.

    Thin wrapper over gpiozero's ``DistanceSensor`` that centralizes pin
    selection (from ``constants``) and adds a presence helper. Owns the
    underlying gpiozero device; call ``close()`` (or use it as a context
    manager) to release the GPIO pins.
    """

    def __init__(self, max_distance=DEFAULT_MAX_DISTANCE_M,
                 trigger_pin=RANGE_TRIG_PIN, echo_pin=RANGE_ECHO_PIN):
        """Open the range sensor and configure its pins.

        Args:
            max_distance: Maximum measurable distance in meters. Readings
                          saturate at this value; also sets gpiozero's timeout.
            trigger_pin:  BCM pin driving the sensor's TRIG (output). Defaults
                          to ``constants.RANGE_TRIG_PIN``.
            echo_pin:     BCM pin reading the sensor's ECHO (input, via voltage
                          divider). Defaults to ``constants.RANGE_ECHO_PIN``.
        """
        self.max_distance = max_distance
        self.trigger_pin = trigger_pin
        self.echo_pin = echo_pin
        self._sensor = DistanceSensor(
            echo=echo_pin,
            trigger=trigger_pin,
            max_distance=max_distance,
        )

    def distance_m(self):
        """Return the current distance to the nearest object in meters.

        Saturates at ``max_distance`` when nothing is within range.
        """
        return self._sensor.distance

    def distance_cm(self):
        """Return the current distance to the nearest object in centimeters.

        Saturates at ``max_distance * 100`` when nothing is within range.
        """
        return self._sensor.distance * 100

    def object_within(self, threshold_m):
        """Return True if an object is at or closer than ``threshold_m`` meters.

        The presence primitive for a sensor-interrupted Sleep mode: poll this
        and treat a True result as the wake/startle trigger.

        Args:
            threshold_m: Distance in meters; a reading at or below this counts
                         as "something is there".

        Returns:
            True if the current reading is <= ``threshold_m``, else False.
        """
        return self.distance_m() <= threshold_m

    def close(self):
        """Release the sensor's GPIO pins. Safe to call more than once."""
        self._sensor.close()

    def __enter__(self):
        """Enter the context manager, returning this sensor."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager, always releasing the GPIO pins."""
        self.close()
        return False


class ApproachDetector:
    """Detects an object *approaching* the sensor, for waking a Sleep/Nap mode.

    Wraps a ``RangeSensor`` and turns a stream of distance readings into a
    single "is something approaching?" decision, so a Mode can poll for a wake
    (see the animation vocabulary: Sleep mode is interrupted by a sensor).

    Two ways to consume it:

    - **Synchronous** — call ``approaching()`` once per poll from your own
      loop. Each call does a blocking sensor read (tens of ms).
    - **Background** — call ``start_polling()`` to spawn a daemon thread that
      samples every ``poll_interval_s`` and *latches* a confirmed approach.
      Callers then check the non-blocking ``triggered()`` flag as often as they
      like. This is what the async napping loop uses so it can react mid-cycle
      (between individual moves) without stalling the event loop on a blocking
      read. Once latched, ``triggered()`` stays True until ``reset()``.

    Detection rule (per the wake spec) — CADENCE-INDEPENDENT so it behaves the
    same whether polled slowly (e.g. the test tool at 0.5 s) or quickly (the
    nap's 0.1 s background poller):

    - **Range gate** — a reading farther than ``gate_m`` is ignored. An object
      beyond the gate can never trigger a wake, and it also RESETS the streak
      so a far reading breaks an in-progress approach.
    - **Closer-steps** — the detector anchors a reference distance and counts
      how many whole ``min_step_m`` chunks the object moves CLOSER than that
      anchor, advancing the anchor each step. An approach is confirmed once
      ``consecutive`` such steps accumulate — i.e. a net approach of
      ``consecutive * min_step_m`` meters (default 3 * 5 cm = 15 cm). Because
      the count is by DISTANCE moved, not by per-sample comparison, a fast poll
      sees the same total approach as a slow one; tiny sub-step samples
      accumulate rather than resetting the streak.
    - **Minimum step** — ``min_step_m`` is the chunk size; sub-``min_step_m``
      wobble around the anchor is ignored, filtering the sensor's ~1 cm jitter
      so a stationary object never "approaches". Moving a full step AWAY
      re-anchors and drops the streak (the object is receding).
    - **Glitch rejection** — the HC-SR04 intermittently returns spurious
      short readings (gpiozero reports ~0.0 m on a missed echo; noise/crosstalk
      yields other garbage). Readings at/below ``min_valid_m`` are ignored as
      no-echo glitches, and a single sample jumping more than ``max_step_m``
      closer than the anchor is rejected as implausible for one poll — both
      without disturbing the anchor/streak. This stops a one-sample spike from
      false-firing the wake while a real approach still accumulates.

    The detector owns its ``RangeSensor`` and is a context manager, so closing
    it releases the GPIO pins::

        from range_sensor import ApproachDetector

        with ApproachDetector() as detector:
            while not detector.approaching():
                time.sleep(0.1)
            # confirmed approach within the gate -> startle / wake
    """

    def __init__(self, sensor=None, gate_m=DEFAULT_APPROACH_GATE_M,
                 consecutive=DEFAULT_APPROACH_CONSECUTIVE,
                 min_step_m=DEFAULT_APPROACH_MIN_STEP_M,
                 max_distance=DEFAULT_MAX_DISTANCE_M,
                 poll_interval_s=DEFAULT_APPROACH_POLL_INTERVAL_S,
                 min_valid_m=DEFAULT_APPROACH_MIN_VALID_M,
                 max_step_m=DEFAULT_APPROACH_MAX_STEP_M):
        """Create an approach detector.

        Args:
            sensor:          An existing ``RangeSensor`` to read from. If None,
                             the detector constructs and owns its own (closed on
                             ``close()``).
            gate_m:          Maximum distance in meters to consider; readings
                             farther than this are ignored and reset the streak.
            consecutive:     Number of consecutive closer readings required to
                             confirm an approach (must be >= 1).
            min_step_m:      Minimum decrease in meters between consecutive
                             readings to count as "getting closer".
            max_distance:    Passed to the owned ``RangeSensor`` when ``sensor``
                             is None. Ignored if a sensor is supplied.
            poll_interval_s: Seconds between samples when using the background
                             poller (``start_polling``).
            min_valid_m:     Readings at or below this are treated as no-echo
                             glitches and ignored (the HC-SR04/gpiozero reports
                             ~0.0 m on a missed echo).
            max_step_m:      A single sample that jumps more than this much
                             CLOSER than the anchor is treated as a spurious
                             glitch and ignored (implausible for one poll).
        """
        self._owns_sensor = sensor is None
        self.sensor = sensor if sensor is not None else RangeSensor(
            max_distance=max_distance)
        self.gate_m = gate_m
        self.consecutive = max(1, int(consecutive))
        self.min_step_m = min_step_m
        self.poll_interval_s = poll_interval_s
        self.min_valid_m = min_valid_m
        self.max_step_m = max_step_m
        # Approach tracking is CADENCE-INDEPENDENT: we don't compare each sample
        # to the immediately-previous one (that makes the min-step threshold
        # depend on how fast we poll — a fast poll sees sub-min_step moves and
        # never advances). Instead we anchor a "step point" and count how many
        # times the object has moved a full ``min_step_m`` CLOSER than that
        # anchor, advancing the anchor by one step each time. Many tiny fast
        # samples then accumulate toward the next step instead of resetting.
        #   _anchor_m: distance at the last confirmed step (or first in-gate read)
        #   _closer_steps: how many min_step_m closer-steps since the anchor chain began
        self._anchor_m = None
        self._closer_steps = 0
        # Background-poller state. _lock guards the streak/flag when the poller
        # thread and the caller touch them concurrently.
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = None
        self._triggered = False

    def reset(self):
        """Forget any in-progress approach streak and clear the latched flag.

        Call after a wake so the next approach starts from scratch. Safe to call
        while the background poller is running.
        """
        with self._lock:
            self._anchor_m = None
            self._closer_steps = 0
            self._triggered = False

    def approaching(self):
        """Take one reading and report whether an approach is now confirmed.

        Call this once per poll. It reads the current distance, updates the
        internal streak per the detection rule, and returns True only on the
        reading that completes ``consecutive`` closer-steps within the gate.

        Returns:
            True if an approaching object is confirmed on this reading, else
            False. Returns False (and resets the streak) for any reading beyond
            ``gate_m``.
        """
        # Read the sensor OUTSIDE the lock (blocking, tens of ms) so a
        # concurrent triggered()/reset() call never waits on the hardware.
        distance = self.sensor.distance_m()

        with self._lock:
            # No-echo glitch: gpiozero reports ~0.0 m when the ping gets no
            # echo. A real target never sits this close, so ignore the sample
            # entirely (don't touch the anchor/streak) rather than treating it
            # as a huge instantaneous approach.
            if distance <= self.min_valid_m:
                return False

            # Outside the gate: ignore, and break any in-progress approach.
            if distance > self.gate_m:
                self._anchor_m = None
                self._closer_steps = 0
                return False

            # First in-gate reading: anchor here, nothing counted yet.
            if self._anchor_m is None:
                self._anchor_m = distance
                self._closer_steps = 0
                return False

            delta = self._anchor_m - distance  # >0 means closer than the anchor

            if delta > self.max_step_m:
                # Implausibly large single jump closer (more than a person could
                # move in one poll) — a spurious short reading. Ignore it: don't
                # advance the anchor or count steps. A genuine fast approach
                # still accumulates over subsequent in-range samples.
                pass
            elif delta >= self.min_step_m:
                # Moved a full step (or more) closer. Count however many whole
                # min_step_m steps this represents (a fast approach can cross
                # several at once) and advance the anchor to the new position.
                steps = int(delta // self.min_step_m)
                self._closer_steps += steps
                self._anchor_m = distance
            elif delta <= -self.min_step_m:
                # Moved a full step AWAY: the object is receding. Re-anchor and
                # drop the streak — this isn't an approach.
                self._anchor_m = distance
                self._closer_steps = 0
            # else: within +/- min_step_m of the anchor — jitter/slow creep.
            # Keep the anchor fixed so many tiny fast samples ACCUMULATE toward
            # the next step instead of resetting the streak (this is the
            # cadence-independence fix: the decision no longer depends on the
            # per-sample delta being >= min_step_m).

            # ``consecutive`` closer-steps confirm the approach: e.g. with the
            # defaults, the object moving 3 x 5 cm = 15 cm closer within the gate.
            confirmed = self._closer_steps >= self.consecutive
            if confirmed:
                self._triggered = True
            return confirmed

    # --- Background polling (non-blocking wake flag) ---------------------- #

    def start_polling(self):
        """Start a daemon thread that samples the sensor and latches approaches.

        The thread calls ``approaching()`` every ``poll_interval_s`` seconds;
        once an approach is confirmed it sets a latched flag that ``triggered()``
        reports without touching the hardware. Idempotent: a second call while
        already polling is a no-op. The thread is a daemon, so it never blocks
        interpreter shutdown, but call ``stop_polling()`` (or ``close()``) to end
        it cleanly and release the sensor.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop, name="ApproachDetector", daemon=True)
        self._thread.start()

    def _poll_loop(self):
        """Background loop: sample until stopped, latching on any read error too.

        A sensor read failure is swallowed (logged) so a transient glitch never
        kills the poller; it simply skips that sample and tries again next tick.
        """
        while not self._stop_event.is_set():
            try:
                self.approaching()
            except Exception as e:
                print(f"range_sensor: background poll read failed: {e}")
            # Wait returns immediately if stop is set, so shutdown is prompt.
            self._stop_event.wait(self.poll_interval_s)

    def triggered(self):
        """Return True if the poller has latched a confirmed approach.

        Non-blocking (no sensor read); safe to call as often as you like from an
        async loop. Stays True until ``reset()`` clears it.
        """
        with self._lock:
            return self._triggered

    def stop_polling(self):
        """Stop the background poller thread if running (waits for it to exit)."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._stop_event = None

    def close(self):
        """Stop the poller and release the sensor's GPIO pins if owned."""
        self.stop_polling()
        if self._owns_sensor:
            self.sensor.close()

    def __enter__(self):
        """Enter the context manager, returning this detector."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager, releasing the sensor if owned."""
        self.close()
        return False
