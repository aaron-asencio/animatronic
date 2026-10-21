# Implementation Plan: Awake Mode Sensor Source

## Overview

Thread a new Awake `Sensor_Source` ("range" | "camera") through the four
existing layers, reusing each layer's established pattern. Build bottom-up so
each task depends only on prior ones: the `config_store` leaf first, then the
`animatronic.py` CLI dispatch and awake-loop trigger logic, then the `webapp.py`
+ template wiring that reads/writes the persisted value. Each step is verified
with `python -m py_compile` and non-safety logic tests (config round-trip, web
route validation, argv plumbing, camera-vs-range trigger logic). Per the
workspace testing rule, no SERVO_SIM collision/limit verification is included;
the in-code safety guards (`SAFE_LIMITS` clamp via `set_angle`/`move_to`, return
to rest after reaction/error, allowlist dispatch) are preserved in the
implementation, not re-simulated.

## Tasks

- [x] 1. Add the Sensor_Source persistence layer to config_store
  - [x] 1.1 Add Sensor_Source constants and coercion helper to `ConfigStore`
    - Add module-level constants near the existing Awake persistence constants:
      `AWAKE_SENSOR_SOURCE_KEY = "sensor_source"`, `SENSOR_SOURCE_RANGE = "range"`,
      `SENSOR_SOURCE_CAMERA = "camera"`,
      `ALLOWED_SENSOR_SOURCES = (SENSOR_SOURCE_RANGE, SENSOR_SOURCE_CAMERA)`,
      `SENSOR_SOURCE_DEFAULT = SENSOR_SOURCE_CAMERA`
    - Add the private `ConfigStore._coerce_sensor_source(self, value)` as the single
      normalization point: return `value` when it is a `str` in
      `ALLOWED_SENSOR_SOURCES`, else `SENSOR_SOURCE_DEFAULT` (the camera default);
      never raise
    - _Requirements: 2.3_

  - [x] 1.2 Add `load_awake_sensor_source` / `save_awake_sensor_source` to `ConfigStore`
    - Implement `ConfigStore.load_awake_sensor_source(self)`: read `awake.sensor_source`
      best-effort via `_load_raw`; return `SENSOR_SOURCE_DEFAULT` (`"camera"`) on missing
      file, missing/non-dict `awake` section, or value not in `ALLOWED_SENSOR_SOURCES`
      (coerce via `_coerce_sensor_source`); never raise
    - Implement `ConfigStore.save_awake_sensor_source(self, source)`: coerce via
      `_coerce_sensor_source` (invalid values become `"camera"`), `_load_raw`, MERGE the
      value into the `awake` section
      (preserving `timeout_min`, pools, and all other sections), `_write_raw`, and
      return the normalized value
    - Mirror the shape of `_load_mode_timeout` / `_save_mode_timeout` exactly
    - _Requirements: 2.1, 2.2, 2.4, 2.5_

  - [x] 1.3 Add module-level `load_awake_sensor_source` / `save_awake_sensor_source` wrappers
    - Add thin module-level wrappers next to `load_awake_timeout` / `save_awake_timeout`
      that delegate to `_default_store`
    - `save_awake_sensor_source(source)` returns the normalized persisted value
    - _Requirements: 2.1, 2.2, 2.5_

  - [ ]* 1.4 Write property test for save/load round-trip
    - **Property 1: Sensor-source save/load round-trip**
    - **Validates: Requirements 2.1, 2.2, 2.5**

  - [ ]* 1.5 Write property test for config preservation on save
    - **Property 2: Saving the sensor source preserves all other config**
    - **Validates: Requirements 2.2**

  - [ ]* 1.6 Write property test for invalid-input normalization
    - **Property 3: Invalid save input normalizes to "camera"**
    - **Validates: Requirements 2.3, 2.5**

  - [ ]* 1.7 Write example/edge tests for load defaults
    - `load` returns `"camera"` on missing key, missing `awake` section, missing
      file, and corrupt JSON
    - _Requirements: 2.4_

- [x] 2. Verify config_store compiles
  - Run `python -m py_compile src/config_store.py`; ensure all tests pass, ask the
    user if questions arise.

- [x] 3. Add the CLI flag and session snapshot in animatronic.py
  - [x] 3.1 Add `--awake-sensor-source` argparse flag and `main()` awake-branch clamp
    - Add the `--awake-sensor-source` argument (dest `awake_sensor_source`,
      default `'camera'`) next to `--awake-timeout-min`
    - In the `main()` `awake` branch, clamp the raw flag with a plain membership test
      against `('range', 'camera')` (fall back to `'camera'`); pass as
      `sensor_source=` into `a.awake(...)`
    - Keep the clamp a membership test only — never `getattr`/`eval`/shell on the raw
      flag; preserve the existing `ServoBusyError` → `BUSY_EXIT_CODE` handling and
      `servo_lock()` usage
    - _Requirements: 3.4, 7.3_

  - [x] 3.2 Add Awake Sensor_Source class constants and extend `awake(...)` signature
    - Add `Animatronic.AWAKE_SOURCE_RANGE = "range"`, `AWAKE_SOURCE_CAMERA = "camera"`,
      and `_AWAKE_CAMERA_POLL_INTERVAL_S = 0.1` near the existing awake interrupt
      constants
    - Add the `sensor_source="camera"` keyword to `awake(...)`; document that it is
      snapshotted once at mode entry and fixed for the session's life
    - _Requirements: 3.1, 3.2_

  - [ ]* 3.3 Write example/edge test for argv snapshot semantics
    - Source read once at entry; a mid-session store change does not reach the armed
      detector (session uses the value captured at launch)
    - _Requirements: 3.2, 3.3_

- [x] 4. Implement the camera-vs-range resolution and trigger helpers
  - [x] 4.1 Implement `_resolve_awake_sensor_source` and `_camera_available`
    - `_resolve_awake_sensor_source(self, requested)`: normalize to the two-value
      allowlist; when `"camera"`, probe availability via `_camera_available` and
      silently fall back to `"range"` if unavailable; store `CameraClient` on the
      camera path; set `self._awake_active_source`; never raise
    - `_camera_available(self, client)`: best-effort launch probe; return
      `frame_w > 0 and frame_h > 0` from `client.get_detections()`; swallow
      exceptions and return `False`
    - _Requirements: 5.1, 5.2, 5.3, 3.1_

  - [x] 4.2 Implement `_poll_camera_person` and unified `_poll_awake_sensor`
    - `_poll_camera_person(self)`: poll the stored `CameraClient`; return True when
      any detection `is_person`; non-person classes and empty frames return False;
      soft camera failure returns False; never raise into the loop
    - `_poll_awake_sensor(self)`: dispatch to `_poll_camera_person()` when the active
      source is camera, else `_poll_nap_sensor()`; return False when neither armed
    - _Requirements: 4.1, 4.2, 4.3_

  - [ ]* 4.3 Write property test: camera source fires on any person detection
    - **Property 5: Camera source fires on any person detection**
    - **Validates: Requirements 4.1**

  - [ ]* 4.4 Write property test: camera source ignores non-person detections
    - **Property 6: Camera source ignores non-person detections**
    - **Validates: Requirements 4.2**

  - [ ]* 4.5 Write property test: exactly one active source resolves per session
    - **Property 7: Exactly one active source resolves per session**
    - **Validates: Requirements 3.1, 4.3, 5.1, 5.3**

  - [ ]* 4.6 Write example/edge tests for camera availability fallback
    - `camera` + unavailable probe → range path armed, no error raised; `camera` +
      available probe → camera path armed
    - _Requirements: 5.1, 5.2_

- [x] 5. Wire the resolution and unified poll into the awake loop
  - [x] 5.1 Replace unconditional range-sensor setup with source resolution at entry
    - In `awake(...)`, initialize `self._awake_camera_client = None` and set
      `self._awake_active_source = self._resolve_awake_sensor_source(sensor_source)`
    - Open `_open_nap_sensor(source="awake", detect_mode="presence")` only on the
      range path; print the resolved active source
    - _Requirements: 3.1, 5.3_

  - [x] 5.2 Route loop/pause trigger checks through `_poll_awake_sensor` and keep cleanup
    - Change the three `self._poll_nap_sensor()` call sites in `_run_awake_loop` and
      `_awake_pause` to `self._poll_awake_sensor()`; leave `_reset_nap_sensor()`,
      `_awake_approach_reaction()`, timeout, and `nap_signal` stop handling unchanged
    - Keep `awake()` cleanup (`_safe_rest` + `_close_nap_sensor` +
      `nap_signal.clear_stop`) and the error-path `_safe_rest` backstop; drop the
      `CameraClient` reference on exit (no explicit close needed)
    - Do not add any direct `kit.servo[n].angle` writes; the camera trigger yields a
      boolean that funnels into the existing clamped reaction path
    - _Requirements: 4.3, 5.3, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2_

  - [ ]* 5.3 Write example/edge tests for preserved Awake behaviors
    - Timeout still returns `AWAKE_INTERRUPT_TIMEOUT` and stop still returns
      `AWAKE_INTERRUPT_STOP` for both sources; ambient loop and shared
      `_awake_approach_reaction` convergence unchanged
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 6. Verify animatronic.py compiles and safety guards intact
  - Run `python -m py_compile src/animatronic.py`; confirm by reading that reactions
    still route through the clamped `set_angle`/`move_to` path, servos return to rest
    on completion/error, and the camera branch adds no direct servo writes. Ensure all
    tests pass, ask the user if questions arise.
  - _Requirements: 7.1, 7.2, 7.3_

- [x] 7. Wire the sensor source into the web control panel
  - [x] 7.1 Add the `AWAKE_SENSOR_SOURCES` allowlist and render context in webapp.py
    - Add `AWAKE_SENSOR_SOURCES = ('range', 'camera')` next to the Awake timeout bounds
    - Add `awake_sensor_source=config_store.load_awake_sensor_source()` to the `index()`
      render context, mirroring `awake_timeout_min`
    - _Requirements: 1.2_

  - [x] 7.2 Extend the `/awake/config` POST route to validate and persist `sensor_source`
    - Accept an optional `sensor_source` alongside the optional `timeout_min`; validate
      `sensor_source` against `AWAKE_SENSOR_SOURCES`, rejecting anything else with 400
    - Run BOTH validations before any write so an invalid field persists nothing; on
      success call `config_store.save_awake_sensor_source(source)` and include it in the
      response
    - _Requirements: 1.3, 1.4_

  - [x] 7.3 Add the Sensor_Source `<select>` and `saveAwakeSensorSource()` to index.html
    - Add a new row in the Awake Config section after the timeout `<select>` with
      exactly two options (`range`, `camera`), pre-selecting the persisted
      `awake_sensor_source`
    - Add `saveAwakeSensorSource()` next to `saveAwakeTimeout()` that POSTs
      `{ sensor_source: value }` to `/awake/config` and toasts on success
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 7.4 Pass the persisted source to the subprocess in `run_awake`
    - Read `config_store.load_awake_sensor_source()` in `run_awake` and append
      `f'--awake-sensor-source={sensor_source}'` as a separate token on the existing
      `shell=False` list argv; never interpolate into a shell string, never
      `getattr`/`eval`
    - Read at launch time (not cached) so the next Start picks up the latest choice and
      a mid-session change is not seen by the running session
    - _Requirements: 3.1, 3.4, 3.2, 3.3_

  - [ ]* 7.5 Write property test: web route rejects invalid submissions
    - **Property 4: Web route rejects invalid sensor-source without changing state**
    - **Validates: Requirements 1.4**

  - [ ]* 7.6 Write example/edge tests for template render and argv plumbing
    - Template renders exactly two sensor-source options and pre-selects the persisted
      value; POST each valid value → store readback matches; `run_awake` argv carries
      `--awake-sensor-source=<persisted>` as a separate token with `shell=False`
    - _Requirements: 1.1, 1.2, 1.3, 3.1, 3.4_

- [x] 8. Final checkpoint - compile and verify integration
  - Run `python -m py_compile src/config_store.py src/webapp.py src/animatronic.py`;
    ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (property, unit, integration, and example/edge
  tests) and can be skipped for a faster MVP.
- Each task references specific requirements for traceability; property-test tasks
  reference the correctness property from design.md they validate.
- Checkpoints use `python -m py_compile` and non-safety logic verification only. Per
  the workspace testing rule, no SERVO_SIM collision/limit verification is run — the
  operator validates servo travel on hardware.
- The in-code safety guards are preserved, not removed: every servo write continues
  through the `SAFE_LIMITS`-clamping `set_angle`/`move_to` path, servos return to rest
  after reactions and errors, and action dispatch stays behind the existing allowlist.
- Build order is bottom-up: `config_store` (leaf) → `animatronic.py` CLI + awake loop →
  `webapp.py`/template wiring, so each layer's consumer is implemented after its
  provider.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.5", "1.6", "1.7"] },
    { "id": 3, "tasks": ["3.1", "3.2"] },
    { "id": 4, "tasks": ["3.3", "4.1"] },
    { "id": 5, "tasks": ["4.2", "4.6"] },
    { "id": 6, "tasks": ["4.3", "4.4", "4.5", "5.1"] },
    { "id": 7, "tasks": ["5.2"] },
    { "id": 8, "tasks": ["5.3", "7.1"] },
    { "id": 9, "tasks": ["7.2", "7.4"] },
    { "id": 10, "tasks": ["7.3"] },
    { "id": 11, "tasks": ["7.5", "7.6"] }
  ]
}
```
