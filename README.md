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
├── constants.py            # Servo channel numbers and default positions
├── trunkcontroller.py      # Low-level async servo primitives (move, pan, tilt …)
├── movements.py            # High-level async gesture choreography (wave, nod …)
├── concurrentMovements.py  # Thread-based gestures using ThreadPoolExecutor
├── animatronic.py          # Named routines pairing gestures with audio
├── controller.py           # CLI entry point for individual gesture testing
├── audio_player.py         # PyAudio WAV/MP3 file player with jaw-motor sync
├── audio_streamer.py       # Live mic passthrough with effects and jaw-motor sync
├── micwebcontroller.py     # Flask app: mic stream + jaw tuning + voice effects (port 5000)
├── webapp.py               # Flask control panel — replaces the Node-RED dashboard (port 8000)
├── templates/
│   └── index.html          # Control-panel UI served by webapp.py
├── audio/                  # WAV/MP3 files (deployed to ~/Music/ on the Pi)
├── requirements.txt        # Pinned Python dependencies
└── config/
    ├── flows.json           # Node-RED flow definitions (legacy dashboard)
    ├── murdercity_flows.json
    └── alsa/               # ALSA sound-card configuration
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

```bash
sudo python3 animatronic.py --action=<action>
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
python3 controller.py --action=<action>
```

Available gesture actions: `wave`, `yes`, `no`, `smno`, `lookAround`,
`scan`, `slowScan`, `swivelHead`, `come`, `comein`, `neckEllipse`,
`lookAroundSmall`.

### Run the face-palm concurrent movement demo

```bash
python3 concurrentMovements.py
```

---

## ALSA audio configuration

ALSA config files for the sound card are in `config/alsa/`. Copy or symlink
them to their system locations on the Pi:

```bash
# Verify card indices on the Pi
aplay -l    # playback devices
arecord -l  # capture devices
```

- Output device: `sysdefault:CARD=Device` (index 2)
- Input device: `sysdefault:CARD=Device_1` (index 1)

---

## Web control panel (recommended)

`webapp.py` is a self-contained Flask + HTML control panel that replaces the
Node-RED dashboard. It serves a single page with all the controls and talks to
the same underlying scripts, so there is no `flows.json` to import and no
Node-RED process to run.

### What it controls

| Section | What it does | How |
|---------|--------------|-----|
| **Routines** | Full gesture + audio routines | Runs `animatronic.py --action=<name>` as a subprocess |
| **Movements** | Gesture-only tests (no audio) | Runs `controller.py --action=<name>` as a subprocess |
| **Voice FX** | Mic start/stop, style presets, per-effect toggles/sliders | Proxied to `micwebcontroller.py` |
| **Jaw Tuning** | Sensitivity / noise floor / drop threshold | Proxied to `micwebcontroller.py` |
| **Automation** | Timed random routine (5 min) and movement (45 sec) loops | Background threads in `webapp.py` |

### Running it

`webapp.py` still needs `micwebcontroller.py` running for the Voice FX and Jaw
Tuning tabs to work (that process owns the mic stream and effects engine).

```bash
cd /home/pi/workspace/animatronic-v2

# 1. Start the mic controller (owns the PyAudio stream + effects) on port 5000
sudo .venv/bin/python3 micwebcontroller.py &

# 2. Start the control panel on port 8000 (auto-reload is on by default)
sudo .venv/bin/python3 webapp.py
```

> Auto-reload restarts the app when you edit code. For the live display, run
> `WEBAPP_DEV=0 sudo .venv/bin/python3 webapp.py` to disable it — see
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
  process that tries to move the servos (web app, automation loop, manual CLI,
  or a leftover Node-RED exec) fails fast and exits with code 3. The OS releases
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
.venv/bin/python3 webapp.py
```

For the **live display**, disable auto-reload so a reload triggered mid-routine
can't interrupt servo motion. Set `WEBAPP_DEV=0` (also accepts `false`/`no`/`off`):

```bash
WEBAPP_DEV=0 sudo .venv/bin/python3 webapp.py
```

(The reloader is reloader-safe: the automation threads start only in the worker
process, never doubled across the watcher and worker.)

### How it maps to the old Node-RED dashboard

Everything the Node-RED "Halloween Controller" did is preserved:

- The **Routines** and **Movements** button groups became button grids that POST
  to `/routine/<action>` and `/movement/<action>`. Actions are validated against
  an allowlist before a subprocess is launched — the same security boundary the
  old `exec` nodes relied on.
- The **Voice FX** and **Jaw Tuning** tabs POST to `/effects` and `/jaw`, which
  proxy straight through to `micwebcontroller.py` — the same endpoints the
  Node-RED sliders hit.
- The two **Automation** toggles replace the `looptimer` + `random` + `Switch`
  node chains with plain background threads. This also fixes the old bug where
  the random generator only produced 0–2 while the switch handled up to 10
  cases: the loops now pick uniformly from the full action pools.

The Node-RED flow (`config/flows.json`) is kept as a legacy option but is no
longer required.

---

## Starting Node-RED (legacy)

If you prefer the original Node-RED dashboard instead of the web control panel,
the system needs two services running on the Pi: the Flask mic controller and
Node-RED.

### Flask web app (`micwebcontroller.py`)

Handles mic stream start/stop requests from Node-RED via HTTP. Node-RED's
"Enable Mic Streaming" toggle POSTs to `http://localhost:5000/handler`.

```bash
# Run as root (requires GPIO access for the jaw motor)
cd /home/pi/workspace/animatronic-v2
sudo .venv/bin/python3 micwebcontroller.py
```

The server starts on port 5000. Verify it's up:

```bash
curl http://localhost:5000/status
# {"streaming": false}
```

To run it in the background and keep it alive across SSH sessions:

```bash
sudo nohup .venv/bin/python3 micwebcontroller.py > /tmp/webcontroller.log 2>&1 &
```

### Node-RED

Node-RED provides the "Halloween Controller" dashboard UI. If it's not
already running as a service:

```bash
# Start Node-RED (runs on port 1880)
node-red

# Or if installed as a service on the Pi
sudo systemctl start nodered
sudo systemctl enable nodered   # auto-start on boot
```

Open the dashboard in a browser on the same network:

```
http://<pi-ip-address>:1880/ui
```

To edit flows or import an updated `flows.json`:

```
http://<pi-ip-address>:1880
```

Menu → Import → select `config/flows.json`.

### Typical startup order

1. `sudo .venv/bin/python3 micwebcontroller.py` — start the Flask web app
2. `sudo systemctl start nodered` (or `node-red`) — start Node-RED
3. Open `http://<pi-ip>:1880/ui` in a browser
4. Use the dashboard buttons to trigger routines or enable automation

---

## Voice tuning and effects

When the mic stream is running (`micwebcontroller.py`), live audio is analysed
to drive the jaw motor and passed through a chain of voice effects before
playback. Everything is tunable at runtime from the Node-RED dashboard — no
restart needed while you experiment.

The dashboard is split across three tabs to keep it uncluttered:

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
2. Add the filename to the `music` list in `animatronic.py` (with an index comment).
3. Create a method on `Animatronic` calling `self.run_action_and_audio("gesture_name", self.music[n])`.
4. Add the action name to the `action_map` dict in `main()` in `animatronic.py`.
5. Optionally register the gesture in `controller.py` for audio-free testing.
6. Test the gesture alone first: `python3 controller.py --action=<gesture>`.
