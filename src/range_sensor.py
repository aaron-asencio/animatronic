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
import os
import time

from gpiozero import DistanceSensor

from constants import RANGE_ECHO_PIN, RANGE_TRIG_PIN

# Debug logging for approach detection. Set RANGE_SENSOR_DEBUG=1 to record every
# sensor reading and how the ApproachDetector classified it (glitch / beyond
# gate / anchor / closer-step / receding / jitter / FIRE). Off by default so
# normal runs are quiet.
#
# Output goes to the file named by RANGE_SENSOR_DEBUG_FILE (default
# /tmp/range_debug.log) rather than stdout, so it survives the mic controller's
# continuous "RMS:" spam and reaches us even though the nap runs as a subprocess
# (a stdout print would be interleaved/lost). Each line is timestamped. To
# diagnose a false wake: set RANGE_SENSOR_DEBUG=1 before launching the webapp,
# reproduce, then read the log file.
_DEBUG = os.environ.get("RANGE_SENSOR_DEBUG", "") not in ("", "0", "false", "False")
_DEBUG_FILE = os.environ.get("RANGE_SENSOR_DEBUG_FILE", "/tmp/range_debug.log")

# gpiozero DistanceSensor caps out around 4 m for the HC-SR04; readings saturate
# at max_distance. This default matches rangetest.py.
DEFAULT_MAX_DISTANCE_M = 4.0

# --- Approach-detection defaults (used by ApproachDetector) ---
# Ignore anything farther than this: an object beyond the gate never counts as
# approaching, so a distant wall / passer-by can't trigger a wake.
DEFAULT_APPROACH_GATE_M = 3.0
# Require this many CONSECUTIVE closer samples (in a row) to confirm a real
# approach. This is the key guard against spurious spikes: one bad reading can
# advance the streak by at most one and its revert resets it, so it takes a
# sustained approach across this many polls to fire.
DEFAULT_APPROACH_CONSECUTIVE = 3
# A sample must be at least this much closer than the PREVIOUS sample to count
# as "getting closer"; smaller changes are stationary jitter and reset the
# streak. Measured HC-SR04 noise on this build is ~1.2 cm, so 3 cm sits safely
# above the noise floor while still registering a slow, genuine approach
# (~4-5 cm per 0.1 s poll).
DEFAULT_APPROACH_MIN_STEP_M = 0.03
# How often the background poller samples the sensor when watching for an
# approach. The HC-SR04 read itself takes tens of ms; ~0.1 s keeps the wake
# responsive without hammering the sensor (or its echo timeout).
DEFAULT_APPROACH_POLL_INTERVAL_S = 0.1
# GLITCH REJECTION. The HC-SR04 intermittently returns spurious readings —
# gpiozero reports 0.0 m on a MISSED ECHO, and noise/crosstalk yields other
# garbage-short spikes (observed on this build: a steady 1.45 m stream jumped to
# 1.15 m for one sample, then snapped back). The consecutive-samples rule is the
# primary guard (one spike can add at most one to the streak and its revert
# resets it), but these two thresholds reject the obvious cases outright:
#   - Readings at/below MIN_VALID_M are treated as no-echo glitches and ignored
#     (a real target essentially never sits this close to the sensor).
#   - A sample that jumps more than MAX_STEP_M closer than the PREVIOUS sample is
#     implausible for one 0.1 s poll (a fast human walk ~1.5 m/s is ~15 cm), so
#     it is treated as a spurious spike and RESETS the streak. Set at 0.30 m to
#     catch the observed ~0.30 m spike directly.
DEFAULT_APPROACH_MIN_VALID_M = 0.03   # <= this reads as a no-echo glitch
DEFAULT_APPROACH_MAX_STEP_M = 0.30    # a bigger single jump closer = spurious spike

# --- Detection mode + presence defaults (used by ApproachDetector) ---
# "approach" = fire on a sustained getting-closer trend (motion toward the
# sensor). "presence" = fire whenever an object simply sits within the gate for
# a couple of readings (no motion needed) — the natural fit for "someone is
# standing in front of me", and the same contract a PIR sensor will satisfy.
DEFAULT_DETECT_MODE = "approach"
# Consecutive in-gate readings required to confirm PRESENCE. Two ~0.1 s reads
# debounce a lone spurious short spike while still firing within ~0.2 s of
# someone stepping in front.
DEFAULT_PRESENCE_CONSECUTIVE = 2


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

    Detection rule (per the wake spec). An approach must be a SUSTAINED closer
    trend across several polls in a row — not a single big jump — so a lone
    spurious short reading (the HC-SR04's characteristic spike, which reverts on
    the next sample) can never fire the wake:

    - **Range gate** — a reading farther than ``gate_m`` is ignored and RESETS
      the streak, so an object beyond the gate can never trigger a wake and a
      far reading breaks an in-progress approach.
    - **Consecutive closer samples** — each sample is compared to the PREVIOUS
      one. A sample that is closer by a plausible per-poll amount
      (``[min_step_m, max_step_m]``) advances the streak by exactly ONE; an
      approach is confirmed once ``consecutive`` such samples occur IN A ROW
      (default 3). Anything that breaks the chain resets the streak to zero.
    - **Minimum step** — ``min_step_m`` is the floor for "closer": sub-
      ``min_step_m`` wobble counts as stationary jitter and RESETS the streak,
      filtering the sensor's ~1 cm noise so a still object never "approaches".
    - **Spike / glitch rejection** — a reading at/below ``min_valid_m`` is a
      no-echo glitch (gpiozero reports ~0.0 m) and is ignored; a jump closer by
      more than ``max_step_m`` is implausible for one poll (a fast walk is
      ~15 cm per 0.1 s) and is treated as a spurious spike that RESETS the
      streak. Because one spike can advance the streak by at most one (and its
      revert next sample resets it), it takes a genuine, sustained approach to
      reach ``consecutive`` in a row.

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
                 max_step_m=DEFAULT_APPROACH_MAX_STEP_M,
                 publish_source=None,
                 detect_mode=DEFAULT_DETECT_MODE,
                 presence_gate_m=None,
                 presence_consecutive=DEFAULT_PRESENCE_CONSECUTIVE,
                 gate_provider=None):
        """Create an approach/presence detector.

        Args:
            sensor:          An existing ``RangeSensor`` to read from. If None,
                             the detector constructs and owns its own (closed on
                             ``close()``).
            gate_m:          Maximum distance in meters to consider; readings
                             farther than this are ignored and reset the streak.
            consecutive:     Number of consecutive closer samples (in a row)
                             required to confirm an approach (must be >= 1).
            min_step_m:      Minimum decrease in meters between the previous
                             sample and this one to count as "getting closer";
                             smaller changes are stationary jitter and reset the
                             streak.
            max_distance:    Passed to the owned ``RangeSensor`` when ``sensor``
                             is None. Ignored if a sensor is supplied.
            poll_interval_s: Seconds between samples when using the background
                             poller (``start_polling``).
            min_valid_m:     Readings at or below this are treated as no-echo
                             glitches and ignored (the HC-SR04/gpiozero reports
                             ~0.0 m on a missed echo).
            max_step_m:      A sample that jumps more than this much CLOSER than
                             the previous sample is treated as a spurious spike
                             and resets the streak (implausible for one poll).
            publish_source:  When set to a label string (e.g. ``"awake"``), each
                             reading is published via ``range_publish`` for the
                             live dashboard gauge. When ``None`` (default)
                             nothing is published, so a detector used purely for
                             detection has no side effect on the shared file.
            detect_mode:     ``"approach"`` (default) fires on a sustained
                             getting-closer trend; ``"presence"`` fires whenever
                             an object simply sits within ``presence_gate_m`` for
                             ``presence_consecutive`` readings — no motion
                             required. Presence is the right fit for "someone is
                             standing in front of me" and is the contract a PIR
                             sensor will later satisfy directly.
            presence_gate_m: Distance in meters within which an object counts as
                             "present" (presence mode). Defaults to ``gate_m``.
            presence_consecutive: Consecutive in-range readings required to
                             confirm presence (debounces a lone spike). Must be
                             >= 1.
            gate_provider:   Optional zero-arg callable returning the CURRENT
                             gate distance in meters, re-read on every sample so
                             a live sensitivity change (e.g. a dashboard slider)
                             takes effect without recreating the detector. When
                             set it overrides both ``gate_m`` and
                             ``presence_gate_m`` per reading. ``None`` (default)
                             keeps the fixed values passed above.
        """
        self.publish_source = publish_source
        self.detect_mode = detect_mode
        self.presence_gate_m = gate_m if presence_gate_m is None else presence_gate_m
        self.presence_consecutive = max(1, int(presence_consecutive))
        self.gate_provider = gate_provider
        self._present_streak = 0
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
        # Approach is confirmed by CONSECUTIVE closer SAMPLES, not by distance
        # chunks: each qualifying sample advances the streak by exactly one, and
        # any spike / reversal / stationary-gap resets it. This makes a lone
        # spurious short reading (which reverts on the next sample) unable to
        # fire — a real approach shows sustained closer motion across several
        # polls. ``_prev_m`` is the previous in-gate reading we compare against.
        self._prev_m = None
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
            self._prev_m = None
            self._closer_steps = 0
            self._present_streak = 0
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

        # Publish the raw reading for the live dashboard gauge (best-effort).
        self._maybe_publish(distance)

        # ``why`` records how this sample was classified, for RANGE_SENSOR_DEBUG.
        why = None
        with self._lock:
            # No-echo glitch: gpiozero reports ~0.0 m when the ping gets no
            # echo. A real target never sits this close, so ignore the sample
            # entirely (don't touch the anchor/streak) rather than treating it
            # as a huge instantaneous approach.
            if distance <= self.min_valid_m:
                self._debug(distance, f"GLITCH<=min_valid({self.min_valid_m})")
                return False

            # Outside the gate: ignore, and break any in-progress approach.
            gate = self._live_gate(self.gate_m)
            if distance > gate:
                self._prev_m = None
                self._closer_steps = 0
                self._debug(distance, f"beyond gate({gate}) - reset")
                return False

            # First in-gate reading: nothing to compare against yet.
            if self._prev_m is None:
                self._prev_m = distance
                self._closer_steps = 0
                self._debug(distance, "first sample")
                return False

            delta = self._prev_m - distance  # >0 means closer than LAST sample

            # Compare each sample to the PREVIOUS one. A qualifying closer sample
            # advances the streak by exactly ONE; anything else resets it. So an
            # approach must be SUSTAINED across ``consecutive`` polls in a row —
            # a single spurious spike (which reverts next sample) can never reach
            # the threshold. Always advance _prev_m to track the real signal.
            if delta > self.max_step_m:
                # Implausibly large jump closer for one poll (~a fast walk is
                # ~15cm/0.1s) - a spurious short spike. Reset the streak; do NOT
                # count it. Keep prev at the spike so the revert next sample
                # reads as a big recede (also a reset), never a false approach.
                self._closer_steps = 0
                why = f"SPIKE closer>{self.max_step_m} (delta={delta:.3f}) - reset"
            elif delta >= self.min_step_m:
                # A plausible closer step: advance the streak by one.
                self._closer_steps += 1
                why = f"closer sample (delta={delta:.3f})"
            elif delta <= -self.min_step_m:
                # Moved away by more than jitter: receding - reset the streak.
                self._closer_steps = 0
                why = f"receding (delta={delta:.3f}) - reset"
            else:
                # Within +/- min_step_m: stationary/jitter. Not getting closer,
                # so the approach is not sustained - reset the streak. (Real
                # approach shows closer motion on consecutive polls; a stationary
                # sample breaks that chain.)
                self._closer_steps = 0
                why = f"jitter (delta={delta:.3f}) - reset"

            self._prev_m = distance

            # ``consecutive`` closer samples IN A ROW confirm the approach.
            confirmed = self._closer_steps >= self.consecutive
            if confirmed:
                self._triggered = True
                why += "  ==> FIRE"
            self._debug(distance, f"{why} [streak={self._closer_steps}, prev={self._prev_m:.3f}]")
            return confirmed

    def present(self):
        """Take one reading and report whether an object is PRESENT within gate.

        The presence counterpart to ``approaching()``: it fires when an object
        simply sits within ``presence_gate_m`` for ``presence_consecutive``
        readings in a row — NO motion required — so someone standing still in
        front of the sensor triggers it (unlike ``approaching()``, which needs a
        getting-closer trend). This is the natural "is someone there?" test and
        the same contract a PIR sensor will later satisfy.

        Debouncing: a no-echo glitch (``<= min_valid_m``) is IGNORED (neither
        advances nor resets the streak), so a single dropped ping doesn't break
        a real presence. A valid reading beyond the gate resets the streak.

        Returns:
            True on the reading that completes ``presence_consecutive`` in-gate
            readings (latches ``triggered()``), else False.
        """
        distance = self.sensor.distance_m()
        self._maybe_publish(distance)

        with self._lock:
            # No-echo glitch: ignore entirely so one dropped ping doesn't reset a
            # genuine, sustained presence.
            if distance <= self.min_valid_m:
                self._debug(distance, f"presence GLITCH<=min_valid({self.min_valid_m})")
                return False

            gate = self._live_gate(self.presence_gate_m)
            if distance <= gate:
                self._present_streak += 1
                confirmed = self._present_streak >= self.presence_consecutive
                why = f"present (<= {gate}m)"
                if confirmed:
                    self._triggered = True
                    why += "  ==> FIRE"
                self._debug(distance, f"{why} [streak={self._present_streak}]")
                return confirmed

            # Valid reading beyond the gate: no one there — reset the streak.
            self._present_streak = 0
            self._debug(distance, f"absent (> {gate}m) - reset")
            return False

    def _live_gate(self, fallback):
        """Return the current gate in meters (live provider, else ``fallback``).

        Consults ``gate_provider`` when one was supplied so a runtime
        sensitivity change is honoured on every sample; otherwise returns the
        fixed value passed at construction. A provider error falls back too.
        """
        if self.gate_provider is None:
            return fallback
        try:
            m = self.gate_provider()
            return fallback if m is None else float(m)
        except Exception:
            return fallback

    def _maybe_publish(self, distance):
        """Publish a raw reading for the dashboard gauge (best-effort).

        Shared by ``approaching()`` and ``present()``. Only publishes when a
        ``publish_source`` label was set, so a detector used purely for
        detection has no side effect on the shared reading file. A no-echo
        glitch (~0.0 m) is published as ``None`` so the gauge shows "--" rather
        than a bogus 0. Never raises into the poll loop.
        """
        if self.publish_source is None:
            return
        try:
            import range_publish
            shown = None if distance <= self.min_valid_m else distance
            range_publish.publish(shown, source=self.publish_source)
        except Exception:
            pass

    def _debug(self, distance, note):
        """Append one classified reading to the debug log when enabled.

        Writes to ``_DEBUG_FILE`` (not stdout) so the trace survives the mic
        controller's continuous stdout spam and is captured even when the nap
        runs as a subprocess. Best-effort: never raises into the poll loop.
        """
        if not _DEBUG:
            return
        try:
            ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
            with open(_DEBUG_FILE, "a") as fh:
                fh.write(f"{ts} [range] {distance:.3f} m | {note}\n")
        except OSError:
            pass  # logging must never break detection

    # --- Background polling (non-blocking wake flag) ---------------------- #

    def start_polling(self):
        """Start a daemon thread that samples the sensor and latches approaches.

        The thread samples every ``poll_interval_s`` seconds using the
        mode-appropriate classifier (``approaching()`` or ``present()`` per
        ``detect_mode``); once a trigger is confirmed it sets a latched flag that
        ``triggered()`` reports without touching the hardware. Idempotent: a
        second call while already polling is a no-op. The thread is a daemon, so
        it never blocks interpreter shutdown, but call ``stop_polling()`` (or
        ``close()``) to end it cleanly and release the sensor.
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
                # Sample via the mode-appropriate classifier. Both publish the
                # reading and latch triggered() on confirmation.
                if self.detect_mode == "presence":
                    self.present()
                else:
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
