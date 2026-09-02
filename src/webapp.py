"""
webapp.py

Flask + HTML control panel for the animatronic — a self-contained replacement
for the Node-RED "Halloween Controller" dashboard.

It provides the same controls the Node-RED flow did:
  - Routines   : full gesture + audio routines  (animatronic.py --action=<name>)
  - Movements  : gesture-only tests             (controller.py  --action=<name>)
  - Mic stream : start/stop live mic passthrough (proxied to micwebcontroller)
  - Voice FX   : style preset + per-effect toggles/sliders (proxied)
  - Jaw Tuning : sensitivity / noise floor / drop threshold (proxied)
  - Automation : timed random routine + movement loops (background threads)

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

from flask import Flask, render_template, request, jsonify
import subprocess
import threading
import random
import time
import os
import json
import urllib.request
import urllib.error

from servo_lock import is_locked

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

# ── Allowlists ───────────────────────────────────────────────────────────────
# Only actions in these sets may be dispatched. This is the security boundary:
# nothing from the request is ever interpolated into a shell — we pass a fixed
# script path plus a validated --action value as separate argv entries.
ROUTINE_ACTIONS = {
    'startParty', 'hello', 'happyHalloween', 'howYallDoin', 'cantHear',
    'niceDay', 'blah', 'krusty', 'waiting', 'exorcist', 'vaderFather',
    'torture', 'vaderBeaten', 'yoda', 'yodaFear', 'evilLaugh', 'vincentPrice',
    'owl',
}

MOVEMENT_ACTIONS = {
    'wave', 'come', 'comein', 'reachOut', 'yawnCover',
    'nod', 'nodYes', 'lookUp', 'lookAround', 'lookAroundSmall', 'neckEllipse',
    'swivelHead', 'scan', 'slowScan', 'shakeHead', 'no', 'smno',
    'waveAndNod', 'waveAndLookAround', 'waveAndSwivel', 'comeAndLook',
    'comeAndSwivel', 'reachAndLook', 'yawnAndLookUp', 'patrol',
}

VOICE_STYLES = ['natural', 'demon', 'ghost', 'robot', 'chipmunk', 'possessed']
VOICE_EFFECTS = ['pitch', 'distortion', 'echo', 'reverb', 'tremolo',
                 'bitcrush', 'ring_mod']

# Pools the automation loops draw from (mirrors the old Node-RED Switch nodes).
ROUTINE_POOL = ['blah', 'exorcist', 'startParty', 'waiting', 'krusty', 'vaderFather']
# Only valid MOVEMENT_ACTIONS — the old Node-RED Switch used 'yes'/'no' labels,
# but the controller's actual actions are 'nodYes'/'no'. Using canonical names here.
MOVEMENT_POOL = ['slowScan', 'nodYes', 'no', 'lookAround', 'lookAroundSmall',
                 'scan', 'neckEllipse', 'swivelHead', 'come', 'comein', 'wave']

# ── Automation state ─────────────────────────────────────────────────────────
automation = {
    'routine_enabled': False,
    'movement_enabled': False,
    'routine_interval': 300,   # seconds (5 min) — matches old looptimer
    'movement_interval': 45,   # seconds        — matches old looptimer
}
_last_action = {'value': 'idle'}   # for status display

# Max wall-clock seconds any single gesture subprocess may run. No legitimate
# routine approaches this — it's a safety backstop so a hung process (e.g.
# blocked on I2C) can't hold the servo lock, and the arm, forever. When exceeded
# the watchdog kills the process, which releases the lock.
GESTURE_TIMEOUT = 90


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
        print(f"[watchdog] {label} exceeded {GESTURE_TIMEOUT}s — killing")
        _terminate_proc(proc, f'watchdog timeout {GESTURE_TIMEOUT}s')
        with _launch_lock:
            # Only clear if this is still the tracked process.
            if _active_proc['proc'] is proc:
                _active_proc['proc'] = None
                _active_proc['label'] = None
                _last_action['value'] = f'timeout:{label}'


def _mic_is_streaming():
    """Ask the mic controller whether it's currently streaming.

    Routines/movements and the mic stream both drive the jaw motor GPIO, so
    they cannot run at the same time (lgpio raises 'GPIO busy'). We check here
    so we can refuse cleanly instead of spawning a subprocess that crashes.
    Returns False if the mic controller is unreachable (nothing holding the pin).
    """
    body, code = _proxy('GET', '/status')
    if code == 200 and isinstance(body, dict):
        return bool(body.get('is_streaming'))
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
        if _gesture_busy():
            active = _active_proc['label'] or 'another process'
            return False, f'Servos busy — {active} is still running.'
        # The mic stream holds the jaw-motor GPIO; a gesture can't claim it too.
        if _mic_is_streaming():
            return False, ('Mic streaming is on — it uses the jaw motor. '
                           'Turn off the mic before running a routine.')
        proc = launcher(action)
        label = f'{kind}:{action}'
        _active_proc['proc'] = proc
        _active_proc['label'] = label
        _last_action['value'] = label
        # Start a watchdog so a hung routine can't hold the lock forever.
        threading.Thread(target=_watchdog, args=(proc, label), daemon=True).start()
        return True, f'{kind} started: {action}'


# ── Automation loops (background daemon threads) ─────────────────────────────
def _routine_loop():
    """Fire a random full routine every routine_interval seconds when enabled."""
    while True:
        if automation['routine_enabled']:
            action = random.choice(ROUTINE_POOL)
            # Skip this tick if the servos are already busy — never stack routines.
            ok, message = launch_gesture('routine', action, run_routine)
            if not ok:
                print(f"[auto-routine] skipped: {message}")
        # Sleep in 1s slices so interval/toggle changes take effect quickly.
        for _ in range(automation['routine_interval']):
            if not automation['routine_enabled']:
                break
            time.sleep(1)
        if not automation['routine_enabled']:
            time.sleep(1)


def _movement_loop():
    """Fire a random gesture-only movement every movement_interval seconds."""
    while True:
        if automation['movement_enabled']:
            action = random.choice(MOVEMENT_POOL)
            # Skip this tick if the servos are already busy — never stack gestures.
            ok, message = launch_gesture('movement', action, run_movement)
            if not ok:
                print(f"[auto-movement] skipped: {message}")
        for _ in range(automation['movement_interval']):
            if not automation['movement_enabled']:
                break
            time.sleep(1)
        if not automation['movement_enabled']:
            time.sleep(1)


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


# ── Routes: page ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template(
        'index.html',
        routines=sorted(ROUTINE_ACTIONS),
        movements=sorted(MOVEMENT_ACTIONS),
        styles=VOICE_STYLES,
        effects=VOICE_EFFECTS,
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
    """Emergency stop: kill any running gesture and disable automation.

    Killing the gesture subprocess releases the servo lock. Automation is turned
    off too, so the loops don't immediately relaunch something.
    """
    # Turn off automation first so a loop can't relaunch between kill and reply.
    automation['routine_enabled'] = False
    automation['movement_enabled'] = False
    message = stop_active_gesture(reason='force stop from UI')
    print(f"[stop] {message} (automation disabled)")
    return jsonify({'status': 'success', 'message': message, 'automation': automation})


# ── Routes: mic stream (proxied) ─────────────────────────────────────────────
@app.route('/mic/<state>', methods=['POST'])
def mic(state):
    if state not in ('start', 'stop'):
        return jsonify({'status': 'error', 'message': 'state must be start or stop'}), 400
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


# ── Routes: automation ───────────────────────────────────────────────────────
@app.route('/automation', methods=['POST'])
def set_automation():
    data = request.json or {}
    if 'routine_enabled' in data:
        automation['routine_enabled'] = bool(data['routine_enabled'])
    if 'movement_enabled' in data:
        automation['movement_enabled'] = bool(data['movement_enabled'])
    if 'routine_interval' in data:
        automation['routine_interval'] = max(5, int(data['routine_interval']))
    if 'movement_interval' in data:
        automation['movement_interval'] = max(5, int(data['movement_interval']))
    print(f"[automation] {automation}")
    return jsonify({'status': 'success', 'automation': automation})


# ── Routes: status ───────────────────────────────────────────────────────────
@app.route('/status', methods=['GET'])
def status():
    """Aggregate local automation state with the mic controller's status."""
    mic_body, _ = _proxy('GET', '/status')
    return jsonify({
        'automation': automation,
        'last_action': _last_action['value'],
        'servos_busy': _gesture_busy(),
        'mic': mic_body,
    })


def _start_automation_threads():
    """Start the automation loops as daemon threads (die with the process)."""
    threading.Thread(target=_routine_loop, daemon=True).start()
    threading.Thread(target=_movement_loop, daemon=True).start()


if __name__ == '__main__':
    # Auto-reload on code change is ON by default. Disable it for the live
    # display with WEBAPP_DEV=0, since a reload triggered mid-routine would
    # interrupt servo motion. Accepts 0/false/no/off (case-insensitive) to opt
    # out; anything else (or unset) keeps the reloader on.
    dev_reload = os.environ.get('WEBAPP_DEV', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')

    # With the reloader active, this module is imported in two processes: the
    # watcher (parent) and the worker (child, where WERKZEUG_RUN_MAIN == 'true').
    # Only start the automation threads in the process that actually serves
    # requests, otherwise the loops would run twice.
    if not dev_reload or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _start_automation_threads()

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
