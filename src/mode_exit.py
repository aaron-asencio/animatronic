"""Shared mode end-reason <-> process exit-code mapping.

This tiny leaf module is the SINGLE source of truth for how a chainable Mode
(napping / awake) tells its parent ``webapp.py`` WHY it ended. The mode runs as
a subprocess, and the only channel the non-blocking ``Popen`` launcher observes
is the process exit code — so ``animatronic.py``'s ``main()`` maps the mode's
returned end-reason string to one of these exit codes via
``reason_to_exit_code()``, and ``webapp.py``'s chain watcher reads the code back
and classifies it via ``CHAINABLE_EXIT_CODES``.

It imports ONLY the standard library (nothing — pure constants/helpers) and no
project upper-layer modules (no ``Animatronic``, ``Movements``, ServoKit,
PyAudio, numpy), so ``webapp.py`` can share the table without pulling in heavy
``animatronic`` internals. This respects "higher layers call lower"; it is a
leaf alongside ``servo_lock``'s ``BUSY_EXIT_CODE``.

Debug output (where used) is via print() to match the rest of the codebase (no
logging framework).
"""

# Process exit codes a chainable Mode (napping/awake) uses to tell the parent
# webapp.py WHY it ended. Kept distinct from servo_lock.BUSY_EXIT_CODE (3) and
# from the conventional 0 (clean) / 1 (crash) so a chainable natural end is
# never confused with a busy refusal or a crash.
EXIT_TIMEOUT = 10   # mode ended because its timeout elapsed  -> chain
EXIT_SENSOR = 11    # mode ended because a sensor event fired  -> chain
EXIT_STOP = 12      # mode wound down on an external stop       -> do NOT chain

# Reason strings returned by napping()/awake() loops (already defined on the
# Animatronic class as NAP_INTERRUPT_* / AWAKE_INTERRUPT_*; these are the SAME
# string values, duplicated here as the leaf-level canonical names so webapp.py
# needs no Animatronic import).
REASON_TIMEOUT = "timeout"
REASON_SENSOR = "sensor"
REASON_STOP = "stop"

REASON_TO_EXIT = {
    REASON_TIMEOUT: EXIT_TIMEOUT,
    REASON_SENSOR: EXIT_SENSOR,
    REASON_STOP: EXIT_STOP,
}

# Exit codes that mean "ended naturally -> eligible to chain". STOP is excluded
# on purpose (operator intent breaks the chain), as are the busy (3), crash (1),
# and clean (0) codes.
CHAINABLE_EXIT_CODES = frozenset({EXIT_TIMEOUT, EXIT_SENSOR})


def reason_to_exit_code(reason):
    """Map a mode end-reason string to its process exit code.

    Args:
        reason: One of ``REASON_TIMEOUT`` / ``REASON_SENSOR`` / ``REASON_STOP``,
            or an unknown value / ``None``.

    Returns:
        The matching exit code. An unknown or ``None`` reason maps to
        ``EXIT_STOP`` (fail safe: never chain on an unrecognised end).
    """
    return REASON_TO_EXIT.get(reason, EXIT_STOP)


def exit_code_to_reason(code):
    """Map a process exit code back to its end-reason string.

    The inverse of ``reason_to_exit_code`` for the three known codes.

    Args:
        code: A process exit code.

    Returns:
        The matching reason string for ``EXIT_TIMEOUT`` / ``EXIT_SENSOR`` /
        ``EXIT_STOP``; ``REASON_STOP`` for any other/unknown code (fail safe:
        an unrecognised code is treated as a non-chaining stop).
    """
    for reason, mapped in REASON_TO_EXIT.items():
        if mapped == code:
            return reason
    return REASON_STOP
