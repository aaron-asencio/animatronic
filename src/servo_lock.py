"""
servo_lock.py

Cross-process mutual exclusion for anything that drives the servos.

SAFETY-CRITICAL: two gesture routines running at once can command the same
servo to conflicting angles. If the arm is driven into a mechanical block it
cannot reach the commanded angle, the servo stalls at locked-rotor current,
overheats, and can burn out the motor/wiring — a fire hazard.

This module provides a single system-wide lock so that only ONE gesture-driving
process can run at a time, regardless of how it was launched (web app,
automation loop, manual CLI, or the legacy Node-RED exec nodes).

Implementation: an advisory file lock via ``fcntl.flock`` on a fixed lockfile.
The lock is held for the lifetime of the acquiring process and released
automatically by the OS if that process exits or is killed — so there is no
stale-lock problem after a crash.

Usage (blocking guard at an entry point):

    from servo_lock import servo_lock, ServoBusyError
    try:
        with servo_lock():          # raises immediately if already held
            asyncio.run(do_gesture())
    except ServoBusyError:
        print("Another routine is already running; skipping.")
        sys.exit(3)                 # exit code 3 == busy

Use ``servo_lock(wait=True)`` to block until the lock is free instead of
failing fast.
"""

import fcntl
import os
import stat
import contextlib

# Single fixed lockfile shared by every process on this machine.
#
# It lives in the project directory (owned by the normal user) rather than /tmp.
# Rationale: the gesture scripts run as root (sudo) while the web app runs as a
# normal user. If the file is created by root in /tmp it becomes root-owned and
# the user-run web app can't open it (the PermissionError we hit). Root can
# freely create/lock a file in a user-owned directory, but a user cannot open a
# root-owned file — so anchoring the lock in the user-owned project dir makes
# access work in both directions. An env var can override the location.
_DEFAULT_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".servo.lock")
LOCK_PATH = os.environ.get("SERVO_LOCK_PATH", _DEFAULT_LOCK)

# Exit code used by CLI entry points when the servos are already in use.
BUSY_EXIT_CODE = 3


class ServoBusyError(Exception):
    """Raised when the servo lock is already held by another process."""


def _open_lockfile():
    """Open (creating if needed) the shared lockfile for read/write.

    The gesture scripts run as root (sudo) while the web app runs as a normal
    user, so the file may be created by either. We make it world-read/write so
    both can open it regardless of which created it:

    - O_CREAT with 0o666 sets the mode ONLY on creation, and even then it is
      masked by the process umask (commonly 022 -> 0o644), which would lock out
      the other user. So we ALSO chmod 0o666 explicitly after opening, which is
      not umask-masked.
    - chmod only succeeds for the file owner (or root). If we are not the owner
      and the mode is already permissive enough, the open still works and the
      chmod failure is harmless — so we ignore chmod errors.

    Returns an open file descriptor.
    """
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    # Best-effort widen perms so the other (root/user) party can open it too.
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP |
                  stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)  # 0o666
    except OSError:
        pass  # Not the owner; the existing mode must already allow our access.
    return fd


@contextlib.contextmanager
def servo_lock(wait=False):
    """Acquire the system-wide servo lock for the duration of the block.

    Args:
        wait: If False (default), raise ServoBusyError immediately when another
              process holds the lock. If True, block until it becomes free.

    Raises:
        ServoBusyError: when wait=False and the lock is already held.

    The lockfile records the holding PID for diagnostics.
    """
    # Open (create) the lockfile. Keep the fd open for the whole block — the
    # flock is tied to this open file description and released when it closes.
    fd = _open_lockfile()

    # Try to acquire first. If this fails we close the fd and bail out WITHOUT
    # entering the try/finally, so the fd is only ever closed once.
    flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
    except (BlockingIOError, OSError):
        os.close(fd)
        raise ServoBusyError(
            "Servos are already in use by another process. "
            "Refusing to run a second routine concurrently."
        )

    # We hold the lock. From here the finally block owns releasing/closing fd.
    try:
        # Record our PID for humans debugging a stuck lock.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
        except OSError:
            pass  # PID bookkeeping is best-effort; the lock itself is what matters.

        yield
    finally:
        # Release the lock and close the fd. flock is also auto-released by the
        # OS if the process dies before reaching here.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def is_locked():
    """Return True if another process currently holds the servo lock.

    Non-destructive probe: tries a non-blocking acquire and immediately
    releases if it succeeds. Used by the web app to report 'busy' without
    actually taking the lock.

    FAIL-SAFE: if the lockfile can't even be opened (e.g. a permission problem),
    we return False rather than raising. This is a status probe, not the safety
    boundary — the real guarantee is the flock the gesture scripts hold. A
    status probe must never crash the /status route.
    """
    try:
        fd = _open_lockfile()
    except OSError:
        # Cannot inspect the lock; report "not busy" so the UI stays usable.
        # The gesture scripts still enforce exclusion via their own flock.
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got it — nobody else holds it. Release right away.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)
