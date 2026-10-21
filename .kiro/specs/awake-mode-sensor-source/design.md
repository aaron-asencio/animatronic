# Design Document

## Overview

This feature adds a selectable **Sensor_Source** for Awake mode: Camera_Service
person detection (`camera`, the default) or the existing HC-SR04 ultrasonic
proximity sensor (`range`). The choice is a **persisted default** stored in
`src/config/tuning.json` through the existing `config_store` helper pattern and
read once at Awake launch. It never affects an already-running session.

The design threads one new string value through four layers that already exist,
reusing each layer's established pattern rather than introducing new machinery:

1. **Config_Store** — a new `load_awake_sensor_source` / `save_awake_sensor_source`
   pair, structurally identical to `load_awake_timeout` / `save_awake_timeout`
   and the awake-pools helpers. It lives in the existing `awake` section of
   `tuning.json`, defaults to `"camera"`, and normalizes any invalid value to
   `"camera"`.
2. **Web_Control_Panel** — the Awake Config section gains a two-value `<select>`
   pre-seeded from the persisted value, a `/awake/config` POST that validates
   against exactly `{range, camera}`, and a `run_awake` that reads the persisted
   source and passes it to the subprocess as a new `--awake-sensor-source` CLI
   flag on the existing list-argv (shell=False) dispatch path.
3. **CLI dispatch** — `animatronic.py` parses `--awake-sensor-source`, clamps it
   to the two-value allowlist, and passes it into `Animatronic.awake(...)` where
   it is snapshotted for the session (exactly like `timeout_seconds` and
   `chain_sensor_end`).
4. **Awake loop** — a single source-resolution step at mode entry arms **exactly
   one** sensor path. `range` keeps today's `ApproachDetector` +
   `PublishedReadingSensor` path (`_open_nap_sensor` / `_poll_nap_sensor`).
   `camera` polls `CameraClient.get_detections()` and treats any `is_person`
   detection as a trigger. If `camera` is selected but Camera_Service is
   unavailable at launch, the session silently falls back to `range`.

All existing Awake behaviors — the timeout interrupt, the `nap_signal`
cross-process stop, the ambient routine/gesture loop, and the react-and-resume
approach reaction — are preserved unchanged. Every servo write continues to go
through the `SAFE_LIMITS`-clamping `set_angle` / `move_to` path, servos return
to rest after reactions and errors, and all action dispatch stays behind the
existing allowlist.

Per the workspace testing rule, this design does **not** include SERVO_SIM
collision/limit verification for the Awake reactions (the operator validates
servo travel on hardware). Verification relies on `python -m py_compile` and
non-safety logic checks (config round-trip, web route validation, argv
plumbing, and the camera-vs-range trigger logic). The in-code safety guards are
retained, not removed.

## Architecture

The value flows one direction, top layer to bottom, matching the project's
"higher layers call lower ones, never the reverse" rule. `config_store` remains
a leaf that imports only the standard library.

```
Operator (browser)
   │  selects "range" | "camera" in Awake Config <select>
   ▼
webapp.py  /awake/config  (POST)
   │  validate ∈ {range, camera}  → config_store.save_awake_sensor_source(...)
   ▼
config/tuning.json   awake.sensor_source = "range" | "camera"
   ▲                                   │ (read at next launch only)
   │ load                              ▼
webapp.py  run_awake()  → config_store.load_awake_sensor_source()
   │  argv: [python, animatronic.py, --action=awake,
   │         --awake-timeout-min=N, --awake-sensor-source=<src>]   (shell=False)
   ▼
animatronic.py  main()  → parse+clamp --awake-sensor-source → a.awake(sensor_source=...)
   │  snapshot source once at mode entry; resolve availability
   ▼
Animatronic.awake(...)
   ├── source == range  → _open_nap_sensor(presence)  → _poll_nap_sensor()  ─┐
   └── source == camera → CameraClient.get_detections() → any is_person      │ exactly ONE
                          (fallback to range if camera unavailable at launch)┘ armed per session
                                   │ trigger (boolean)
                                   ▼
                        _awake_approach_reaction()   (existing react-and-resume)
```

The source-resolution step is the only new branch inside the loop's setup; the
ambient loop, timeout, and stop paths below it are untouched.

## Components and Interfaces

### 1. Config_Store (`src/config_store.py`)

A new module-level constant set and a load/save pair, placed in the existing
`awake` section alongside `awake.timeout_min` and the awake pools. This mirrors
the Sleep/Awake timeout helpers exactly: a private `ConfigStore` method pair plus
thin module-level wrappers against `_default_store`.

New constants (grouped with the existing Awake persistence constants):

```python
# --- Awake sensor-source persistence ----------------------------------------
# The Sensor_Source for Awake mode's interrupt trigger, persisted inside the
# existing ``awake`` section so it coexists with ``timeout_min`` and the pools.
# Exactly two values; any other value normalizes to the safe default "camera".
# Default "camera" when unset. (Note: this config default is distinct from the
# camera→range availability fallback that happens at launch when Camera_Service
# is unreachable — see _resolve_awake_sensor_source.)
AWAKE_SENSOR_SOURCE_KEY = "sensor_source"
SENSOR_SOURCE_RANGE = "range"
SENSOR_SOURCE_CAMERA = "camera"
ALLOWED_SENSOR_SOURCES = (SENSOR_SOURCE_RANGE, SENSOR_SOURCE_CAMERA)
SENSOR_SOURCE_DEFAULT = SENSOR_SOURCE_CAMERA
```

A shared private coercion keeps the normalization rule in one place:

```python
def _coerce_sensor_source(self, value):
    """Return ``value`` if it is an allowed Sensor_Source, else the default.

    The single normalization point for the Awake Sensor_Source: any value that
    is not exactly one of ``ALLOWED_SENSOR_SOURCES`` (wrong type, unknown
    string, wrong case, etc.) becomes ``SENSOR_SOURCE_DEFAULT`` ("camera", the
    default source). Never raises.
    """
    if isinstance(value, str) and value in ALLOWED_SENSOR_SOURCES:
        return value
    return SENSOR_SOURCE_DEFAULT
```

`ConfigStore` methods (mirroring `_load_mode_timeout` / `_save_mode_timeout` in
shape — read best-effort, write via `_load_raw` + merge + `_write_raw`):

```python
def load_awake_sensor_source(self):
    """Load the persisted Awake Sensor_Source, defaulting to "camera".

    Reads ``awake.sensor_source``. Any missing file/section, non-dict section,
    or value not in ``ALLOWED_SENSOR_SOURCES`` yields ``SENSOR_SOURCE_DEFAULT``
    ("camera"). Never raises.

    Returns:
        "range" or "camera".
    """
    raw = self._load_raw()
    section = raw.get(AWAKE_KEY, {})
    if not isinstance(section, dict):
        return SENSOR_SOURCE_DEFAULT
    return self._coerce_sensor_source(section.get(AWAKE_SENSOR_SOURCE_KEY))

def save_awake_sensor_source(self, source):
    """Persist the Awake Sensor_Source, normalizing invalid values to "camera".

    The value is normalized via ``_coerce_sensor_source`` (any non-allowed value
    becomes "camera") before writing. The existing raw JSON is loaded first and
    the value is MERGED into the ``awake`` section so ``timeout_min`` and the
    awake pools (and every other top-level section) survive.

    Args:
        source: The requested Sensor_Source; invalid values coerce to "camera".

    Returns:
        The normalized value that was persisted ("range" or "camera").
    """
    clean = self._coerce_sensor_source(source)
    raw = self._load_raw()
    section = raw.get(AWAKE_KEY)
    if not isinstance(section, dict):
        section = {}
    section[AWAKE_SENSOR_SOURCE_KEY] = clean
    raw[AWAKE_KEY] = section
    self._write_raw(raw)
    print(f"Awake sensor source saved: {clean}")
    return clean
```

Module-level wrappers (next to `load_awake_timeout` / `save_awake_timeout`):

```python
def load_awake_sensor_source():
    """Load the Awake Sensor_Source via the default store ("camera" default)."""
    return _default_store.load_awake_sensor_source()

def save_awake_sensor_source(source):
    """Persist the Awake Sensor_Source via the default store.

    Returns:
        The normalized value persisted ("range" or "camera").
    """
    return _default_store.save_awake_sensor_source(source)
```

Note the deliberate contrast between the two normalization layers: the **store**
coerces an invalid value to `"camera"` (so it never persists junk), while the
**web route** (below) *rejects* an invalid submission with a 400 and writes
nothing. The store's coercion is the last-line safety net; the route is the
operator-facing validation.

### 2. Web_Control_Panel (`src/webapp.py` + `src/templates/index.html`)

**Constant** (next to the Awake timeout bounds):

```python
# Awake sensor-source allowlist for the web route. Exactly the two values the
# Config_Store accepts; the route REJECTS anything else (400), while the store
# coerces an invalid value to "camera" as a last-line default.
AWAKE_SENSOR_SOURCES = ('range', 'camera')
```

**`index()` render context** — one new kwarg, mirroring `awake_timeout_min`:

```python
awake_sensor_source=config_store.load_awake_sensor_source(),
```

**Template `index.html`** — a new row in the Awake Config section, directly
after the existing "Awake timeout" `<select>`, pre-selecting the persisted value
exactly like the timeout `<select>` does:

```html
<div class="row">
  <label>Awake sensor source<br><small style="color:var(--muted)">which
     sensor triggers Awake's reaction: the proximity range sensor, or camera
     person detection (falls back to range if the camera is unavailable)</small></label>
  <select id="awake-sensor-source" onchange="saveAwakeSensorSource()">
    <option value="range"{% if awake_sensor_source == 'range' %} selected{% endif %}>Range sensor (proximity)</option>
    <option value="camera"{% if awake_sensor_source == 'camera' %} selected{% endif %}>Camera (person detection)</option>
  </select>
</div>
```

**Template JS** — a `saveAwakeSensorSource()` next to `saveAwakeTimeout()`:

```javascript
async function saveAwakeSensorSource() {
  const value = document.getElementById('awake-sensor-source').value;
  const data = await post('/awake/config', null, { sensor_source: value });
  if (data && data.status === 'success') {
    toast('Awake sensor: ' + (value === 'camera' ? 'Camera' : 'Range sensor'));
  }
}
```

**`/awake/config` route** — extended to accept an optional `sensor_source`
alongside the existing optional `timeout_min`, so one endpoint owns all Awake
config writes. Each field is independent and optional; an invalid `sensor_source`
is rejected with 400 and nothing is written (Req 1.4):

```python
@app.route('/awake/config', methods=['POST'])
def awake_config():
    """Persist the Awake config dropdown choices (timeout and/or sensor source).

    Body may carry ``timeout_min`` (int in [0,120], 0 = no timeout) and/or
    ``sensor_source`` ("range" | "camera"). Each is optional and validated
    independently; an invalid value is rejected with 400 and NOTHING is written
    (the persisted values are left unchanged). Config write only — no servo
    command.
    """
    data = request.json or {}

    value = data.get('timeout_min')
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int):
            return jsonify({'status': 'error',
                            'message': 'timeout_min must be an integer'}), 400
        if not (AWAKE_MIN_TIMEOUT_MIN <= value <= AWAKE_MAX_TIMEOUT_MIN):
            return jsonify({'status': 'error',
                            'message': 'timeout_min must be between 0 and 120'}), 400

    source = data.get('sensor_source')
    if source is not None and source not in AWAKE_SENSOR_SOURCES:
        return jsonify({'status': 'error',
                        'message': "sensor_source must be 'range' or 'camera'"}), 400

    result = {'status': 'success'}
    if value is not None:
        result['timeout_min'] = config_store.save_awake_timeout(value)
    if source is not None:
        result['sensor_source'] = config_store.save_awake_sensor_source(source)
    return jsonify(result)
```

Both validation checks run **before** any write, so a request carrying a valid
`timeout_min` and an invalid `sensor_source` persists neither (fails fast with
400) — the persisted Sensor_Source is left unchanged (Req 1.4).

**`run_awake` / `launch_awake`** — `run_awake` reads the persisted source and
appends it as a separate argv token on the existing `shell=False` list (never a
shell string, never interpolated). The value is drawn from `config_store`, which
only ever returns an allowlisted value, so an allowlisted source is the only
thing that can reach the argv (Req 3.4):

```python
def run_awake(timeout_min):
    """Launch animatronic.py --action=awake (the awake MODE).

    Reads the persisted Sensor_Source from config_store and passes it as the
    ``--awake-sensor-source`` flag. config_store returns only an allowlisted
    value ("range"/"camera"), so the subprocess argv can carry only an
    allowlisted source — the value is never interpolated into a shell string
    (shell=False, separate argv token) and never passed to getattr/eval.
    """
    sensor_source = config_store.load_awake_sensor_source()
    cmd = [VENV_PYTHON, ANIMATRONIC, '--action=awake',
           f'--awake-timeout-min={int(timeout_min)}',
           f'--awake-sensor-source={sensor_source}']
    print(f"[awake] {' '.join(cmd)}")
    nap_signal.clear_stop()
    return subprocess.Popen(cmd, cwd=PROJECT_DIR)
```

Reading at `run_awake` time (per launch) rather than caching means the operator's
latest saved choice is picked up on the next Start, and a change made during a
running session is not seen by that session (Req 3.2, 3.3).

### 3. CLI dispatch (`src/animatronic.py`)

**Argparse** — a new flag next to `--awake-timeout-min`:

```python
parser.add_argument('--awake-sensor-source', dest='awake_sensor_source',
                    default='camera',
                    help="Awake mode: sensor interrupt source, 'camera' "
                         "(Camera_Service person detection, default) or 'range' "
                         "(HC-SR04 proximity). Any other value "
                         "is treated as 'camera'. Only used with --action=awake.")
```

**`main()` awake branch** — the raw flag is clamped to the two-value allowlist
with a plain membership test (never `getattr`/`eval`/shell), exactly like the
timeout is clamped to `[0,120]`, then passed into `awake(...)`:

```python
elif args.action == 'awake':
    reason = None
    try:
        with servo_lock():
            awake_min = max(0, min(120, args.awake_timeout_min))
            # Clamp the sensor source to the two-value allowlist; any other
            # value (including junk) falls back to the default source
            # ('camera'). Membership test only — never getattr/eval/shell on
            # the raw flag.
            source = (args.awake_sensor_source
                      if args.awake_sensor_source in ('range', 'camera')
                      else 'camera')
            reason = a.awake(timeout_seconds=awake_min * 60,
                             chain_sensor_end=False,
                             sensor_source=source)
    except ServoBusyError:
        print("Servos busy - another routine is already running. Aborting.")
        sys.exit(BUSY_EXIT_CODE)
    sys.exit(mode_exit.reason_to_exit_code(reason))
```

### 4. Awake loop (`src/animatronic.py`, `Animatronic`)

**New class constants** (near the existing awake interrupt constants):

```python
# Awake Sensor_Source values (mirror config_store.SENSOR_SOURCE_*). The source
# is snapshotted at mode entry and fixed for the session's life.
AWAKE_SOURCE_RANGE = "range"
AWAKE_SOURCE_CAMERA = "camera"

# Camera person-detection poll cadence for the camera Sensor_Source. Matches the
# tracking loop's ~0.1s period so a person is noticed promptly without hammering
# Camera_Service.
_AWAKE_CAMERA_POLL_INTERVAL_S = 0.1
```

**`awake(...)` signature** — one new keyword, snapshotted at entry exactly like
`timeout_seconds` and `chain_sensor_end`:

```python
def awake(self, timeout_seconds=300, chain_sensor_end=False,
          sensor_source="camera"):
    """AWAKE mode ...

    Args:
        ...
        sensor_source: The interrupt source for THIS session, one of "range"
            (HC-SR04 proximity, via _open_nap_sensor/_poll_nap_sensor) or
            "camera" (Camera_Service person detection). Resolved ONCE at mode
            entry and fixed for the session's life — a mid-session config change
            never affects a running session. If "camera" is selected but
            Camera_Service is unavailable at launch, the session silently falls
            back to "range" (there is always exactly one active source).
    """
```

**Source resolution at entry** — replaces the current unconditional
`self._open_nap_sensor(source="awake", detect_mode="presence")` with a single
resolution step that arms exactly one path and records which one is active:

```python
# Resolve the single active sensor source for this session (Req 3.1, 5.3).
# "camera" arms the camera path only if Camera_Service is reachable at launch;
# otherwise it silently falls back to the always-available range sensor. The
# result is recorded in self._awake_active_source and never changes for the
# session's life.
self._awake_camera_client = None
self._awake_active_source = self._resolve_awake_sensor_source(sensor_source)

if self._awake_active_source == self.AWAKE_SOURCE_CAMERA:
    print("[awake] sensor source: camera (person detection)")
    # Camera path armed; no HC-SR04 detector is opened.
else:
    print("[awake] sensor source: range (HC-SR04 proximity)")
    # Existing range path, unchanged: presence detection on the published feed.
    self._open_nap_sensor(source="awake", detect_mode="presence")
```

**New helpers** — resolution, availability probe, camera trigger poll, and a
unified sensor poll the loop calls instead of `_poll_nap_sensor` directly:

```python
def _resolve_awake_sensor_source(self, requested):
    """Resolve the active Awake sensor source once, with camera fallback.

    Normalizes ``requested`` to the two-value allowlist (anything else ->
    "camera", the default). When the result is "camera", probes Camera_Service
    availability at launch; if unavailable, silently falls back to "range" (the
    always-available HC-SR04 path) so there is always exactly one working source
    (Req 5.1-5.3). This availability fallback is distinct from the config
    default — the default is "camera", the availability-fallback target is
    "range". Builds and stores the CameraClient on the camera path. Never raises
    into the caller.

    Returns:
        The active source for this session: "range" or "camera".
    """
    source = (requested if requested in (self.AWAKE_SOURCE_RANGE,
                                         self.AWAKE_SOURCE_CAMERA)
              else self.AWAKE_SOURCE_CAMERA)
    if source != self.AWAKE_SOURCE_CAMERA:
        return self.AWAKE_SOURCE_RANGE

    client = CameraClient(base_url=DEFAULT_CAMERA_URL)
    if not self._camera_available(client):
        print("[awake] camera selected but Camera_Service unavailable at "
              "launch; falling back to the range sensor")
        return self.AWAKE_SOURCE_RANGE
    self._awake_camera_client = client
    return self.AWAKE_SOURCE_CAMERA

def _camera_available(self, client):
    """Best-effort launch-time probe of Camera_Service reachability.

    Camera_Service fails SOFT: CameraClient.get_detections() returns
    ([], 0, 0) when the service is unreachable or returns a bad payload (it
    prints the reason and never raises). A zero frame size is the tell that no
    live camera is behind the client, so "available" means a probe returned a
    positive frame dimension. Never raises.

    Returns:
        True if Camera_Service answered with a valid frame, else False.
    """
    try:
        _detections, frame_w, frame_h = client.get_detections()
        return frame_w > 0 and frame_h > 0
    except Exception as e:
        print(f"[awake] camera availability probe failed: {e}")
        return False

def _poll_camera_person(self):
    """Return True if Camera_Service currently reports a person (camera source).

    Polls the stored CameraClient and treats ANY ``is_person`` Detection as a
    trigger; non-person detections (dogs, other classes) and empty frames do
    NOT fire (Req 4.1, 4.2). Reuses the same is_person contract Tracking uses
    (vision_models.Detection.is_person). A soft camera failure yields an empty
    list, i.e. no trigger. Never raises into the loop.

    Returns:
        True when at least one person is detected this poll, else False.
    """
    client = getattr(self, "_awake_camera_client", None)
    if client is None:
        return False
    try:
        detections, _frame_w, _frame_h = client.get_detections()
    except Exception as e:
        print(f"[awake] camera poll failed: {e}")
        return False
    return any(d.is_person for d in detections)

def _poll_awake_sensor(self):
    """Return True if the ACTIVE Sensor_Source reports a trigger this poll.

    The single trigger entry point the awake loop uses, dispatching to exactly
    the active source resolved at mode entry (Req 3.1, 4.3, 5.3):

    - "camera" -> _poll_camera_person() (any person detection); the range
      detector is never consulted.
    - "range"  -> _poll_nap_sensor() (existing HC-SR04 presence latch); the
      camera client is never polled.

    Non-blocking either way. Returns False when neither source is armed.
    """
    if getattr(self, "_awake_active_source", None) == self.AWAKE_SOURCE_CAMERA:
        return self._poll_camera_person()
    return self._poll_nap_sensor()
```

**Loop + pause edits** — the three existing `self._poll_nap_sensor()` call sites
in `_run_awake_loop` and `_awake_pause` are changed to `self._poll_awake_sensor()`
so the active source drives the trigger. The reaction and latch-reset calls are
unchanged: a camera trigger funnels into the **same** `_awake_approach_reaction()`
(react-and-resume) as a range trigger (Req 6.4). `_reset_nap_sensor()` stays as
the latch reset — it is a no-op when no HC-SR04 detector is armed (the camera
path has nothing to latch), so a single camera person detection naturally
re-polls fresh each tick without a special reset path.

**Camera cleanup** — `awake()`'s existing cleanup (`_safe_rest` +
`_close_nap_sensor` + `nap_signal.clear_stop`) is retained. `_close_nap_sensor`
already no-ops when no detector is armed, so the camera path needs no HC-SR04
teardown; the `CameraClient` is a stateless read-only HTTP client holding no
OS resources, so no explicit close is required (dropping the reference on exit
is sufficient). The error path in `awake()` keeps `_safe_rest()` as the backstop.

### Why one unified poll, not a parallel loop

The loop already polls a single boolean trigger between actions and during the
pause. Adding `_poll_awake_sensor` as a thin dispatcher keeps the loop body
identical and guarantees the **exactly-one-source** invariant structurally:
whichever source was resolved at entry is the only one the dispatcher reads.
There is no separate camera thread (the HC-SR04 path uses a background poller
because `PublishedReadingSensor` latches; the camera path is a cheap ~0.1s HTTP
GET polled inline, matching how `tracking`/`scan` already consume detections).

## Data Models

### `tuning.json` — `awake` section (extended)

The `awake` section gains one key; existing keys are untouched and preserved by
the merge-on-write in `save_awake_sensor_source`:

```json
{
  "awake": {
    "timeout_min": 5,
    "routine_pool": { "brains": 3 },
    "gesture_pool": { "lookAroundRandom": 5 },
    "sensor_source": "camera"
  }
}
```

| Key             | Type   | Domain                 | Default   | Owner (save/load)                               |
|-----------------|--------|------------------------|-----------|-------------------------------------------------|
| `sensor_source` | string | `"range"` \| `"camera"`| `"camera"` | `save_awake_sensor_source` / `load_awake_sensor_source` |

### In-memory session state (`Animatronic`)

| Attribute               | Type                  | Set at        | Meaning                                              |
|-------------------------|-----------------------|---------------|------------------------------------------------------|
| `_awake_active_source`  | `"range"`\|`"camera"` | `awake()` entry | The single resolved active source for the session. |
| `_awake_camera_client`  | `CameraClient`\|`None`| `awake()` entry | Non-None only when the camera path is active.      |

### Detection (reused, unchanged — `src/vision_models.py`)

The camera path reuses `vision_models.Detection` and its `is_person` property
(`label == "person"`) via `CameraClient.get_detections()`, identical to how
`tracking` and `scan` consume person detections. No new detection model is added.

## Error Handling

| Condition | Layer | Behavior |
|-----------|-------|----------|
| Invalid `sensor_source` submitted to the web route | `webapp.awake_config` | Reject with 400; **no** write (persisted value unchanged). Req 1.4. |
| Invalid value reaches the store's save helper | `config_store.save_awake_sensor_source` | Coerce to `"camera"`, persist and return it. Req 2.3. |
| Missing key / missing section / missing file / corrupt JSON on load | `config_store.load_awake_sensor_source` | Return `"camera"` (default), never raise. Req 2.4. |
| Invalid/junk `--awake-sensor-source` CLI value | `animatronic.main` | Membership-test clamp to `"camera"` (the default). Never `getattr`/`eval`/shell. Req 3.4. |
| `camera` selected but Camera_Service unreachable at launch | `Animatronic._resolve_awake_sensor_source` / `_camera_available` | Silent fallback to `"range"`; launch completes with no operator-facing error. Req 5.1, 5.2. |
| Camera_Service drops out mid-session (camera path active) | `Animatronic._poll_camera_person` | `get_detections()` fails soft (`[], 0, 0`) → no trigger that tick; the ambient loop, timeout, and stop still run. Mode keeps running (does not crash). |
| Reaction raises during a trigger (either source) | `Animatronic._awake_approach_reaction` + `awake()` | Existing `except → _safe_rest`; `awake()`'s `_safe_rest` backstop returns servos to rest. Req 7.1, 7.2 preserved. |
| Servos busy at launch | `animatronic.main` awake branch | Existing `ServoBusyError` → exit `BUSY_EXIT_CODE`. Unchanged. |

**Exactly-one-source invariant:** `_resolve_awake_sensor_source` always returns
one of `{range, camera}` and arms only that path; `_poll_awake_sensor` reads only
the resolved source. There is no code path that arms both or neither (Req 5.3).

**Safety preservation:** no new direct `kit.servo[n].angle` writes are
introduced. The camera trigger produces only a boolean; the reaction it fans
into is the existing `_awake_approach_reaction`, whose gestures/routines write
through the `SAFE_LIMITS`-clamped `set_angle` / `move_to` path and return to rest
on completion and error (Req 7.1–7.3). Per the workspace testing rule, these
servo-safety guards are **not** re-verified via SERVO_SIM; they are preserved in
code and checked by reading, with `py_compile` catching syntax/import errors.

## Testing Strategy

Dual approach — property tests for logic that varies meaningfully with input,
example/edge tests for fixed-structure or boundary behavior, and code-level
checks (no SERVO_SIM) for the preserved hardware-safety guards.

**Property tests** (min. 100 iterations each; tagged
`Feature: awake-mode-sensor-source, Property {n}: {text}`):
- Config round-trip and normalization (Properties 1–3).
- Web-route rejection of invalid submissions (Property 4).
- Camera trigger person-only logic (Properties 5, 6).
- Source-resolution exactly-one-source invariant (Property 7).

**Example / edge tests:**
- Template renders exactly two sensor-source options; pre-selects the persisted
  value (AC 1.1, 1.2).
- POST each valid value → store readback matches (AC 1.3).
- `load` returns `"camera"` on missing key / missing file / corrupt JSON
  (AC 2.4).
- `run_awake` argv carries `--awake-sensor-source=<persisted>` as a separate
  token, `shell=False` (AC 3.1, 3.4).
- Source read once at entry; a mid-session store change doesn't reach the armed
  detector (AC 3.2, 3.3).
- `camera` + unavailable probe → range path armed, no error raised (AC 5.1,
  5.2); `camera` + available probe → camera path armed.
- Timeout still returns `AWAKE_INTERRUPT_TIMEOUT` and stop still returns
  `AWAKE_INTERRUPT_STOP` for both sources; ambient loop and the shared
  `_awake_approach_reaction` convergence unchanged (AC 6.1–6.4).

**Code-level / smoke (no SERVO_SIM per workspace rule):**
- `python -m py_compile src/config_store.py src/webapp.py src/animatronic.py`
  for syntax/import errors.
- Confirm reactions still route through the existing clamped movement path and
  that the camera branch adds no direct servo writes (AC 7.1–7.3).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

### Property 1: Sensor-source save/load round-trip

For any valid Sensor_Source value (`"range"` or `"camera"`), saving it via
`save_awake_sensor_source` and then loading it via `load_awake_sensor_source`
returns the same value.

**Validates: Requirements 2.1, 2.2, 2.5**

### Property 2: Saving the sensor source preserves all other config

For any pre-existing `tuning.json` content, saving an Awake Sensor_Source leaves
every other section and key unchanged (`profiles`, `voice_styles`, `scan`,
`awake.timeout_min`, `awake.routine_pool`, `awake.gesture_pool`, etc.) — only
`awake.sensor_source` is added or replaced.

**Validates: Requirements 2.2**

### Property 3: Invalid save input normalizes to "camera"

For any value that is not exactly `"range"` or `"camera"` (wrong type, unknown
string, wrong case), `save_awake_sensor_source` persists `"camera"` and returns
`"camera"`; a subsequent load also returns `"camera"`.

**Validates: Requirements 2.3, 2.5**

### Property 4: Web route rejects invalid sensor-source without changing state

For any submitted `sensor_source` string that is not `"range"` or `"camera"`,
the `/awake/config` route rejects the request (HTTP 400) and the persisted
Sensor_Source is unchanged from its value before the request.

**Validates: Requirements 1.4**

### Property 5: Camera source fires on any person detection

For any list of Detections that contains at least one `is_person` detection, the
camera-source trigger (`_poll_camera_person`) evaluates True.

**Validates: Requirements 4.1**

### Property 6: Camera source ignores non-person detections

For any list of Detections in which no detection `is_person` (including the
empty list and lists of only non-person classes such as dogs), the
camera-source trigger evaluates False.

**Validates: Requirements 4.2**

### Property 7: Exactly one active source resolves per session

For any combination of a configured source (`"range"`, `"camera"`, or any
invalid value) and a camera-availability outcome (available / unavailable at
launch), `_resolve_awake_sensor_source` returns exactly one of `{"range",
"camera"}`: an invalid value normalizes to the default `"camera"` (then follows
the camera availability outcome); `"camera"` with the service available resolves
to `"camera"`; `"camera"` with the service unavailable resolves to `"range"`
(the availability fallback); `"range"` resolves to `"range"`. The camera path is
armed if and only if the resolved source is `"camera"`.

**Validates: Requirements 3.1, 4.3, 5.1, 5.3**
