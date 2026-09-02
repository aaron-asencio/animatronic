---
inclusion: always
---

# Security Guidelines — animatronic-v2

This project runs as root on a Raspberry Pi with direct hardware access (I2C, GPIO, audio). The threat model is primarily around command injection, unintended hardware actuation, and unsafe subprocess usage.

## Subprocess and Shell Commands

- `animatronic.py` no longer uses `subprocess` for audio — audio runs via `AudioPlayer` (file) or `AudioStreamer` (mic) in-process. There are no shell commands to inject into for the audio path.
- Never interpolate user-supplied values directly into shell command strings. If a file path or action name originates from outside the process (CLI args, HTTP request, Node-RED message), validate it against an explicit allowlist before use.
- If any new subprocess calls are introduced, prefer `shell=False` with a list of arguments. Reserve `shell=True` only when absolutely required by the called program.
- Always quote file paths in shell command strings to handle spaces: use `shlex.quote()`.

## CLI Argument Handling

- `animatronic.py` and `controller.py` accept `--action` from the command line. The value is dispatched through an explicit `dict`/`if-elif` allowlist — this pattern must be preserved for any new entry points.
- Never pass raw `args.action` to `getattr()`, `eval()`, `exec()`, or shell commands without allowlist validation first.
- If a new HTTP or socket interface is added (e.g. to trigger actions remotely), apply the same allowlist check before dispatching to `Animatronic` methods.

## Root Execution

- Scripts run as root for I2C/GPIO access. Minimize what runs at that privilege level — hardware initialization and PyAudio device access are the only operations that require it.
- Do not add network-facing code (web servers, sockets) to scripts that run as root without explicit privilege separation.
- Do not write user-controlled data to the filesystem from a root process without path validation (e.g. no `open(user_input, 'w')`).

## Hardware Safety

- Sending a servo to an out-of-range angle can physically damage the mechanism. Always clamp angles to `[0, 270]` before writing to `kit.servo[n].angle`.
- After any movement sequence, return servos to their resting positions. Do not leave servos under active load — this causes motor heating and wear.
- The `ServoKit` instance is shared at class level across all instances of `TrunkController` and `ConcurrentMovements`. Concurrent writes to the same servo channel from multiple threads will produce undefined physical behavior — coordinate access carefully when using `ThreadPoolExecutor`.

## Configuration and Secrets

- Hardcoded paths (`~/Music/`) are acceptable for this embedded deployment but should be isolated in one place (`Animatronic.audio_dir`). Do not scatter Pi-specific paths throughout the codebase.
- If credentials or tokens are ever needed (e.g. for a remote trigger API), store them in environment variables or a secrets file outside the repo — never commit them.
- ALSA config files in `config/alsa/` contain hardware settings; treat them as infrastructure config and review changes carefully before deploying.

## Dependency Management

- Primary runtime dependencies are `adafruit-circuitpython-servokit`, `pyaudio`, and `numpy`. Pin to specific versions in `requirements.txt`.
- The `.venv` directory is present in the workspace; use it consistently so system Python packages don't bleed into the project environment.
