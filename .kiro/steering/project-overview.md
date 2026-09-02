---
inclusion: always
---

# Project Overview — animatronic-v2

A Raspberry Pi–based animatronic controller that synchronizes servo-driven physical gestures with audio playback. The system is operated interactively via a Flask web control panel (`src/webapp.py` on port 8000), backed by `src/micwebcontroller.py` (port 5000) for mic streaming, jaw sync, and audio effects.

## System Components

| Component | Role |
|-----------|------|
| `src/animatronic.py` | Full routines: gesture + synchronized audio |
| `src/controller.py` | Gesture-only CLI testing (no audio) |
| `src/movements.py` | Async gesture choreography |
| `src/trunkcontroller.py` | Low-level servo primitives |
| `src/concurrentMovements.py` | Thread-based concurrent gesture execution |
| `src/constants.py` | Servo channel assignments and shared constants |
| `src/audio_player.py` | PyAudio-based file playback with jaw-motor sync |
| `src/audio_streamer.py` | Live mic passthrough with audio effects and jaw-motor sync |
| `src/config_store.py` | Shared tuning config store (jaw profiles now, servo limits later) |
| `src/servo_lock.py` | Cross-process servo lock (fcntl mutex) |
| `src/webapp.py` | Flask control panel UI (port 8000) |
| `src/micwebcontroller.py` | Mic stream + jaw + effects (port 5000) |
| `src/calibrate.py` | Interactive servo limit finder |
| `src/eyetest.py` | Eye LED test |
| `src/config/alsa/` | ALSA sound card configuration for the Pi |
| `audio/` | Local mirror of audio files (deployed to `~/Music/` on the Pi) |

## Web Control Panel

The Flask control panel (`src/webapp.py`, port 8000) is the primary operator UI. It runs the Python entry points as subprocesses:

- **Routines**: run `src/animatronic.py --action=<name>` (gesture + audio).
- **Movements**: run `src/controller.py --action=<name>` (gesture only).
- **Mic / jaw / effects**: proxied to `src/micwebcontroller.py` (port 5000), which owns the live mic stream and audio effects.

Action names are dispatched through an explicit allowlist before being passed to the subprocess. Tuning changes made in the panel persist to `src/config/tuning.json`.

## Audio

Audio is handled entirely in-process by two Python modules:

- `src/audio_player.py` (`AudioPlayer`) — PyAudio WAV file player. Streams the file chunk-by-chunk, drives the jaw motor (`MOUTH_MOTOR_PIN`) from peak amplitude on each frame. Run in a daemon thread alongside the gesture coroutine.
- `src/audio_streamer.py` (`AudioStreamer`) — Live microphone passthrough. Applies pitch-shift and echo effects, drives the jaw motor from mic input. Used for the `mic` action. Call `.start()` / `.stop()` to manage the stream.

Audio files must be present at `~/Music/` on the Pi. The local `audio/` directory is the source of truth.

## ALSA Audio

- Default sound card is set to card index `2` in both `.asoundrc` and `alsa.conf`.
- The USB audio device is referenced as `sysdefault:CARD=Device` (output) and `sysdefault:CARD=Device_1` (mic input).
- To verify card indices on the Pi: `aplay -l` (playback) and `arecord -l` (capture).
- ALSA config files in `src/config/alsa/` need to be symlinked or copied to their system locations on the Pi.

## Deployment Notes

- All Python scripts run as root (`sudo`) for I2C and GPIO access. Scripts now live under `src/` (e.g. `sudo python3 src/animatronic.py`).
- Audio files must be present at `~/Music/` on the Pi. The local `audio/` directory is the source of truth.
- The Pi path for the project is `/home/pi/workspace/animatronic/` (note: not `animatronic-v2`). All scripts now live under `src/`.

## Automation Behavior

The web control panel (`src/webapp.py`) includes two independent automation loops, implemented as background threads:

- **Routine automation** (toggled from the control panel): Fires every 5 minutes, randomly picks a full gesture + audio routine from: `blah`, `exorcist`, `startParty`, `waiting`, `krusty`, `vaderFather`.
- **Movement automation** (toggled from the control panel): Fires every 45 seconds, randomly picks a gesture-only movement from: `slowScan`, `yes`, `no` (and other movements defined in the automation logic).
