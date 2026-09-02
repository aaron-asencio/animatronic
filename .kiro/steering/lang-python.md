---
inclusion: fileMatch
fileMatchPattern: "**/*.py"
---

# Python Guidelines — animatronic-v2

This project controls a Raspberry Pi-based animatronic figure using servo motors (Adafruit PCA9685/ServoKit) and synchronized audio playback (AudioPlayer / AudioStreamer). All runtime scripts execute as root on the Pi.

## Architecture

The codebase is layered. Higher layers call lower ones — never the reverse.

```
animatronic.py / controller.py      ← top-level: gesture + audio routines / CLI dispatch
    └── movements.py                ← mid-level: named multi-joint async gestures
            └── trunkcontroller.py  ← low-level: individual servo primitives (async)
                    └── adafruit_servokit / PCA9685 hardware
concurrentMovements.py              ← alternative mid-level: thread-based gestures
constants.py                        ← servo channel assignments and shared constants
```

- `TrunkController` owns the `ServoKit` instance at **class level** (shared across all instances). Never create multiple `ServoKit` objects.
- `Movements` composes `TrunkController` calls into recognizable gestures.
- `Animatronic` pairs a `Movements` coroutine with `AudioPlayer` (file playback) or `AudioStreamer` (live mic) to synchronize gesture and audio.
- `ConcurrentMovements` is a thread-based alternative to `Movements` for gestures that need `ThreadPoolExecutor` parallelism (e.g. `facePalm`).
- `driver.py` is a legacy sandbox — do not import from it or add production logic to it.

## Async vs. Threading

- `TrunkController` and `Movements` methods are `async` coroutines; use `asyncio.sleep` for all delays inside them.
- Call `asyncio.run()` at the top of the call stack only (in `Animatronic.runActionAndAudio` or CLI entry points). Never call it inside a running event loop.
- For concurrent gestures within the async layer, use `asyncio.create_task()` + `asyncio.gather()`.
- `ConcurrentMovements` uses `ThreadPoolExecutor` with blocking `time.sleep`. Do not mix `asyncio.sleep` into this path.

## Servo / Hardware Conventions

- All servo channels and names are defined in `constants.py`. Always use `constants.*` names — never hardcode channel numbers inline.
- All servos use a **270-degree actuation range**. Set `actuation_range = 270` on every servo when initializing `ServoKit`.
- Sweep angles one degree at a time in a loop with a configurable `delay` (seconds per step). This is intentional for smooth motion — do not replace with direct angle jumps.
- After any movement, return servos to a known resting position via `returnToStart` / `neckCenter` to prevent mechanical stress.
- `NECK_CENTER = 90` is the neutral pan angle; always return to it after head gestures.
- `moveByDir` (in `TrunkController`) calls `returnToStart` before and after the sweep — use it when the caller doesn't manage position explicitly. `moveByDirection` does not auto-return — use it when the caller controls the full sequence.

## Code Style

- `snake_case` for variables and functions; `PascalCase` for class names.
- Local angle bounds follow the pattern `SERVO_NAME_MIN` / `SERVO_NAME_MAX` as `ALL_CAPS` locals inside each method. Do not promote to class or module constants unless shared across multiple methods.
- Docstrings use Google-style format with `Args:` sections. Document the purpose of each parameter briefly.
- Use f-strings for all string interpolation in print/debug output.
- Debug output uses `print()` directly — there is no logging framework. Keep this consistent; do not introduce `logging` unless changing the whole codebase.

## Adding New Gestures

1. Add any new servo primitive to `TrunkController` as an `async` method if it doesn't already exist.
2. Compose the gesture in `Movements` using `TrunkController` calls.
3. To pair with audio: add the filename to `Animatronic.music` (with an index comment), add a method calling `self.runActionAndAudio(gesture, self.music[n])`, and register it in `action_map` in `main()`.
4. Register the gesture in `controller.py` if gesture-only CLI testing is needed.

## Audio

- Audio files live in `~/Music/` on the Pi. The local `audio/` folder mirrors them for development reference.
- File playback uses `AudioPlayer.play_audio_file(path)` — runs in a daemon thread started before `asyncio.run(gesture_coroutine())`. The thread is joined after the coroutine returns.
- Live mic passthrough uses `AudioStreamer.start()` / `AudioStreamer.stop()` — manages its own PyAudio stream lifecycle. Call `start()` before the gesture and `stop()` after.
- Both classes drive the jaw motor (`MOUTH_MOTOR_PIN`) from the audio amplitude on each chunk. Do not run both simultaneously on the same motor.

## Running

- Full routines require root: `sudo /usr/bin/python3 animatronic.py --action=<name>`
- Gesture-only testing (no audio): `python3 controller.py --action=<name>`
- Thread-based gesture demo: `python3 concurrentMovements.py` (has `__main__` guard)
