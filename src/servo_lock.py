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

import constants

# ---------------------------------------------------------------------------
# Channel groups
# ---------------------------------------------------------------------------
# The servo lock is per channel GROUP, not whole-robot: Tracking_Mode (neck)
# and an arm-only Gesture (arm) write DISJOINT channels and so may run
# concurrently, each holding only its own group's lock.
#
# Group -> channel membership is derived from ``constants`` (never hardcode the
# channel ints) so the groups track the single source of truth in constants.py.
NECK_GROUP = "neck"
ARM_GROUP = "arm"

GROUP_CHANNELS = {
    NECK_GROUP: (constants.NECK_PAN, constants.NECK_TILT),
    ARM_GROUP: (constants.RT_ELBOW_ROTATOR, constants.RT_ELBOW_TILT,
                constants.RT_SHOULDER_TILT, constants.RT_SHOULDER_ROTATOR),
}

# Lockfiles live in the project directory (owned by the normal user) rather
# than /tmp. Rationale: the gesture scripts run as root (sudo) while the web app
# runs as a normal user. If the file is created by root in /tmp it becomes
# root-owned and the user-run web app can't open it (the PermissionError we
# hit). Root can freely create/lock a file in a user-owned directory, but a user
# cannot open a root-owned file — so anchoring the locks in the user-owned
# project dir makes access work in both directions. An env var can override the
# location.
#
# LEGACY: ``LOCK_PATH`` was the single whole-robot lockfile. ``servo_lock`` no
# longer uses it — the whole-robot lock is now "hold every per-group lock"
# (see ``servo_lock``), so exclusion lives entirely in the per-group lockfiles
# below. ``LOCK_PATH`` is kept defined only so any lingering importer of the
# name still resolves; nothing in the active lock path reads or writes it.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOCK = os.path.join(_SRC_DIR, ".servo.lock")
LOCK_PATH = os.environ.get("SERVO_LOCK_PATH", _DEFAULT_LOCK)

# One lockfile per group, each anchored in the same user-owned project dir with
# the same world-rw treatment, so root-run gesture scripts and the user-run
# Control_Panel can both open them. Because each group has its own flock'ed
# file, the OS still auto-releases that group's lock on process death.
_DEFAULT_NECK_LOCK = os.path.join(_SRC_DIR, ".servo.neck.lock")
_DEFAULT_ARM_LOCK = os.path.join(_SRC_DIR, ".servo.arm.lock")
GROUP_LOCK_PATHS = {
    NECK_GROUP: os.environ.get("SERVO_NECK_LOCK_PATH", _DEFAULT_NECK_LOCK),
    ARM_GROUP: os.environ.get("SERVO_ARM_LOCK_PATH", _DEFAULT_ARM_LOCK),
}

# Exit code used by CLI entry points when the servos are already in use.
BUSY_EXIT_CODE = 3


class ServoBusyError(Exception):
    """Raised when the servo lock is already held by another process."""


def _resolve_group(group):
    """Return the canonical group name, raising ValueError for unknown groups.

    Args:
        group: A group identifier — one of ``NECK_GROUP`` / ``ARM_GROUP``.

    Returns:
        The validated group name.

    Raises:
        ValueError: if ``group`` is not a known channel group.
    """
    if group not in GROUP_CHANNELS:
        raise ValueError(
            f"Unknown servo channel group {group!r}; "
            f"expected one of {sorted(GROUP_CHANNELS)}."
        )
    return group


def _open_lockfile(path=None):
    """Open (creating if needed) a lockfile for read/write.

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

    Args:
        path: Lockfile path to open. Defaults to the legacy whole-robot
              ``LOCK_PATH`` so existing callers are unaffected.

    Returns an open file descriptor.
    """
    if path is None:
        path = LOCK_PATH
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    # Best-effort widen perms so the other (root/user) party can open it too.
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP |
                  stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)  # 0o666
    except OSError:
        pass  # Not the owner; the existing mode must already allow our access.
    return fd


@contextlib.contextmanager
def _hold_lockfile(path, wait, busy_message):
    """Hold an advisory flock on ``path`` for the duration of the block.

    Shared implementation behind both the whole-robot ``servo_lock`` and the
    per-group ``group_lock`` — identical wait/non-wait semantics, PID
    bookkeeping, and OS auto-release, differing only in which lockfile is used
    and the busy message.

    Args:
        path: Lockfile path to acquire.
        wait: If False, raise ServoBusyError immediately when the lock is held
              by another process. If True, block until it becomes free.
        busy_message: Message for the ServoBusyError raised on a non-waiting
              acquire that finds the lock already held.

    Raises:
        ServoBusyError: when wait=False and the lock is already held.
    """
    # Open (create) the lockfile. Keep the fd open for the whole block — the
    # flock is tied to this open file description and released when it closes.
    fd = _open_lockfile(path)

    # Try to acquire first. If this fails we close the fd and bail out WITHOUT
    # entering the try/finally, so the fd is only ever closed once.
    flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
    except (BlockingIOError, OSError):
        os.close(fd)
        raise ServoBusyError(busy_message)

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


def _rest_groups(groups):
    """Drive ONLY the given groups' channels to ``constants.REST_POSITIONS``.

    Called when a group lock is released (normal exit or error) so a writer
    never leaves its channels energized against a jam (Req 8.7). The write is
    strictly scoped: it touches ONLY the channels in each released group's
    ``GROUP_CHANNELS`` and never any channel outside the released groups
    (Property 16, a write-scoping guarantee).

    Every write goes through ``TrunkController.set_angle``, so each commanded
    angle is still clamped to ``constants.SAFE_LIMITS`` — this helper only
    chooses WHICH channels to rest, never bypasses the safety clamp.

    FAIL-SAFE: this runs in the lock's release path, so it must never raise and
    mask the original control flow (mirroring ``TrunkController.return_to_rest``).
    Any failure constructing the controller or writing a channel is caught and
    reported, and the remaining channels are still attempted. Under
    ``SERVO_SIM=1`` the writes go to the fake kit (logged, no hardware).

    ``TrunkController`` is imported lazily (not at module top) so that
    status-only consumers — e.g. the Control_Panel probing lock state via
    ``is_locked`` / ``locks_status`` — never pull in the servo/hardware libs or
    construct a ``ServoKit`` just to read a lock.

    Args:
        groups: Iterable of already-resolved group names to rest.
    """
    try:
        from trunkcontroller import TrunkController
        trunk = TrunkController("servo_lock rest-on-release")
    except Exception as e:
        # Can't build the controller (missing hardware libs, etc.). Don't mask
        # the surrounding flow — the OS still releases the flock regardless.
        print(f"servo_lock: could not rest on release (controller unavailable): {e}")
        return

    for group in groups:
        for channel in GROUP_CHANNELS[group]:
            rest_angle = constants.REST_POSITIONS.get(channel)
            if rest_angle is None:
                # No rest position defined for this channel; skip rather than
                # guess an angle.
                continue
            try:
                trunk.set_angle(channel, rest_angle)
            except Exception as e:
                name = constants.servos.get(channel, f"ch{channel}")
                print(f"servo_lock: failed to rest {name} on release: {e}")


def _probe_lockfile(path):
    """Return True if another process currently holds the flock on ``path``.

    Non-destructive probe: tries a non-blocking acquire and immediately
    releases if it succeeds.

    FAIL-SAFE: if the lockfile can't even be opened (e.g. a permission problem),
    we return False rather than raising. This is a status probe, not the safety
    boundary — the real guarantee is the flock the gesture scripts hold. A
    status probe must never crash the /status route.
    """
    try:
        fd = _open_lockfile(path)
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


@contextlib.contextmanager
def servo_lock(wait=False):
    """Acquire the whole-robot servo lock for the block — ALL channel groups.

    Backward-compatible entry point for existing neck+arm routines (the CLI
    ``--action`` branches in ``animatronic.py`` / ``controller.py`` and the
    ``napping`` / ``awake`` Modes). Those routines may drive BOTH the neck and
    the arm, so "whole-robot" exclusion means holding EVERY channel group at
    once. Rather than keep a separate legacy lockfile, this now delegates to
    ``group_locks(*GROUP_CHANNELS)`` so there is a single source of truth for
    what "the servos are in use" means:

    - Acquisition is all-or-none across every group (Req 8.6): if any group is
      already held by another process, no group is taken and ``ServoBusyError``
      is raised. This preserves the original fail-fast semantics — a second
      whole-robot routine still cannot start while any part of the robot is in
      use.
    - On exit or error, EACH group's channels are returned to
      ``constants.REST_POSITIONS`` through the per-group rest-on-release path
      (``TrunkController.set_angle``, SAFE_LIMITS-clamped), so the whole robot
      rests — matching the previous whole-robot behavior.

    Because it now holds the per-group locks, a whole-robot ``servo_lock`` also
    correctly excludes a group-only writer (e.g. Tracking_Mode holding just the
    neck) and vice versa: the group-only writer's lock makes the all-or-none
    acquisition here fail, so the two can never drive overlapping channels.

    Args:
        wait: If False (default), raise ServoBusyError immediately when any
              group is already held by another process. If True, block until
              every group becomes free.

    Raises:
        ServoBusyError: when wait=False and any channel group is already held.

    Each per-group lockfile records the holding PID for diagnostics.
    """
    with group_locks(*GROUP_CHANNELS, wait=wait):
        yield


def is_locked():
    """Return True if ANY channel group is currently held by another process.

    Non-destructive probe used by the web app to report 'busy' without actually
    taking the lock. Fail-safe to not-held if a lockfile can't be opened.

    NOTE (meaning change): previously this probed a single whole-robot lockfile
    (``LOCK_PATH``). Now that ``servo_lock`` holds the per-group locks instead,
    whole-robot "locked" means "ANY group is held" — so a group-only writer
    (e.g. Tracking_Mode on the neck) now correctly reports the servos as busy.
    This is strictly more conservative than before and keeps every existing
    ``is_locked`` consumer (the web app's busy check / spawn-avoidance) working:
    a whole-robot ``servo_lock`` holder still makes this return True because it
    holds every group.
    """
    return any(locks_status().values())


@contextlib.contextmanager
def group_lock(group, wait=False):
    """Hold ONE channel group's lock for the duration of the block.

    Tracking_Mode (neck) and an arm-only Gesture (arm) each take only their own
    group's lock, so they run concurrently on disjoint channels. The group's
    lockfile records the holding PID and is auto-released by the OS on process
    death, exactly like the whole-robot lock.

    Args:
        group: The channel group to lock — ``NECK_GROUP`` or ``ARM_GROUP``.
        wait: If False (default), raise ServoBusyError immediately when that
              group's lock is already held by another process. If True, block
              until it becomes free.

    Raises:
        ValueError: if ``group`` is not a known channel group.
        ServoBusyError: when wait=False and that group's lock is already held.

    Rest-on-release (Req 8.7): on normal exit OR error, this group's channels
    are returned to ``constants.REST_POSITIONS`` via ``TrunkController.set_angle``
    (SAFE_LIMITS clamp) and NO channel outside this group is touched.
    """
    group = _resolve_group(group)
    with _hold_lockfile(
        GROUP_LOCK_PATHS[group], wait,
        f"Servo channel group {group!r} is already in use by another process. "
        "Refusing to drive its channels concurrently.",
    ):
        try:
            yield
        finally:
            # Rest only this group's channels, whether the block exited
            # normally or raised — a writer never leaves its channels energized.
            _rest_groups((group,))


@contextlib.contextmanager
def group_locks(*groups, wait=False):
    """Atomically hold MULTIPLE channel groups for the block — all-or-none.

    Acquires every requested group in a DETERMINISTIC order (sorted by group
    name) so that two writers requesting the same set can never deadlock by
    taking them in opposite orders. Acquisition is all-or-none (Req 8.6): if any
    requested group's lock is unavailable in non-waiting mode, every group
    already taken in this call is released and ``ServoBusyError`` is raised, so
    no partial hold remains.

    On exit or error, EACH held group's channels are returned to
    ``constants.REST_POSITIONS`` and NO channel outside the held groups is
    touched (Req 8.7) — rest-on-release is delegated to the per-group
    ``group_lock`` acquired for each group, so the same scoped, SAFE_LIMITS-
    clamped rest path applies here. If acquisition fails partway, the groups
    already taken are released (and rested) as the stack unwinds.

    Args:
        *groups: One or more channel groups to lock — ``NECK_GROUP`` /
            ``ARM_GROUP``. Duplicates are collapsed; each distinct group is
            acquired once.
        wait: If False (default), raise ServoBusyError immediately when any
            requested group's lock is already held. If True, block until each
            becomes free (acquired in the same deterministic order).

    Raises:
        ValueError: if any group is not a known channel group.
        ServoBusyError: when wait=False and any requested group is already held;
            any groups taken before the failure are released first.
    """
    # Validate and de-dup up front, then acquire in a deterministic order so a
    # consistent lock ordering prevents deadlock between concurrent requesters.
    resolved = sorted({_resolve_group(g) for g in groups})

    with contextlib.ExitStack() as stack:
        for group in resolved:
            # group_lock handles the flock AND the scoped rest-on-release for
            # this group. If a later group raises ServoBusyError, ExitStack
            # unwinds the groups already entered here — releasing and resting
            # each — so no partial hold survives (all-or-none, Req 8.6).
            stack.enter_context(group_lock(group, wait=wait))
        yield


def group_is_locked(group):
    """Return True if another process currently holds ``group``'s lock.

    Non-destructive per-group probe (non-blocking acquire + immediate release),
    the per-group analogue of ``is_locked``. Fail-safe to not-held if the
    group's lockfile can't be opened, so it never crashes the /status route.

    Args:
        group: The channel group to probe — ``NECK_GROUP`` or ``ARM_GROUP``.

    Raises:
        ValueError: if ``group`` is not a known channel group.
    """
    group = _resolve_group(group)
    return _probe_lockfile(GROUP_LOCK_PATHS[group])


def locks_status():
    """Return the held/free state of every channel group for the Control_Panel.

    Returns:
        A dict mapping each group name to a bool that is True when the group's
        lock is currently held by some process. Each probe is non-destructive
        and fail-safe to not-held.
    """
    return {group: group_is_locked(group) for group in GROUP_CHANNELS}
