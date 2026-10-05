---
inclusion: always
---

# Animation Vocabulary — animatronic-v2

This is the shared vocabulary for how the animatronic moves and performs. Use
these terms with the exact meanings below in all code, comments, commit
messages, and discussion of behavior. Do not invent synonyms or reuse a term
for a different concept.

The composition levels build on one another:

```
Gesture  →  Gestures  →  Routine  →  Act
(motion)    (sequence)   (+audio)    (composition)

Stream  = live mic passthrough (audio-driven jaw, gestures allowed)
Mode    = a continuous background loop (Mic stream, Sleep, Awake, Tracking, or Scan)
```

Each term maps onto a specific layer of the architecture:

| Term    | Implemented by |
|---------|----------------|
| Gesture | a `Movements` coroutine |
| Routine | an `Animatronic` action (gesture + audio, often via the Performance Framework) |
| Stream  | the live mic path owned by `AudioStreamer` / `micwebcontroller.py` |

## Core rules

- A **Gesture** carries no audio and never touches the jaw/audio path, so
  Gesture(s) are always safe to layer over a live mic stream.
- A **Routine** or **Act** owns audio and the jaw motor, so starting one
  **interrupts** the live mic stream. The two cannot own the jaw/audio path at
  the same time.
- Clamp motion through `SAFE_LIMITS` and keep concurrent joints on **disjoint**
  servo channels (see the Concurrency rule under Gesture).

## Gesture

A **Gesture** is a single coordinated servo movement with **no audio** — a
named, self-contained piece of choreography (one `Movements` coroutine, e.g.
`wave`, `yawn_cover`, `face_palm`, `menacing_reach`) that drives one or more
joints and returns them toward rest.

- **Testable**: hardware-testable on its own via
  `src/controller.py --action=<name>`.
- **Channels**: a Gesture owns a specific set of servo channels (arm channels
  4–7, head channels 0–1).
- **Concurrency**: coordinate concurrent joints within a gesture using
  `move_to(...)` / `asyncio.gather()`, but only over **disjoint** channels.
- **Non-interrupting**: a Gesture does **not** interrupt a live mic stream.

### "Randomize within range with centering"

Several gestures use the `randomized_centering_move` primitive to make motion
look organic instead of metronomic: it keeps a joint moving **unpredictably but
safely** around a nominal center, without ever repeating its current position.

The joint oscillates inside a band `[center - half_range, center + half_range]`
with three logical positions:

- `LT` = `center - half_range` (low)
- `CENTER` = `center`
- `RT` = `center + half_range` (high)

Each call classifies the joint's **current** logical position (nearest of the
three; ties → `CENTER`) and randomly transitions to one of the **two other**
positions — so it always moves and never re-selects where it already is:

```
from RT     → {LT, CENTER}
from LT     → {RT, CENTER}
from CENTER → {RT, LT}
```

The chosen destination gets endpoint **jitter** of
`± (jitter_pct × half_range)` degrees, is **clamped** back into the band, and
is written through the normal `SAFE_LIMITS` clamp. In short, the primitive
guarantees:

- **Centering** — motion always trends back around a known-safe center, so the
  joint never drifts to an extreme or accumulates offset over a long loop.
- **Randomize within range** — timing (`steps`, `delay`) and endpoint jitter
  vary per call so repeated swings look natural while staying inside a bounded,
  safe band.
- **Deterministic when needed** — all randomness comes from the shared `random`
  module, so `random.seed(x)` reproduces an identical command sequence
  (required for the phased-vs-standalone equivalence tests).

Per-gesture `state_attr` keeps each joint's transition state separate, so two
gestures sharing the primitive don't interfere.

## Gestures

**Gestures** (plural) means **multiple gestures combined sequentially** — one
gesture runs to completion, then the next begins. It is a chained sequence of
individual Gestures, still with **no audio**.

- Runs gesture-by-gesture in order.
- Like a single Gesture, a sequence does **not** interrupt a live mic stream and
  is safe to run while the stream is live.

## Routine

A **Routine** combines one or more Gestures with **audio**, run either:

- **synchronously** — gesture then audio, via `run_action_and_audio`; or
- **concurrently** — audio-synced motion via the Performance Framework (e.g.
  `blah`, `brains`, `hypnotic`, `snore`).

Rules:

- A Routine is an `Animatronic` action dispatched by name through `action_map`.
- Because it plays audio and drives the jaw motor, a Routine **interrupts the
  live mic stream** — the stream and a Routine cannot own the jaw/audio path at
  the same time.

## Act

An **Act** combines **multiple Routines into a larger composition** — the
highest composition level, sequencing several audio-backed Routines into one
performance. Because it is built from Routines (which carry audio), an Act
**interrupts the live mic stream**.

## Stream

A **Stream** is the **live microphone passthrough** — `AudioStreamer` applying
effects and driving the jaw motor from mic input, owned by
`micwebcontroller.py` and toggled from the voice-FX dashboard.

- **Gesture(s) can run during a live stream.** They carry no audio and don't
  touch the jaw/audio path, so they layer safely on top.
- **Routines and Acts cannot run during a live stream.** They own audio and the
  jaw motor, so starting one interrupts the stream (see Mode).

## Mode

A **Mode** is a background behavior that **runs continuously until
interrupted**. Five modes exist — Mic stream, Sleep, Awake, Tracking, and Scan.

The five differ in whether they drive audio and which servo lock they hold,
which in turn decides what can run alongside them:

| Mode | Audio / jaw? | Servo lock held | Interrupts live mic? |
|------|--------------|-----------------|----------------------|
| Mic stream | Yes (mic) | none | — (is the stream) |
| Sleep | No | whole-robot `servo_lock()` | No |
| Awake | Yes (runs Routines) | whole-robot `servo_lock()` | Yes |
| Tracking | No | **Neck_Group only** (channels 0–1) | No |
| Scan | Yes (arm-only responder may run a Routine) | whole-robot `servo_lock()` | Yes |

Because Tracking owns only the Neck_Group, a disjoint arm-only Gesture (channels
4–7) may run concurrently with it; the other servo-driving Modes hold the whole
robot. The mode definitions follow.

### Mic stream mode

Continuously runs the live mic passthrough (see Stream).

- **Interrupted by**: a Routine, an Act, or turning the stream off on the
  voice-FX dashboard.
- Gestures may run over the top without interrupting it.

### Sleep mode

Continuously runs a resting/idle behavior until a sensor interrupts it. Like
Awake mode, it is **filler**: idle behavior that fills the time until a more
deliberate action is wanted.

- **Interrupted by**: the HC-SR04 ultrasonic range sensor
  (`src/range_sensor.py`) — `ApproachDetector` detects an object approaching
  within the range gate (`object_within` / consecutive closer readings).
- Sleep mode can also be configured with a **timeout**. When set, the timeout
  elapsing is itself the interruption signal — no sensor is required.
- **Pressing a web action button** also interrupts it — requesting any
  Routine/Movement from the control panel makes the mode wind down and yield so
  the requested action can run (implemented via `nap_signal` / the mode's
  cross-process stop signal).
- An interruption **can trigger a response**, e.g.:
  - **snoring → startle response**
  - **inactive → look around / wave response**

### Awake mode

Continuously runs active behavior — the animatronic performs Routines (e.g.
`patrol`, and others **TBD**) on a loop rather than resting — until interrupted.
Like Sleep mode, it is **filler**: idle-but-alive behavior that fills the time
until a more deliberate action is wanted.

- **Interrupted by**:
  - a **timeout** (the elapsing is itself the interruption signal, as in Sleep
    mode);
  - the **HC-SR04 range sensor** (`src/range_sensor.py` / `ApproachDetector`),
    as in Sleep mode; or
  - **pressing a web action button** — requesting any Routine/Movement from the
    control panel arouses Awake mode: it winds down and yields so the requested
    action can run. This mirrors how the web app preempts the napping Mode (see
    `nap_signal` / the mode's cross-process stop signal).
- Because the Routines it runs own audio and the jaw motor, Awake mode
  **interrupts the live mic stream** (like any Routine — see Stream/Mode) and
  cannot share the jaw/audio path with it.
- The conceptual opposite of Sleep mode: Sleep idles until roused, Awake is
  actively performing until it winds down (timeout) or is roused to a different
  behavior (sensor, or an operator's web button).

### Tracking mode

Continuously pans/tilts the neck to follow a detected person, reading
detections from the non-root Camera_Service (`src/camera_service.py`) and
driving only the neck. Launched via `animatronic.py --action=tracking`.

- **Owns only the Neck_Group** (channels 0–1): it acquires the Neck_Group lock,
  **not** the whole-robot `servo_lock()`. Because it leaves the Arm_Group
  (channels 4–7) free, a disjoint arm-only Gesture may run concurrently.
- **No audio / jaw.** Tracking is pure neck motion, so it does **not** interrupt
  the live mic stream — unlike Awake/Scan, it carries no audio.
- **Interrupted by**:
  - a **Scan_Sweep reacquire timeout** (the person is lost and not reacquired
    within the window); or
  - **pressing a web action button / stop** — the cross-process stop signal
    (`nap_signal`) winds it down, as with Sleep and Awake.
- A detection may yield a **pending Routine trigger** (via
  `detection_routine_map.py`), but Tracking never dispatches it itself: the
  trigger is dispatched only **after** Tracking releases the Neck_Group, so a
  Routine and Tracking never own the neck at the same time.

### Scan mode

Continuously runs the neck tracker **plus** a concurrent arm-only responder
that can perform a Routine (so it may drive the whole arm + jaw/audio). Launched
via `animatronic.py --action=scan`.

- **Owns the whole robot**: unlike Tracking, it holds the whole-robot
  `servo_lock()` for its entire run, because the responder may drive the arm and
  audio.
- **Carries audio** (the responder's Routine), so Scan **interrupts the live
  mic stream** like Awake and any Routine.
- **Interrupted by**:
  - a **timeout** (wind-down after `--scan-timeout-min` minutes, clamped
    1–120); or
  - **pressing a web action button / stop** — the cross-process stop signal
    (`nap_signal`) winds it down.

## Quick Reference

| Level    | Audio?    | Interrupts live mic stream? | Can run during live stream? |
|----------|-----------|-----------------------------|-----------------------------|
| Gesture  | No        | No                          | Yes                         |
| Gestures | No        | No                          | Yes                         |
| Routine  | Yes       | Yes                         | No                          |
| Act      | Yes       | Yes                         | No                          |
| Stream   | Yes (mic) | —                           | (is the stream)             |
