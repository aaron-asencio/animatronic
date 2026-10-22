"""
motion_publish.py

Cross-process publish/read of the latest HC-SR501 PIR motion state, so the web
dashboard can show a live motion indicator no matter which process currently
owns the sensor's GPIO pin.

Only ONE process may open the HC-SR501 at a time (a single GPIO device). The
web app's lightweight poller owns it and PUBLISHES each reading here (a tiny
JSON file), and the web app's ``/motion`` endpoint just READS the file. This
mirrors ``range_publish`` / ``servo_lock`` / ``nap_signal``: a single fixed
file in the project directory, user-owned so both a root-run process and the
user-run web app can create/read it. It is machine-local runtime state, not
committed.

Payload (JSON)::

    {"motion": true, "ts": 1730000000.5, "source": "webapp"}

``motion`` is the boolean motion state at ``ts`` (a Unix timestamp used to
decide staleness). ``source`` is a free-form label for debugging (which owner
wrote it).

A STALE reading reads as ``None`` — i.e. callers treat a missing or stale
reading as "no motion" (fail safe: an indicator that sticks ON after the
publisher dies would be misleading).

Usage — sensor owner side::

    import motion_publish
    motion_publish.publish(motion, source="webapp")   # every poll

Usage — reader (web app) side::

    import motion_publish
    reading = motion_publish.read_latest()   # dict or None
"""

import json
import os
import time

# Single fixed file shared by every process on this machine, anchored in the
# project directory (user-owned) for the same reason as range_publish: a
# root-run process and the user-run web app must both be able to read/write it.
# An env var can override it (e.g. for tests).
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".motion_reading")
READING_PATH = os.environ.get("MOTION_READING_PATH", _DEFAULT_PATH)

# A reading older than this many seconds is considered STALE — the owner stopped
# publishing (e.g. the poller died or the sensor is wedged). Readers treat a
# stale reading as "no motion".
STALE_AFTER_S = 3.0


def publish(motion, source="unknown"):
    """Write the latest motion state to the shared file (best-effort).

    Args:
        motion: Current motion state; coerced to a bool before writing.
        source: Free-form label identifying the publishing owner (for debug).

    Returns:
        True on success, False if the file could not be written (never raises).
    """
    payload = {
        "motion": bool(motion),
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
        print(f"motion_publish: could not publish reading ({e})")
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
        A dict ``{"motion", "ts", "source", "age_s"}`` for a fresh reading, or
        ``None`` when no reading exists, it is stale, or it cannot be parsed. A
        stale/missing reading reads as None so callers treat it as no motion.
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


def clear():
    """Remove the shared reading file if present (best-effort, never raises)."""
    try:
        os.remove(READING_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"motion_publish: could not clear reading ({e})")
