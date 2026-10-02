# Implementation Plan: Camera Person Detection & Tracking

## Overview

This plan implements vision-based perception for the animatronic in the six
hardware-verifiable build steps the requirements/design are organized around.
Each build step produces something the **operator validates on the physical
robot** before the next step starts (capture → live feed → detection → person
position → head tracking → triggered Routines). The safety-critical per-group
servo lock refactor and the cross-cutting Tracking_Mode / webapp / IR work are
sequenced where they unblock those steps.

Language: **Python** (the design is written in Python throughout; all modules
live under `src/`, run via `.venv/bin/python3`, debug with `print()`, async only
at the top of the call stack).

Testing follows the design's Correctness Properties (Hypothesis property tests
tagged `Feature: camera-person-detection-tracking, Property N`) plus unit /
integration tests, all hardware-free (fake/sim camera, `SERVO_SIM=1`, mocked
Edge TPU delegate).

### Safety note (steering — operator does hardware validation)

The operator validates physical servo travel limits, collisions, and
`FORBIDDEN_COMBINATIONS` on the real robot and can cut power. Therefore **no
task below runs a `SERVO_SIM` collision/limit verification** — none inspect
`CLAMPED` warnings, check commanded angles against `SAFE_LIMITS` ranges, or
simulate `FORBIDDEN_COMBINATIONS`. The in-code guards stay intact in every
implementation task: every servo write goes through `TrunkController.set_angle`
(SAFE_LIMITS clamp), modes recenter to `REST_POSITIONS` on wind-down/error, and
releasing a group rests only that group's channels. A single hardware-free
`SERVO_SIM=1` smoke run (runs clean, returns to rest) is a convenience only,
never framed as a safety/collision gate.

Lines marked **"OPERATOR (on hardware)"** are the human verification point
between build steps — they are NOT agent tasks and are listed only to mark the
established workflow boundary.

## Tasks

- [x] 1. Dependencies and data models (shared foundation)
  - [x] 1.1 Add verified vision dependencies to `requirements.txt` (bookworm / aarch64 / Python 3.11)
    - Target confirmed: Raspberry Pi 4B, Debian 12 bookworm, aarch64, Python
      3.11.2; project venv is 3.11.2. `numpy 2.3.4` is already pinned and
      satisfies the vision deps.
    - Detector runtime (DEFAULT): pin `tflite-runtime==2.14.0` — the only 3.11
      release, verified cp311/aarch64 wheel
      (`cp311-cp311-manylinux_2_34_aarch64`). Uses the classic
      `from tflite_runtime.interpreter import Interpreter` API the design
      references.
    - Detector runtime (ALTERNATIVE, future-proof): `ai-edge-litert==2.2.0` is
      the maintained LiteRT successor, also has a verified cp311/aarch64 wheel;
      API is `from ai_edge_litert.interpreter import Interpreter`. Pick ONE
      runtime, not both — it is a one-line import difference in `detector.py`.
      Document the chosen one in a comment in `requirements.txt`.
    - Overlay rendering: pin `opencv-python-headless==5.0.0.93` (headless, numpy
      2 compatible, verified cp311/aarch64 wheel) for server-side
      bounding-box/label overlay drawing. Lighter alternative: `Pillow==12.3.0`
      if the ~40 MB OpenCV wheel is undesirable — choose one.
    - picamera2 is NOT a pip dependency on bookworm: it is provided by apt as
      `python3-picamera2` (bound to system `libcamera`), already installed on
      this Pi (`python3-picamera2 0.3.31-1`, `python3-libcamera 0.5.2`,
      `rpicam-apps 1.9.0`). Do NOT add picamera2 to `requirements.txt`. Instead,
      document that Camera_Service must run against an interpreter that can
      import the apt-provided picamera2: either recreate the project venv with
      `--system-site-packages`, OR run Camera_Service with the system `python3`
      that already has `python3-picamera2`. Add this as a comment/note in
      `requirements.txt` and the project README/setup notes.
    - Edge TPU (Coral) is OPTIONAL and the shakiest on bookworm/3.11: `pycoral`
      has no official 3.11 aarch64 wheel and `libedgetpu` comes from Google's apt
      repo. Keep it OUT of the pinned requirements; the design already makes Edge
      TPU optional with CPU fallback, so ship CPU-only (`tflite-runtime` or
      `ai-edge-litert`) and treat Coral as a later, separately-documented add-on.
    - Add an inline note in `requirements.txt` that these vision pins are
      specific to bookworm/aarch64/py3.11 and must be re-verified if the Pi OS or
      Python version changes.
    - _Requirements: 3.1, 3.5, 1.1, 1.7, 9.6_
  - [x] 1.2 Create `src/vision_models.py` with the feature's data models
    - Define `Detection` (label, score, x1/y1/x2/y2; `is_person`, `area`,
      `center` properties), `Offset` (dx, dy, has_target), `DetectionRule`
      (required_classes, action, cooldown_s), and `TrackingConfig` (resolution,
      capture_fps, stream_fps, conf_threshold, deadband fracs, max_step_deg,
      scan_timeout_s, ir_mode, ir_ambient_threshold, use_edge_tpu).
    - Implement `TrackingConfig` field clamping on construction/read into each
      field's documented `[min, max]` range (resolution 320x240..1920x1080,
      capture_fps 5..30, stream_fps 1..30, conf 0.0..1.0, max_step 1..30,
      scan_timeout 1..120, cooldown 1..600).
    - `is_person` returns true iff label == `person`.
    - _Requirements: 1.3, 2.2, 3.2, 3.3, 3.7, 4.5, 5.6, 6.10, 7.8, 10.3_
  - [ ]* 1.3 Property test — config clamping
    - **Property 2: Config values are clamped into their valid range**
    - **Validates: Requirements 1.3, 2.2, 3.3, 5.6, 6.10, 7.8**
  - [ ]* 1.4 Property test — person classification flag
    - **Property 5: Person classification flag** (`is_person` iff label == `person`)
    - **Validates: Requirements 3.7**

- [x] 2. Per-channel-group servo lock refactor (safety-critical, unblocks Step 5/6)
  - [x] 2.1 Refactor `src/servo_lock.py` into per-group locks
    - Define `NECK_GROUP`/`ARM_GROUP` and `GROUP_CHANNELS` derived from
      `constants` (never hardcode channel ints): neck → `(NECK_PAN, NECK_TILT)`,
      arm → `(RT_ELBOW_ROTATOR, RT_ELBOW_TILT, RT_SHOULDER_TILT,
      RT_SHOULDER_ROTATOR)`.
    - Create one lockfile per group anchored in the user-owned `src/` dir with
      the existing world-rw `fchmod` treatment: `.servo.neck.lock`
      (`SERVO_NECK_LOCK_PATH`), `.servo.arm.lock` (`SERVO_ARM_LOCK_PATH`); keep
      `ServoBusyError` and `BUSY_EXIT_CODE = 3`.
    - Implement `group_lock(group, wait=False)` (non-waiting raises
      `ServoBusyError`; waiting blocks; writes holder PID) and
      `group_is_locked(group)` / `locks_status()` (non-destructive probes;
      fail-safe to not-held when the lockfile can't be opened).
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.8, 8.11, 8.13, 8.14, 8.15, 9.6_
  - [x] 2.2 Implement multi-group atomic acquisition and rest-on-release
    - `group_locks(*groups, wait=False)`: acquire in deterministic (sorted)
      order; all-or-none — on any partial failure release everything already
      taken and raise `ServoBusyError`.
    - On exit/error, drive ONLY each released group's `GROUP_CHANNELS` to
      `constants.REST_POSITIONS` via `TrunkController.set_angle` (never touch
      channels outside the released groups).
    - _Requirements: 8.6, 8.7_
  - [x] 2.3 Add backward-compatible whole-robot `servo_lock()`
    - Reimplement `servo_lock(wait=False)` as "acquire ALL groups" via
      `group_locks(NECK_GROUP, ARM_GROUP, ...)` so existing neck+arm routines in
      `animatronic.py` keep working unchanged and rest both groups on release.
    - _Requirements: 8.9_
  - [ ]* 2.4 Property test — same-group mutual exclusion
    - **Property 13: Same-group lock mutual exclusion** (second non-waiting
      acquire of a held group raises busy; real `fcntl.flock` across subprocesses)
    - **Validates: Requirements 8.11, 8.4, 8.2**
  - [ ]* 2.5 Property test — disjoint groups concurrent
    - **Property 14: Disjoint groups run concurrently**
    - **Validates: Requirements 8.12**
  - [ ]* 2.6 Property test — multi-group all-or-none
    - **Property 15: Multi-group acquisition is all-or-none** (no partial hold
      remains after a failed `group_locks` request)
    - **Validates: Requirements 8.6**
  - [ ]* 2.7 Property test — release rests only the released group's channels
    - **Property 16: Release rests only the released group's channels** (run the
      rest-on-release path under `SERVO_SIM=1`; assert written channel set ⊆ that
      group's `GROUP_CHANNELS` — this is a write-scoping check, NOT a SAFE_LIMITS
      range/collision check)
    - **Validates: Requirements 8.7**
  - [ ]* 2.8 Property test — status probe fail-safe
    - **Property 17: Status probe fail-safe** (`group_is_locked` returns False
      when the lockfile can't be opened)
    - **Validates: Requirements 8.15**
  - [ ]* 2.9 Compile check
    - `python -m py_compile src/servo_lock.py src/vision_models.py`.

- [x] 3. Checkpoint — lock + models
  - Ensure all tests pass, ask the user if questions arise.
  - OPERATOR (on hardware): confirm existing neck+arm routines still acquire the
    whole-robot lock and rest correctly (human verification, not an agent task).

- [x] 4. Build Step 1 — Camera capture (Camera_Service capture loop)
  - [x] 4.1 Create `src/camera_service.py` capture loop + latest-frame buffer
    - Non-root long-lived process (`.venv/bin/python3 src/camera_service.py`, no
      `sudo`). Configure a picamera2 video stream at the configured
      resolution/fps (defaults sustaining ≥5 fps); background thread
      continuously `capture_array()` into a single lock-guarded latest-frame slot
      (depth-1, newest-wins, no buffering beyond 1 frame).
    - Init failure within 10s: `print()` an init error naming the camera device
      and `sys.exit(non-zero)` so the Control_Panel keeps running. Capture
      failure after init: `print()` a capture error and retain the last good
      frame as the exposed frame.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 9.6_
  - [ ]* 4.2 Property test — latest-frame buffer newest-wins / depth 1
    - **Property 1: Latest-frame buffer is newest-wins, depth 1** (test the
      buffer abstraction directly with a sequence of pushed frames)
    - **Validates: Requirements 1.2, 1.4**
  - [ ]* 4.3 Unit test — camera init/capture error handling
    - With a fake/sim camera backend: init timeout → non-zero exit + named
      error; post-init capture failure → last good frame retained.
    - _Requirements: 1.5, 1.6_
  - [ ]* 4.4 Compile check
    - `python -m py_compile src/camera_service.py`.
  - OPERATOR (on hardware): run `camera_service.py` on the Pi, confirm camera
    initializes and sustains ≥5 fps (human verification, not an agent task).

- [x] 5. Build Step 2 — Live feed in the control panel
  - [x] 5.1 Add Camera_Service HTTP interface (`/stream`, `/status`)
    - localhost-only Flask app on `:8001` (loopback bind; frames/detections
      never leave the device). `GET /stream` serves `multipart/x-mixed-replace`
      MJPEG of latest frames, throttled to a configurable 1–30 fps independent of
      capture fps, with server-side overlay when `?overlay=1`. `GET /status`
      returns `{camera_ok, capturing, fps, last_frame_age_s, edge_tpu, ir}`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 9.7_
  - [x] 5.2 Add read-only camera routes to `src/webapp.py`
    - Using the existing `_proxy` (urllib) pattern: `GET /camera/stream`,
      `GET /camera/detections`, `GET /camera/status`. Camera routes are strictly
      read-only — they issue NO servo command. If Camera_Service is unreachable,
      `_proxy` returns 502 and the panel shows "camera unavailable" while all
      other controls stay usable.
    - _Requirements: 2.1, 2.5, 2.7_
  - [x] 5.3 Add Live_Feed view + status indicators to the control-panel UI
    - Add an `<img src="/camera/stream">` Live_Feed with overlay toggle; poll
      `/camera/status` to render a "camera unavailable" indicator (no broken
      stream) and a "stalled feed" indicator within 5s (via `last_frame_age_s`).
      Overlay enabled with no detections → feed shows no boxes.
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.6_
  - [ ]* 5.4 Unit test — camera routes read-only + unavailable/stalled status
    - Assert camera routes issue no servo command; unreachable Camera_Service →
      "camera unavailable" and other controls usable; stale `last_frame_age_s` →
      stalled indicator.
    - _Requirements: 2.5, 2.6, 2.7_
  - [ ]* 5.5 Compile check
    - `python -m py_compile src/camera_service.py src/webapp.py`.
  - OPERATOR (on hardware): open the panel, confirm Live_Feed appears within ~2s
    and the status indicators behave (human verification, not an agent task).

- [x] 6. Build Step 3 — Person and object detection
  - [x] 6.1 Create `src/detector.py` (TFLite COCO SSD-MobileNet)
    - `Detector(model_path, labels_path, conf_threshold=0.5, use_edge_tpu=False)`
      with `detect(frame) -> list[Detection]`. Normalize each raw model output to
      a score in `[0.0, 1.0]` and a pixel bbox bounded by frame dimensions;
      filter to detections with `score >= conf_threshold`; flag `person`-label
      detections `is_person`.
    - _Requirements: 3.1, 3.2, 3.3, 3.7_
  - [x] 6.2 Implement Edge TPU delegate with CPU fallback
    - When `use_edge_tpu` is set, try the `libedgetpu` delegate + `*_edgetpu`
      model; on delegate-missing/device-absent (`ValueError`/`OSError`) fall back
      to a CPU `Interpreter` and `print()` a clear "Edge TPU unavailable → CPU
      fallback" indication.
    - _Requirements: 3.5, 3.6_
  - [x] 6.3 Wire Detector into Camera_Service `/detections`
    - Run the Detector over captured frames and expose
      `GET /detections` → `{frame_id, width, height, ts, detections:[...]}` for
      the Control_Panel overlay and Tracking_Mode. Report `edge_tpu` state in
      `/status`.
    - _Requirements: 3.4, 3.6_
  - [ ]* 6.4 Property test — detection normalization
    - **Property 3: Detections are normalized to valid score and in-frame bbox**
    - **Validates: Requirements 3.2**
  - [ ]* 6.5 Property test — confidence filtering is exact
    - **Property 4: Confidence threshold filtering is exact** (reported ==
      exactly those with score ≥ threshold)
    - **Validates: Requirements 3.3**
  - [ ]* 6.6 Unit test — Edge TPU → CPU fallback indication
    - With a mocked `load_delegate` raising, assert fallback uses the CPU
      interpreter and prints the fallback indication.
    - _Requirements: 3.5, 3.6_
  - [ ]* 6.7 Compile check
    - `python -m py_compile src/detector.py src/camera_service.py`.
  - OPERATOR (on hardware): confirm people/objects are detected and overlaid on
    the panel (human verification, not an agent task).

- [x] 7. Build Step 4 — Person position detection (Tracking_Controller pure math)
  - [x] 7.1 Create `src/tracking_controller.py` selection + offset
    - `select_target(detections, w, h)`: largest bbox area, tie-break
      closest-to-center, deterministic for identical input.
    - `compute_offset(target, w, h, cfg)`: signed pixel Offset of bbox center vs
      Frame_Center (+x right, +y below); zero both axes inside the deadband
      half-width; no target → `Offset(0,0,has_target=False)`.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [ ]* 7.2 Property test — target selection
    - **Property 6: Target selection is largest-area with deterministic
      center tie-break**
    - **Validates: Requirements 4.1, 4.2**
  - [ ]* 7.3 Property test — offset sign convention
    - **Property 7: Offset sign convention** (+x iff right of center, +y iff
      below center)
    - **Validates: Requirements 4.3**
  - [ ]* 7.4 Property test — deadband zeroing
    - **Property 8: Deadband zeroing**
    - **Validates: Requirements 4.4**
  - [ ]* 7.5 Compile check
    - `python -m py_compile src/tracking_controller.py`.

- [x] 8. Build Step 5 — Head tracking (neck command mapping + smoothing)
  - [x] 8.1 Implement `next_neck_targets` direction mapping + smoothing
    - Map Offset sign to pan/tilt steps honoring `constants.py` axis directions:
      person LEFT (dx<0) → INCREASE `NECK_PAN`; RIGHT (dx>0) → DECREASE
      `NECK_PAN`; BELOW (dy>0) → INCREASE `NECK_TILT`; ABOVE (dy<0) → DECREASE
      `NECK_TILT`. Cap each per-update step magnitude at `cfg.max_step_deg`.
      Return `{}` when there is no target (caller issues no command, holds angle).
      Returned channel set is a subset of `{NECK_PAN, NECK_TILT}` (0,1) only.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.8, 5.9_
  - [ ]* 8.2 Property test — neck command reduces the offset (correct direction)
    - **Property 9: Neck command reduces the offset (correct direction)** per the
      `constants.py` axis conventions (direction logic only — NOT a SAFE_LIMITS
      range/collision check)
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
  - [ ]* 8.3 Property test — smoothing caps the per-update step
    - **Property 10: Smoothing caps the per-update step**
    - **Validates: Requirements 5.6**
  - [ ]* 8.4 Property test — tracking writes only Neck_Group channels
    - **Property 11: Tracking writes only Neck_Group channels** (commanded
      channel set ⊆ {0,1})
    - **Validates: Requirements 5.8, 8.3**
  - [ ]* 8.5 Property test — no target yields no command
    - **Property 12: No target yields no command** (empty target map; neck holds)
    - **Validates: Requirements 4.5, 5.9**
  - [ ]* 8.6 Compile check
    - `python -m py_compile src/tracking_controller.py`.

- [x] 9. Checkpoint — perception + tracking math
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Tracking_Mode — `tracking` action in `animatronic.py` (Req 6)
  - [x] 10.1 Add a camera-service client + `Animatronic.tracking(...)` loop
    - Add a thin HTTP client to GET `/detections` from Camera_Service
      (`--camera-url` default `http://localhost:8001`). Implement
      `Animatronic.tracking(...)` mirroring `napping`/`awake`: clear `nap_signal`
      at start; acquire ONLY the `Neck_Group` lock (`group_lock(NECK_GROUP)`) so
      an arm Gesture can coexist; loop `GET /detections` → `select_target` →
      `compute_offset` → `next_neck_targets` → `TrunkController.set_angle` on
      channels 0,1 within 500 ms of a new detection; carry no audio and never
      drive the jaw motor; run the loop in `asyncio.run(...)` at the top of the
      call stack with NO watchdog.
    - On `nap_signal.stop_requested()` (or any loop error): recenter the
      Neck_Group to `REST_POSITIONS`, release the lock, clear `nap_signal`, exit
      within 1s.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.11, 5.5, 5.7, 5.8_
  - [x] 10.2 Implement leave-frame Scan_Sweep with configurable timeout
    - When no `person` detection is present, run a slow pan across the `NECK_PAN`
      safe range (building on `TrunkController.slow_scan`, honoring the
      configurable scan range), every command through `set_angle`. If a person
      reappears mid-sweep, stop and resume tracking that person. If the scan
      timeout elapses with no reacquire, recenter to `REST_POSITIONS` and yield
      to the previously active Mode.
    - _Requirements: 6.7, 6.8, 6.9, 6.10_
  - [x] 10.3 Register `tracking` in `action_map` + CLI flags
    - Register `tracking` in `action_map` (camelCase key → snake_case method);
      add CLI flags mirroring `--nap-timeout`: `--scan-timeout` (default 10,
      1–120), `--max-step`, `--deadband`, `--conf`, `--camera-url`. Add a
      gesture-only note / entry in `src/controller.py` where relevant for
      hardware testing. Keep `main()` dispatch allowlist-only (never
      `getattr`/`eval`/shell on `args.action`).
    - _Requirements: 6.1, 6.10, 9.3_
  - [ ]* 10.4 Integration test — Tracking_Mode loop against a fake camera (SERVO_SIM)
    - Stub Camera_Service serving canned `/detections`; run the mode under
      `SERVO_SIM=1`; assert it tracks, winds down on `nap_signal` within 1s,
      recenters, and releases the Neck_Group. (Flow/wiring only — a clean smoke
      run that returns to rest; NOT a CLAMPED/SAFE_LIMITS/collision check.)
    - _Requirements: 6.1, 6.4, 6.5, 6.7, 6.10_
  - [ ]* 10.5 Compile check
    - `python -m py_compile src/animatronic.py src/controller.py`.
  - OPERATOR (on hardware): confirm the head tracks a person and Scan_Sweep
    reacquires/recenters safely (human verification, not an agent task).

- [x] 11. webapp Tracking Mode route + path validation (Req 9)
  - [x] 11.1 Add `TRACKING_ACTIONS` allowlist + `/tracking/<state>` route
    - Add `TRACKING_ACTIONS = {'tracking'}`; `POST /tracking/<state>`:
      `start` → `launch_tracking(...)` under `_launch_lock`, tracked in
      `_active_proc` with label `tracking`, NO watchdog (open-ended Mode);
      `stop` → `nap_signal.request_stop()`. `launch_tracking` mirrors
      `launch_napping`/`launch_awake` (check-and-spawn, fixed `ANIMATRONIC` path
      + validated `--action=tracking` as separate argv, never shell) but does
      NOT auto-stop the mic (Gesture-like toward the Stream). Add `'tracking'`
      to `_MODE_LABELS` so `_preempt_mode_if_running` treats it as preemptible.
    - _Requirements: 6.3, 6.5, 9.1, 9.2, 9.3_
  - [x] 11.2 Add path-value validation for model/config-name routes
    - Any route accepting a value that becomes a filesystem path (selectable
      detector `model`, tuning config name, `POST /camera/ir` mode) validates it
      against an explicit allowlist OR confirms `os.path.realpath` stays within
      the designated base dir (`MODELS_DIR`). On failure: reject, read/write
      nothing, return an "invalid value" response.
    - _Requirements: 9.4, 9.5, 10.3_
  - [ ]* 11.3 Property test — allowlist gates all dispatch
    - **Property 18: Allowlist gates all dispatch** (dispatch iff name in
      allowlist)
    - **Validates: Requirements 7.6, 9.1, 9.2**
  - [ ]* 11.4 Property test — path values stay within the base directory
    - **Property 19: Path values stay within the designated base directory**
      (traversal/out-of-base rejected)
    - **Validates: Requirements 9.4, 9.5**
  - [ ]* 11.5 Unit test — tracking route + preemption wiring
    - Unknown tracking action → 400, no subprocess; `tracking` treated as a Mode
      by `_preempt_mode_if_running`; `launch_tracking` does not auto-stop the mic.
    - _Requirements: 6.3, 6.5, 9.1, 9.2_
  - [ ]* 11.6 Compile check
    - `python -m py_compile src/webapp.py`.

- [x] 12. Build Step 6 — Detection-triggered Routines (Detection_Routine_Map)
  - [x] 12.1 Create `src/detection_routine_map.py` with arbitration + cooldown
    - Configurable map of `DetectionRule` entries, each mapping one detection
      condition to one Routine action name that must exist in `action_map`.
      Seed defaults `person → wave` and `person+dog → walkYourDog`. Arbitration:
      when multiple conditions match, pick the one matching the greatest number
      of detected classes; ties → earliest-defined entry. Cooldown: after a
      condition fires, block that condition→Routine pair until a configurable
      1–600s (default 30s) cooldown elapses since the Routine completed.
    - _Requirements: 7.1, 7.3, 7.4, 7.5, 7.8_
  - [x] 12.2 Wire triggering into Tracking_Mode with allowlist dispatch + yield
    - From the Tracking_Mode loop (it already reads `/detections`), evaluate the
      map; dispatch the mapped action ONLY if present in `action_map` (reject
      unknown names, run no subprocess/servo command, `print()` the rejected
      name; never `getattr`/`eval`/shell). Before a triggered Routine drives
      jaw/audio, wind down Tracking_Mode and release the Neck_Group so the
      Routine and Tracking_Mode never own the Neck_Group simultaneously.
    - _Requirements: 7.2, 7.6, 7.7, 9.1, 9.2, 9.3_
  - [ ]* 12.3 Property test — detection→Routine arbitration
    - **Property 20: Detection→Routine arbitration is most-specific,
      earliest-on-tie**
    - **Validates: Requirements 7.5**
  - [ ]* 12.4 Property test — cooldown blocks re-fire
    - **Property 21: Cooldown blocks re-fire within the window**
    - **Validates: Requirements 7.8**
  - [ ]* 12.5 Unit test — default entries + unknown-action rejection
    - Default `person→wave` / `person+dog→walkYourDog` present; a rule mapping to
      an action absent from `action_map` is rejected with no dispatch and the
      rejected name reported.
    - _Requirements: 7.3, 7.4, 7.6_
  - [ ]* 12.6 Compile check
    - `python -m py_compile src/detection_routine_map.py src/animatronic.py`.
  - OPERATOR (on hardware): confirm a person triggers the wave Routine and
    person+dog triggers walk-your-dog, with Tracking yielding first (human
    verification, not an agent task).

- [x] 13. IR night operation inside Camera_Service (Req 10)
  - [x] 13.1 Implement IR control with auto-switch hysteresis + graceful degrade
    - IR owned by the non-root Camera_Service. Modes `on`/`off`/`auto`;
      `POST /ir` sets mode (validated against a fixed set). `auto`: enable IR
      when ambient light (frame-luminance mean of a downscaled frame, or a light
      sensor if present) falls below the low threshold, disable when it rises
      above the high threshold (hysteresis). If IR hardware is absent/fails,
      keep operating in available light and report "IR unavailable". Report IR
      state changes to the Control_Panel via `/status`; proxy `POST /camera/ir`
      in `webapp.py`.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_
  - [ ]* 13.2 Property test — IR auto-switch hysteresis
    - **Property 22: IR auto-switch follows the ambient hysteresis rule**
      (deterministic enable-below / disable-above for identical input)
    - **Validates: Requirements 10.4**
  - [ ]* 13.3 Unit test — IR mode validation + graceful degrade
    - Invalid IR mode rejected; absent/failing IR hardware → "IR unavailable" in
      `/status`; state changes reported.
    - _Requirements: 10.3, 10.5, 10.6_
  - [ ]* 13.4 Compile check
    - `python -m py_compile src/camera_service.py src/webapp.py`.

- [x] 14. Final checkpoint — full feature wiring
  - Ensure all tests pass, ask the user if questions arise.
  - Confirm Sleep/Awake modes can use person Detections as the interrupting
    sensor signal (Req 6.12) is wired where those modes consume a sensor source.
  - _Requirements: 6.12_

## Notes

- Tasks marked with `*` are optional (tests + compile checks) and can be skipped
  for a faster MVP; core implementation tasks are never optional.
- Each task references specific requirement sub-clauses and, where it implements
  a design Correctness Property, names that property so its test is written with
  the task.
- Property tests use Hypothesis (already a project dependency), ≥100 iterations
  each, tagged `Feature: camera-person-detection-tracking, Property N`.
- All tests are hardware-free: fake/sim camera, `SERVO_SIM=1`, mocked Edge TPU
  delegate; lock semantics tested via real `fcntl.flock` across subprocesses.
- **No task performs `SERVO_SIM` collision/limit verification.** In-code guards
  (every write through `set_angle`, recenter to `REST_POSITIONS` on
  wind-down/error, per-group rest-on-release) are kept intact in the
  implementation tasks. Physical travel/collision validation is the operator's
  on-hardware step between build steps.
- Wiring conventions (steering): new Mode registered in `action_map` with a
  camelCase key; `controller.py` for gesture-only CLI where relevant; webapp
  allowlists (`TRACKING_ACTIONS`, `action_map`); snake_case; `print()` debug;
  `asyncio.run` only at the top of the call stack; pinned deps.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.2", "4.1"] },
    { "id": 2, "tasks": ["2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9", "4.2", "4.3", "4.4"] },
    { "id": 3, "tasks": ["5.1", "6.1", "7.1"] },
    { "id": 4, "tasks": ["5.2", "6.2", "7.2", "7.3", "7.4", "7.5"] },
    { "id": 5, "tasks": ["5.3", "6.3", "8.1"] },
    { "id": 6, "tasks": ["5.4", "5.5", "6.4", "6.5", "6.6", "6.7", "8.2", "8.3", "8.4", "8.5", "8.6"] },
    { "id": 7, "tasks": ["10.1"] },
    { "id": 8, "tasks": ["10.2", "10.3"] },
    { "id": 9, "tasks": ["10.4", "10.5", "11.1", "13.1"] },
    { "id": 10, "tasks": ["11.2", "11.3", "11.4", "11.5", "11.6", "13.2", "13.3", "13.4"] },
    { "id": 11, "tasks": ["12.1"] },
    { "id": 12, "tasks": ["12.2"] },
    { "id": 13, "tasks": ["12.3", "12.4", "12.5", "12.6"] }
  ]
}
```
