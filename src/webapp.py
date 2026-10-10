"""
webapp.py

Flask + HTML control panel for the animatronic — a self-contained replacement
for the Node-RED "Halloween Controller" dashboard.

It provides the same controls the Node-RED flow did:
  - Routines   : full gesture + audio routines  (animatronic.py --action=<name>)
  - Movements  : gesture-only tests             (controller.py  --action=<name>)
  - Mic stream : start/stop live mic passthrough (proxied to micwebcontroller)
  - Voice FX   : style preset + per-effect toggles/sliders (proxied)
  - Jaw Tuning : adaptive envelope (silence floor / open+close ratio / adapt speed / close hold), proxied

Design notes:
  - Routines and movements run as subprocesses using the venv Python, exactly
    like the old Node-RED `exec` nodes. This keeps hardware access in the
    dedicated scripts and avoids sharing a ServoKit across processes.
  - Mic / jaw / effects are handled by micwebcontroller.py (the PyAudio stream
    lives there). This app proxies those requests so there is one control panel.
  - Run this app as root for consistency with the other scripts, though it does
    not touch hardware directly.

Usage:
    sudo .venv/bin/python3 webapp.py
    # then open http://<pi-ip>:8000/ in a browser
"""

from flask import Flask, render_template, request, jsonify, Response
import subprocess
import threading
import time
import os
import json
import urllib.request
import urllib.error

from servo_lock import is_locked
import nap_signal
import mode_exit
import range_publish
import config_store
# Read-only reference data for the mode panels (sensors + interrupt allowlists).
# Defined next to the mode methods in animatronic.py so there is one source of
# truth; this app only displays it (passed to the index template below).
from animatronic import MODE_INTERRUPT_REFERENCE
# The arm-only-safe Scan action allowlist (name -> ScanActionKind). This is the
# single source of truth for what Scan may actually run while the neck tracks a
# person, so the Scan response-pool UI is built from it — the operator can only
# select from the arm-only-safe set, never the full routine/gesture lists (which
# include head-coupled actions Scan would silently filter out anyway).
from detection_routine_map import SCAN_SAFE_ARM_ACTIONS, ScanActionKind

app = Flask(__name__)

# ── Paths / config ───────────────────────────────────────────────────────────
# Absolute paths so subprocess launches work regardless of cwd (matches the
# hardcoded deployment paths used elsewhere in the project).
# PROJECT_DIR is the src/ directory containing this module. The .venv lives at
# the repo root (the parent of src/), while the gesture scripts (animatronic.py
# and controller.py) are siblings of this module inside src/.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PROJECT_DIR)
VENV_PYTHON = os.path.join(REPO_ROOT, '.venv', 'bin', 'python3')
ANIMATRONIC = os.path.join(PROJECT_DIR, 'animatronic.py')
CONTROLLER = os.path.join(PROJECT_DIR, 'controller.py')

# The mic controller (PyAudio stream + effects engine) runs separately on 5000.
MIC_CONTROLLER_URL = 'http://localhost:5000'

# The Camera_Service (non-root camera capture + detection pipeline) runs
# separately on 8001, bound to loopback on the Pi (see camera_service.py
# HTTP_PORT). The control panel proxies its read-only views (Live_Feed MJPEG,
# detections, status). Camera routes are strictly READ-ONLY — they issue NO
# servo command (Req 2.7).
CAMERA_SERVICE_URL = 'http://localhost:8001'

# Base directory for selectable detector models. Any route that accepts a value
# which becomes a filesystem path (e.g. a selectable detector `model` name) must
# resolve it under this base and confirm it does not escape — see
# `_validate_within_base` (Req 9.4, 9.5). Kept in ONE place so Pi-specific paths
# are not scattered through the code (see code-security steering).
MODELS_DIR = os.path.join(PROJECT_DIR, 'models')

# ── Allowlists ───────────────────────────────────────────────────────────────
# Only actions in these sets may be dispatched. This is the security boundary:
# nothing from the request is ever interpolated into a shell — we pass a fixed
# script path plus a validated --action value as separate argv entries.
ROUTINE_ACTIONS = {
    'startParty', 'blah', 'krusty',
    'vincentPrice', 'moreCandy', 'snuckUp', 'brains', 'yawn', 'hypnotic',
    'awaken', 'clearThroat', 'coughLong', 'coughMedium', 'burp', 'fart',
    'fartGhost', 'sneeze', 'comeGetCandy', 'niceDay', 'maximus', 'exorcist',
}

MOVEMENT_ACTIONS = {
    'wave', 'comeHere', 'beckon', 'menacingReach', 'yawnCover', 'facePalm',
    'fanButt', 'fanNose', 'tapSide', 'talkingWithHands', 'talkingHandsII',
    'yes', 'lookAroundSmall', 'lookAroundRandom', 'neckEllipse',
    'swivelHead', 'shakeHead', 'snapHead', 'smno', 'snuckUp', 'awaken',
    'waveAndSwivelSmooth', 'handVisor',
}

# Tracking Mode allowlist. Tracking is launched via animatronic.py like any
# other action, so its name is gated the same way as routines/movements: the
# only permitted value is the fixed 'tracking' action. This is the security
# boundary for the /tracking route — the request's path value is checked against
# this set before any subprocess is spawned, and the action is passed as a
# separate, fixed argv entry (never interpolated into a shell) (Req 9.1-9.3).
TRACKING_ACTIONS = {'tracking'}

# Scan Mode allowlist. Scan is launched via animatronic.py like any other
# action, so its name is gated the same way: the only permitted value is the
# fixed 'scan' action. This set is checked before any subprocess is spawned, and
# the action is passed as a separate, fixed argv entry (never interpolated into
# a shell) (Req 9.1-9.3).
SCAN_ACTIONS = {'scan'}

# Puppeteer Mode allowlist. Like TRACKING_ACTIONS/SCAN_ACTIONS, the only
# permitted action name is the fixed 'puppeteer'. Checked before any subprocess
# is spawned (FR6); the action is passed as a separate, fixed argv entry (never
# interpolated into a shell) (Req 9.1-9.3).
PUPPETEER_ACTIONS = {'puppeteer'}

# Arm-only movement actions: the STANDALONE gesture (what /movement/<name> runs
# via controller.py) drives ONLY arm/wrist channels (3-7) and NEVER the neck
# (0-1), so the button is safe to layer over Puppeteer's Neck_Group hold (FR11).
# This is a FAIL-SAFE allowlist: a gesture is listed ONLY if its standalone form
# has been verified neck-free against movements.py. Anything NOT listed is
# treated as head/neck and DISABLED while Puppeteer runs. A conservative error
# (omitting a truly-arm-only gesture) only over-disables a button; the dangerous
# error — tagging a head-coupled gesture 'arm' so its subprocess fights the
# tracker for the Neck_Group lock — is what this list, and the guardrail test
# (tests/test_puppeteer_suppression.py), prevent.
#
# VERIFIED standalone footprints from movements.py (channel docstrings):
#   come_here          {3,4,5,6,7}   neck-free  -> arm
#   beckon             {3,4,5,6,7}   neck-free  -> arm
#   menacing_reach     {4,5,6,7}     neck-free  -> arm
#   tap_side           {3,4,5,6,7}   neck-free  -> arm
#   talking_with_hands {3,5,6,7} oscillated; ch4 held static at 270 (still a
#                      write) -> {3,4,5,6,7}; neck-free -> arm
#   talking_hands_ii   {3,4,5,6,7}   neck-free  -> arm
# NECK-DRIVING in standalone form (NOT arm-only; omitted from this set):
#   fan_butt   start_pose writes NECK_PAN=90, NECK_TILT=85
#   fan_nose   start_pose writes NECK_PAN, NECK_TILT
#   hand_visor look-around drives NECK_PAN[70,110]/NECK_TILT[85,95]
#   wave, yawnCover, facePalm, yes, lookAroundSmall, lookAroundRandom,
#   neckEllipse, swivelHead, shakeHead, snapHead, smno, snuckUp, awaken,
#   waveAndSwivelSmooth — all drive the neck.
ARM_ONLY_MOVEMENTS = {
    'comeHere', 'beckon', 'menacingReach', 'tapSide', 'talkingWithHands',
    'talkingHandsII',
}

# The arm-only-safe Scan response-pool candidates, split by kind from the single
# source of truth (SCAN_SAFE_ARM_ACTIONS). Only these appear in the Scan
# response-pool UI: an operator can select/weight only actions Scan can actually
# run arm-only while the neck tracks a person. Sorted for stable UI ordering.
SCAN_POOL_ROUTINES = sorted(
    name for name, kind in SCAN_SAFE_ARM_ACTIONS.items()
    if kind is ScanActionKind.ROUTINE
)
SCAN_POOL_GESTURES = sorted(
    name for name, kind in SCAN_SAFE_ARM_ACTIONS.items()
    if kind is ScanActionKind.GESTURE
)

# IR control modes. The only permitted values for a `POST /camera/ir` request
# (which becomes an IR mode name, Req 10.3). Kept here with the other allowlists
# as the single source of truth so the camera-IR route (added by task 13.1) can
# gate its incoming mode against this fixed set via `_validate_allowlist` before
# proxying anything to Camera_Service.
IR_MODES = {'on', 'off', 'auto'}

VOICE_STYLES = ['natural', 'demon', 'ghost', 'robot', 'possessed']
VOICE_EFFECTS = ['pitch', 'distortion', 'echo', 'reverb', 'tremolo',
                 'bitcrush', 'ring_mod']

_last_action = {'value': 'idle'}   # for status display

# Max wall-clock seconds any single gesture subprocess may run. No legitimate
# routine approaches this — it's a safety backstop so a hung process (e.g.
# blocked on I2C) can't hold the servo lock, and the arm, forever. When exceeded
# the watchdog kills the process, which releases the lock.
GESTURE_TIMEOUT = 90

# Nap timeout bounds (minutes). The nap is a Mode, not a gesture, so it has no
# GESTURE_TIMEOUT watchdog — it runs until its own timeout, a sensor wake, or an
# external stop. The floor is 0, which means "no timeout": the nap runs until it
# is stopped manually (web stop button) or a sensor/preemption interrupts it —
# the timeout deadline is simply never applied. The ceiling caps a single
# timed nap at 120 minutes so a stray large value can't hold the servo lock
# indefinitely.
NAP_MIN_TIMEOUT_MIN = 0   # 0 = no timeout (run until manual stop / sensor)
NAP_MAX_TIMEOUT_MIN = 120  # caps a single timed nap at 120 minutes

# Awake mode timeout bounds (minutes). Like napping, Awake is a Mode with no
# GESTURE_TIMEOUT watchdog — it runs ambient Routines until its own timeout, a
# sensor, or an external stop. The floor is 0 = "no timeout" (run until manual
# stop / sensor; the deadline is never applied); the ceiling caps a single
# timed awake session at 120 minutes.
AWAKE_MIN_TIMEOUT_MIN = 0   # 0 = no timeout (run until manual stop / sensor)
AWAKE_MAX_TIMEOUT_MIN = 120  # caps a single timed awake session at 120 minutes


# ── Subprocess launchers ─────────────────────────────────────────────────────
def run_routine(action):
    """Launch animatronic.py --action=<action> (gesture + audio)."""
    cmd = [VENV_PYTHON, ANIMATRONIC, f'--action={action}']
    print(f"[routine] {' '.join(cmd)}")
    # Popen (non-blocking) so a long routine doesn't block the HTTP response.
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_movement(action):
    """Launch controller.py --action=<action> (gesture only)."""
    cmd = [VENV_PYTHON, CONTROLLER, f'--action={action}']
    print(f"[movement] {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_napping(timeout_min):
    """Launch animatronic.py --action=napping (the napping MODE).

    A Mode runs until interrupted; it holds the servo lock for its whole run.
    Popen (non-blocking) so the HTTP response returns immediately.

    Args:
        timeout_min: Minutes before the nap's timeout wake fires.
    """
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=napping',
           f'--nap-timeout-min={int(timeout_min)}']
    print(f"[napping] {' '.join(cmd)}")
    # Fresh run: clear any stale stop request so the mode doesn't exit at once.
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_awake(timeout_min):
    """Launch animatronic.py --action=awake (the awake MODE).

    A Mode runs until interrupted; it holds the servo lock for its whole run.
    Popen (non-blocking) so the HTTP response returns immediately.

    Args:
        timeout_min: Minutes before the awake timeout ends the mode.
    """
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=awake',
           f'--awake-timeout-min={int(timeout_min)}']
    print(f"[awake] {' '.join(cmd)}")
    # Fresh run: clear any stale stop request so the mode doesn't exit at once.
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_tracking(scan_timeout_seconds=None):
    """Launch animatronic.py --action=tracking (the Tracking MODE).

    Mirrors run_napping/run_awake: a fixed VENV_PYTHON + ANIMATRONIC path and a
    fixed ``--action=tracking`` passed as separate argv entries (never a shell),
    so nothing from the request is ever interpolated into a command string
    (Req 9.3). Unlike the other Modes, Tracking is Gesture-like toward the mic
    Stream — it carries no audio and never drives the jaw motor (Req 6.2/6.3),
    so the caller does NOT auto-stop the mic.

    Args:
        scan_timeout_seconds: Optional Scan_Sweep reacquire timeout to forward
            as ``--scan-timeout`` (an int; animatronic.py clamps it to 1-120).
            ``None`` omits the flag so animatronic.py uses its own default.

    Returns:
        The spawned subprocess.Popen.
    """
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=tracking']
    # Optional tuning flags are forwarded only as separate, validated argv
    # entries — kept as ints here (coerced by the route) and never as shell
    # text. animatronic.py owns the real clamp (1-120); this just forwards it.
    if scan_timeout_seconds is not None:
        cmd.append(f'--scan-timeout={int(scan_timeout_seconds)}')
    print(f"[tracking] {' '.join(cmd)}")
    # Fresh run: clear any stale stop request so the mode doesn't exit at once
    # (same as run_napping/run_awake).
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_scan(timeout_min):
    """Launch animatronic.py --action=scan (the Scan MODE).

    Mirrors run_tracking: a fixed VENV_PYTHON + ANIMATRONIC path and a fixed
    ``--action=scan`` passed as separate argv entries (never a shell), so nothing
    from the request is ever interpolated into a command string (Req 9.1-9.3).
    The timeout minutes are forwarded as a separate, validated ``--scan-timeout-min``
    argv entry (coerced to int here; animatronic.py owns the real 1-120 clamp).

    A Mode runs open-endedly (until its timeout or an external stop), so unlike a
    routine no watchdog is attached.

    Args:
        timeout_min: The scan-mode timeout in minutes (an int).

    Returns:
        The spawned subprocess.Popen.
    """
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=scan',
           f'--scan-timeout-min={int(timeout_min)}']
    print(f"[scan] {' '.join(cmd)}")
    # Fresh run: clear any stale stop request so the mode doesn't exit at once
    # (same as run_tracking/run_napping/run_awake).
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


def run_puppeteer(scan_timeout_seconds=None):
    """Launch animatronic.py --action=puppeteer (the Puppeteer MODE).

    Mirrors ``run_tracking``: a fixed VENV_PYTHON + ANIMATRONIC path and a fixed
    ``--action=puppeteer`` passed as separate argv entries (never a shell), so
    nothing from the request is ever interpolated into a command string (FR7).
    Like tracking it holds only the Neck_Group lock and carries no audio of its
    own (the mic Stream is started separately by ``launch_puppeteer``).

    Args:
        scan_timeout_seconds: Optional Scan_Sweep reacquire timeout to forward
            as ``--scan-timeout`` (an int; animatronic.py clamps it). ``None``
            omits the flag so animatronic.py uses its own default.

    Returns:
        The spawned subprocess.Popen.
    """
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=puppeteer']
    if scan_timeout_seconds is not None:
        cmd.append(f'--scan-timeout={int(scan_timeout_seconds)}')
    print(f"[puppeteer] {' '.join(cmd)}")
    # Fresh run: clear any stale stop request so the mode doesn't exit at once
    # (same as the other mode launchers).
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)


# ── Gesture launch coordinator ───────────────────────────────────────────────
# SAFETY: only one gesture-driving subprocess may run at a time. The child
# processes enforce this at the hardware level via a cross-process file lock
# (servo_lock), but we also track the active child here so the web app can:
#   - reject a request immediately with a clear "busy" message, and
#   - avoid spawning doomed subprocesses (which would just exit code 3).
#
# _launch_lock serialises the check-and-spawn so two near-simultaneous requests
# to THIS process can't both pass the busy check.
_launch_lock = threading.Lock()
_active_proc = {'proc': None, 'label': None}

# ── Sleep/Awake mode auto-chaining state ─────────────────────────────────────
# When napping or awake ends NATURALLY (timeout/sensor), a per-launch watcher
# daemon chains to the OTHER mode, up to a configurable number of transitions. A
# stop or any operator-initiated launch breaks the chain. All reads/writes of
# this dict follow the uniform "callers hold _launch_lock" rule.
#   remaining        — auto-transitions still allowed in THIS chain
#   generation       — monotonic token bumped on every operator launch & every
#                       stop/preempt; a watcher captures it at launch and
#                       re-checks it before (and after) relaunching, so an
#                       operator action in the exit->relaunch window invalidates
#                       the chain
#   nap_timeout_min  — operator's chosen nap timeout for THIS chain (minutes)
#   awake_timeout_min— operator's chosen awake timeout for THIS chain (minutes)
_chain = {'remaining': 0, 'generation': 0,
          'nap_timeout_min': 0, 'awake_timeout_min': 0}


def _gesture_busy():
    """True if a gesture subprocess we launched is still running, or another
    process on the machine holds the servo lock."""
    proc = _active_proc['proc']
    if proc is not None and proc.poll() is None:
        return True
    # Also honour a lock held by any other process (manual CLI, etc.).
    return is_locked()


def _terminate_proc(proc, reason):
    """Terminate a subprocess: SIGTERM, then SIGKILL if it doesn't exit.

    Killing the process releases the servo lock it holds (the OS drops the
    flock on process death). Safe to call on an already-exited process.
    """
    if proc is None or proc.poll() is not None:
        return False
    print(f"[stop] terminating gesture ({reason}) pid={proc.pid}")
    try:
        proc.terminate()  # SIGTERM — lets Python run finally blocks / cleanup
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"[stop] pid={proc.pid} ignored SIGTERM; sending SIGKILL")
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        print(f"[stop] error terminating pid={getattr(proc, 'pid', '?')}: {e}")
    return True


def stop_active_gesture(reason='manual stop'):
    """Force-stop whatever gesture subprocess we launched, if any.

    Returns a human-readable message describing what happened.
    """
    with _launch_lock:
        # Bump the generation so any in-flight chain watcher is invalidated — a
        # force stop BREAKS the chain. Done before/with termination so the chain
        # is cancelled even if termination is slow.
        _bump_generation()
        proc = _active_proc['proc']
        label = _active_proc['label']
        if proc is None or proc.poll() is not None:
            _active_proc['proc'] = None
            _active_proc['label'] = None
            return 'Nothing was running.'
        _terminate_proc(proc, reason)
        _active_proc['proc'] = None
        _active_proc['label'] = None
        _last_action['value'] = f'stopped:{label}'
        return f'Stopped {label}.'


def _watchdog(proc, label):
    """Kill a gesture subprocess if it runs longer than GESTURE_TIMEOUT.

    Runs in a daemon thread. This is the backstop that prevents a hung routine
    (e.g. blocked on I2C) from holding the servo lock indefinitely.
    """
    try:
        proc.wait(timeout=GESTURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"[watchdog] {label} exceeded {GESTURE_TIMEOUT}s - killing")
        _terminate_proc(proc, f'watchdog timeout {GESTURE_TIMEOUT}s')
        with _launch_lock:
            # Only clear if this is still the tracked process.
            if _active_proc['proc'] is proc:
                _active_proc['proc'] = None
                _active_proc['label'] = None
                _last_action['value'] = f'timeout:{label}'


# ── Sleep/Awake mode auto-chaining helpers ───────────────────────────────────
# The uniform locking rule is "callers hold _launch_lock". _bump_generation does
# NO locking of its own; every call site must already hold _launch_lock
# (stop_active_gesture and _preempt_mode_if_running already do; the two stop
# routes wrap their bump in `with _launch_lock:`). Self-acquiring here would
# deadlock the non-reentrant lock at the sites that already hold it.
def _bump_generation():
    """Invalidate any in-flight chain watcher by advancing the generation token.

    Must be called while holding ``_launch_lock``. Every stop/preempt/operator
    launch bumps the token so a watcher launched under an older generation drops
    its chain (operator intent always wins).
    """
    _chain['generation'] += 1


def _clear_active_if(proc):
    """Clear ``_active_proc`` only if it still points at ``proc``.

    Mirrors the guard ``_watchdog`` uses so a stale observer never clears a slot
    another launch has since repopulated. Must be called while holding
    ``_launch_lock``.

    Args:
        proc: The subprocess whose slot should be cleared.
    """
    if _active_proc['proc'] is proc:
        _active_proc['proc'] = None
        _active_proc['label'] = None


def _await_lock_free(proc, timeout_s=15):
    """Wait, bounded, until ``proc`` has exited AND the servo lock is free.

    Best-effort gate (mirrors ``_preempt_mode_if_running``'s two-condition wait)
    so the watcher does not relaunch between the ending child's loop-exit and
    its process-exit. Does NOT hold ``_launch_lock`` while sleeping. Relaunch
    correctness against a concurrent lock holder is actually enforced by
    ``launch_fn``'s ``_gesture_busy()`` plus the child's own ``servo_lock()``
    fail-fast, not by this timing gate.

    Args:
        proc: The ending mode subprocess to wait on.
        timeout_s: Max seconds to wait.

    Returns:
        True once ``proc`` has exited and the lock is free; False on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None and not is_locked():
            return True
        time.sleep(0.2)
    return False


def _chain_timeout_min(mode):
    """Return the chain's captured timeout (minutes) for ``mode``.

    Must be called while holding ``_launch_lock``.

    Args:
        mode: ``'napping'`` or ``'awake'``.

    Returns:
        The captured timeout minutes for that mode's side of the chain.
    """
    if mode == 'napping':
        return _chain['nap_timeout_min']
    return _chain['awake_timeout_min']


def _terminate_if_still(proc):
    """Terminate ``proc`` ONLY if it is still the active tracked process.

    Cancellation-by-identity primitive used by ``_chain_watch`` step 7 (never
    ``stop_active_gesture``, which re-reads the slot and could kill an operator's
    brand-new action). Re-acquires ``_launch_lock`` and, only if
    ``_active_proc['proc'] is proc`` and ``proc`` is still running, terminates it
    and clears the slot. Otherwise it is a no-op.

    Args:
        proc: The exact chained child to cancel.

    Returns:
        True if ``proc`` was terminated; False if it was a no-op.
    """
    with _launch_lock:
        if _active_proc['proc'] is proc and proc.poll() is None:
            _terminate_proc(proc, 'chain cancelled by operator')
            _clear_active_if(proc)
            return True
    return False


def _chain_watch(proc, mode_label, generation):
    """Watch a chainable mode subprocess and chain to the OTHER mode on a natural end.

    Daemon-thread target started by ``launch_napping`` / ``launch_awake`` for the
    two chainable modes only (never tracking/scan/puppeteer/mic). Blocks on
    ``proc.wait()``, reads the exit code, and — only if the code is chainable
    (timeout/sensor), the generation token still matches (no stop/preempt/operator
    launch intervened), and the chain budget remaining > 0 — relaunches the other
    mode via ``_launch_mode`` (continuing the SAME chain, decrementing remaining).

    Implements Decision 4's Phase A/B/C exactly. A crash (1) or busy (3) exit is
    NON-chainable (fail-safe) and ends the chain with no transition — there is
    deliberately no crash-retry logic.

    Args:
        proc: The mode subprocess to watch.
        mode_label: ``'napping'`` or ``'awake'`` (the mode that was launched).
        generation: The ``_chain['generation']`` snapshot taken at launch.
    """
    proc.wait()
    code = proc.returncode
    other = 'awake' if mode_label == 'napping' else 'napping'

    # --- Phase A: decide whether to chain (all under _launch_lock) ---
    with _launch_lock:
        # 1. Still our launch? A stop/preempt/other launch moves generation
        #    -> drop the chain silently.
        if _chain['generation'] != generation:
            _clear_active_if(proc)
            return
        # 2. The slot must still be OUR process (defense in depth).
        if _active_proc['proc'] is not proc:
            return
        # 3. Only chain on a natural end (timeout/sensor). STOP/crash/busy -> no.
        if code not in mode_exit.CHAINABLE_EXIT_CODES:
            _clear_active_if(proc)
            return
        # 4. Budget check.
        if _chain['remaining'] <= 0:
            _clear_active_if(proc)
            return
        # 5. Decrement BEFORE launching so a crash mid-launch can't loop.
        _chain['remaining'] -= 1
        # 6. Clear the slot so the next launcher's _gesture_busy() passes.
        _clear_active_if(proc)
        timeout_min = _chain_timeout_min(other)

    # --- Phase B: wait for the ending child to fully exit AND free the lock ---
    if not _await_lock_free(proc, timeout_s=15):
        print("[chain] lock stuck - abandoning")
        return

    # --- Phase C: relaunch via the SAME serialized path an operator uses ---
    # _launch_mode re-acquires _launch_lock itself (not reentrant), so we cannot
    # hold the lock across this call. We spawn, then re-check generation under
    # the lock and stop ONLY the just-spawned child if an operator intervened.
    launch_fn = launch_awake if other == 'awake' else launch_napping
    ok, _msg = _launch_mode(launch_fn, timeout_min)   # reset_chain defaults False
    if not ok:
        return  # busy/409 -> abandon the chain (operator action won)

    # 7. Final anti-race re-check: if generation moved during the spawn window,
    #    an operator issued a stop/launch after Phase A -> operator wins. Stop
    #    ONLY the child we just spawned (by identity), never an unrelated action.
    #    Capture the cancellation target under the lock, then release it before
    #    calling _terminate_if_still (which re-acquires _launch_lock itself and
    #    re-validates identity, so it must NOT be called while the lock is held).
    cancel_target = None
    with _launch_lock:
        if _chain['generation'] != generation:
            spawned = _active_proc['proc']
            if (spawned is not None and spawned.poll() is None
                    and _active_proc['label'] == other):
                cancel_target = spawned
    if cancel_target is not None:
        print("[chain] cancelled by operator")
        _terminate_if_still(cancel_target)


def _mic_is_streaming():
    """Ask the mic controller whether it's currently streaming.

    Routines/movements and the mic stream both drive the jaw motor GPIO, so
    they cannot run at the same time (lgpio raises 'GPIO busy'). We check here
    so we can stop the mic before spawning a subprocess that would crash.
    Returns False if the mic controller is unreachable (nothing holding the pin).
    """
    body, code = _proxy('GET', '/status')
    if code == 200 and isinstance(body, dict):
        return bool(body.get('is_streaming'))
    return False


def _stop_mic():
    """Stop the mic stream so it releases the jaw-motor / eye GPIO pins.

    The mic controller frees MOUTH_MOTOR_PIN/EYE_LIGHT_PIN when its stream
    stops, so a gesture subprocess can then claim them without a 'GPIO busy'
    error. Returns True if the mic is confirmed not streaming afterward.
    """
    body, code = _proxy('POST', '/handler', {'action': 'stop'})
    print(f"[launch] auto-stopped mic before gesture (status {code})")
    # The pins are freed on the mic controller side once its stream thread
    # joins (inside the /handler stop). Give it a brief moment to settle before
    # the subprocess tries to claim the same pins.
    time.sleep(0.4)
    return not _mic_is_streaming()


# Labels of the background Modes (napping, awake, tracking, scan, puppeteer)
# that hold a servo lock and respond to the nap_signal cross-process stop. A
# web-requested action preempts any of these (see _preempt_mode_if_running). Both
# 'tracking' and 'puppeteer' are Modes too: they run open-endedly and wind down
# on the same nap_signal stop, so when a Routine/Movement is requested they are
# preempted like napping/awake. (Tracking and Puppeteer only hold the Neck_Group,
# so an arm-only Gesture can coexist with them — that concurrency is handled by
# the per-group servo lock, not here.)
_MODE_LABELS = ('napping', 'awake', 'tracking', 'scan', 'puppeteer')
# Exact-membership set for mode detection. A one-shot gesture carries a
# 'kind:action' label (e.g. 'movement:menacingReach'); Modes carry a BARE label.
# Mode checks test EXACT membership against this set — never a startswith/prefix
# test on the _MODE_LABELS tuple — so a future action name that merely begins
# with a mode name can't be misread as a mode.
_MODE_LABELS_SET = frozenset(_MODE_LABELS)


def _preempt_mode_if_running(wait_seconds=15):
    """If a background MODE is running, ask it to stop and wait for it to finish.

    A Mode (napping or awake) runs continuously and holds the servo lock, so a
    normal routine/movement request would be refused as "busy". Instead, when
    the active tracked process is one of these Modes, we set the cross-process
    stop signal (nap_signal) so the mode winds down cleanly (returns to rest,
    releases the servo lock, exits), then wait — bounded — for the servo lock to
    actually free before returning. This lets a web-requested action preempt a
    Mode: the action waits for the mode to finish and release the lock, then
    runs. It is what makes a web action button "arouse" Awake mode (and wake a
    nap), per the animation-vocabulary Mode rules.

    Must be called while holding ``_launch_lock``.

    Args:
        wait_seconds: Max seconds to wait for the mode to exit and free the lock.

    Returns:
        True if no mode was running, or the mode stopped and the lock is now
        free. False if a mode was running but did not release the lock within
        the timeout (caller should treat this as still-busy).
    """
    label = _active_proc['label']
    proc = _active_proc['proc']
    # Exact membership (not startswith) so a one-shot gesture label like
    # 'movement:menacingReach' can never be misread as a mode.
    is_mode = bool(label) and label in _MODE_LABELS_SET
    if not is_mode or proc is None or proc.poll() is not None:
        return True  # no background mode active

    print(f"[launch] preempting {label} mode - requesting stop and waiting")
    nap_signal.request_stop()
    # Bump the generation immediately (before the bounded wait) so even a preempt
    # that TIMES OUT has invalidated any in-flight chain watcher — a late-exiting
    # mode can never chain after a preempt. Called while holding _launch_lock
    # (this function's contract).
    _bump_generation()

    # Wait for the mode process to exit AND the servo lock to free, so the new
    # action can take the lock cleanly.
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None and not is_locked():
            _active_proc['proc'] = None
            _active_proc['label'] = None
            nap_signal.clear_stop()
            if label == 'puppeteer':
                # FR10: Puppeteer owns a live mic Stream it started; stop it so
                # the jaw/audio path is free for whatever preempted it. The
                # double-stop with napping/awake/scan (whose launchers also
                # auto-stop the mic on their next launch) is benign: _stop_mic()
                # treats micwebcontroller's 400 "Not streaming" as success.
                _stop_mic()
            print(f"[launch] {label} mode stopped; servo lock free")
            return True
        time.sleep(0.2)

    print(f"[launch] {label} mode did not stop within timeout")
    if label == 'puppeteer':
        # TIMEOUT path: the Puppeteer subprocess did not release the Neck_Group
        # within the window, so preemption FAILS (returns False; the caller
        # 409s). But the mic Stream holds NO servo lock and is owned by the web
        # layer, so we can and MUST still stop it here — otherwise a wedged neck
        # tracker would leave the operator's mic Stream running with no owning
        # mode (FR10). Stopping it is safe (independent of the stuck neck).
        _stop_mic()
    return False


def launch_gesture(kind, action, launcher):
    """Serialised launch of a gesture subprocess.

    Args:
        kind:     'routine' or 'movement' (for the status label).
        action:   validated action name.
        launcher: run_routine or run_movement.

    Returns:
        (ok: bool, message: str). ok=False means the servos are busy.
    """
    with _launch_lock:
        # If a background MODE (napping/awake) is running, ask it to wind down
        # and wait for it to release the servo lock, then proceed (a requested
        # action preempts/arouses the mode).
        if not _preempt_mode_if_running():
            return False, 'A background mode is stopping — try again in a moment.'
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        # The mic stream holds the jaw-motor GPIO; a gesture can't claim it too.
        # Auto-stop the mic (it releases the pins on stop) instead of refusing.
        if _mic_is_streaming():
            if not _stop_mic():
                return False, ('Mic streaming is on and could not be stopped; '
                               'it uses the jaw motor. Turn off the mic and retry.')
        proc = launcher(action)
        label = f'{kind}:{action}'
        _active_proc['proc'] = proc
        _active_proc['label'] = label
        _last_action['value'] = label
        # Start a watchdog so a hung routine can't hold the lock forever.
        threading.Thread(target=_watchdog, args=(proc, label), daemon=True).start()
        return True, f'{kind} started: {action}'


# ── Mic controller proxy helper ──────────────────────────────────────────────
def _proxy(method, path, json_body=None):
    """Forward a request to micwebcontroller.py and return (json, status).

    Uses the stdlib urllib so there is no extra dependency to install on the Pi.
    """
    url = f'{MIC_CONTROLLER_URL}{path}'
    try:
        if method == 'GET':
            req = urllib.request.Request(url, method='GET')
        else:
            payload = json.dumps(json_body or {}).encode('utf-8')
            req = urllib.request.Request(
                url, data=payload, method='POST',
                headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode('utf-8')
            try:
                return json.loads(body), resp.status
            except ValueError:
                return {'status': 'error', 'message': 'non-JSON response'}, resp.status
    except urllib.error.HTTPError as e:
        # The endpoint responded with a 4xx/5xx — surface its JSON body if any.
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'status': 'error', 'message': f'HTTP {e.code}'}, e.code
    except urllib.error.URLError as e:
        return {'status': 'error',
                'message': f'mic controller unreachable at {MIC_CONTROLLER_URL}: {e.reason}'}, 502


# ── Camera_Service proxy helper ──────────────────────────────────────────────
def _camera_proxy(path):
    """Forward a GET to Camera_Service (8001) and return (json, status).

    The JSON twin of :func:`_proxy`, but targeting Camera_Service instead of the
    mic controller. Camera routes are strictly READ-ONLY — this helper only ever
    issues a GET and never a servo command (Req 2.7). Used for the small JSON
    endpoints (``/detections``, ``/status``); the MJPEG ``/stream`` is handled
    separately by :func:`_camera_stream` so it can pass the multipart body
    through instead of JSON-decoding it.

    If Camera_Service is unreachable, returns a 502 with a "camera unavailable"
    message so the panel can show that status while all other controls stay
    usable (Req 2.5).

    Args:
        path: The Camera_Service path to GET (e.g. ``/status``, ``/detections``).

    Returns:
        A ``(body, status)`` tuple: the decoded JSON (or an error dict) and the
        HTTP status code (502 when Camera_Service is unreachable).
    """
    url = f'{CAMERA_SERVICE_URL}{path}'
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode('utf-8')
            try:
                return json.loads(body), resp.status
            except ValueError:
                return {'status': 'error', 'message': 'non-JSON response'}, resp.status
    except urllib.error.HTTPError as e:
        # Camera_Service responded with a 4xx/5xx — surface its JSON body if any.
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'status': 'error', 'message': f'HTTP {e.code}'}, e.code
    except urllib.error.URLError as e:
        return {'status': 'error', 'camera': 'unavailable',
                'message': f'camera unavailable at {CAMERA_SERVICE_URL}: {e.reason}'}, 502


# ── Path-value validation helpers (Req 9.4, 9.5, 10.3) ───────────────────────
# SAFETY: any value from a request that becomes a filesystem path is a path
# traversal boundary. These two pure helpers are the only sanctioned way to gate
# such a value — a route either (a) matches the value against an explicit
# allowlist of permitted names, or (b) resolves the value under a designated
# base dir and confirms the real path does not escape it. Both reject on failure
# WITHOUT reading or writing anything, so the caller can return an "invalid
# value" response and touch no path. They are deliberately dependency-free and
# side-effect-free so they are directly unit/property testable (Property 19).
def _validate_allowlist(value, allowed_set):
    """Return ``value`` iff it is a member of ``allowed_set``, else ``None``.

    The simplest path-value gate: when the set of legal names is known up front
    (e.g. IR modes ``on``/``off``/``auto``, Req 10.3), the value is only ever
    one of those exact strings and nothing is derived into a path at all. Pure:
    reads/writes no filesystem path.

    Args:
        value: The request-supplied value to check (any type; only exact
            membership matters).
        allowed_set: The explicit allowlist of permitted values.

    Returns:
        ``value`` when it is in ``allowed_set``; otherwise ``None`` (reject).
    """
    return value if value in allowed_set else None


def _validate_within_base(value, base_dir):
    """Resolve ``value`` under ``base_dir`` and return the real path iff it stays
    inside ``base_dir``; otherwise return ``None`` (reject).

    This is the traversal boundary for a value that becomes a filesystem path
    (e.g. a selectable detector ``model`` name, Req 9.4/9.5). The value is
    joined onto ``base_dir`` and fully resolved with ``os.path.realpath`` (which
    collapses ``..`` segments and follows symlinks), then compared against the
    resolved base using ``os.path.commonpath``. Any value that escapes — a
    traversal sequence like ``../../etc/passwd``, an absolute path like
    ``/etc/passwd`` (``os.path.join`` discards the base when the second arg is
    absolute, so the escape is still caught by the containment check), or a
    symlink pointing outside — resolves outside the base and is rejected.

    The function performs NO read or write of the resolved path: resolution is
    purely lexical/`lstat`-level via ``realpath``, so a rejected value never
    opens a file. The caller must still only read/write the returned path when
    the result is not ``None``.

    Args:
        value: The request-supplied value to turn into a path under ``base_dir``.
        base_dir: The designated base directory the resolved path must stay in.

    Returns:
        The resolved real path (a ``str``) when it is inside ``base_dir``;
        otherwise ``None`` (reject — read/write nothing).
    """
    # A non-string or empty value can never be a valid path name — reject.
    if not isinstance(value, str) or value == '':
        return None
    base_real = os.path.realpath(base_dir)
    # os.path.join discards base_real when `value` is absolute; the containment
    # check below still rejects it, so an absolute input cannot escape.
    candidate = os.path.realpath(os.path.join(base_real, value))
    try:
        # commonpath raises ValueError on mixed drives/relative-abs mixes; treat
        # any such oddity as an escape (reject) rather than letting it through.
        if os.path.commonpath([base_real, candidate]) == base_real:
            return candidate
    except ValueError:
        return None
    return None


# ── Routes: page ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template(
        'index.html',
        routines=sorted(ROUTINE_ACTIONS),
        # Each movement button carries its channel group so the template (and
        # Puppeteer gating) can tell arm-only gestures (ch 4-7, safe over the
        # Neck_Group hold) from head/neck ones. Anything not in the fail-safe
        # ARM_ONLY_MOVEMENTS allowlist is treated as 'head' and disabled under
        # Puppeteer (FR11).
        movements=[
            {'name': m, 'group': ('arm' if m in ARM_ONLY_MOVEMENTS else 'head')}
            for m in sorted(MOVEMENT_ACTIONS)
        ],
        # The Scan response pool is built ONLY from the arm-only-safe set, not
        # the full routine/gesture lists above, so the operator can't select a
        # head-coupled action Scan would silently drop.
        scan_pool_routines=SCAN_POOL_ROUTINES,
        scan_pool_gestures=SCAN_POOL_GESTURES,
        scan_pools=config_store.load_scan_pools(),
        # The Awake response pool, unlike Scan, is seeded from the FULL
        # routine/gesture allowlists (not the arm-only-safe subset): Awake is a
        # whole-robot mode holding the whole-robot servo_lock, so any routine or
        # gesture is a legal pick. Sorted for stable UI ordering.
        awake_pool_routines=sorted(ROUTINE_ACTIONS),
        awake_pool_gestures=sorted(MOVEMENT_ACTIONS),
        awake_pools=config_store.load_awake_pools(),
        # The persisted scan timeout (0..120, 0 = no timeout) so the scan
        # <select> can pre-select the stored value's <option>.
        scan_timeout_min=config_store.load_scan_timeout(),
        # The persisted Sleep/Awake timeouts (0..120, 0 = no timeout) so those
        # <select>s survive a hard refresh instead of resetting to the default.
        nap_timeout_min=config_store.load_nap_timeout(),
        awake_timeout_min=config_store.load_awake_timeout(),
        # The persisted mode auto-chaining budget (0..20, 0 = off) so the Config
        # <select> can pre-select the stored value's <option>.
        chain_max_transitions=config_store.load_chain_max_transitions(),
        styles=VOICE_STYLES,
        effects=VOICE_EFFECTS,
        mode_reference=MODE_INTERRUPT_REFERENCE,
    )


# ── Routes: routines / movements ─────────────────────────────────────────────
@app.route('/routine/<action>', methods=['POST'])
def routine(action):
    if action not in ROUTINE_ACTIONS:
        return jsonify({'status': 'error', 'message': f'Unknown routine: {action}'}), 400
    ok, message = launch_gesture('routine', action, run_routine)
    if not ok:
        # 409 Conflict — the servos are in use; caller should retry later.
        return jsonify({'status': 'busy', 'message': message}), 409
    return jsonify({'status': 'success', 'action': action, 'message': message})


@app.route('/movement/<action>', methods=['POST'])
def movement(action):
    if action not in MOVEMENT_ACTIONS:
        return jsonify({'status': 'error', 'message': f'Unknown movement: {action}'}), 400
    ok, message = launch_gesture('movement', action, run_movement)
    if not ok:
        return jsonify({'status': 'busy', 'message': message}), 409
    return jsonify({'status': 'success', 'action': action, 'message': message})


# ── Route: force stop ────────────────────────────────────────────────────────
@app.route('/stop', methods=['POST'])
def stop():
    """Emergency stop: kill any running gesture.

    Killing the gesture subprocess releases the servo lock.
    """
    message = stop_active_gesture(reason='force stop from UI')
    print(f"[stop] {message}")
    return jsonify({'status': 'success', 'message': message})


# ── Route: napping mode ──────────────────────────────────────────────────────
def launch_napping(timeout_min, reset_chain=False):
    """Serialised launch of the napping MODE subprocess.

    Mirrors ``launch_gesture``'s check-and-spawn under ``_launch_lock`` and
    tracks the process in ``_active_proc`` (label ``napping``) so the busy
    check, preemption, and force-stop all see it. Refuses if the servos are
    already busy. No watchdog is attached: unlike a routine, a Mode is meant to
    run open-endedly (until timeout/sensor/stop), so the GESTURE_TIMEOUT
    backstop would wrongly kill it.

    On every real spawn it starts a ``_chain_watch`` daemon so a natural end can
    auto-chain to awake. When ``reset_chain`` is True (an operator start, not a
    watcher relaunch) it also bumps the generation and re-seeds the chain budget
    from config — so a redundant Start (the ``_launch_mode`` "already running"
    no-op) never re-seeds an in-flight chain.

    Args:
        timeout_min: Nap timeout in minutes (0 = no timeout).
        reset_chain: When True, treat this as a fresh operator-started chain:
            bump the generation and re-seed ``_chain`` from config. The watcher's
            relaunch leaves this False to continue the same chain.

    Returns:
        (ok, message). ok=False means the servos were busy.
    """
    with _launch_lock:
        # Auto-stop the mic (a Mode owns the jaw/audio path, like a routine).
        if _mic_is_streaming():
            if not _stop_mic():
                return False, ('Mic streaming is on and could not be stopped; '
                               'turn off the mic and retry.')
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        proc = run_napping(timeout_min)
        _active_proc['proc'] = proc
        _active_proc['label'] = 'napping'
        _last_action['value'] = 'napping'
        if reset_chain:
            # Fresh operator-started chain: new identity + budget from config.
            _bump_generation()
            _chain['remaining'] = config_store.load_chain_max_transitions()
            # Seed BOTH timeout keys deterministically so a stale timeout from a
            # prior chain can't leak into the opposite side of the new chain.
            _chain['nap_timeout_min'] = int(timeout_min)
            _chain['awake_timeout_min'] = 0
        # Snapshot generation AFTER any bump, then start the chain watcher.
        generation = _chain['generation']
        threading.Thread(target=_chain_watch, args=(proc, 'napping', generation),
                         daemon=True).start()
        if int(timeout_min) == 0:
            return True, 'napping started (no timeout)'
        return True, f'napping started (timeout {int(timeout_min)} min)'


@app.route('/nap/<state>', methods=['POST'])
def nap(state):
    """Start or stop the napping MODE.

    - ``start``: launch napping (optional JSON ``{"timeout_min": <minutes>}``,
      0..120 where 0 = no timeout; default 0 = no timeout when omitted). A Mode
      runs until interrupted.
    - ``stop``: ask a running nap to wind down via the cross-process stop
      signal; it wakes the head, releases the servo lock, and exits.
    """
    if state == 'start':
        data = request.json or {}
        minutes = data.get('timeout_min')
        if minutes is not None:
            # Mirror /scan's strict validation: reject a non-int AND an
            # out-of-range value with 400 rather than silently clamping. bool is
            # an int subclass, so reject it explicitly. The range is [0,120]
            # (integer range only — the 10-minute increment is a UI concern);
            # 0 = no timeout.
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be an integer'}), 400
            if not (NAP_MIN_TIMEOUT_MIN <= minutes <= NAP_MAX_TIMEOUT_MIN):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be between 0 and 120'}), 400
        else:
            # Omitted: use the persisted timeout (0 = No timeout) so a hard
            # browser refresh — which reseeds the dropdown from storage — starts
            # with the operator's last choice instead of silently resetting.
            minutes = config_store.load_nap_timeout()
        # Persist the chosen value so it survives a restart / hard refresh and is
        # the next default (returns the clamped int actually stored).
        minutes = config_store.save_nap_timeout(minutes)
        # Route through _launch_mode so requesting napping while a DIFFERENT mode
        # runs preempts it gracefully (FR8) instead of a 409 "busy" refusal.
        # reset_chain=True so a fresh operator Start seeds a new chain (the
        # launcher does the reset on the real spawn, NOT the "already running"
        # no-op).
        ok, message = _launch_mode(launch_napping, minutes, reset_chain=True)
        if not ok:
            return jsonify({'status': 'busy', 'message': message}), 409
        return jsonify({'status': 'success', 'message': message})
    if state == 'stop':
        # Signal the mode to wind down; it releases the lock and exits itself.
        # Bump the generation (under the lock) so any in-flight chain watcher is
        # invalidated — a stop BREAKS the chain, it does not transition.
        nap_signal.request_stop()
        with _launch_lock:
            _bump_generation()
        return jsonify({'status': 'success', 'message': 'nap stop requested'})
    return jsonify({'status': 'error', 'message': "state must be 'start' or 'stop'"}), 400


# ── Route: awake mode ────────────────────────────────────────────────────────
def launch_awake(timeout_min, reset_chain=False):
    """Serialised launch of the awake MODE subprocess.

    Mirrors ``launch_napping``: check-and-spawn under ``_launch_lock``, track the
    process in ``_active_proc`` (label ``awake``) so the busy check, preemption,
    and force-stop all see it. Refuses if the servos are already busy. No
    watchdog is attached — like napping, a Mode runs open-endedly (until
    timeout/sensor/stop), so the GESTURE_TIMEOUT backstop would wrongly kill it.

    On every real spawn it starts a ``_chain_watch`` daemon so a natural end can
    auto-chain to napping. When ``reset_chain`` is True (an operator start) it
    also bumps the generation and re-seeds the chain budget from config.

    Args:
        timeout_min: Awake timeout in minutes (0 = no timeout).
        reset_chain: When True, treat this as a fresh operator-started chain:
            bump the generation and re-seed ``_chain`` from config. The watcher's
            relaunch leaves this False to continue the same chain.

    Returns:
        (ok, message). ok=False means the servos were busy.
    """
    with _launch_lock:
        # Auto-stop the mic (a Mode owns the jaw/audio path, like a routine).
        if _mic_is_streaming():
            if not _stop_mic():
                return False, ('Mic streaming is on and could not be stopped; '
                               'turn off the mic and retry.')
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        proc = run_awake(timeout_min)
        _active_proc['proc'] = proc
        _active_proc['label'] = 'awake'
        _last_action['value'] = 'awake'
        if reset_chain:
            # Fresh operator-started chain: new identity + budget from config.
            _bump_generation()
            _chain['remaining'] = config_store.load_chain_max_transitions()
            # Seed BOTH timeout keys deterministically so a stale timeout from a
            # prior chain can't leak into the opposite side of the new chain.
            _chain['awake_timeout_min'] = int(timeout_min)
            _chain['nap_timeout_min'] = 0
        # Snapshot generation AFTER any bump, then start the chain watcher.
        generation = _chain['generation']
        threading.Thread(target=_chain_watch, args=(proc, 'awake', generation),
                         daemon=True).start()
        if int(timeout_min) == 0:
            return True, 'awake started (no timeout)'
        return True, f'awake started (timeout {int(timeout_min)} min)'


@app.route('/awake/<state>', methods=['POST'])
def awake(state):
    """Start or stop the awake MODE.

    - ``start``: launch awake (optional JSON ``{"timeout_min": <minutes>}``,
      0..120 where 0 = no timeout; default 0 = no timeout when omitted). A Mode
      runs ambient Routines until interrupted.
    - ``stop``: ask a running awake mode to wind down via the cross-process stop
      signal; it finishes the current routine, returns to rest, releases the
      servo lock, and exits.
    """
    if state == 'start':
        data = request.json or {}
        minutes = data.get('timeout_min')
        if minutes is not None:
            # Mirror /scan's strict validation: reject a non-int AND an
            # out-of-range value with 400 rather than silently clamping. bool is
            # an int subclass, so reject it explicitly. The range is [0,120]
            # (integer range only — the 10-minute increment is a UI concern);
            # 0 = no timeout.
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be an integer'}), 400
            if not (AWAKE_MIN_TIMEOUT_MIN <= minutes <= AWAKE_MAX_TIMEOUT_MIN):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be between 0 and 120'}), 400
        else:
            # Omitted: use the persisted timeout (0 = No timeout) so a hard
            # browser refresh starts with the operator's last choice.
            minutes = config_store.load_awake_timeout()
        # Persist the chosen value so it survives a restart / hard refresh.
        minutes = config_store.save_awake_timeout(minutes)
        # Route through _launch_mode for graceful mode-switch preemption (FR8).
        # reset_chain=True so a fresh operator Start seeds a new chain.
        ok, message = _launch_mode(launch_awake, minutes, reset_chain=True)
        if not ok:
            return jsonify({'status': 'busy', 'message': message}), 409
        return jsonify({'status': 'success', 'message': message})
    if state == 'stop':
        # Signal the mode to wind down; it releases the lock and exits itself.
        # Bump the generation (under the lock) so any in-flight chain watcher is
        # invalidated — a stop BREAKS the chain, it does not transition.
        nap_signal.request_stop()
        with _launch_lock:
            _bump_generation()
        return jsonify({'status': 'success', 'message': 'awake stop requested'})
    return jsonify({'status': 'error', 'message': "state must be 'start' or 'stop'"}), 400


# ── Route: tracking mode ─────────────────────────────────────────────────────
def launch_tracking(scan_timeout_seconds=None):
    """Serialised launch of the Tracking MODE subprocess.

    Mirrors ``launch_napping``/``launch_awake``: check-and-spawn under
    ``_launch_lock`` and track the process in ``_active_proc`` (label
    ``tracking``) so the busy check, preemption, and force-stop all see it.
    Refuses if the servos are already busy. No watchdog is attached — like the
    other Modes, Tracking runs open-endedly (until a stop signal or its own
    Scan_Sweep timeout), so the GESTURE_TIMEOUT backstop would wrongly kill it.

    Unlike ``launch_napping``/``launch_awake``, this does NOT auto-stop the mic:
    Tracking carries no audio and never touches the jaw motor, so it is
    Gesture-like toward the live mic Stream and the two can run at the same time
    (Req 6.2/6.3). The ``'tracking'`` label is in ``_MODE_LABELS``, so a later
    Routine/Movement request preempts it via ``_preempt_mode_if_running``.

    Args:
        scan_timeout_seconds: Optional Scan_Sweep reacquire timeout forwarded to
            ``run_tracking`` as an int ``--scan-timeout`` flag. ``None`` lets
            animatronic.py use its own default.

    Returns:
        (ok, message). ok=False means the servos were busy.
    """
    with _launch_lock:
        # NOTE: deliberately no mic auto-stop here (see docstring, Req 6.3).
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        proc = run_tracking(scan_timeout_seconds)
        _active_proc['proc'] = proc
        _active_proc['label'] = 'tracking'
        _last_action['value'] = 'tracking'
        return True, 'tracking started'


@app.route('/tracking/<state>', methods=['POST'])
def tracking(state):
    """Start or stop the Tracking MODE.

    - ``start``: validate against the ``TRACKING_ACTIONS`` allowlist (Req
      9.1/9.2), then launch Tracking (optional JSON ``{"scan_timeout":
      <seconds>}`` forwarded to animatronic.py, which clamps it to 1-120). A
      Mode runs until interrupted; it is NOT given a GESTURE_TIMEOUT watchdog.
      Because Tracking is Gesture-like toward the mic Stream it does not stop a
      live mic.
    - ``stop``: ask a running Tracking Mode to wind down via the cross-process
      stop signal; it recenters the Neck_Group, releases the lock, and exits.

    An unknown ``state`` returns 400 and launches no subprocess.
    """
    if state == 'start':
        # Allowlist gate: 'tracking' is the only permitted action name, checked
        # before any subprocess is spawned (Req 9.1/9.2).
        if 'tracking' not in TRACKING_ACTIONS:
            return jsonify({'status': 'error',
                            'message': 'tracking action not permitted'}), 400
        data = request.json or {}
        scan_timeout = data.get('scan_timeout')
        if scan_timeout is not None:
            try:
                scan_timeout = int(scan_timeout)
            except (TypeError, ValueError):
                return jsonify({'status': 'error',
                                'message': 'scan_timeout must be an integer'}), 400
        # Route through _launch_mode for graceful mode-switch preemption (FR8).
        ok, message = _launch_mode(launch_tracking, scan_timeout)
        if not ok:
            return jsonify({'status': 'busy', 'message': message}), 409
        return jsonify({'status': 'success', 'message': message})
    if state == 'stop':
        # Signal the mode to wind down; it releases the lock and exits itself.
        nap_signal.request_stop()
        return jsonify({'status': 'success', 'message': 'tracking stop requested'})
    return jsonify({'status': 'error', 'message': "state must be 'start' or 'stop'"}), 400


# ── Route: scan mode ─────────────────────────────────────────────────────────
def launch_scan(timeout_min):
    """Serialised launch of the Scan MODE subprocess.

    Mirrors ``launch_napping``/``launch_awake``: check-and-spawn under
    ``_launch_lock`` and track the process in ``_active_proc`` (label ``scan``)
    so the busy check, preemption, and force-stop all see it. Refuses if the
    servos are already busy. No watchdog is attached — like the other Modes,
    Scan runs open-endedly (until its timeout or a stop signal), so the
    GESTURE_TIMEOUT backstop would wrongly kill it.

    UNLIKE tracking (which is Gesture-like toward the mic Stream), Scan's Routine
    responses drive the jaw motor, so Scan owns the jaw/audio path and auto-stops
    the mic at launch (like napping/awake). The ``'scan'`` label is in
    ``_MODE_LABELS``, so a later Routine/Movement request preempts it via
    ``_preempt_mode_if_running``.

    Args:
        timeout_min: The scan-mode timeout in minutes (an int) forwarded to
            ``run_scan``.

    Returns:
        (ok, message). ok=False means the servos were busy.
    """
    with _launch_lock:
        # Scan owns the jaw/audio path (its Routine responses drive the jaw), so
        # auto-stop the mic like napping/awake.
        if _mic_is_streaming():
            if not _stop_mic():
                return False, ('Mic streaming is on and could not be stopped; '
                               'it uses the jaw motor. Turn off the mic and retry.')
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        proc = run_scan(timeout_min)
        _active_proc['proc'] = proc
        _active_proc['label'] = 'scan'
        _last_action['value'] = 'scan'
        if int(timeout_min) == 0:
            return True, 'scan started (no timeout)'
        return True, f'scan started (timeout {int(timeout_min)} min)'


@app.route('/scan/<state>', methods=['POST'])
def scan(state):
    """Start or stop the Scan MODE.

    - ``start``: validate against the ``SCAN_ACTIONS`` allowlist (Req 9.1/9.2),
      then launch Scan. The timeout minutes come from JSON ``{"timeout_min":
      <int>}`` — if provided it MUST be an int in [0, 120] (0 = no timeout; both
      a non-int AND an out-of-range value are rejected with 400, stricter than
      ``/tracking``); if omitted it falls back to the persisted
      ``config_store.load_scan_timeout()``.
      The chosen value is persisted via ``config_store.save_scan_timeout`` before
      launch. A Mode runs until interrupted; it is NOT given a GESTURE_TIMEOUT
      watchdog.
    - ``stop``: ask a running Scan Mode to wind down via the cross-process stop
      signal; it cancels any in-flight response, recenters the Neck_Group,
      releases the lock, and exits.

    An unknown ``state`` returns 400 and launches no subprocess.
    """
    if state == 'start':
        # Allowlist gate: 'scan' is the only permitted action name, checked
        # before any subprocess is spawned (Req 9.1/9.2).
        if 'scan' not in SCAN_ACTIONS:
            return jsonify({'status': 'error',
                            'message': 'scan action not permitted'}), 400
        data = request.json or {}
        minutes = data.get('timeout_min')
        if minutes is not None:
            # Stricter than /tracking: reject a non-int AND an out-of-range value
            # with 400 (launch nothing) rather than silently clamping. bool is an
            # int subclass, so reject it explicitly.
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be an integer'}), 400
            if not (0 <= minutes <= 120):
                return jsonify({'status': 'error',
                                'message': 'timeout_min must be between 0 and 120'}), 400
        else:
            # Omitted: use the persisted (or default 60) timeout. 0 = no timeout.
            minutes = config_store.load_scan_timeout()
        # Persist the chosen value so it survives a restart and is the next
        # default (returns the clamped int actually stored).
        minutes = config_store.save_scan_timeout(minutes)
        # Route through _launch_mode for graceful mode-switch preemption (FR8).
        ok, message = _launch_mode(launch_scan, minutes)
        if not ok:
            return jsonify({'status': 'busy', 'message': message}), 409
        return jsonify({'status': 'success', 'message': message})
    if state == 'stop':
        # Signal the mode to wind down; it releases the lock and exits itself.
        nap_signal.request_stop()
        return jsonify({'status': 'success', 'message': 'scan stop requested'})
    return jsonify({'status': 'error', 'message': "state must be 'start' or 'stop'"}), 400


# ── Route: puppeteer mode ────────────────────────────────────────────────────
def launch_puppeteer(scan_timeout_seconds=None):
    """Serialised launch of the Puppeteer MODE: neck tracking + live mic Stream.

    Mirrors ``launch_tracking`` (no mic auto-stop — Puppeteer is Gesture-like
    toward the Stream and holds only the Neck_Group), but ALSO STARTS the live
    mic Stream so entering Puppeteer gives the operator mic + head tracking in
    one action (FR2). Preemption of a DIFFERENT running Mode is handled by
    ``_launch_mode`` before this is called; this function keeps its own
    ``_gesture_busy()`` backstop.

    The mic Stream holds NO servo lock and runs in micwebcontroller, so it layers
    over neck tracking + arm gestures. Starting it is BEST-EFFORT: it happens
    AFTER the subprocess is tracked so a mic failure leaves the neck tracker
    running and ``_active_proc`` consistent; the failure is surfaced in the
    message and the status poll's mic indicator rather than killing the mode.

    Args:
        scan_timeout_seconds: Optional Scan_Sweep reacquire timeout forwarded to
            ``run_puppeteer`` as an int ``--scan-timeout`` flag. ``None`` lets
            animatronic.py use its own default.

    Returns:
        (ok, message). ok=False means the servos were busy.
    """
    with _launch_lock:
        # NOTE: deliberately no mic auto-stop here — Puppeteer STARTS the mic.
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        proc = run_puppeteer(scan_timeout_seconds)
        _active_proc['proc'] = proc
        _active_proc['label'] = 'puppeteer'
        _last_action['value'] = 'puppeteer'
        # Start the live mic Stream (FR2) via the exact proxy call the mic toggle
        # uses. micwebcontroller answering 400 "Already streaming" is still a
        # success for us: _mic_is_streaming() reports True.
        body, code = _proxy('POST', '/handler', {'action': 'start'})
        if not _mic_is_streaming():
            msg = (body or {}).get('message') if isinstance(body, dict) else None
            return True, ('puppeteer started (neck tracking); '
                          f'mic Stream did NOT start: {msg or f"status {code}"}')
        return True, 'puppeteer started (neck tracking + live mic)'


@app.route('/puppeteer/<state>', methods=['POST'])
def puppeteer(state):
    """Start or stop the Puppeteer MODE.

    - ``start``: validate against the ``PUPPETEER_ACTIONS`` allowlist BEFORE any
      subprocess/mic action (FR6), then launch Puppeteer through ``_launch_mode``
      (optional JSON ``{"scan_timeout": <seconds>}`` forwarded to animatronic.py,
      which clamps it). ``_launch_mode`` gracefully preempts a different running
      Mode first (FR8) and no-ops if Puppeteer is already running (FR9).
    - ``stop``: ask a running Puppeteer Mode to wind down via the cross-process
      stop signal (recenters the Neck_Group, releases the lock) AND stop the mic
      Stream it started (FR10).

    An unknown ``state`` returns 400 and launches nothing (AC4).
    """
    if state == 'start':
        # Allowlist gate BEFORE any subprocess/mic action (FR6).
        if 'puppeteer' not in PUPPETEER_ACTIONS:
            return jsonify({'status': 'error',
                            'message': 'puppeteer action not permitted'}), 400
        data = request.json or {}
        scan_timeout = data.get('scan_timeout')
        if scan_timeout is not None:
            try:
                scan_timeout = int(scan_timeout)
            except (TypeError, ValueError):
                return jsonify({'status': 'error',
                                'message': 'scan_timeout must be an integer'}), 400
        ok, message = _launch_mode(launch_puppeteer, scan_timeout)
        if not ok:
            return jsonify({'status': 'busy', 'message': message}), 409
        return jsonify({'status': 'success', 'message': message})
    if state == 'stop':
        # Signal wind-down; the mode recenters the neck and releases the lock.
        # The mic Stream Puppeteer started is stopped here too (FR10).
        nap_signal.request_stop()
        _stop_mic()
        return jsonify({'status': 'success', 'message': 'puppeteer stop requested'})
    return jsonify({'status': 'error', 'message': "state must be 'start' or 'stop'"}), 400


# ── Mode launch coordinator (graceful mode-switch preemption) ────────────────
# Maps each mode launcher to the _active_proc label it sets, so _launch_mode can
# identify the requested mode without changing any launcher's signature. Defined
# AFTER every launch_* function so the names resolve at module load.
_LAUNCHER_LABEL = {
    launch_napping: 'napping',
    launch_awake: 'awake',
    launch_tracking: 'tracking',
    launch_scan: 'scan',
    launch_puppeteer: 'puppeteer',
}


def _launch_mode(launch_fn, *args, reset_chain=False):
    """Launch a background Mode, gracefully preempting a DIFFERENT running Mode.

    FR8/FR9 policy for the five mode launchers (napping/awake/tracking/scan/
    puppeteer):

      * If the SAME mode is already running -> no-op, reported as already
        running (FR9); no second subprocess is spawned.
      * If a DIFFERENT mode is running -> ``_preempt_mode_if_running()`` signals
        it to wind down and WAITS (bounded) for it to release its servo lock
        (NECK_GROUP or whole-robot) before the new mode's ``launch_fn`` claims
        servos, preserving per-group lock safety (NFR3). Only then is
        ``launch_fn`` called.
      * If nothing is running — OR a one-shot gesture (routine/movement) is
        mid-flight — ``launch_fn`` is called directly. A Mode request does NOT
        preempt an in-flight one-shot gesture; ``launch_fn``'s own
        ``_gesture_busy()`` backstop returns busy (-> 409) while the gesture
        holds the lock.

    ``launch_fn`` is the mode's existing serialised launcher; it still performs
    its own ``_gesture_busy()`` check (and mic auto-stop, where applicable) as a
    backstop. ``_launch_lock`` is released before ``launch_fn`` is called because
    ``launch_fn`` re-acquires it itself.

    Args:
        launch_fn: One of the launch_* mode functions (keys of _LAUNCHER_LABEL).
        *args: Positional args forwarded to ``launch_fn`` (e.g. a timeout).
        reset_chain: Forwarded ONLY to the two chainable launchers
            (launch_napping/launch_awake); tracking/scan/puppeteer take no such
            kwarg, so it is never passed to them (no TypeError). Operator starts
            pass True; the watcher's relaunch leaves it False.

    Returns:
        (ok, message) from the preemption/launch — ok=False means busy (-> 409).
    """
    requested = _LAUNCHER_LABEL[launch_fn]
    with _launch_lock:
        active = _active_proc['label']
        proc = _active_proc['proc']
        running = bool(active) and proc is not None and proc.poll() is None
        active_is_mode = active in _MODE_LABELS_SET
        if running and active == requested:
            # FR9: same mode already running -> no-op (don't relaunch).
            return True, f'{requested} already running'
        if running and active_is_mode and active != requested:
            # FR8: a DIFFERENT mode is running -> wind it down and wait for its
            # lock to free before launch_fn claims servos.
            if not _preempt_mode_if_running():
                return False, 'A background mode is stopping — try again in a moment.'
        # else: nothing running, OR a one-shot gesture is mid-flight. Fall
        # through to launch_fn, whose _gesture_busy() backstop handles the latter.
    # Release _launch_lock before launch_fn re-acquires it. When a different mode
    # was preempted it has freed its servo lock (verified in
    # _preempt_mode_if_running via is_locked()), so launch_fn's _gesture_busy()
    # now passes.
    #
    # Forward reset_chain ONLY to the two chainable launchers — tracking/scan/
    # puppeteer take no such kwarg, so forwarding it unconditionally would raise
    # TypeError on those three start paths.
    if launch_fn in (launch_napping, launch_awake):
        return launch_fn(*args, reset_chain=reset_chain)
    return launch_fn(*args)


# ── Routes: scan response pool (config read/write; NO servo command) ─────────
@app.route('/scan/pool', methods=['GET'])
def scan_pool_get():
    """Return the persisted Scan response pool.

    Read-only: emits ``{"routine_pool": {name: weight}, "gesture_pool":
    {name: weight}}`` straight from ``config_store.load_scan_pools()``. Issues
    no servo command and dispatches no action — this only reads config.
    """
    return jsonify(config_store.load_scan_pools())


@app.route('/scan/pool', methods=['POST'])
def scan_pool_post():
    """Persist the operator-selected Scan response pool (SECURITY BOUNDARY).

    Body: ``{"routine_pool": {name: weight}, "gesture_pool": {name: weight}}``.
    EVERY submitted routine name is validated against ``ROUTINE_ACTIONS`` and
    every gesture name against ``MOVEMENT_ACTIONS`` via :func:`_validate_allowlist`
    BEFORE anything is persisted. If ANY name is unknown the whole request is
    rejected with 400 and nothing is written — a raw submitted name is never
    passed to ``getattr``/``eval``/``subprocess`` and this handler runs NO servo
    command (config read/write only). Validated maps are then sanitized
    (weights coerced to int, clamped to [1,10], <1 dropped) and saved via
    ``config_store.save_scan_pools``, which preserves ``scan.timeout_min``.
    """
    data = request.json or {}
    routine_pool = data.get('routine_pool') or {}
    gesture_pool = data.get('gesture_pool') or {}
    if not isinstance(routine_pool, dict) or not isinstance(gesture_pool, dict):
        return jsonify({'status': 'error',
                        'message': 'pool must be an object'}), 400

    # Allowlist gate: reject the WHOLE request if any name is unknown, persist
    # nothing. Names are the single security boundary — only known action names
    # may ever be written to tuning.json or later dispatched by Scan.
    for name in routine_pool:
        if _validate_allowlist(name, ROUTINE_ACTIONS) is None:
            print(f"[scan/pool] rejected unknown routine name: {name!r}")
            return jsonify({'status': 'error',
                            'message': f'Unknown routine: {name}'}), 400
    for name in gesture_pool:
        if _validate_allowlist(name, MOVEMENT_ACTIONS) is None:
            print(f"[scan/pool] rejected unknown gesture name: {name!r}")
            return jsonify({'status': 'error',
                            'message': f'Unknown gesture: {name}'}), 400

    clean_r = config_store.sanitize_scan_pool(routine_pool, ROUTINE_ACTIONS)
    clean_g = config_store.sanitize_scan_pool(gesture_pool, MOVEMENT_ACTIONS)
    stored = config_store.save_scan_pools(clean_r, clean_g)
    print(f"[scan/pool] saved routine_pool={clean_r} gesture_pool={clean_g}")
    return jsonify({'status': 'success', **stored})


# ── Routes: awake response pool (config read/write; NO servo command) ────────
@app.route('/awake/pool', methods=['GET'])
def awake_pool_get():
    """Return the persisted Awake response pool.

    Read-only: emits ``{"routine_pool": {name: weight}, "gesture_pool":
    {name: weight}}`` straight from ``config_store.load_awake_pools()``. Issues
    no servo command and dispatches no action — this only reads config.
    """
    return jsonify(config_store.load_awake_pools())


@app.route('/awake/pool', methods=['POST'])
def awake_pool_post():
    """Persist the operator-selected Awake response pool (SECURITY BOUNDARY).

    Body: ``{"routine_pool": {name: weight}, "gesture_pool": {name: weight}}``.
    EVERY submitted routine name is validated against ``ROUTINE_ACTIONS`` and
    every gesture name against ``MOVEMENT_ACTIONS`` via :func:`_validate_allowlist`
    BEFORE anything is persisted. Unlike Scan (which validates against the
    arm-only-safe subset), the Awake pool validates against the FULL allowlists
    because Awake is a whole-robot mode. If ANY name is unknown the whole request
    is rejected with 400 and nothing is written — a raw submitted name is never
    passed to ``getattr``/``eval``/``subprocess`` and this handler runs NO servo
    command (config read/write only). Validated maps are then sanitized (weights
    coerced to int, clamped to [1,10], <1 dropped) and saved via
    ``config_store.save_awake_pools``, which preserves ``awake.timeout_min``.
    """
    data = request.json or {}
    routine_pool = data.get('routine_pool') or {}
    gesture_pool = data.get('gesture_pool') or {}
    if not isinstance(routine_pool, dict) or not isinstance(gesture_pool, dict):
        return jsonify({'status': 'error',
                        'message': 'pool must be an object'}), 400

    # Allowlist gate: reject the WHOLE request if any name is unknown, persist
    # nothing. Names are the single security boundary — only known action names
    # may ever be written to tuning.json or later dispatched by Awake. Awake
    # uses the FULL allowlists (whole-robot mode), not the Scan arm-only subset.
    for name in routine_pool:
        if _validate_allowlist(name, ROUTINE_ACTIONS) is None:
            print(f"[awake/pool] rejected unknown routine name: {name!r}")
            return jsonify({'status': 'error',
                            'message': f'Unknown routine: {name}'}), 400
    for name in gesture_pool:
        if _validate_allowlist(name, MOVEMENT_ACTIONS) is None:
            print(f"[awake/pool] rejected unknown gesture name: {name!r}")
            return jsonify({'status': 'error',
                            'message': f'Unknown gesture: {name}'}), 400

    clean_r = config_store.sanitize_scan_pool(routine_pool, ROUTINE_ACTIONS)
    clean_g = config_store.sanitize_scan_pool(gesture_pool, MOVEMENT_ACTIONS)
    stored = config_store.save_awake_pools(clean_r, clean_g)
    print(f"[awake/pool] saved routine_pool={clean_r} gesture_pool={clean_g}")
    return jsonify({'status': 'success', **stored})


# ── Route: mode auto-chaining config (config write; NO servo command) ────────
@app.route('/chain/config', methods=['POST'])
def chain_config():
    """Persist the Sleep/Awake mode auto-chaining budget.

    Body: ``{"max_transitions": <int in [0, 20]>}`` — the number of
    auto-transitions the chain may perform before it stops (0 = chaining
    disabled). Mirrors ``/nap``/``/awake``/``/scan`` strict validation: a non-int
    or bool is rejected with 400, and an out-of-range value is rejected with 400
    (``save_chain_max_transitions`` also clamps defensively as a second layer).
    This handler runs NO servo command — it is a config write only.
    """
    data = request.json or {}
    value = data.get('max_transitions')
    # bool is an int subclass, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        return jsonify({'status': 'error',
                        'message': 'max_transitions must be an integer'}), 400
    if not (config_store.CHAIN_MAX_TRANSITIONS_MIN <= value
            <= config_store.CHAIN_MAX_TRANSITIONS_MAX):
        return jsonify({'status': 'error',
                        'message': 'max_transitions must be between 0 and 20'}), 400
    stored = config_store.save_chain_max_transitions(value)
    return jsonify({'status': 'success', 'max_transitions': stored})


# ── Routes: Sleep / Awake timeout autosave (config write; NO servo command) ──
# These persist the mode-timeout dropdown choice the instant the operator picks
# it, so the value survives a hard browser refresh WITHOUT needing to Start the
# mode first. /nap/start and /awake/start also persist (and fall back to) the
# stored value, so either path keeps the dropdown sticky. Config write only.
@app.route('/nap/config', methods=['POST'])
def nap_config():
    """Persist the Sleep (napping) timeout dropdown choice.

    Body: ``{"timeout_min": <int in [0, 120]>}`` (0 = no timeout). Mirrors the
    strict validation of ``/nap/start`` (non-int/bool and out-of-range both
    rejected with 400); ``save_nap_timeout`` clamps defensively as well. Runs NO
    servo command — it does not start or affect a running mode.
    """
    data = request.json or {}
    value = data.get('timeout_min')
    if isinstance(value, bool) or not isinstance(value, int):
        return jsonify({'status': 'error',
                        'message': 'timeout_min must be an integer'}), 400
    if not (NAP_MIN_TIMEOUT_MIN <= value <= NAP_MAX_TIMEOUT_MIN):
        return jsonify({'status': 'error',
                        'message': 'timeout_min must be between 0 and 120'}), 400
    stored = config_store.save_nap_timeout(value)
    return jsonify({'status': 'success', 'timeout_min': stored})


@app.route('/awake/config', methods=['POST'])
def awake_config():
    """Persist the Awake timeout dropdown choice.

    Body: ``{"timeout_min": <int in [0, 120]>}`` (0 = no timeout). Same strict
    validation and defensive clamp as ``/nap/config``. Config write only.
    """
    data = request.json or {}
    value = data.get('timeout_min')
    if isinstance(value, bool) or not isinstance(value, int):
        return jsonify({'status': 'error',
                        'message': 'timeout_min must be an integer'}), 400
    if not (AWAKE_MIN_TIMEOUT_MIN <= value <= AWAKE_MAX_TIMEOUT_MIN):
        return jsonify({'status': 'error',
                        'message': 'timeout_min must be between 0 and 120'}), 400
    stored = config_store.save_awake_timeout(value)
    return jsonify({'status': 'success', 'timeout_min': stored})


# ── Routes: mic stream (proxied) ─────────────────────────────────────────────
@app.route('/mic/<state>', methods=['POST'])
def mic(state):
    # start/stop control the mic passthrough; record_start/record_stop capture
    # the FX-processed output to a WAV in audio/ (proxied to the mic controller).
    valid = ('start', 'stop', 'record_start', 'record_stop')
    if state not in valid:
        return jsonify({'status': 'error',
                        'message': f'state must be one of {valid}'}), 400
    body, code = _proxy('POST', '/handler', {'action': state})
    return jsonify(body), code


# ── Routes: jaw tuning (proxied) ─────────────────────────────────────────────
@app.route('/jaw', methods=['POST'])
def jaw():
    data = request.json or {}
    # This control panel drives the live mic passthrough, so jaw tuning
    # here targets the mic profile. Default it if the client omitted it so
    # micwebcontroller's profile allowlist check passes.
    data.setdefault('profile', 'mic')
    body, code = _proxy('POST', '/config', data)
    return jsonify(body), code


# ── Routes: voice effects (proxied) ──────────────────────────────────────────
@app.route('/effects', methods=['POST'])
def effects():
    data = request.json or {}
    body, code = _proxy('POST', '/effects', data)
    return jsonify(body), code


@app.route('/effects/save', methods=['POST'])
def effects_save():
    """Save the current live effect chain as the tuned override for a style."""
    data = request.json or {}
    body, code = _proxy('POST', '/effects/save', data)
    return jsonify(body), code


@app.route('/effects/revert', methods=['POST'])
def effects_revert():
    """Revert the last saved style to its previous saved value (single level)."""
    data = request.json or {}
    body, code = _proxy('POST', '/effects/revert', data)
    return jsonify(body), code


# ── Routes: status ───────────────────────────────────────────────────────────
@app.route('/status', methods=['GET'])
def status():
    """Aggregate local run state with the mic controller's status."""
    # Proxy the mic status BEFORE taking _launch_lock, then hold it only long
    # enough to copy the chain budget into a local — never across proxy work.
    mic_body, _ = _proxy('GET', '/status')
    with _launch_lock:
        chain_remaining = _chain['remaining']
    chain = {'remaining': chain_remaining, 'active': chain_remaining > 0}
    return jsonify({
        'last_action': _last_action['value'],
        'servos_busy': _gesture_busy(),
        'mic': mic_body,
        'chain': chain,
    })


# ── Routes: camera (read-only proxy to Camera_Service) ───────────────────────
# These three routes expose Camera_Service's Live_Feed, detections, and status
# through the control panel. They are strictly READ-ONLY: each only ever issues
# a GET to Camera_Service and NEVER a servo command (Req 2.7). If Camera_Service
# is unreachable the proxy returns 502 so the panel can show "camera
# unavailable" while every other control stays usable (Req 2.5).
def _camera_stream():
    """Stream Camera_Service's MJPEG Live_Feed straight through to the browser.

    Unlike :func:`_camera_proxy`, the Live_Feed is a long-lived
    ``multipart/x-mixed-replace`` response that must NOT be buffered or
    JSON-decoded — it is passed through chunk-by-chunk from Camera_Service's
    ``/stream`` so the browser's ``<img>`` renders frames as they arrive. The
    upstream multipart content-type is preserved so the boundary matches.

    If Camera_Service is unreachable, a 502 JSON "camera unavailable" is
    returned instead of a broken stream, so the panel stays usable (Req 2.5).

    Returns:
        A streaming Flask ``Response`` carrying the upstream MJPEG, or a
        ``(json, 502)`` tuple when Camera_Service cannot be reached.
    """
    url = f'{CAMERA_SERVICE_URL}/stream'
    # Forward the overlay toggle (?overlay=1) to Camera_Service unchanged.
    if request.args.get('overlay') == '1':
        url += '?overlay=1'
    try:
        req = urllib.request.Request(url, method='GET')
        # Open the upstream stream; do NOT use a `with` block / context manager
        # here — the connection must stay open for the life of the generator
        # below, which reads from it lazily as the browser consumes frames.
        upstream = urllib.request.urlopen(req, timeout=5)
    except urllib.error.URLError as e:
        print(f"[camera] stream unavailable: {e.reason}")
        return jsonify({'status': 'error', 'camera': 'unavailable',
                        'message': f'camera unavailable at {CAMERA_SERVICE_URL}: '
                                   f'{e.reason}'}), 502

    content_type = upstream.headers.get(
        'Content-Type', 'multipart/x-mixed-replace; boundary=frame')

    def _passthrough():
        """Yield upstream MJPEG bytes until the client or upstream disconnects."""
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                yield chunk
        except Exception as e:
            print(f"[camera] stream ended: {e}")
        finally:
            upstream.close()

    return Response(_passthrough(), mimetype=content_type)


@app.route('/camera/stream', methods=['GET'])
def camera_stream():
    """Read-only Live_Feed passthrough from Camera_Service (MJPEG)."""
    return _camera_stream()


@app.route('/camera/detections', methods=['GET'])
def camera_detections():
    """Read-only proxy of Camera_Service's latest detections (JSON)."""
    body, code = _camera_proxy('/detections')
    return jsonify(body), code


@app.route('/camera/status', methods=['GET'])
def camera_status():
    """Read-only proxy of Camera_Service's status (JSON) for the panel.

    Lets the panel render a "camera unavailable" indicator (502 from the proxy)
    and a "stalled feed" indicator from ``last_frame_age_s`` (Req 2.5, 2.6).
    """
    body, code = _camera_proxy('/status')
    return jsonify(body), code


def _camera_proxy_post(path, json_body):
    """Forward a POST to Camera_Service (8001) and return (json, status).

    The POST twin of :func:`_camera_proxy`. Used by camera routes that set a
    value on Camera_Service (e.g. selecting a detector model). Like the GET
    proxy it returns a 502 "camera unavailable" when Camera_Service cannot be
    reached, so the panel stays usable (Req 2.5).

    Args:
        path: The Camera_Service path to POST (e.g. ``/model``).
        json_body: The JSON-serialisable request body to send.

    Returns:
        A ``(body, status)`` tuple: decoded JSON (or an error dict) and the HTTP
        status code (502 when Camera_Service is unreachable).
    """
    url = f'{CAMERA_SERVICE_URL}{path}'
    try:
        payload = json.dumps(json_body or {}).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode('utf-8')
            try:
                return json.loads(body), resp.status
            except ValueError:
                return {'status': 'error', 'message': 'non-JSON response'}, resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'status': 'error', 'message': f'HTTP {e.code}'}, e.code
    except urllib.error.URLError as e:
        return {'status': 'error', 'camera': 'unavailable',
                'message': f'camera unavailable at {CAMERA_SERVICE_URL}: {e.reason}'}, 502


@app.route('/camera/model', methods=['POST'])
def camera_model():
    """Select the detector model Camera_Service loads, by file name.

    SAFETY (path traversal boundary, Req 9.4/9.5): the request ``model`` value
    becomes a filesystem path (the model file Camera_Service loads), so it is
    validated with :func:`_validate_within_base` against ``MODELS_DIR`` BEFORE
    anything is read, written, or forwarded. A value that escapes the base dir —
    a traversal sequence (``../``), an absolute path, or an out-of-base
    symlink — resolves outside ``MODELS_DIR`` and is rejected with an "invalid
    value" response; no path is touched and nothing is proxied.

    On success the request is forwarded to Camera_Service, which owns the actual
    model load. We forward only the validated base name (never the resolved
    absolute path) so Camera_Service re-resolves it under its own models dir.

    Body: JSON ``{"model": "<model-file-name>"}``.
    """
    data = request.json or {}
    model = data.get('model')
    resolved = _validate_within_base(model, MODELS_DIR)
    if resolved is None:
        # Reject: read/write nothing, forward nothing (Req 9.5).
        print(f"[camera] rejected invalid model value: {model!r}")
        return jsonify({'status': 'error', 'message': 'invalid value'}), 400
    # Forward only the validated name; Camera_Service re-resolves under its base.
    body, code = _camera_proxy_post('/model', {'model': os.path.basename(resolved)})
    return jsonify(body), code


@app.route('/camera/ir', methods=['POST'])
def camera_ir():
    """Set the IR_Illuminator mode on Camera_Service, by validated mode name.

    SAFETY (Req 9.4/10.3): the request ``mode`` value is validated against the
    fixed ``IR_MODES`` allowlist via :func:`_validate_allowlist` BEFORE anything
    is forwarded. The IR illuminator itself is owned by the non-root
    Camera_Service (Req 10); the Control_Panel only proxies the mode change. An
    invalid mode is rejected with an "invalid value" response and nothing is
    proxied to Camera_Service.

    On a valid mode the request is forwarded to Camera_Service ``POST /ir``,
    which applies it to the hardware (or degrades to "IR unavailable" if the
    hardware is absent, Req 10.5). Like the other camera routes, an unreachable
    Camera_Service surfaces as a 502 so the panel stays usable (Req 2.5).

    Body: JSON ``{"mode": "<on|off|auto>"}``.
    """
    data = request.json or {}
    mode = data.get('mode')
    if _validate_allowlist(mode, IR_MODES) is None:
        # Reject: forward nothing (Req 10.3, 9.5).
        print(f"[camera] rejected invalid IR mode value: {mode!r}")
        return jsonify({'status': 'error', 'message': 'invalid value'}), 400
    body, code = _camera_proxy_post('/ir', {'mode': mode})
    return jsonify(body), code


# ── Route: range sensor gauge ────────────────────────────────────────────────
@app.route('/range', methods=['GET'])
def range_reading():
    """Return the latest HC-SR04 distance reading for the dashboard gauge.

    Reads the shared reading published by whichever process currently owns the
    sensor (a running mode, or this app's own poller). Returns ``distance_m``
    (meters), ``distance_cm``, the publishing ``source``, and the reading
    ``age_s``. When there is no fresh reading (sensor unavailable, or the read
    got no echo), ``distance_m`` is ``null`` so the gauge can show "--".
    """
    gate_m = range_publish.get_gate_m()
    reading = range_publish.read_latest()
    if reading is None:
        return jsonify({'distance_m': None, 'distance_cm': None,
                        'source': None, 'age_s': None,
                        'gate_m': gate_m})
    meters = reading.get('distance_m')
    return jsonify({
        'distance_m': meters,
        'distance_cm': None if meters is None else round(meters * 100, 1),
        'source': reading.get('source'),
        'age_s': round(reading.get('age_s', 0.0), 2),
        'gate_m': gate_m,
    })


# ── Route: range sensor sensitivity (detection gate) ─────────────────────────
@app.route('/range/sensitivity', methods=['POST'])
def range_sensitivity():
    """Set the detection gate (meters) used by the modes' presence/approach.

    Body: JSON ``{"gate_m": <0.5..5.0>}``. The value is clamped and persisted to
    the shared range config; a running mode's detector reads it live, so the
    change takes effect without restarting the mode.
    """
    data = request.json or {}
    try:
        gate_m = float(data.get('gate_m'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'gate_m must be a number'}), 400
    stored = range_publish.set_gate_m(gate_m)
    if stored is None:
        return jsonify({'status': 'error', 'message': 'could not store gate'}), 500
    return jsonify({'status': 'success', 'gate_m': stored})


# ── Range sensor gauge poller ────────────────────────────────────────────────
# The HC-SR04 is a single GPIO device; only one process may open it at a time.
# While a background Mode (napping/awake) runs, that process owns the sensor and
# PUBLISHES readings via range_publish. When no mode is running, THIS web app
# owns the sensor and publishes its own readings. Either way the dashboard reads
# the shared file through /range. The poller below owns the sensor only when a
# mode is not publishing, and releases the GPIO pins the moment a mode takes over
# (so it never fights the mode process for the pins).
RANGE_POLL_INTERVAL_S = 0.2
_range_state = {'sensor': None}


def _range_poll_loop():
    """SOLE owner of the HC-SR04: read it and publish for the gauge + modes.

    The web app is the one and only process that opens the range sensor, so
    there is never GPIO contention over the pins. It reads every
    ``RANGE_POLL_INTERVAL_S`` and publishes each reading (source ``"webapp"``)
    via ``range_publish``. BOTH the dashboard gauge and the background Modes
    (napping/awake) consume that shared reading — the Modes evaluate
    approach/presence from the published distance rather than opening the sensor
    themselves (see ``range_publish.PublishedReadingSensor``). This keeps the
    gauge alive continuously (it never goes blank when a Mode starts) and lets a
    Mode react to presence without fighting for the pins.

    All hardware access is best-effort — if the sensor isn't wired or GPIO is
    unavailable, the loop keeps retrying without crashing the app.
    """
    while True:
        try:
            # Open the sensor lazily and keep it for the app's lifetime.
            if _range_state['sensor'] is None:
                try:
                    from range_sensor import RangeSensor
                    _range_state['sensor'] = RangeSensor()
                except Exception as e:
                    # Not wired / no GPIO — try again shortly.
                    print(f"[range] sensor unavailable: {e}")
                    time.sleep(1.0)
                    continue

            try:
                meters = _range_state['sensor'].distance_m()
                # Label the reading with the MODE currently consuming it (awake
                # / napping) so the gauge shows who's using the sensor, else
                # 'idle' when no mode is running.
                label = _active_proc['label'] if _active_proc['proc'] and \
                    _active_proc['proc'].poll() is None else None
                source = label if label in _MODE_LABELS else 'idle'
                range_publish.publish(meters, source=source)
            except Exception as e:
                print(f"[range] read failed: {e}")
                # Drop the sensor so a wedged device gets reopened next tick.
                try:
                    _range_state['sensor'].close()
                except Exception:
                    pass
                _range_state['sensor'] = None
        except Exception as e:
            print(f"[range] poll loop error: {e}")
        time.sleep(RANGE_POLL_INTERVAL_S)


def _start_background_threads():
    """Start the background daemon threads (die with the process).

    Currently just the HC-SR04 range poller, which owns the sensor and
    publishes readings for the dashboard gauge and the background Modes.
    """
    threading.Thread(target=_range_poll_loop, daemon=True).start()


if __name__ == '__main__':
    # Auto-reload on code change is ON by default. Disable it for the live
    # display with WEBAPP_DEV=0, since a reload triggered mid-routine would
    # interrupt servo motion. Accepts 0/false/no/off (case-insensitive) to opt
    # out; anything else (or unset) keeps the reloader on.
    dev_reload = os.environ.get('WEBAPP_DEV', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')

    # With the reloader active, this module is imported in two processes: the
    # watcher (parent) and the worker (child, where WERKZEUG_RUN_MAIN == 'true').
    # Only start the background threads in the process that actually serves
    # requests, otherwise the range poller would run twice.
    if not dev_reload or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _start_background_threads()

    # In dev, also watch the sibling project modules so edits to e.g.
    # servo_lock.py / animatronic.py trigger a restart too. (webapp.py itself is
    # always watched.) Imported modules are auto-watched, but listing them makes
    # the intent explicit and covers files imported lazily.
    extra_files = None
    if dev_reload:
        watch = ['servo_lock.py', 'micwebcontroller.py', 'animatronic.py',
                 'controller.py', 'movements.py', 'trunkcontroller.py',
                 'audio_player.py', 'audio_streamer.py', 'constants.py']
        extra_files = [os.path.join(PROJECT_DIR, f) for f in watch
                       if os.path.exists(os.path.join(PROJECT_DIR, f))]

    # Port 8000 so it doesn't collide with micwebcontroller (5000) or Node-RED (1880).
    app.run(host='0.0.0.0', port=8000, threaded=True,
            debug=dev_reload, use_reloader=dev_reload,
            extra_files=extra_files)
