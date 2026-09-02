# animatronic-v2

A Raspberry Pi–based animatronic controller that synchronises servo-driven
physical gestures with audio playback via PyAudio (`AudioPlayer` /
`AudioStreamer`).

---

## Hardware

| Component | Details |
|-----------|---------|
| Controller | Raspberry Pi (any model with I2C) |
| Servo driver | Adafruit 16-channel PCA9685 PWM board |
| Servo channels | 16-channel, all configured for **270° actuation range** |
| Audio output | USB audio device (`sysdefault:CARD=Device`, index 2) |
| Mic input | USB audio device (`sysdefault:CARD=Device_1`, index 1) |

### Servo channel assignments

| Channel | Constant | Joint |
|---------|----------|-------|
| 0 | `NECK_PAN` | Left/right head rotation |
| 1 | `NECK_TILT` | Up-down head tilt |
| 4 | `RT_ELBOW_ROTATOR` | Forearm rotation (wrist/palm) |
| 5 | `RT_ELBOW_TILT` | Elbow bend |
| 6 | `RT_SHOULDER_TILT` | Shoulder forward/back |
| 7 | `RT_SHOULDER_ROTATOR` | Shoulder raise/lower |

---

## Project structure

```
animatronic-v2/
├── src/
│   ├── constants.py            # Servo channels, SAFE_LIMITS, REST_POSITIONS
│   ├── trunkcontroller.py      # Low-level async servo primitives
│   ├── movements.py            # High-level async gesture choreography
│   ├── concurrentMovements.py  # Thread-based gestures (ThreadPoolExecutor)
│   ├── animatronic.py          # Named routines pairing gestures with audio
│   ├── controller.py           # CLI entry point for gesture testing
│   ├── audio_player.py         # WAV/MP3 file player with jaw-motor sync
│   ├── audio_streamer.py       # Live mic passthrough + effects + jaw sync
│   ├── config_store.py         # Shared tuning config (jaw profiles; servo limits later)
│   ├── servo_lock.py           # Cross-process servo mutex (fcntl)
│   ├── calibrate.py            # Interactive single-servo limit finder
│   ├── eyetest.py              # Flash the eye LED (EYE_LIGHT_PIN) hardware test
│   ├── micwebcontroller.py     # Flask: mic stream + jaw tuning + voice FX (port 5000)
│   ├── webapp.py               # Flask control panel UI (port 8000)
│   ├── model/  utils/  action/ # Supporting packages
│   ├── templates/index.html    # Control-panel UI served by webapp.py
│   └── config/
│       ├── tuning.json         # Jaw tuning profiles (auto-created; version-controlled)
│       └── alsa/               # ALSA sound-card configuration
├── tests/                      # pytest suite (hypothesis property + unit tests)
├── audio/                      # WAV/MP3 files (deployed to ~/Music/ on the Pi)
├── .venv/                      # Python virtualenv (repo root)
├── requirements.txt
└── README.md
```

### Layer overview

```
animatronic.py / controller.py   ← you call these
        │
        ▼
    movements.py                 ← gesture choreography (async)
        │
        ▼
  trunkcontroller.py             ← servo primitives (async, adafruit_servokit)
        │
        ▼
  PCA9685 PWM board → servos
```

`concurrentMovements.py` sits alongside `movements.py` and uses
`ThreadPoolExecutor` instead of asyncio for gestures that need true
thread-level parallelism (e.g. `face_palm`).

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
| `numpy` | 2.3.4 | Amplitude analysis for jaw-motor sync |
| `PyAudio` | 0.2.14 | Audio file playback and mic streaming |

---

## Development setup

```bash
# Clone and create a virtual environment
git clone <repo-url> animatronic-v2
cd animatronic-v2
python3 -m venv .venv
source .venv/bin/activate

# Install pinned dependencies
pip install -r requirements.txt
```

> Most runtime commands require `sudo` so Python can access the I2C bus and
> GPIO pins. The venv must still be active (or the full venv Python path used)
> when running with sudo.

---

## Running

### Run a named animatronic routine (gesture + audio)

Run from the repo root (the venv lives at the repo root):

```bash
sudo .venv/bin/python3 src/animatronic.py --action=<action>
```

Available actions:

| Action | Gesture | Audio |
|--------|---------|-------|
| `startParty` | Wave + swivel head | `sb_party_switch.wav` |
| `hello` | Wave | `hello-everyone.wav` |
| `happyHalloween` | Wave | `happy-halloween.wav` |
| `howYallDoin` | Wave | `how-yall.wav` |
| `cantHear` | Wave | `cant-hear.wav` |
| `niceDay` | Wave | `walk.wav` |
| `blah` | Head-shake no | `blah.wav` |
| `krusty` | Neck ellipse | `krusty-laugh.wav` |
| `waiting` | Come + look around | `were-waiting.wav` |
| `exorcist` | Come + look around | `beetel-exorcist.wav` |
| `vaderFather` | Come + look around | `vader-father.wav` |
| `torture` | Come + look around | `spongebob-torture.mp3` |
| `vaderBeaten` | Patrol | `vader-beaten.wav` |
| `yoda` | Patrol | `yoda-agent-evil.wav` |
| `mic` | — | Live microphone passthrough (AudioStreamer) |

### Test an individual gesture (no audio)

```bash
sudo .venv/bin/python3 src/controller.py --action=<action>
```

Available gesture actions: `wave`, `yes`, `no`, `smno`, `lookAround`,
`scan`, `slowScan`, `swivelHead`, `come`, `comein`, `neckEllipse`,
`lookAroundSmall`.

### Run the face-palm concurrent movement demo

```bash
sudo .venv/bin/python3 src/concurrentMovements.py
```

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

`webapp.py` is a self-contained Flask + HTML control panel — the only UI for the
system. It serves a single page with all the controls and talks to the same
underlying scripts.

### What it controls

| Section | What it does | How |
|---------|--------------|-----|
| **Routines** | Full gesture + audio routines | Runs `src/animatronic.py --action=<name>` as a subprocess |
| **Movements** | Gesture-only tests (no audio) | Runs `src/controller.py --action=<name>` as a subprocess |
| **Voice FX** | Mic start/stop, style presets, per-effect toggles/sliders | Proxied to `micwebcontroller.py` |
| **Jaw Tuning** | Sensitivity / noise floor / drop threshold | Proxied to `micwebcontroller.py` |
| **Automation** | Timed random routine (5 min) and movement (45 sec) loops | Background threads in `webapp.py` |

### Running it

`webapp.py` still needs `micwebcontroller.py` running for the Voice FX and Jaw
Tuning tabs to work (that process owns the mic stream and effects engine).

```bash
# Run from the repo root

# 1. Start the mic controller (owns the PyAudio stream + effects) on port 5000
sudo .venv/bin/python3 src/micwebcontroller.py &

# 2. Start the control panel on port 8000 (auto-reload is on by default)
sudo .venv/bin/python3 src/webapp.py
```

> Auto-reload restarts the app when you edit code. For the live display, run
> `WEBAPP_DEV=0 sudo .venv/bin/python3 src/webapp.py` to disable it — see
> [Dev auto-reload](#dev-auto-reload) below.

Then open the panel in a browser on the same network:

```
http://<pi-ip-address>:8000/
```

Ports at a glance:

- `8000` — web control panel (`webapp.py`)
- `5000` — mic stream + effects (`micwebcontroller.py`)

### Busy interlock (safety)

Only one gesture routine may drive the servos at a time. Running two at once can
command a servo into a mechanical block, stalling it at locked-rotor current —
which overheats and can burn out the motor and wiring (a fire hazard).

Two layers enforce this:

- **Hardware-level lock** — `servo_lock.py` holds a cross-process file lock
  (`/tmp/animatronic_servo.lock`) for the duration of every routine. Any second
  process that tries to move the servos (web app, automation loop, or manual
  CLI) fails fast and exits with code 3. The OS releases
  the lock automatically if a process crashes, so there are no stale locks.
- **UI interlock** — while a routine runs, the control panel shows a "moving"
  banner, disables all Routine and Movement buttons, and marks the status bar
  `servos: RUNNING`. The buttons re-enable automatically when the routine ends.
  If a request slips through anyway, the server rejects it with HTTP 409 and the
  UI shows a "busy" message rather than stacking a second routine.

The automation loops also skip their tick if the servos are already busy, so
timed playback never stacks on top of a running routine.

### Dev auto-reload

Auto-reload is **on by default**: Flask restarts the app automatically when you
edit `webapp.py` or any of the sibling project modules (`servo_lock.py`,
`animatronic.py`, `controller.py`, etc.). Just run it normally:

```bash
.venv/bin/python3 src/webapp.py
```

For the **live display**, disable auto-reload so a reload triggered mid-routine
can't interrupt servo motion. Set `WEBAPP_DEV=0` (also accepts `false`/`no`/`off`):

```bash
WEBAPP_DEV=0 sudo .venv/bin/python3 src/webapp.py
```

(The reloader is reloader-safe: the automation threads start only in the worker
process, never doubled across the watcher and worker.)

### Running the mic controller in the background

`micwebcontroller.py` (port 5000) owns the PyAudio stream and effects engine, so
it must be running for the Voice FX and Jaw Tuning tabs to work. To run it in the
background and keep it alive across SSH sessions:

```bash
sudo nohup .venv/bin/python3 src/micwebcontroller.py > /tmp/micwebcontroller.log 2>&1 &
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

The control panel is split across three tabs to keep it uncluttered:

- **Controller** — Routines and Movements
- **Voice FX** — voice style presets and per-effect toggles/sliders
- **Jaw Tuning** — jaw motor sensitivity controls

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
| `chipmunk` | Pitch shifted up. |
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

## Adding a new routine

1. Add an audio file to `audio/` and copy it to `~/Music/` on the Pi.
2. Add the filename to the `music` list in `src/animatronic.py` (with an index comment).
3. Create a method on `Animatronic` calling `self.run_action_and_audio("gesture_name", self.music[n])`.
4. Add the action name to the `action_map` dict in `main()` in `src/animatronic.py`.
5. Optionally register the gesture in `src/controller.py` for audio-free testing.
6. Test the gesture alone first: `sudo .venv/bin/python3 src/controller.py --action=<gesture>`.
