# animatronic-v2

A Raspberry Pi–based animatronic controller that synchronises servo-driven
physical gestures with audio playback via PyAudio (`AudioPlayer` /
`AudioStreamer`), with an optional camera vision pipeline (live feed, on-device
object/person detection, head tracking) and an ultrasonic range sensor for
presence/approach wake triggers.

---

## Table of contents

- [Vocabulary](#vocabulary)
- [Hardware](#hardware)
  - [Servo channel assignments](#servo-channel-assignments)
  - [GPIO pins (not PCA9685 channels)](#gpio-pins-not-pca9685-channels)
- [Project structure](#project-structure)
  - [Layer overview](#layer-overview)
- [Dependencies](#dependencies)
- [Development setup](#development-setup)
- [Running](#running)
  - [Routines (gesture + audio)](#routines-gesture--audio)
  - [Movements (gesture only, no audio)](#movements-gesture-only-no-audio)
  - [Modes (continuous background behaviours)](#modes-continuous-background-behaviours)
  - [Audio files](#audio-files)
- [Kinematic collision model (offline authoring aid)](#kinematic-collision-model-offline-authoring-aid)
- [Hardware troubleshooting](#hardware-troubleshooting)
- [ALSA audio configuration](#alsa-audio-configuration)
- [Web control panel](#web-control-panel)
  - [Busy interlock (safety)](#busy-interlock-safety)
  - [Dev auto-reload](#dev-auto-reload)
- [Voice tuning and effects](#voice-tuning-and-effects)
- [Camera vision](#camera-vision)
- [Range sensor](#range-sensor)
- [Adding a new routine](#adding-a-new-routine)

---

## Vocabulary

The motion/performance layers build on one another. These terms have precise
meanings throughout the code and docs (full definitions in
`.kiro/steering/animation-vocabulary.md`):

| Term | Audio? | Implemented by | Interrupts live mic? |
|------|--------|----------------|----------------------|
| **Gesture** | No | a `Movements` coroutine (`controller.py --action=…`) | No |
| **Gestures** | No | a chained sequence of Gestures | No |
| **Routine** | Yes | an `Animatronic` action (`animatronic.py --action=…`) | Yes |
| **Act** | Yes | several Routines composed into one performance | Yes |
| **Stream** | Yes (mic) | `AudioStreamer` / `micwebcontroller.py` | — (is the stream) |
| **Mode** | varies | a continuous background loop (mic / sleep / awake / tracking / scan / puppeteer) | varies |

A Gesture never touches the jaw/audio path, so Gestures layer safely over a
live mic stream. A Routine, Act, or audio-driven Mode owns the jaw motor and so
interrupts the stream.

---

## Hardware

| Component | Details |
|-----------|---------|
| Controller | Raspberry Pi (any model with I2C) |
| Servo driver | Adafruit 16-channel PCA9685 PWM board |
| Servo channels | 16-channel, all configured for **270° actuation range** |
| Audio output | USB audio device (`sysdefault:CARD=Device`, index 2) |
| Mic input | USB audio device (`sysdefault:CARD=Device_1`, index 1) |
| Camera | Raspberry Pi Camera Module 3 NoIR (imx708) — optional |
| Range sensor | HC-SR04 ultrasonic (ECHO via a 5V→3.3V divider) — optional |

### Servo channel assignments

Defined in `src/constants.py`. Angles are clamped on every write to the
per-channel `SAFE_LIMITS` (the mechanism's safe range, not the 0–270 electrical
range).

| Channel | Constant | Joint | SAFE_LIMITS | Rest |
|---------|----------|-------|-------------|------|
| 0 | `NECK_PAN` | Left/right head rotation (90 = forward) | 5–175 | 90 |
| 1 | `NECK_TILT` | Up/down head tilt (90 = level, higher = chin down) | 30–160 | 90 |
| 3 | `RT_WRIST_TILT` | Wrist bend (90 = inline with forearm) | 10–230 | 90 |
| 4 | `RT_ELBOW_ROTATOR` | Forearm twist (150 = palm to side, 270 = palm up) | 0–270 | 150 |
| 5 | `RT_ELBOW_TILT` | Elbow bend (5 = straight) | 0–160 | 5 |
| 6 | `RT_SHOULDER_TILT` | Shoulder raise/lower (abduction; 135 = straight out) | 45–270 | 55 |
| 7 | `RT_SHOULDER_ROTATOR` | Raise/lower whole arm (0 = at side, 270 = ~170° up) | 0–270 | 0 |

> Arm gestures own channels 4–7; head gestures own channels 0–1. Any arm
> gesture may run concurrently with any head gesture (disjoint channels). Per-
> axis limits do **not** catch multi-axis collisions — `FORBIDDEN_COMBINATIONS`
> in `constants.py` hard-guards the measured hand-to-face danger zone, and the
> offline kinematic model (below) predicts the rest.

### GPIO pins (not PCA9685 channels)

Driven via `gpiozero`, separate from the servo driver:

| Pin | Constant | Device |
|-----|----------|--------|
| 6 | `EYE_LIGHT_PIN` | Eye LED |
| 15 | `MOUTH_MOTOR_PIN` | Jaw motor (pulsed from audio amplitude) |
| 12 | `IR_ILLUMINATOR_PIN` | IR illuminator for NoIR night vision |
| 23 | `RANGE_TRIG_PIN` | HC-SR04 trigger (output) |
| 24 | `RANGE_ECHO_PIN` | HC-SR04 echo (input, via voltage divider) |

---

## Project structure

```
animatronic-v2/
├── src/
│   ├── constants.py            # Channels, SAFE_LIMITS, REST_POSITIONS, FORBIDDEN_COMBINATIONS, GPIO pins
│   ├── trunkcontroller.py      # Low-level async servo primitives (clamping, move_to, return_to_rest)
│   ├── movements.py            # High-level async gesture choreography
│   ├── performance.py          # Reusable runner for audio-synced concurrent gestures
│   ├── positions.py            # Shared pose/position helpers
│   ├── animatronic.py          # Named routines + Modes (napping/awake/tracking/scan); CLI entry point
│   ├── controller.py           # CLI entry point for gesture-only testing
│   ├── audio_player.py         # WAV/MP3 file player with jaw-motor + eye-LED sync
│   ├── audio_streamer.py       # Live mic passthrough + voice effects + jaw sync
│   ├── config_store.py         # Shared tuning config (jaw profiles, effect presets)
│   ├── servo_lock.py           # Cross-process servo mutex (fcntl); whole-robot + Neck_Group locks
│   ├── nap_signal.py           # Cross-process stop signal that preempts a running Mode
│   ├── range_sensor.py         # HC-SR04 helper + ApproachDetector (presence / approach wake)
│   ├── range_publish.py        # Publishes range readings for the dashboard gauge
│   ├── camera_service.py       # Non-root process owning the camera + detector (loopback :8001)
│   ├── detector.py             # TFLite COCO SSD-MobileNet object detector
│   ├── vision_models.py        # Detection / DetectionRule / TrackingConfig data models
│   ├── detection_routine_map.py# Detection → Routine arbitration with per-rule cooldown
│   ├── tracking_controller.py  # Pure neck-tracking math (target select, offset, next targets)
│   ├── calibrate.py            # Interactive single-servo limit finder
│   ├── calibrate_joints.py     # Interactive kinematic-model calibration harness
│   ├── probe_collision.py      # Supervised collision-boundary probe (model vs. hardware)
│   ├── eyetest.py / jawtest.py # Standalone LED / jaw-motor hardware tests
│   ├── micwebcontroller.py     # Flask: mic stream + jaw tuning + voice FX (port 5000)
│   ├── webapp.py               # Flask control panel UI (port 8000)
│   ├── kinematics/             # Hardware-free 3D self-collision model + CLI
│   ├── model/  utils/  action/ # Supporting packages
│   ├── templates/index.html    # Control-panel UI served by webapp.py
│   └── config/
│       ├── tuning.json         # Jaw/effect tuning profiles (version-controlled)
│       ├── calibration.json    # Per-joint kinematic calibration (provisional seeds)
│       └── alsa/               # ALSA sound-card configuration
├── tests/                      # pytest suite (hypothesis property + unit tests)
├── audio/                      # WAV/MP3 files — the source of truth, resolved directly at runtime
├── models/                     # Camera detector: coco_labels.txt (tracked) + *.tflite (gitignored)
├── deploy/                     # Deployment assets (systemd unit for Camera_Service)
├── .venv/                      # Python virtualenv (repo root, built --system-site-packages)
├── requirements.txt
└── README.md
```

### Layer overview

```
animatronic.py / controller.py   ← you call these (routines + Modes / gesture-only)
        │
        ├── performance.py        ← audio-synced concurrent gesture runner
        ▼
    movements.py                  ← gesture choreography (async)
        │
        ▼
  trunkcontroller.py              ← servo primitives (async, adafruit_servokit)
        │
        ▼
  PCA9685 PWM board → servos
```

The camera pipeline (`camera_service.py` → `detector.py` →
`tracking_controller.py` / `detection_routine_map.py`) runs as a separate
non-root process and feeds Tracking/Scan Modes and the control panel; it never
drives servos itself.

---

## Dependencies

### Python packages

Install from the pinned requirements file:

```bash
pip install -r requirements.txt
```

Key packages:

| Package | Version | Purpose |
|---------|---------|---------|
| `adafruit-circuitpython-servokit` | 1.3.22 | PCA9685 servo driver ([docs](https://docs.circuitpython.org/projects/servokit/en/latest/)) |
| `Adafruit-Blinka` | 8.66.2 | CircuitPython hardware abstraction for Linux ([docs](https://learn.adafruit.com/circuitpython-on-raspberrypi-linux)) |
| `RPi.GPIO` | 0.7.1 | Raspberry Pi GPIO access |
| `numpy` | >=1.24,<2 | Amplitude analysis for jaw sync; pinned `<2` so the venv's numpy matches the apt `python3-picamera2` C-extension ABI (see [Camera vision](#camera-vision)) |
| `PyAudio` | 0.2.14 | Audio file playback and mic streaming |
| `tflite-runtime` | 2.14.0 | TFLite inference for the camera object detector |
| `opencv-python-headless` | 4.11.0.86 | JPEG encode + overlay drawing for the camera feed (4.x for numpy-1.x compat) |
| `picamera2` | apt (`python3-picamera2`) | Camera capture — NOT pip; provided by the OS, hence the venv is built `--system-site-packages` |

---

## Development setup

```bash
# Clone and create a virtual environment
git clone <repo-url> animatronic-v2
cd animatronic-v2
python3 -m venv --system-site-packages .venv   # system packages for picamera2
source .venv/bin/activate

# Install pinned dependencies
pip install -r requirements.txt
```

> Most runtime commands require `sudo` so Python can access the I2C bus and
> GPIO pins. The venv must still be active (or the full venv Python path used)
> when running with sudo.

---

## Running

Run everything from the repo root (the venv lives at the repo root). Prefix any
command with `SERVO_SIM=1` for a hardware-free dry run that logs every servo
write instead of touching I2C.

### Routines (gesture + audio)

```bash
sudo .venv/bin/python src/animatronic.py --action=<action>
```

Routine actions (the dispatch allowlist is `Animatronic.build_action_map()`;
only these names are dispatchable — never `getattr`/`eval` on raw input):

| Action | Description |
|--------|-------------|
| `startParty` | Wave + swivel head |
| `niceDay` | Wave hello, "nice day for a walk" (arm leads the audio) |
| `krusty` | Neck ellipse + Krusty laugh |
| `blah` | Concurrent head-shake + palm-present (Performance Framework) |
| `vincentPrice` | Smooth reach + flowing head look-around + laugh |
| `yawn` | Cover-mouth gesture, jaw synced to the yawn |
| `snuckUp` | Head jerk + arm recoil + "snuck up" gasp |
| `awaken` | Groggy stir + lazy head bob |
| `sneeze` | Cover-mouth held through the sneeze, head snap 5s in |
| `brains` | Menacing reach + head scan, audio-synced |
| `hypnotic` | Arm/head sway, jaw off, eyes blink; two-track audio |
| `clearThroat` | Hand-to-mouth throat clear (gated until near-settle) |
| `coughLong` / `coughMedium` | Cover-mouth cough (gated until the hand settles) |
| `maximus` | Head-focus ×3, audio gated until the head settles |
| `burp` | Cover-mouth burp + "excuse me" follow-on (sound leads, hand follows) |
| `fart` | Fart (pure audio) then a cover-mouth "excuse me" |
| `fartGhost` | Gated reaction after the fan-nose gesture arrives |
| `moreCandy` | Jittery sugar-rush shakes (four concurrent joints) |
| `comeGetCandy` | Beckon/come-here + candy call |
| `sleep` | Snore performance |

### Movements (gesture only, no audio)

```bash
sudo .venv/bin/python src/controller.py --action=<action>
```

Gesture actions (from `controller.py`'s `action_map`):

- **Arm (channels 4–7):** `wave`, `beckon`, `comeHere`, `menacingReach`,
  `yawnCover`, `facePalm`, `fanButt`, `fanNose`, `tapSide`, `talkingWithHands`
- **Head (channels 0–1):** `yes`, `lookAroundSmall`, `lookAroundRandom`,
  `neckEllipse`, `swivelHead`, `shakeHead`, `snapHead`, `smno`, `snuckUp`,
  `awaken`, `headFocus`
- **Composite (arm + head):** `waveAndSwivelSmooth`, `handVisor`

### Modes (continuous background behaviours)

A Mode runs continuously until interrupted (a timeout, a sensor, or an external
stop request from the web control panel via `nap_signal`). They are launched
through `animatronic.py` with dedicated CLI branches so each acquires the right
lock.

| Action | Mode | Lock held | Interrupted by |
|--------|------|-----------|----------------|
| `napping` | Sleep — resting/idle until roused | whole-robot `servo_lock()` | `--nap-timeout-min` (0–120, 0 = no timeout), sensor, web stop |
| `awake` | Awake — performs ambient Routines on a loop | whole-robot `servo_lock()` | `--awake-timeout-min` (0–120, 0 = no timeout), sensor, web stop |
| `tracking` | Head tracking (neck follows a detected person) | **Neck_Group only** (arm gestures may run concurrently) | Scan_Sweep timeout, web stop |
| `scan` | Neck tracker + concurrent arm-only responder | whole-robot `servo_lock()` | `--scan-timeout-min` (0–120, 0 = no timeout), web stop |
| `puppeteer` | Live mic Stream + neck-only tracking + operator arm Gestures (triggers suppressed) | **Neck_Group only** (arm gestures may run concurrently) | web stop, mode preemption |
| `mic` | Live mic passthrough (audio only) | **no lock** (does not move servos) | Enter key / web stop |

```bash
# Sleep mode with a 10-minute timeout wake (0 = no timeout, stop manually)
sudo .venv/bin/python src/animatronic.py --action=napping --nap-timeout-min 10

# Awake mode for 20 minutes (0 = no timeout, stop manually)
sudo .venv/bin/python src/animatronic.py --action=awake --awake-timeout-min 20

# Head tracking (needs Camera_Service running — see Camera vision)
sudo .venv/bin/python src/animatronic.py --action=tracking \
    --camera-url http://localhost:8001 --conf 0.5 --deadband 0.05

# Scan mode, winding down after 30 minutes
sudo .venv/bin/python src/animatronic.py --action=scan --scan-timeout-min 30

# Puppeteer mode (live-mic performance): neck-only tracking + operator arm
# gestures. The web control panel starts the mic Stream with it; from the CLI
# it tracks the neck only (triggers suppressed), leaving the mic to the web app.
sudo .venv/bin/python src/animatronic.py --action=puppeteer \
    --camera-url http://localhost:8001
```

Tracking/Scan/Puppeteer accept extra tuning flags: `--max-step`, `--deadband`, `--conf`,
`--scan-timeout`, `--aim-frac`, `--tilt-center`, `--tilt-min`, `--tilt-max`,
`--settle-gain`, `--camera-url`.

### Audio files

Audio lives in the repo's own `audio/` directory — the version-controlled
source of truth. `Animatronic._resolve_audio_dir()` resolves it relative to the
module (`<repo>/audio`), so it is identical regardless of the invoking user
(sudo/pi/aaron) and there is no `~/Music/` deploy step. Override with the
`ANIMATRONIC_AUDIO_DIR` environment variable.

---

## Kinematic collision model (offline authoring aid)

`src/kinematics/` is a hardware-free 3D model that predicts self-collisions for
a pose *before* you drive it to the servos. It runs anywhere (no Pi, no
hardware libraries) and is meant for authoring gestures safely.

A pose is a JSON object mapping servo channel to degrees, e.g. the rest pose
`{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}`.

### Check a pose (text verdict)

```bash
PYTHONPATH=src .venv/bin/python -m kinematics.cli \
  --pose '{"0":90,"1":90,"4":150,"5":145,"6":55,"7":0}'
```

Prints `SAFE`, or `COLLISION: <link_a> <-> <link_b> (joints: ...)` per pose.
Exit code is non-zero if any pose collides. Use `--sequence poses.json` for a
list of poses, and `--margin 0.005` to override the safety inflation (meters).

### View the 3D model

Interactive window (needs a display + a viewer backend such as `pyglet`):

```bash
PYTHONPATH=src .venv/bin/python -m kinematics.cli --pose '{...}' --preview
```

Headless (Raspberry Pi over SSH): there is no display, so render to an image
file instead. This uses matplotlib's offscreen `Agg` backend and needs no
display, X-forwarding, or `pyglet`:

```bash
PYTHONPATH=src .venv/bin/python -m kinematics.cli \
  --pose '{"0":90,"1":90,"4":150,"5":145,"6":55,"7":0}' \
  --preview-out preview.png
```

Then open `preview.png`. Colliding links are drawn red, the rest of the robot
gray. By default the image is a 4-view panel (front, side, top, and a 3/4
perspective) with the verdict and offending joints in the title, so a collision
reads clearly without an interactive window. Add `--single-view` for one 3/4
view. For a sequence, `preview.png` becomes `preview_1.png`, `preview_2.png`,
etc.

The interactive `--preview` window uses trimesh's viewer when `pyglet` is
installed, otherwise a matplotlib 3D window; both draw the full gray body plus
red collision highlights.

> Note: all per-joint calibration values in `src/config/calibration.json` are
> provisional seeds pending hardware validation (see below).

### Calibrating the model against the robot

The model's predictions are only as good as `src/config/calibration.json`,
whose `sign` / `offset_deg` / `scale` per joint start as **unverified seeds**.
`calibrate_joints.py` is an interactive harness that confirms and corrects them
by driving one joint at a time and asking what you observed, then writing the
results back into the JSON (with a timestamped backup).

Always dry-run first (logs every angle, moves nothing):

```bash
SERVO_SIM=1 PYTHONPATH=src .venv/bin/python -m calibrate_joints --dry-run
```

Then calibrate on hardware (root for GPIO/I2C). It moves one joint at a time,
clamped to `SAFE_LIMITS`, and parks servos on exit:

```bash
sudo PYTHONPATH=src .venv/bin/python -m calibrate_joints
# or a subset, by servo channel:
sudo PYTHONPATH=src .venv/bin/python -m calibrate_joints --channels 6 4
# sign + offset only (skip the arc-measurement step):
sudo PYTHONPATH=src .venv/bin/python -m calibrate_joints --no-scale
```

For each joint it checks **direction** (does it move the way the model expects?
flips `sign` if reversed), **offset** (which servo angle is the zero-landmark),
and **scale** (measured physical arc ÷ commanded degrees). After writing, re-run
a known SAFE pose through the CLI to sanity-check. A joint locked to a single
angle in `SAFE_LIMITS` (e.g. `RT_ELBOW_TILT` when locked straight) can't have
its arc measured until that range is temporarily widened.

### Validating collision predictions (boundary probe)

Once calibrated, `probe_collision.py` confirms the model flags a collision **at
or before** parts physically touch. You give it a base pose and one joint to
step toward a suspected collision; at each step it shows the model verdict
*before* moving, then you press `c` the instant parts touch (or `q` to abort).
It reports whether the model flagged before contact and logs any disagreement.

Dry-run to rehearse (no motion):

```bash
SERVO_SIM=1 PYTHONPATH=src .venv/bin/python -m probe_collision \
  --base '{"0":90,"1":90,"4":150,"5":5,"6":170,"7":0}' \
  --probe-channel 7 --toward 270 --dry-run
```

Then on hardware (root; keep a hand on the power):

```bash
sudo PYTHONPATH=src .venv/bin/python -m probe_collision \
  --base '{"0":90,"1":90,"4":150,"5":5,"6":170,"7":0}' \
  --probe-channel 7 --toward 270 --step 2
```

Only the probe joint moves (clamped to `SAFE_LIMITS`); it parks on exit. A
`PASS` means the model predicted the collision early (conservative = good); a
`FAIL` means it flagged late or missed it — increase `--margin` or re-check the
involved joint's calibration. If `--toward` is beyond the joint's `SAFE_LIMITS`,
the sweep stops at the limit and prints a one-line NOTE (no clamp spam).

#### Discovering real limits (supervised, drives past SAFE_LIMITS)

Per-axis `SAFE_LIMITS` were set conservatively before the model existed, so they
may cost range of motion. To find where a joint *actually* collides, `--allow-
beyond-safe MIN MAX` lets the probe drive past `SAFE_LIMITS` within MIN..MAX
(still clamped to the 0–270 electrical range). **This can drive a joint into the
body on purpose** — it requires typing `YES` to arm, tags every step that is
`[BEYOND SAFE_LIMITS]`, and you remain the safety stop (`c` = contact, `q` =
abort, hand on the power).

```bash
# find where shoulder tilt actually contacts the body below the current floor:
sudo PYTHONPATH=src .venv/bin/python -m probe_collision \
  --base '{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}' \
  --probe-channel 6 --toward 20 --step 2 --allow-beyond-safe 20 170
```

Once you find the true contact angle, set that joint's `SAFE_LIMITS` in
`constants.py` to just inside it.

---

## Hardware troubleshooting

Quick standalone scripts to verify each piece of hardware in isolation. All
require `sudo` (GPIO/I2C access) and are run from the repo root. Each script
returns the hardware to a safe resting state (LED off, jaw closed) when it
finishes, even on Ctrl+C.

### Test the LED eyes

Flashes the eye LED on `EYE_LIGHT_PIN` on and off to confirm wiring:

```bash
# 5 blinks at the default 0.5s on/off
sudo .venv/bin/python src/eyetest.py

# Custom: 10 fast blinks
sudo .venv/bin/python src/eyetest.py --count 10 --on-time 0.25 --off-time 0.25
```

Options: `--count` (blink cycles, default 5), `--on-time` / `--off-time`
(seconds, default 0.5).

### Test the jaw motor

Triggers the jaw motor on `MOUTH_MOTOR_PIN` open and closed — the same device
the audio pipeline pulses from amplitude:

```bash
# 5 open/close cycles at the default 0.3s
sudo .venv/bin/python src/jawtest.py

# Custom: 10 quick cycles
sudo .venv/bin/python src/jawtest.py --count 10 --on-time 0.15 --off-time 0.15
```

Options: `--count` (cycles, default 5), `--on-time` / `--off-time` (seconds,
default 0.3).

### Test / calibrate the servos

`calibrate.py` moves a single servo channel one degree at a time so you can find
each joint's safe travel and record it in `SAFE_LIMITS` (`src/constants.py`).
Angles are clamped to the known-safe `SAFE_LIMITS` by default.

```bash
# Read a channel's current angle (no movement)
sudo .venv/bin/python src/calibrate.py --channel 1 --read

# Move channel 1 to 45 degrees (clamped to SAFE_LIMITS)
sudo .venv/bin/python src/calibrate.py --channel 1 --angle 45

# Nudge a few degrees from the current position (safer for probing)
sudo .venv/bin/python src/calibrate.py --channel 1 --nudge 5

# Bus / wiring health check (no channel needed)
sudo .venv/bin/python src/calibrate.py --health
```

Key options: `--channel N` (servo channel; see the channel table above),
`--angle N` (absolute target, clamped to safe limits), `--nudge N` (relative
move from current angle), `--read` (report angle without moving), `--health`
(bus/wiring check), `--step-delay S` (seconds per degree, default 0.20),
`--hold S` (seconds to hold at target, default 3.0).

> **Safety:** driving a servo past its mechanical stop stalls the motor at
> locked-rotor current, which overheats and can burn it out. Only probe beyond
> the known-safe range with `--unsafe`, one small `--nudge` at a time, with a
> hand on the power switch.

---

## ALSA audio configuration

ALSA config files for the sound card are in `src/config/alsa/`. Copy or symlink
them to their system locations on the Pi:

```bash
# Verify card indices on the Pi
aplay -l    # playback devices
arecord -l  # capture devices
```

- Output device: `sysdefault:CARD=Device` (index 2)
- Input device: `sysdefault:CARD=Device_1` (index 1)

---

## Web control panel

`webapp.py` is a self-contained Flask + HTML control panel — the primary
operator UI. It serves a single page with all the controls and launches the
Python entry points as subprocesses, dispatching every action through an
explicit allowlist (`ROUTINE_ACTIONS` / `MOVEMENT_ACTIONS` / `TRACKING_ACTIONS`
/ `SCAN_ACTIONS` / `PUPPETEER_ACTIONS` / `IR_MODES`) before any subprocess is spawned.

### What it controls

| Section | What it does | How |
|---------|--------------|-----|
| **Routines** | Full gesture + audio routines | Runs `src/animatronic.py --action=<name>` as a subprocess |
| **Movements** | Gesture-only tests (no audio) | Runs `src/controller.py --action=<name>` as a subprocess |
| **Modes** | Start/stop Sleep (`/nap`), Awake (`/awake`), Tracking (`/tracking`), Scan (`/scan`), Puppeteer (`/puppeteer`) | Launches the Mode subprocess; a stop request writes the `nap_signal` to wind it down. Starting a Mode gracefully preempts a different running Mode first. Puppeteer also starts/stops the live mic Stream. |
| **Voice FX** | Mic start/stop, style presets, per-effect toggles/sliders | Proxied to `micwebcontroller.py` |
| **Jaw Tuning** | Sensitivity / noise floor / drop threshold | Proxied to `micwebcontroller.py` |
| **Camera** | Live feed, detections, status, model select, IR mode — shown in the fixed right column (always visible) | Read-only proxy of Camera_Service (loopback `:8001`) |
| **Range** | HC-SR04 distance gauge in the fixed right column; detection-gate sensitivity under the Config tab | `/range`, `/range/sensitivity` |

The left column holds four tabs — **Routines**, **Gestures**, **Voice FX**, and
**Config** (which consolidates Napping, Awake, Scan, the sensor-range gate, and
Jaw Tuning). The top of the page carries the live-mic toggle and the per-mode
Start/Stop controls; the right column always shows the range gauge and the live
camera feed with its detection-overlay toggle.

### Running it

`webapp.py` needs `micwebcontroller.py` running for the Voice FX and Jaw Tuning
controls (that process owns the mic stream + effects engine), and
`camera_service.py` running for the live camera feed in the right column.

```bash
# Run from the repo root

# 1. Start the mic controller (owns the PyAudio stream + effects) on port 5000
sudo .venv/bin/python src/micwebcontroller.py &

# 2. Start the control panel on port 8000 (auto-reload is on by default)
sudo .venv/bin/python src/webapp.py
```

> Auto-reload restarts the app when you edit code. For the live display, run
> `WEBAPP_DEV=0 sudo .venv/bin/python src/webapp.py` to disable it — see
> [Dev auto-reload](#dev-auto-reload) below.

Then open the panel in a browser on the same network:

```
http://<pi-ip-address>:8000/
```

Ports at a glance:

- `8000` — web control panel (`webapp.py`)
- `5000` — mic stream + effects (`micwebcontroller.py`)
- `8001` — Camera_Service (`camera_service.py`, loopback only)

### Busy interlock (safety)

Only one gesture routine may drive the servos at a time. Running two at once can
command a servo into a mechanical block, stalling it at locked-rotor current —
which overheats and can burn out the motor and wiring (a fire hazard).

Two layers enforce this:

- **Hardware-level lock** — `servo_lock.py` holds a cross-process file lock for
  the duration of every routine. Any second process that tries to move the
  servos (web app or manual CLI) fails fast and exits with
  the busy exit code. The OS releases the lock automatically if a process
  crashes, so there are no stale locks. Tracking Mode is special: it takes only
  the **Neck_Group** lock (channels 0–1), so a disjoint arm-only gesture can run
  concurrently.
- **UI interlock** — while a routine runs, the control panel shows a "moving"
  banner, disables all Routine and Movement buttons, and marks the status bar
  `servos: RUNNING`. The buttons re-enable automatically when the routine ends.
  If a request slips through anyway, the server rejects it with HTTP 409 and the
  UI shows a "busy" message rather than stacking a second routine.

A watchdog (`GESTURE_TIMEOUT`, 90s) kills a hung gesture subprocess so it can't
hold the lock forever.

### Dev auto-reload

Auto-reload is **on by default**: Flask restarts the app automatically when you
edit `webapp.py` or any of the sibling project modules (`servo_lock.py`,
`animatronic.py`, `controller.py`, etc.). Just run it normally:

```bash
.venv/bin/python src/webapp.py
```

For the **live display**, disable auto-reload so a reload triggered mid-routine
can't interrupt servo motion. Set `WEBAPP_DEV=0` (also accepts `false`/`no`/`off`):

```bash
WEBAPP_DEV=0 sudo .venv/bin/python src/webapp.py
```

(The reloader is reloader-safe: the background threads — now just the HC-SR04
range poller — start only in the worker process, never doubled across the
watcher and worker.)

### Running the mic controller in the background

`micwebcontroller.py` (port 5000) owns the PyAudio stream and effects engine, so
it must be running for the Voice FX and Jaw Tuning tabs to work. To run it in the
background and keep it alive across SSH sessions:

```bash
sudo nohup .venv/bin/python src/micwebcontroller.py > /tmp/micwebcontroller.log 2>&1 &
```

Verify it's up:

```bash
curl http://localhost:5000/status
# {"streaming": false}
```

---

## Voice tuning and effects

When the mic stream is running (`micwebcontroller.py`), live audio is analysed
to drive the jaw motor and passed through a chain of voice effects before
playback. Everything is tunable at runtime from the web control panel — no
restart needed while you experiment.

The control panel is split across tabs to keep it uncluttered:

- **Controller** — Routines, Movements, and Modes
- **Voice FX** — voice style presets and per-effect toggles/sliders
- **Jaw Tuning** — jaw motor sensitivity controls
- **Camera** — live feed, detections, model/IR controls
- **Range** — distance gauge and detection-gate sensitivity

### Jaw tuning

The jaw opens based on the peak amplitude of each audio chunk. Three sliders on
the **Jaw Tuning** tab control the behavior:

| Control | Range | What it does |
|---------|-------|--------------|
| **Sensitivity** | 50–2000 | Peak amplitude divisor. **Lower = more sensitive.** If the jaw barely moves, tune this down (try 300–500). |
| **Noise Floor** | 0–2000 | Absolute peak below which the jaw stays fully closed. Eliminates jitter from mic background noise. Set it just above your ambient noise level. |
| **Drop Threshold** | 0.0–1.0 | Controls how a falling signal is handled. A **sharp** drop (ratio below threshold) snaps the jaw closed; a **gradual** drop (ratio at or above threshold) holds it open so natural speech decay doesn't chatter. Lower = closes more eagerly between words. |

**Tuning tips**

- Start by raising **Noise Floor** until the jaw stops twitching in silence.
  Ambient noise typically peaks around 150–400, so a floor of 600–850 works well.
- Then lower **Sensitivity** until normal speaking volume opens the jaw fully.
- Use **Drop Threshold** last to fine-tune how crisply the jaw closes between
  words.

You can also set these over HTTP:

```bash
curl -X POST http://localhost:5000/config \
     -H 'Content-Type: application/json' \
     -d '{"sensitivity": 400, "noise_floor": 800, "drop_threshold": 0.2}'
```

### Voice effects

The mic passthrough runs each chunk through an effects chain in this fixed
order, with clipping protection at the end:

```
pitch → ring_mod → bitcrush → distortion → tremolo → echo → reverb
```

Each effect has an on/off toggle and an intensity slider on the **Voice FX**
tab. Stacking several at high intensity saturates rather than causing harsh
digital wraparound.

| Effect | Range | What it does |
|--------|-------|--------------|
| **Pitch** | 0.4–1.6 | Shifts pitch. **< 1.0 = deeper**, **> 1.0 = higher.** |
| **Distortion** | 0.0–1.0 | Gritty, driven clipping for a possessed edge. |
| **Echo** | 0.0–1.0 | Haunting repeats from the echo buffer (amount = decay). |
| **Reverb** | 0.0–1.0 | Otherworldly feedback-delay wash. |
| **Tremolo** | 0.0–1.0 | Pulsing amplitude wobble (amount = depth). |
| **Bitcrush** | 0.0–1.0 | Broken, lo-fi / robotic texture (reduces bit depth). |
| **Ring Mod** | 0.0–1.0 | Metallic ring modulation for a robot tone (amount = wet mix). |

### Style presets

The **Voice Style** dropdown loads a full preset in one click:

| Style | Character |
|-------|-----------|
| `natural` | All effects off — clean passthrough. |
| `demon` | Deep pitch + distortion + echo + light reverb. |
| `ghost` | Slight pitch + heavy echo + reverb + tremolo. |
| `robot` | Distortion + bitcrush + ring modulation. |
| `possessed` | Everything cranked — deep, distorted, echoing, reverberant. |

**Effect tips**

- For a scarier voice, start with the `possessed` or `demon` preset.
- To go even harsher, load `demon`, then push **Distortion** toward 0.8+ and
  drop **Pitch** toward 0.6.
- `ghost` is good for a distant, airy feel; `robot` for a mechanical/alien tone.

Set effects over HTTP too:

```bash
# Load a full style preset
curl -X POST http://localhost:5000/effects \
     -H 'Content-Type: application/json' -d '{"style": "possessed"}'

# Toggle a single effect and set its intensity
curl -X POST http://localhost:5000/effects \
     -H 'Content-Type: application/json' \
     -d '{"effect": "distortion", "enabled": true, "amount": 0.8}'
```

`GET /status` returns the current jaw config, effect config, and available
styles.

---

## Camera vision

An optional camera pipeline adds a live feed, on-device object/person detection,
head tracking, and IR night operation. It runs as a **separate, non-root
process** — `src/camera_service.py` — that owns the Raspberry Pi Camera Module 3
(NoIR), and the web control panel proxies its views into the **Camera** tab.
Tracking/Scan Modes consume its detections read-only; the service never drives
servos.

### Why the venv is built with `--system-site-packages`

`picamera2` is not a pip package on Raspberry Pi OS (bookworm); it ships via apt
as `python3-picamera2`, bound to the system `libcamera`. So the project venv is
created with system site-packages visible:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Because the apt `picamera2` C-extensions (e.g. `simplejpeg`) are compiled against
the **system numpy 1.x ABI**, `numpy` is pinned `<2` and `opencv-python-headless`
to the 4.x line (5.x hard-requires numpy 2). Mixing numpy 2.x in the venv breaks
`import picamera2` with a `numpy.dtype size changed` ABI error.

### Enabling the camera on the Pi

The sensor must be visible to libcamera first:

```bash
rpicam-hello --list-cameras      # should list the imx708 (Camera Module 3)
```

If it reports *No cameras available*, add the overlay to
`/boot/firmware/config.txt` and reboot:

```
camera_auto_detect=0
dtoverlay=imx708
```

### Detector model

Detection uses a quantized **SSD-MobileNet v2 (COCO)** TFLite model with the
80-class COCO label set. The model binary is **not** committed (it is gitignored,
~6 MB); fetch it into `models/` from Google's Coral `test_data` repo:

```bash
mkdir -p models
curl -fsSL -o models/ssd_mobilenet_v2_coco_quant_postprocess.tflite \
  https://github.com/google-coral/test_data/raw/master/ssd_mobilenet_v2_coco_quant_postprocess.tflite
curl -fsSL -o models/coco_labels.txt \
  https://raw.githubusercontent.com/google-coral/test_data/master/coco_labels.txt
```

`models/coco_labels.txt` *is* tracked (it is small and the index mapping matters
— index 0 is `person`). Any `models/*.tflite` is ignored.

> Detection is **optional**: with no model configured, Camera_Service still
> serves the live feed and head tracking; `/detections` is just empty.

### Configuration (environment variables)

`camera_service.py` reads these at startup:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAMERA_MODEL_PATH` | *(unset)* | Path to the `.tflite` detector model. Unset = no detection. |
| `CAMERA_LABELS_PATH` | *(unset)* | Path to the COCO labels file. |
| `CAMERA_CONF_THRESHOLD` | `0.5` | Min confidence to report a detection, clamped `[0,1]`. Lower (e.g. `0.3`) surfaces more / smaller objects at the cost of more false positives. |

### Running as a service (recommended)

A systemd unit is provided at `deploy/camera-service.service` (runs as the
non-root `aaron` user; model + labels + confidence baked in as `Environment=`).
Install and start it:

```bash
sudo cp deploy/camera-service.service /etc/systemd/system/camera-service.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-service          # start now + on every boot

journalctl -u camera-service -f                     # follow logs
curl -s http://127.0.0.1:8001/status                # {"camera_ok":true,"capturing":true,...}
```

After editing the unit (e.g. to change the model or threshold), re-copy it,
`daemon-reload`, then `restart` — a plain `restart` reloads the OLD installed
copy:

```bash
sudo cp deploy/camera-service.service /etc/systemd/system/camera-service.service
sudo systemctl daemon-reload && sudo systemctl restart camera-service
```

Camera_Service binds **loopback only** (`127.0.0.1:8001`), so frames and
detections never leave the device; the control panel on port 8000 proxies them.

### Swapping detector models

To try a different model, drop its `.tflite` in `models/`, point
`CAMERA_MODEL_PATH` at it in the unit, and reinstall + restart (above). The code
auto-identifies the SSD post-process outputs by tensor name, so any standard
COCO SSD-MobileNet / EfficientDet-Lite export with the same label set is a
drop-in. Tune `CAMERA_CONF_THRESHOLD` without touching code.

---

## Range sensor

An optional HC-SR04 ultrasonic sensor provides presence and approach detection —
the wake trigger for Sleep mode and the dashboard distance gauge. It is wired to
`RANGE_TRIG_PIN` (23, output) and `RANGE_ECHO_PIN` (24, input via a 5V→3.3V
voltage divider).

- `range_sensor.py` — `RangeSensor` wraps gpiozero's `DistanceSensor` and exposes
  `distance_m()` / `distance_cm()` and `object_within(threshold_m)`.
  `ApproachDetector` builds on it to detect an object *getting closer* across
  consecutive readings within a range gate.
- `range_publish.py` — publishes the latest reading for the control panel's
  `/range` gauge; `/range/sensitivity` sets the detection-gate threshold used by
  the Modes.

> **Wiring:** the HC-SR04 ECHO pin idles at 5V, but the Pi GPIO tolerates only
> 3.3V. A voltage divider (or level shifter) on the ECHO line is required.

---

## Adding a new routine

1. Add an audio file to `audio/` (the version-controlled source of truth; it is
   resolved directly at runtime — no `~/Music/` copy needed).
2. Add the filename to the `music` list in `src/animatronic.py` (with an index comment).
3. Author the routine method:
   - Simple one-gesture / one-clip: add a `_do_*` coroutine and a method calling
     `self.run_action_and_audio("gesture_name", self.music[n])`.
   - Concurrent / audio-synced / multi-track: define a `PerformanceDefinition`
     and run it via `PerformanceRunner` (see `brains` / `blah` / `hypnotic`).
4. Register the `camelCase` action in `build_action_map()` in `src/animatronic.py`
   (the single dispatch allowlist — never `getattr`/`eval` on raw `--action`).
5. Add the action to `ROUTINE_ACTIONS` in `src/webapp.py` so it appears in the
   control panel.
6. Optionally register the gesture in `src/controller.py` for audio-free testing,
   then test it alone first:
   `sudo .venv/bin/python src/controller.py --action=<gesture>`.
