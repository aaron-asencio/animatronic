# animatronic-v2

A Raspberry Pi–based animatronic controller that synchronises servo-driven
physical gestures with audio playback via
[lightshowpi](https://lightshowpi.org/).

---

## Hardware

| Component | Details |
|-----------|---------|
| Controller | Raspberry Pi (any model with I2C) |
| Servo driver | Adafruit 16-channel PCA9685 PWM board |
| Servo channels | 16-channel, all configured for **270° actuation range** |
| Audio | lightshowpi `synchronized_lights.py` (GPIO + audio output) |

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
├── driver.py               # Early prototype / sandbox (not used at runtime)
├── audio/                  # WAV/MP3 files played by lightshowpi
├── requirements.txt        # Pinned Python dependencies
└── config/
    ├── flows.json           # Node-RED flow definitions
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
| `numpy` | 2.3.4 | FFT processing (used by lightshowpi) |
| `PyAudio` | 0.2.14 | Audio input for mic mode |

### lightshowpi

Must be installed separately — see [lightshowpi setup](https://lightshowpi.org/install/).

Expected install path on the Pi: `/home/pi/workspace/lightshowpi/`

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
| `mic` | — | Live microphone input |

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

## lightshowpi configuration

lightshowpi config files must live inside the lightshowpi `config/` directory.
Absolute paths cause lightshowpi to ignore the file and fall back to defaults.

```bash
# Play a file with light sync
sudo python3 /home/pi/workspace/lightshowpi/py/synchronized_lights.py \
     --file="/home/pi/Music/sb_party_switch.wav"

# Live microphone input mode
sudo python3 /home/pi/workspace/lightshowpi/py/synchronized_lights.py \
     --config="overrides-mic.cfg"
```

ALSA configuration files for the sound card are in `config/alsa/`.
Run `config/create_LSP_config_links.sh` on the Pi to symlink them to their
system locations.

---

## Adding a new routine

1. Add an audio file to `audio/` and copy it to `~/Music/` on the Pi.
2. Add the filename to the `music` list in `animatronic.py` (with an index comment).
3. Create a method on `Animatronic` calling `self.run_action_and_audio("gesture_name", self.music[n])`.
4. Add the action name to the `action_map` dict in `main()` in `animatronic.py`.
5. Optionally register the gesture in `controller.py` for audio-free testing.
6. Test the gesture alone first: `python3 controller.py --action=<gesture>`.
