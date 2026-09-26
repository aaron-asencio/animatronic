"""
range_publish.py

Cross-process publish/read of the latest HC-SR04 range reading, so the web
dashboard can show a live distance gauge no matter which process currently owns
the sensor's GPIO pins.

Only ONE process may open the HC-SR04 at a time (a single GPIO device). Which
process that is changes at runtime:

  - While a Mode runs (napping / awake), the Mode's ``ApproachDetector`` owns
    the sensor.
  - Otherwise the web app's own lightweight poller owns it.

Rather than fight over the pins, whichever process owns the sensor PUBLISHES
each reading here (a tiny JSON file), and the web app's ``/range`` endpoint just
READS the file. This mirrors ``servo_lock`` / ``nap_signal``: a single fixed
file in the project directory, user-owned so both the root-run Mode process and
the user-run web app can create/read it. It is machine-local runtime state, not
committed.

Payload (JSON)::

    {"distance_m": 1.23, "ts": 1730000000.5, "source": "awake"}

``distance_m`` is ``null`` when the last read failed / no echo. ``ts`` is a
Unix timestamp used to decide staleness. ``source`` is a free-form label for
debugging (which owner wrote it).

Usage — sensor owner side::

    import range_publish
    range_publish.publish(distance_m, source="awake")   # every poll

Usage — reader (web app) side::

    import range_publish
    reading = range_publish.read_latest()   # dict or None
"""

import json
import os
import time

# Single fixed file shared by every process on this machine, anchored in the
# project directory (user-owned) for the same reason as servo_lock/nap_signal:
# the Mode runs as root (sudo) while the web app runs as a normal user. An env
# var can override it (e.g. for tests).
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".range_reading")
READING_PATH = os.environ.get("RANGE_READING_PATH", _DEFAULT_PATH)

# A reading older than this many seconds is considered STALE — the owner stopped
# publishing (e.g. the mode exited and no poller took over, or the sensor is
# wedged). Readers should treat a stale reading as "no data".
STALE_AFTER_S = 3.0

# Runtime sensor sensitivity (detection gate) in meters, shared cross-process
# like the reading itself. The dashboard slider writes it; the running Mode's
# detector reads it live so a change takes effect without restarting the mode.
# Stored as its own tiny file (machine-local runtime state, not committed).
_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    ".range_config")
CONFIG_PATH = os.environ.get("RANGE_CONFIG_PATH", _DEFAULT_CONFIG_PATH)

# Slider bounds + default for the detection gate (meters). The mode triggers on
# an object within this distance; a smaller value = less sensitive (must be
# closer), larger = more sensitive (reacts from farther away).
GATE_MIN_M = 0.5
GATE_MAX_M = 5.0
GATE_DEFAULT_M = 3.0


def set_gate_m(meters):
    """Persist the detection gate (meters), clamped to [GATE_MIN_M, GATE_MAX_M].

    Written by the dashboard sensitivity slider; read live by the running mode's
    detector. Best-effort; never raises.

    Args:
        meters: Desired gate distance in meters (clamped to the allowed range).

    Returns:
        The clamped value actually stored, or ``None`` if it could not be
        written.
    """
    try:
        m = max(GATE_MIN_M, min(GATE_MAX_M, float(meters)))
    except (TypeError, ValueError):
        return None
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o666)
        try:
            os.write(fd, json.dumps({"gate_m": m}).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o666)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
        return m
    except OSError as e:
        print(f"range_publish: could not set gate ({e})")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def get_gate_m(default=GATE_DEFAULT_M):
    """Return the configured detection gate in meters, or ``default``.

    Read every poll by the mode's detector so a slider change takes effect live.
    Falls back to ``default`` when the config file is missing/unreadable, and
    clamps any stored value into the allowed range.

    Args:
        default: Value to return when no valid config exists.

    Returns:
        The gate distance in meters.
    """
    try:
        with open(CONFIG_PATH, "r") as fh:
            data = json.load(fh)
        m = float(data.get("gate_m"))
    except (FileNotFoundError, ValueError, TypeError, OSError):
        return default
    return max(GATE_MIN_M, min(GATE_MAX_M, m))


def publish(distance_m, source="unknown"):
    """Write the latest range reading to the shared file (best-effort).

    Args:
        distance_m: Distance in meters, or ``None`` when the read failed / no
            echo (published as JSON ``null`` so the reader can show "--").
        source: Free-form label identifying the publishing owner (for debug).

    Returns:
        True on success, False if the file could not be written (never raises).
    """
    payload = {
        "distance_m": None if distance_m is None else float(distance_m),
        "ts": time.time(),
        "source": source,
    }
    tmp = f"{READING_PATH}.{os.getpid()}.tmp"
    try:
        # Write to a temp file then atomically rename, so a reader never sees a
        # half-written JSON document.
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o666)
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o666)  # let the other party overwrite/clear it
        except OSError:
            pass
        os.replace(tmp, READING_PATH)
        return True
    except OSError as e:
        print(f"range_publish: could not publish reading ({e})")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read_latest(max_age_s=STALE_AFTER_S):
    """Read the latest published reading, or None if missing/stale/unreadable.

    Args:
        max_age_s: Consider the reading stale (return None) if it is older than
            this many seconds.

    Returns:
        A dict ``{"distance_m", "ts", "source", "age_s"}`` for a fresh reading
        (``distance_m`` may be ``None`` for a no-echo read), or ``None`` when no
        reading exists, it is stale, or it cannot be parsed.
    """
    try:
        with open(READING_PATH, "r") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return None
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    age = time.time() - ts
    if age > max_age_s:
        return None
    payload["age_s"] = age
    return payload


def read_distance_m(default=None, max_age_s=STALE_AFTER_S):
    """Return just the latest published distance in meters, or ``default``.

    Convenience wrapper over ``read_latest`` for consumers that only need the
    number. Returns ``default`` when there is no fresh reading OR the last read
    got no echo (``distance_m`` is ``None``).

    Args:
        default: Value to return when no fresh valid distance is available.
        max_age_s: Staleness cutoff forwarded to ``read_latest``.

    Returns:
        The distance in meters, or ``default``.
    """
    reading = read_latest(max_age_s=max_age_s)
    if reading is None or reading.get("distance_m") is None:
        return default
    return reading["distance_m"]


class PublishedReadingSensor:
    """A ``RangeSensor``-compatible shim that reads the PUBLISHED distance.

    Exposes the one method ``ApproachDetector`` needs — ``distance_m()`` — but
    instead of touching the HC-SR04 GPIO it returns the latest value published
    by whichever process actually owns the sensor (normally the web app). This
    lets a Mode reuse the full ``ApproachDetector`` approach/presence logic
    while a SINGLE process owns the pins, eliminating cross-process GPIO
    contention (the cause of a blank gauge / missed triggers during a Mode).

    When there is no fresh reading (owner not publishing yet, or a no-echo
    read), ``distance_m`` returns ``absent_m`` — a distance far beyond any gate —
    so the detector simply reads it as "nothing there" rather than a false close
    reading. ``close()`` is a no-op (it owns no hardware).

    Args:
        absent_m: Distance (meters) reported when no fresh reading exists.
            Defaults to a large value so it always reads as out-of-gate.
        max_age_s: Staleness cutoff for a published reading.
    """

    def __init__(self, absent_m=99.0, max_age_s=STALE_AFTER_S):
        self.absent_m = absent_m
        self.max_age_s = max_age_s

    def distance_m(self):
        """Return the latest published distance, or ``absent_m`` if none/stale."""
        return read_distance_m(default=self.absent_m, max_age_s=self.max_age_s)

    def close(self):
        """No-op: this shim owns no GPIO."""
        pass


def clear():
    """Remove the shared reading file if present (best-effort, never raises)."""
    try:
        os.remove(READING_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"range_publish: could not clear reading ({e})")
