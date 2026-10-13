---
inclusion: fileMatch
fileMatchPattern: "src/{micwebcontroller,audio_streamer,audio_player}.py"
---

# Live-Audio / Voice-FX Troubleshooting — animatronic-v2

Hard-won diagnostics for the LIVE mic path (`micwebcontroller.py` duplex
stream, `audio_streamer.py`, `audio_player.py` jaw sync). The live mic uses a
real-time PortAudio callback on a shared duplex ALSA device on the Pi. That
callback has a HARD deadline, and most "mic went silent / crackly / wedged"
bugs trace to the same handful of causes. Read this BEFORE theorizing — it
exists because the voice-FX-silence bug cost a lot of effort chasing
device-independent proxies that passed tests but never touched the real
hardware fault.

## Diagnose FIRST: read the PortAudio `Status:` line

`audio_callback` prints `Status: <n>` when PortAudio reports a non-zero status
flag. **`<n>` is a BITMASK — decode it before anything else.** It usually names
the fault outright and saves a long hunt.

| Bit | Value | Flag | Meaning |
|-----|-------|------|---------|
| 0 | 1 | `paInputUnderflow` | input data was lost |
| 1 | 2 | `paInputOverflow` | input ring overflowed (capture not drained) |
| 2 | 4 | `paOutputUnderflow` | output ring starved — **the callback missed its deadline** |
| 3 | 8 | `paOutputOverflow` | output ring overflowed |
| 4 | 16 | `paPrimingOutput` | priming (benign, at start) |

Common composite: **`Status: 6` = 4 | 2 = `paOutputUnderflow | paInputOverflow`**
printed EVERY frame = the duplex stream has WEDGED. The output starved while the
input backed up; on a shared duplex ALSA device the link does NOT self-recover,
so the mic goes permanently silent until the stream is reopened or the process
restarts. If you see repeating `Status: 6` (or any `& 4`), the callback is being
delayed past the ~21 ms CHUNK deadline — go to "Real-time callback rules" below.
Values are confirmable at runtime: `python -c "import pyaudio as p; print(p.paOutputUnderflow)"`.

## The CHUNK deadline (the budget everything must fit)

One callback must finish well under one buffer period:

```
CHUNK / RATE = 1024 / 48000 Hz ≈ 21.3 ms
```

Everything the callback does per frame — `talk()` (jaw envelope + GPIO),
`apply_effects()` (the whole FX chain), and any pending reset — must fit inside
that with margin, on the PI (slower than any dev box). If it doesn't, the output
ring underruns and the stream wedges (`Status: 6`).

## Real-time callback rules (the invariants a fix must preserve)

The `audio_callback` in `micwebcontroller.py` runs on PortAudio's own
real-time thread. It MUST NOT:

1. **Acquire any lock a non-real-time (Flask request) thread can hold.** Under
   the GIL a lock hand-off during an FX change delays the callback past the
   deadline and wedges the duplex stream. The live path is deliberately
   **lock-free**: `effects_config` is only ever REBOUND wholesale (never mutated
   in place), the callback reads it with a single atomic reference read per
   frame (no torn read), and the per-frame DSP state (echo deque, reverb buffer,
   phases) is owned EXCLUSIVELY by the callback thread. A style change on the
   request thread bumps `_dsp_reset_generation`; the callback performs the reset
   itself at the top of a frame. There is intentionally NO `_dsp_lock` — do not
   reintroduce one on this path.
2. **Do blocking I/O.** No `print()` per frame, no file/network/stdout I/O
   inside the callback or inside `talk()`/`apply_effects()`. A single slow
   stdout write under load already blows the 21 ms budget. Rate-limit any
   diagnostic and keep it off the hot path (see `_note_stream_status` /
   `_note_callback_dsp_error`).
3. **Run per-sample Python loops in a DSP effect.** `for i in range(len(audio))`
   over 1024 samples is ~20 ms+ by itself — over budget on its own. Keep every
   `_fx_*` function VECTORISED (numpy whole-array ops). `_fx_reverb` is the
   cautionary tale: a per-sample feedback loop cost ~22 ms/frame and wedged any
   reverb-enabled preset until it was vectorised (a fixed delay line vectorises
   exactly as a gather/scatter because the delay buffer, 9600, is longer than
   one block, 1024). If you add an effect, budget it against the 21 ms deadline.

Single-owner consequence: because the lock is gone, `apply_effects()` /
`_fx_*` / the echo+reverb buffers are safe for ONE caller only (the callback).
Never call them concurrently from another thread. The request thread touches
`effects_config` (atomic rebind) and the reset generation — nothing else.

## Why device-independent tests are NOT enough here

The wedge lives in the ALSA/PortAudio duplex layer, not in Python. A test that
drives `apply_effects` with stub audio can confirm DSP math and even lock
behavior, but it CANNOT reproduce the hardware underrun. So:

- Treat a green suite as necessary, not sufficient, for live-audio changes.
- The real confirmation is on the Pi: start the mic stream and change the voice
  FX (style preset AND single effect) BEFORE, DURING, and AFTER the stream, and
  confirm the passthrough stays audible with no `Status: 6` spam and no restart
  needed. Per project hardware policy the operator runs this; agents do not run
  `SERVO_SIM`/collision sims, and there is no sim for the duplex audio wedge.
- When a live-audio bug "passes all tests but still fails on hardware", suspect
  a real-time-deadline / duplex-wedge cause (this doc), not the DSP math.

## Recovery

A wedged duplex stream never self-heals. `micwebcontroller.py` detects a
PERSISTENT output underflow (`_note_stream_status` → `_stream_needs_recovery`)
and the stream thread reopens the stream to re-prime ALSA without dropping
`is_streaming`. If you change the stream lifecycle, preserve: (a) the
`is_streaming` short-circuit as the FIRST thing in the callback, (b) prompt
thread exit on stop (set the recovery event so the watch wait wakes), and
(c) the clean DSP-state reset across a reopen.

## Related ALSA / device notes

- Expected device indices: input (capture) and output (playback) are set at the
  top of `micwebcontroller.py` (`input_device_index` / `output_device_index`).
  The noisy `ALSA lib pcm_*` / JACK lines at stream start are ALSA probing
  nonexistent PCMs and are HARMLESS — do not chase them; they are not the bug.
- Card indices and the `sysdefault:CARD=Device` wiring live in
  `src/config/alsa/` and `project-overview.md`. Verify on the Pi with
  `aplay -l` / `arecord -l`.
