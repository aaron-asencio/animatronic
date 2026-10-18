# Implementation Plan: System Volume Control

## Overview

Add a system volume control to the Flask control panel. Work proceeds backend-first
so every layer can be verified before the next builds on it: the pure parse/format
helper, then the two `subprocess`-backed helpers, then the `GET`/`POST /volume`
routes, and finally the `index.html` slider + JS that initializes from `GET /volume`
and applies on release via `POST /volume`. All `wpctl` calls go through
`subprocess.run([...], shell=False)` with a fixed argument list; the only
request-derived value reaching a subprocess is the clamped integer formatted into a
single `f"{percent}%"` argv element.

Per the project rules this feature is hardware-adjacent (audio sink) but involves
**no servo movement routines**, so verification is `python -m py_compile` for syntax
plus non-safety logic tests (route wiring, parse/clamp logic, mocked-`subprocess`
assertions, DOM checks). No `SERVO_SIM` checks apply.

## Tasks

- [x] 1. Add volume parsing + command constants to `src/webapp.py`
  - Add module-level constants `WPCTL`, `DEFAULT_SINK`, and the `_VOLUME_RE` regex
    near the other `webapp.py` module constants; ensure `re` and `subprocess` are
    imported.
  - Implement `_parse_wpctl_volume(output)`: search for the gain float in the
    `wpctl get-volume` line, return `round(gain * 100)`, and raise `ValueError` on
    unparseable/empty input.
  - _Requirements: 2.3_

  - [ ]* 1.1 Write property test for the parse/set round-trip
    - **Feature: system-volume-control, Property 1: Volume parse / set round-trip**
    - For any int `N` in `[0, 100]`, parsing a `"Volume: {N/100:.2f}"` line yields
      `N`; for any gain `g >= 0`, `_parse_wpctl_volume` of a well-formed line equals
      `round(g * 100)`. Use hypothesis, min. 100 iterations.
    - **Validates: Requirements 2.3**

- [x] 2. Add the `_read_sink_volume` / `_set_sink_volume` subprocess helpers
  - [x] 2.1 Implement `_read_sink_volume()` in `src/webapp.py`
    - Invoke `subprocess.run([WPCTL, 'get-volume', DEFAULT_SINK], shell=False,
      capture_output=True, text=True)`; raise `ValueError` on non-zero exit, else
      return `_parse_wpctl_volume(proc.stdout)`.
    - _Requirements: 2.2, 2.5, 5.2_

  - [x] 2.2 Implement `_set_sink_volume(percent)` in `src/webapp.py`
    - Invoke `subprocess.run([WPCTL, 'set-volume', DEFAULT_SINK, f'{percent}%'],
      shell=False, capture_output=True, text=True)`; raise `ValueError` on non-zero
      exit. The validated int is formatted into a single argv element, never a shell
      string.
    - _Requirements: 3.3, 5.1, 5.3, 5.4_

- [x] 3. Implement the `GET /volume` route in `src/webapp.py`
  - Add `volume_get()` on `@app.route('/volume', methods=['GET'])`: call
    `_read_sink_volume()`, return `{"status":"success","level":<int>}`; on any
    exception log and return `500 {"status":"error","message":...}` without raising.
  - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [ ]* 3.1 Write property test: `GET /volume` never raises and 500s on failure
    - **Feature: system-volume-control, Property 3: Get_Volume_Route never raises and returns 500 on failure**
    - With `subprocess` mocked to return arbitrary stdout (incl. unparseable/empty)
      plus any non-zero exit code, the route returns HTTP 500 with an error body and
      never propagates an exception. Use the Flask `test_client()` + hypothesis.
    - **Validates: Requirements 2.5**

  - [ ]* 3.2 Write wiring test for `GET /volume` argv + success shape
    - Mocked `subprocess`: assert the exact argv list
      `[WPCTL, 'get-volume', DEFAULT_SINK]` and `shell=False`; a well-formed stdout
      yields `200 {"status":"success","level":<int>}` matching the parsed percent.
    - _Requirements: 2.2, 2.4, 5.2_

- [x] 4. Implement the `POST /volume` route in `src/webapp.py`
  - Add `volume_post()` on `@app.route('/volume', methods=['POST'])`: coerce
    `data.get('level')` via `int(...)`; on `TypeError`/`ValueError` return `400`
    applying nothing; else `applied = max(0, min(100, requested))`, call
    `_set_sink_volume(applied)`, return `{"status":"success","level":applied}`; a
    `_set_sink_volume` failure logs and returns `500` without raising.
  - _Requirements: 3.1, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 5.4_

  - [ ]* 4.1 Write property test: applied level is the clamped coercion of the request
    - **Feature: system-volume-control, Property 2: Applied level is the clamped coercion of the request**
    - For any int-coercible `r`, the route returns and applies
      `max(0, min(100, int(r)))`; the response `level` equals the value passed to
      `wpctl set-volume` (mocked). Use the test client + hypothesis.
    - **Validates: Requirements 3.4, 4.1, 4.2, 4.3**

  - [ ]* 4.2 Write property test: safe wpctl invocation with a single validated percent element
    - **Feature: system-volume-control, Property 4: Set_Volume_Route invokes wpctl safely with a single validated percent element**
    - For any request input: `subprocess` is called with `shell=False` + an arg
      list; when int-coercible, the sink-affecting element matches `^\d+%$` and
      equals `f"{applied}%"`; when missing/non-coercible the route returns `400` and
      invokes no `set-volume` process. Use the test client + hypothesis (incl. inputs
      with shell metacharacters).
    - **Validates: Requirements 4.4, 5.1, 5.3**

  - [ ]* 4.3 Write property test: `POST /volume` returns 500 on non-zero wpctl exit without raising
    - **Feature: system-volume-control, Property 5: Set_Volume_Route returns 500 on a non-zero wpctl exit without raising**
    - For any accepted (clamped) level, a mocked non-zero `wpctl set-volume` exit
      makes the route return HTTP 500 with an error body and never raise.
    - **Validates: Requirements 5.4**

- [x] 5. Checkpoint - backend compiles and tests pass
  - Run `python -m py_compile src/webapp.py` to catch syntax/import errors.
  - Run the volume backend tests (`pytest -q -k volume`).
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Add the volume slider markup to `src/templates/index.html`
  - Inside the `.mic-bar`'s `.mic-right` region, after `#active-trigger` /
    `#chain-readout`, add the `.volume-control` span: a `Volume` label, an
    `<input type="range" id="volume-slider" min="0" max="100" step="1" value="100">`
    wired to `oninput="onVolumeInput(this.value)"` and
    `onchange="onVolumeChange(this.value)"`, and a `#volume-readout` span.
  - _Requirements: 1.1, 1.2, 1.3, 2.6_

- [x] 7. Add the volume slider JavaScript to `src/templates/index.html`
  - Add `onVolumeInput(value)` (readout only, no request), `onVolumeChange(value)`
    (client-clamp to `[0,100]`, `POST /volume`, reflect applied level on success,
    `toast` on error), `setVolumeUI(level)`, and `initVolume()`; self-start
    `initVolume()` on load like the other pollers.
  - _Requirements: 2.1, 2.4, 2.6, 3.1, 3.2, 3.5, 4.5_

  - [ ]* 7.1 Write DOM/example tests for slider placement, attributes, and behavior
    - Render the control panel via the Flask test client and assert: the slider sits
      in `.mic-right` (Req 1.1); `min/max/step` = `0/100/1` (Req 1.2); the readout
      element exists (Req 1.3). Add JS-logic checks (jsdom or an equivalent harness,
      matching the repo's existing frontend-test approach) for: `input` updates the
      readout and sends no POST (Req 1.3, 3.2); `change` POSTs (Req 3.1); a success
      response updates the readout (Req 3.5); load initializes from `GET /volume`
      (Req 2.1, 2.4) and leaves the default on error (Req 2.6); client clamp before
      send (Req 4.5).
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.4, 2.6, 3.1, 3.2, 3.5, 4.5_

- [x] 8. Final checkpoint - full feature compiles and tests pass
  - Run `python -m py_compile src/webapp.py`.
  - Run the full volume test set (`pytest -q -k volume`).
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (tests) and can be skipped for a faster MVP.
- Each task references specific requirements for traceability; property-test
  sub-tasks also name the design property they validate.
- This feature touches audio, not servos: verification is `py_compile` + non-safety
  logic tests (route wiring, parse/clamp, mocked `subprocess`, DOM). No `SERVO_SIM`
  collision/limit checks apply.
- Property tests use mocked `subprocess` (no real `wpctl` calls) with min. 100
  iterations, following the existing `tests/` pytest + hypothesis + Flask
  `test_client()` conventions.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["1.1", "2.1", "2.2"] },
    { "id": 2, "tasks": ["3"] },
    { "id": 3, "tasks": ["3.1", "3.2"] },
    { "id": 4, "tasks": ["4"] },
    { "id": 5, "tasks": ["4.1", "4.2", "4.3"] },
    { "id": 6, "tasks": ["6"] },
    { "id": 7, "tasks": ["7"] },
    { "id": 8, "tasks": ["7.1"] }
  ]
}
```
