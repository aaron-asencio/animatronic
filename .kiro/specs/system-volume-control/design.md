# Design Document

## Overview

This feature adds a system volume control to the Flask control panel
(`src/webapp.py`, port 8000) so the operator can set the Raspberry Pi's default
audio sink level from the browser instead of running `wpctl` by hand. It has two
halves:

- **Backend** — two new routes on the Control_Panel_App:
  - `GET /volume` reads the current Default_Audio_Sink level by invoking
    `wpctl get-volume @DEFAULT_AUDIO_SINK@`, parses the floating-point gain,
    converts it to an integer percentage, and returns it as JSON.
  - `POST /volume` accepts a requested level, coerces it to an integer, clamps
    it to `[0, 100]`, and applies it by invoking
    `wpctl set-volume @DEFAULT_AUDIO_SINK@ <N>%`.
- **Frontend** — a 0–100 range slider with a live percentage readout, rendered
  in the `.mic-bar` area of `src/templates/index.html`, to the right of the
  live-mic and record controls. It initializes from `GET /volume` on page load,
  updates the readout live while dragging (no request), and sends to
  `POST /volume` only on release.

The design follows the existing `webapp.py` conventions exactly: subprocess
calls use `subprocess.run(..., shell=False)` with a fixed argument **list**
(never a shell string), request values are validated/coerced before use, and
failures return a JSON error body with an appropriate HTTP status instead of
raising. These mirror the established `camera_model`, `camera_ir`, and
`range_sensitivity` routes.

## Architecture

```
Browser (index.html .mic-bar)
  │  GET  /volume            (on page load → initialize slider)
  │  POST /volume {level:N}  (on slider release → apply)
  ▼
Control_Panel_App  (src/webapp.py, Flask, port 8000, runs as root)
  │  subprocess.run([... ], shell=False, capture_output=True, text=True)
  ▼
wpctl  (WirePlumber CLI)
  │
  ▼
Default_Audio_Sink  (@DEFAULT_AUDIO_SINK@)
```

The volume routes touch no servo hardware, hold no servo lock, and are
independent of the gesture/mode machinery. They sit alongside the other
read/apply utility routes (`/range`, `/range/sensitivity`, `/camera/*`). No new
module is introduced — the two helpers and two routes live in `webapp.py` next
to the existing routes, consistent with the project's "one control panel"
design.

### Why the parse/apply split

`wpctl get-volume` emits a float **gain** (`1.0` == 100%), while `wpctl
set-volume ... N%` takes an integer **percentage**. The backend owns the
conversion in both directions so the frontend only ever deals in integer
percentages `[0, 100]`:

- read:  `percent = round(gain * 100)`
- apply: argv element `f"{percent}%"`

## Components and Interfaces

### Backend helpers (`src/webapp.py`)

Two small module-level helpers keep the subprocess invocation and parsing in one
place and testable in isolation. They follow the `shell=False` + argument-list
convention and never interpolate request values into a shell string.

```python
import re
import subprocess

# Fixed command fragments — the sink target is a constant, never request-derived.
WPCTL = 'wpctl'
DEFAULT_SINK = '@DEFAULT_AUDIO_SINK@'

# wpctl get-volume prints e.g. "Volume: 1.00" or "Volume: 0.85 [MUTED]".
# We capture the first floating-point number on the line.
_VOLUME_RE = re.compile(r'Volume:\s*([0-9]+(?:\.[0-9]+)?)')


def _parse_wpctl_volume(output):
    """Parse wpctl get-volume output into an integer percentage.

    Args:
        output: The stdout text from ``wpctl get-volume @DEFAULT_AUDIO_SINK@``,
            e.g. ``"Volume: 1.00"`` or ``"Volume: 0.85 [MUTED]"``.

    Returns:
        The gain as an integer percentage, ``round(gain * 100)``.

    Raises:
        ValueError: If no gain value can be parsed from ``output``. Callers
            catch this and return HTTP 500 — the raise never escapes the route.
    """
    match = _VOLUME_RE.search(output or '')
    if not match:
        raise ValueError(f'unparseable wpctl output: {output!r}')
    gain = float(match.group(1))
    return round(gain * 100)


def _read_sink_volume():
    """Read the Default_Audio_Sink level as an integer percentage.

    Invokes ``wpctl get-volume @DEFAULT_AUDIO_SINK@`` with ``shell=False`` and a
    fixed argument list (Req 5.2), then parses the gain (Req 2.2, 2.3).

    Returns:
        The current level as an int in roughly [0, 100+] (wpctl can report
        over-amplified sinks above 100; the GET route reports what it reads).

    Raises:
        ValueError: On a non-zero exit or unparseable output. The route catches
            this and returns HTTP 500 (Req 2.5).
    """
    proc = subprocess.run(
        [WPCTL, 'get-volume', DEFAULT_SINK],
        shell=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ValueError(f'wpctl get-volume exited {proc.returncode}: {proc.stderr!r}')
    return _parse_wpctl_volume(proc.stdout)


def _set_sink_volume(percent):
    """Apply an integer percentage to the Default_Audio_Sink.

    Invokes ``wpctl set-volume @DEFAULT_AUDIO_SINK@ <N>%`` with ``shell=False``
    and a fixed argument list; the validated integer is formatted into a SINGLE
    ``f"{percent}%"`` argv element — never interpolated into a shell string
    (Req 5.1, 5.3).

    Args:
        percent: An already-validated, already-clamped integer in [0, 100].

    Raises:
        ValueError: If wpctl exits non-zero. The route catches this and returns
            HTTP 500 (Req 5.4).
    """
    proc = subprocess.run(
        [WPCTL, 'set-volume', DEFAULT_SINK, f'{percent}%'],
        shell=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ValueError(f'wpctl set-volume exited {proc.returncode}: {proc.stderr!r}')
```

### `GET /volume` — Get_Volume_Route

Reads back the current sink level and returns it as JSON. Mirrors the
read-and-return shape of `/range`. Any read or parse failure is caught and
surfaced as HTTP 500 with an error body — the handler never raises (Req 2.5).

```python
@app.route('/volume', methods=['GET'])
def volume_get():
    """Return the current Default_Audio_Sink level as an integer percentage.

    Invokes ``wpctl get-volume @DEFAULT_AUDIO_SINK@`` (shell=False, arg list),
    parses the float gain, and converts it to an int percent (Req 2.2, 2.3).
    On any read/parse failure returns HTTP 500 with an error body and does NOT
    raise (Req 2.5).
    """
    try:
        level = _read_sink_volume()
    except Exception as e:   # noqa: BLE001 — never let a failure escape the route
        print(f"[volume] get failed: {e}")
        return jsonify({'status': 'error', 'message': 'could not read volume'}), 500
    return jsonify({'status': 'success', 'level': level})
```

### `POST /volume` — Set_Volume_Route

Accepts a requested level, coerces to int, rejects non-coercible/missing with
HTTP 400 (applying nothing), clamps to `[0, 100]`, applies via `_set_sink_volume`,
and returns the applied level. A non-zero `wpctl` exit becomes HTTP 500. Mirrors
the coerce-then-clamp shape of `range_sensitivity`.

```python
@app.route('/volume', methods=['POST'])
def volume_post():
    """Apply a requested Volume_Level to the Default_Audio_Sink.

    Pipeline (Req 4.1-4.4): coerce the requested value to int; if missing or
    non-coercible, reject with HTTP 400 and apply NO change. Otherwise clamp to
    [0, 100] and invoke ``wpctl set-volume @DEFAULT_AUDIO_SINK@ <N>%`` with the
    validated int formatted into a single argv element (Req 3.3, 5.1, 5.3). On a
    non-zero wpctl exit return HTTP 500 (Req 5.4); on success return the applied
    level (Req 3.4).

    Body: JSON ``{"level": <int>}``.
    """
    data = request.json or {}
    try:
        requested = int(data.get('level'))
    except (TypeError, ValueError):
        # Missing or non-coercible → reject, apply nothing (Req 4.4).
        return jsonify({'status': 'error', 'message': 'level must be an integer'}), 400
    applied = max(0, min(100, requested))   # clamp to [0, 100] (Req 4.2, 4.3)
    try:
        _set_sink_volume(applied)
    except Exception as e:   # noqa: BLE001 — non-zero exit / failure → 500, no raise
        print(f"[volume] set failed: {e}")
        return jsonify({'status': 'error', 'message': 'could not set volume'}), 500
    return jsonify({'status': 'success', 'level': applied})
```

### HTTP interface summary

| Route | Method | Request body | Success response | Error responses |
|-------|--------|--------------|------------------|-----------------|
| `/volume` | GET | — | `200 {"status":"success","level":<int>}` | `500 {"status":"error","message":...}` on read/parse failure |
| `/volume` | POST | `{"level":<int>}` | `200 {"status":"success","level":<applied int>}` | `400` on missing/non-coercible level; `500` on non-zero wpctl exit |

### Frontend (`src/templates/index.html`)

**Markup** — added to the existing `.mic-bar`'s `.mic-right` region, after the
`#active-trigger` / `#chain-readout` spans, so it sits to the right of the
live-mic toggle and record button (Req 1.1). It reuses the page's existing
range-input styling conventions.

```html
<!-- inside <div class="mic-right"> … after #chain-readout -->
<span class="volume-control" style="display:flex; align-items:center; gap:8px; margin-left:auto;">
  <label for="volume-slider" style="font-size:0.8rem; color:var(--muted);">Volume</label>
  <input type="range" id="volume-slider" min="0" max="100" step="1" value="100"
         oninput="onVolumeInput(this.value)"
         onchange="onVolumeChange(this.value)">
  <span id="volume-readout" style="font-size:0.8rem; min-width:3ch;">100%</span>
</span>
```

- `min="0" max="100" step="1"` gives the integer 0–100 range (Req 1.2).
- `value="100"` is the default markup position used if the load read-back fails
  (Req 2.6).
- `#volume-readout` is the live percentage readout (Req 1.3).

**Behavior (JS)** — three small functions plus a page-load initializer, matching
the existing self-starting-poller / `fetch` conventions in the file.

```javascript
// ── Volume control ───────────────────────────────────────────────────────
// input (drag): update ONLY the readout, send nothing (Req 3.2).
function onVolumeInput(value) {
  document.getElementById('volume-readout').textContent = value + '%';
}

// change (release): clamp to [0,100] and POST the selected level (Req 3.1, 4.5).
async function onVolumeChange(value) {
  const level = Math.max(0, Math.min(100, Math.round(+value)));
  try {
    const res = await fetch('/volume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({level}),
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      // Reflect the APPLIED (server-clamped) level in the readout (Req 3.5).
      setVolumeUI(data.level);
    } else {
      toast(data.message || 'Could not set volume', true);
    }
  } catch (e) {
    toast('Network error: ' + e.message, true);
  }
}

// Set both the slider position and the readout to a known level.
function setVolumeUI(level) {
  const slider = document.getElementById('volume-slider');
  slider.value = level;
  document.getElementById('volume-readout').textContent = level + '%';
}

// On page load, read the real sink level and initialize the slider (Req 2.1).
// On error, leave the slider at its default markup position and usable (Req 2.6).
async function initVolume() {
  try {
    const res = await fetch('/volume');
    const data = await res.json();
    if (res.ok && data.status === 'success' && typeof data.level === 'number') {
      setVolumeUI(Math.max(0, Math.min(100, data.level)));  // Req 2.4
    }
    // else: leave the slider at its markup default (value="100"), still usable.
  } catch (e) {
    // Network error → leave default markup position, slider stays usable.
  }
}
initVolume();   // self-start on load, like pollCamera()/pollStatus()
```

## Data Models

The feature has one scalar domain value and no persisted state.

- **Volume_Level** — an integer percentage. The authoritative range after
  validation is `[0, 100]`. `GET /volume` reports whatever the sink currently is
  (which `wpctl` may report above 100 for an over-amplified sink); `POST /volume`
  always clamps to `[0, 100]` before applying.
- **Gain** — the floating-point value `wpctl get-volume` prints (`1.0` == 100%).
  Converted to a percentage by `round(gain * 100)` and never surfaced to the
  client.

No config store entry is added — the live sink level is the single source of
truth, read on demand (unlike the timeout/pool values, which persist to
`tuning.json`).

## Error Handling

| Condition | Where | Handling |
|-----------|-------|----------|
| `wpctl get-volume` non-zero exit, or unparseable stdout | `GET /volume` | `_read_sink_volume` raises `ValueError`; route catches, logs, returns `500 {"status":"error",...}`; never raises (Req 2.5) |
| Missing / non-int-coercible `level` | `POST /volume` | `int(...)` raises `TypeError`/`ValueError`; route returns `400`, applies no change (Req 4.4) |
| Requested level out of `[0, 100]` | `POST /volume` | Clamped with `max(0, min(100, requested))` before apply (Req 4.2, 4.3) |
| `wpctl set-volume` non-zero exit | `POST /volume` | `_set_sink_volume` raises `ValueError`; route catches, logs, returns `500` (Req 5.4) |
| `GET /volume` returns an error to the browser | frontend `initVolume` | Slider left at markup default `value="100"`, stays usable (Req 2.6) |
| Network failure on `POST /volume` | frontend `onVolumeChange` | `toast(...)` surfaces the error; no uncaught rejection |

**Security.** Both routes invoke `wpctl` with `subprocess.run([...],
shell=False)` and a fixed argument list (Req 5.1, 5.2). The only request-derived
value that reaches a subprocess is the applied integer, formatted into a single
`f"{percent}%"` argv element after coercion and clamping — it is never
concatenated into a shell command string (Req 5.3). This matches the project's
`shell=False` + validated-argv security convention (`code-security` steering).
The routes run in the root control-panel process but add no new filesystem
writes and no user-controlled paths.

## Testing Strategy

**Dual approach.** Property tests cover the universal input-driven logic (parse,
clamp, injection-safety, failure handling); unit/integration tests with a mocked
`subprocess` cover the fixed command wiring and the client DOM behaviors.

- **Property tests** (min. 100 iterations each, mocked `subprocess` — no real
  `wpctl` calls): the five properties below.
- **Integration/wiring tests** (mocked `subprocess`, example-based): assert the
  exact argv list and `shell=False` for both `wpctl get-volume` and
  `wpctl set-volume` (Req 2.2, 3.3, 5.1, 5.2).
- **Example / DOM tests**: slider placement in `.mic-right` (Req 1.1); range
  attributes `min/max/step` = `0/100/1` (Req 1.2); readout updates on `input`
  with no POST (Req 1.3, 3.2); `change` sends a POST (Req 3.1); success response
  updates the readout (Req 3.5); load initializes from `GET /volume` (Req 2.1,
  2.4) and leaves the default on error (Req 2.6); client-side clamp before send
  (Req 4.5).

Each property test is tagged **Feature: system-volume-control, Property N: …**
and references its design property below.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — a formal statement about what the system should
do. Properties serve as the bridge between human-readable specifications and
machine-verifiable correctness guarantees.*

### Property 1: Volume parse / set round-trip

*For any* integer percentage `N` in `[0, 100]`, formatting it the way
`_set_sink_volume` does (`f"{N}%"`) and formatting the corresponding `wpctl
get-volume` output line for gain `N/100` (e.g. `"Volume: {N/100:.2f}"`), then
parsing that line with `_parse_wpctl_volume`, SHALL yield `N` back; and more
generally, *for any* floating-point gain `g >= 0`, `_parse_wpctl_volume` of a
well-formed `"Volume: g"` line SHALL equal `round(g * 100)`.

**Validates: Requirements 2.3**

### Property 2: Applied level is the clamped coercion of the request

*For any* int-coercible requested level `r`, the `POST /volume` route SHALL apply
and return `applied = max(0, min(100, int(r)))`; i.e. values below 0 clamp to 0,
values above 100 clamp to 100, in-range values pass through unchanged, and the
success response body's `level` equals the value actually passed to
`wpctl set-volume`.

**Validates: Requirements 3.4, 4.1, 4.2, 4.3**

### Property 3: Get_Volume_Route never raises and returns 500 on failure

*For any* `wpctl get-volume` result — arbitrary stdout text (including
unparseable or empty output) combined with any non-zero exit code — the
`GET /volume` route SHALL return an HTTP 500 response with an error status body
and SHALL NOT propagate an unhandled exception.

**Validates: Requirements 2.5**

### Property 4: Set_Volume_Route invokes wpctl safely with a single validated percent element

*For any* request input, `POST /volume` SHALL invoke `wpctl` only via
`subprocess` with `shell=False` and an argument list; when the input is
int-coercible, the sole sink-affecting argument element SHALL match `^\d+%$` and
equal `f"{applied}%"` for the clamped `applied` level (so no shell metacharacter
from the input can survive into the command); and when the input is missing or
non-coercible, the route SHALL return HTTP 400 and invoke no `wpctl set-volume`
process.

**Validates: Requirements 4.4, 5.1, 5.3**

### Property 5: Set_Volume_Route returns 500 on a non-zero wpctl exit without raising

*For any* accepted (clamped) level, if the `wpctl set-volume` process exits with
any non-zero status, the `POST /volume` route SHALL return an HTTP 500 response
with an error status body and SHALL NOT propagate an unhandled exception.

**Validates: Requirements 5.4**
