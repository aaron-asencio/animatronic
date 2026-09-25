---
inclusion: fileMatch
fileMatchPattern: "**/*.py"
---

# Python Guidelines — animatronic-v2

This project controls a Raspberry Pi-based animatronic figure using servo motors (Adafruit PCA9685 / ServoKit) and synchronized audio (`AudioPlayer` for files, `AudioStreamer` for live mic). All runtime scripts execute as root on the Pi for I2C/GPIO access. All modules live under `src/`.

Use `constants.*` names and the safety helpers described below. Never hardcode servo channel numbers or write to `kit.servo[n].angle` directly.

## Architecture

The codebase is layered. Higher layers call lower ones — never the reverse.

```
animatronic.py / controller.py      ← top-level: named gesture+audio routines / gesture-only CLI
    ├── performance.py              ← reusable runner for audio-synced concurrent gestures
    └── movements.py                ← mid-level: named multi-joint async gestures
            └── trunkcontroller.py  ← low-level: individual servo primitives (async)
                    └── adafruit_servokit / PCA9685 hardware
concurrentMovements.py              ← alternative mid-level: thread-based gestures (ThreadPoolExecutor)
constants.py                        ← channel assignments, SAFE_LIMITS, REST_POSITIONS
```

- `TrunkController` owns the `ServoKit` instance at **class level** — shared across all instances. Never construct a second `ServoKit`.
- `Movements` owns a class-level `trunkController` and composes `TrunkController` primitives into named gestures. Arm gestures use channels 4–7; head gestures use channels 0–1. Any arm gesture may run concurrently with any head gesture (disjoint channels).
- `Animatronic` (top level) maps named routines to a gesture + an audio track. Most routines call `run_action_and_audio()`; audio-synced concurrent routines use the Performance Framework (see below).
- `ConcurrentMovements` is a thread-based alternative to `Movements` (blocking `time.sleep`, `ThreadPoolExecutor`) for gestures like `face_palm`. Note `Movements` also has its own async `face_palm`.

## Safety Model (read before changing any movement code)

A servo driven past its mechanical stop stalls, draws locked-rotor current, overheats, and can burn out — a fire hazard. These invariants are non-negotiable:

- **Every servo write goes through `TrunkController.set_angle()`**, which clamps via `clamp_angle()` to the channel's `constants.SAFE_LIMITS` (the mechanism's safe range, not the servo's 0–270 electrical range). Never assign `kit.servo[n].angle` directly.
- **Sweep endpoints are also clamped** in `move`, `move_by_dir`, `move_by_direction`, and `return_to_start`, so a loop never iterates into a jam.
- **Return to a safe rest after every routine and after any error.** `TrunkController.return_to_rest()` drives every channel to `constants.REST_POSITIONS`; each rest angle must stay within `SAFE_LIMITS`. `Animatronic.run_action_and_audio` calls `_safe_rest()` in its error path.
- **Widening a limit requires operator-verified safety.** Only use `TrunkController.verified_pose_override(overrides)` (a context manager) for a specific pose bench-confirmed safe. It is still bounded by the electrical range `[0, 270]`, and it must be scoped so the widened clamp never leaks past the routine (e.g. `snore` opens `Movements._SLEEP_OVERRIDE` for the lead-in→loop→return span, then releases it).
- **Per-axis limits do not catch multi-axis collisions.** `constants.FORBIDDEN_COMBINATIONS` hard-guards measured danger zones (e.g. hand-to-face). Respect these; a full 3D collision model is planned but not complete.
- **Only one routine drives the servos at a time.** Gesture routines run inside `servo_lock()` (an `fcntl` cross-process mutex); concurrent routines can stall a servo. Fail fast with `ServoBusyError` if already held. Audio-only `mic` mode does not take the lock.
- **`SERVO_SIM=1`** runs with a fake kit that logs every write instead of touching I2C — use it to verify flow and commanded angles with zero hardware risk.

## Servo / Hardware Conventions

- All channels, `SAFE_LIMITS`, and `REST_POSITIONS` are defined in `constants.py`. See the AXIS DIRECTION REFERENCE there for how commanded angle maps to physical motion.
- Servos use a **270-degree actuation range** (`SERVO_MAX_ANGLE = 270`). `TrunkController` configures `actuation_range` lazily and **only on the channels in `constants.servos`** — configuring all 16 caused unused servos to twitch on startup. Configuration writes `actuation_range` only, never `.angle`, so setup never commands motion.
- Sweep angles one degree at a time in a loop with a configurable `delay` (seconds per step) for smooth motion. Do not replace with direct angle jumps. For coordinated multi-joint motion that arrives together, use `move_to(targets, ...)` (per-joint interpolation with smoothstep easing and optional staggered starts).
- `NECK_CENTER = 90` is the neutral neck **pan** angle; return there after head gestures (`neck_center()`). Neck tilt's level angle is also ~90 (see `REST_POSITIONS`).
- `move_by_dir` calls `return_to_start` before and after the sweep — use it when the caller doesn't manage position. `move_by_direction` does not auto-return — use it when the caller controls the full sequence.
- `health_check()` reads back the PCA9685 PWM frequency (~50 Hz) to detect a brownout (loose VCC) and restores it; it runs once on first `TrunkController` construction.

## Async vs. Threading

- `TrunkController` and `Movements` methods are `async` coroutines; use `asyncio.sleep` for all delays inside them.
- Call `asyncio.run()` only at the top of the call stack (in `Animatronic` routine methods / CLI entry points). Never call it inside a running event loop.
- For concurrent gestures in the async layer, use `asyncio.create_task()` + `asyncio.gather()`, or the Performance Framework.
- `ConcurrentMovements` uses `ThreadPoolExecutor` with blocking `time.sleep`. Do not mix `asyncio.sleep` into this path.

## Performance Framework (`performance.py`)

Audio-synced concurrent routines (`blah`, `brains`, `hypnotic`, `snore`) are built declaratively instead of via `run_action_and_audio`:

- A `PerformanceDefinition` names an `audio_file`, an optional `GateSpec`, optional `player_options` (e.g. `{"drive_jaw": False}`), and one or more `PerformanceStep`s.
- A `PerformanceStep` holds a `ConcurrentGroup` of `MovementSpec`s. Each `MovementSpec` declares its `owned_channels` (which must be disjoint across the group) plus `lead_in` / `loop_body` / `do_return` phase callables from a single shared `Movements` instance.
- Gating: exactly one movement may set `supplies_gate=True`; its `lead_in` completing opens the audio gate (so audio starts a known offset after the routine begins). `gate=None` starts audio at t=0.
- `loop_for_audio=True` repeats loop bodies while playback is active; `stop_loop_lead_seconds` stops starting new loops that many seconds before audio ends so the movement retracts in time.
- Run with `asyncio.run(PerformanceRunner(defn, mv, audio_dir).run())`. The runner sweeps residual channels home on completion or failure, and cancels any ambient task (e.g. `_blink_eyes`).

## Adding a New Gesture / Routine

1. Add any missing servo primitive to `TrunkController` as an `async` method (writing only through `set_angle`).
2. Compose the gesture in `Movements` from `TrunkController` calls.
3. To pair with audio the simple way: add the filename to `Animatronic.music` (with its index comment), add a private `_do_*` coroutine and a public routine method calling `self.run_action_and_audio("_do_*", self.music[n])`, then register it in `action_map` in `main()`.
4. For audio-synced concurrent motion, define a `PerformanceDefinition` in the routine method and run it via `PerformanceRunner` (pattern: see `brains` / `blah`).
5. `action_map` keys are `camelCase` action names mapping to `snake_case` methods; keep that convention. Only names in `action_map` are dispatchable — this allowlist is a security boundary, never `getattr`/`eval` on raw `--action`.
6. Register a gesture-only entry in `src/controller.py` for hardware testing without audio.

## Audio

- Audio files live in the invoking user's `~/Music/` on the Pi, resolved by `Animatronic._resolve_audio_dir()` (prefers `SUDO_USER`'s home under sudo; override with `ANIMATRONIC_AUDIO_DIR`). The local `audio/` folder mirrors them for development reference.
- File playback: `AudioPlayer.play_audio_file(path)` runs in a daemon thread started before the gesture coroutine and joined after it returns.
- Live mic passthrough: `AudioStreamer.start()` / `.stop()` manages its own PyAudio stream lifecycle.
- Both classes drive the jaw motor (`constants.MOUTH_MOTOR_PIN`) from audio amplitude per chunk. Do not run both on the same motor simultaneously. `AudioPlayer` also drives the eye LED (`EYE_LIGHT_PIN`) from the envelope unless `player_options` disables it.

## Code Style

- `snake_case` for variables, functions, and methods; `PascalCase` for classes. (Method names are snake_case, e.g. `return_to_start`, `neck_center`, `move_by_dir`.)
- Local angle bounds use `SERVO_NAME_MIN` / `SERVO_NAME_MAX` as `ALL_CAPS` locals inside a method. Do not promote to module/class constants unless shared across methods — the enforced safe bounds live in `constants.SAFE_LIMITS`.
- Docstrings use Google-style format with `Args:` (and `Returns:` / `Yields:` where relevant). Briefly document each parameter.
- Use f-strings for interpolation. Debug output uses `print()` directly — there is no logging framework; keep it consistent and do not introduce `logging` piecemeal.

## Running

- Full routines (require root): `sudo /usr/bin/python3 src/animatronic.py --action=<name>` (e.g. `startParty`, `waiting`, `blah`, `brains`).
- Gesture-only testing (no audio): `python3 src/controller.py --action=<name>`.
- Dry run without hardware: prefix any command with `SERVO_SIM=1`.
- Thread-based gesture demo: `python3 src/concurrentMovements.py` (has a `__main__` guard).
