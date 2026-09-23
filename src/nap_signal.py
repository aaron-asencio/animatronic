"""
nap_signal.py

Cross-process STOP-REQUEST signal for long-running Modes (e.g. napping).

A Mode (see the animation vocabulary: a continuous background behaviour that
runs until interrupted) runs as its own process holding the servo lock. When
the operator asks the web app to run something else, the web app cannot just
take the servo lock — the Mode still holds it. Instead it sets this signal to
ask the Mode to wind down cleanly (wake the head, release the lock, exit); the
web app then waits for the lock to free and launches the requested action.

Implementation mirrors ``servo_lock``: a single fixed sentinel FILE in the
project directory (user-owned, so both the root-run Mode process and the
user-run web app can create/read/delete it). The file's mere EXISTENCE is the
"please stop" flag — no content is required. It is machine-local runtime state,
not committed.

Usage — web app (requester) side::

    import nap_signal
    nap_signal.request_stop()          # ask any running Mode to wind down
    # ... wait for the servo lock to free ...

Usage — Mode (napping) side::

    import nap_signal
    nap_signal.clear_stop()            # clear any stale flag at start
    while not interrupted:
        if nap_signal.stop_requested():
            break                      # wind down: wake head, release lock
        ...
    nap_signal.clear_stop()            # tidy up on exit

The Mode is responsible for clearing the flag when it exits so the next Mode
run starts clean.
"""

import os

# Single fixed sentinel file shared by every process on this machine. Anchored
# in the project directory (user-owned) for the same reason as the servo lock:
# the Mode runs as root (sudo) while the web app runs as a normal user, and a
# user cannot delete a root-owned file in /tmp. An env var can override it.
_DEFAULT_SIGNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".nap_stop")
SIGNAL_PATH = os.environ.get("NAP_STOP_SIGNAL_PATH", _DEFAULT_SIGNAL)


def request_stop():
    """Ask any running Mode to wind down and release the servo lock.

    Creates the sentinel file (world-writable so either the root Mode or the
    user web app can later clear it). Idempotent: re-requesting is harmless.

    Returns:
        True if the signal file exists after the call, False if it could not be
        created (best-effort; never raises).
    """
    try:
        fd = os.open(SIGNAL_PATH, os.O_CREAT | os.O_WRONLY, 0o666)
        try:
            os.fchmod(fd, 0o666)  # not umask-masked; let the other party clear it
        except OSError:
            pass
        os.close(fd)
        return True
    except OSError as e:
        print(f"nap_signal: could not set stop request ({e})")
        return False


def stop_requested():
    """Return True if a stop has been requested (the sentinel file exists)."""
    return os.path.exists(SIGNAL_PATH)


def clear_stop():
    """Remove the stop-request sentinel if present (best-effort, never raises)."""
    try:
        os.remove(SIGNAL_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"nap_signal: could not clear stop request ({e})")
