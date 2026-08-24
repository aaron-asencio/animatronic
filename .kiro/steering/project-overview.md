---
inclusion: always
---

# Project Overview — animatronic-v2

A Raspberry Pi–based animatronic controller that synchronizes servo-driven physical gestures with audio playback. The system is operated interactively via a Node-RED dashboard web UI running on the Pi.

## System Components

| Component | Role |
|-----------|------|
| `animatronic.py` | Full routines: gesture + synchronized audio |
| `controller.py` | Gesture-only CLI testing (no audio) |
| `movements.py` | Async gesture choreography |
| `trunkcontroller.py` | Low-level servo primitives |
| `concurrentMovements.py` | Thread-based concurrent gesture execution |
| `constants.py` | Servo channel assignments and shared constants |
| `config/flows.json` | Node-RED dashboard flow definitions |
| `config/overrides.cfg` | lightshowpi config for playlist/file mode |
| `config/overrides-mic.cfg` | lightshowpi config for live microphone input |
| `config/alsa/` | ALSA sound card configuration for the Pi |
| `audio/` | Local mirror of audio files (deployed to `~/Music/` on the Pi) |

## Node-RED Integration

Node-RED provides the primary operator UI ("Halloween Controller" dashboard). It runs on the Pi and drives the Python scripts via `exec` nodes.

- **Routines group**: Buttons trigger `animatronic.py --action=<name>` (gesture + audio).
- **Movements group**: Buttons trigger `controller.py --action=<name>` (gesture only).
- **LightshowPi group**: Direct lightshowpi controls.
- **Automation**: Toggle switches enable timed random routine or movement playback via `looptimer` + `random` + `Switch` function nodes.

The exec node command pattern is:
```
export SYNCHRONIZED_LIGHTS_HOME=/home/pi/workspace/lightshowpi; sudo python3 /home/pi/workspace/animatronic/animatronic.py
```
The `--action="<name>"` argument is passed as `msg.payload` from the button node.

When adding a new action:
1. Add the button node in `flows.json` wired to the appropriate `exec` node.
2. Set `payload` to `--action="<actionName>"` (note the quotes inside the string).
3. Import the updated `flows.json` into Node-RED via Menu → Import.

## lightshowpi Configuration

Two config files live in `config/` and must be deployed to the lightshowpi `config/` directory on the Pi (not used as absolute paths — lightshowpi ignores files referenced by absolute path):

- `overrides.cfg` — playlist/file playback mode. Audio output card: `sysdefault:CARD=Device`. Uses 8 GPIO pins (`0–7`), `onoff` mode.
- `overrides-mic.cfg` — live microphone input mode (`audio-in`). Single GPIO pin (`24`). Frequency range tuned to human voice: `85–255 Hz`. Input card: `sysdefault:CARD=Device_1`.

`SYNCHRONIZED_LIGHTS_HOME` must be set in the environment before running lightshowpi. The exec node in Node-RED exports this variable inline.

## ALSA Audio

- Default sound card is set to card index `2` in both `.asoundrc` and `alsa.conf`.
- The USB audio device is referenced as `sysdefault:CARD=Device` (output) and `sysdefault:CARD=Device_1` (mic input).
- To verify card indices on the Pi: `aplay -l` (playback) and `arecord -l` (capture).
- ALSA config files in `config/alsa/` need to be symlinked or copied to their system locations on the Pi. The `config/create_LSP_config_links.sh` script handles this.

## Deployment Notes

- All Python scripts run as root (`sudo`) for I2C and GPIO access.
- Audio files must be present at `~/Music/` on the Pi. The local `audio/` directory is the source of truth.
- The Pi path for the project is `/home/pi/workspace/animatronic/` (note: not `animatronic-v2` — confirm the actual deployment path before editing Node-RED exec commands).
- `SYNCHRONIZED_LIGHTS_HOME` must point to `/home/pi/workspace/lightshowpi`.

## Automation Behavior

Node-RED includes two independent automation loops:

- **Routine automation** (`Routine Automation` switch): Fires every 5 minutes, randomly picks a full gesture + audio routine from: `blah`, `exorcist`, `startParty`, `waiting`, `krusty`, `vaderFather`.
- **Movement automation** (`Movement Automation` switch): Fires every 45 seconds, randomly picks a gesture-only movement from: `slowScan`, `yes`, `no` (and others defined in the Switch function node — note the node currently only generates random integers 0–2 but the switch handles up to 10 cases; values above 2 will always default to `slowScan`).
