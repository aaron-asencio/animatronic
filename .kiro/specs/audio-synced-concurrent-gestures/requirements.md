# Requirements Document

## Introduction

This feature delivers a **reusable performance framework** for the animatronic
figure. The framework's job is to take simple movements and a spoken-dialog
audio track and turn them into a lifelike performance. Movements can be composed
two ways: **concurrently** (several movements running together) and
**sequentially** (movements chained one after another), and the two compose —
a performance is an ordered sequence of steps where each step runs a group of
movements concurrently. The composed movements **loop for the duration of the
audio**, with the dialog optionally **gated** to start when a movement reaches a
chosen physical state. This is expected to be the primary way performances are
built going forward, so the framework — not any single routine — is the
deliverable.

The design goal is **composition over authoring**. Simple movements
(`menacing_reach`, `look_around_random`, waves, nods, scans, etc.) already
exist and are validated. A performance author should be able to declare "run
these movements together and/or in this order, over this audio, gated like so"
and get a correct, safe, lifelike result — without writing new choreography,
without duplicating audio/loop/sequencing plumbing, and without hand-counting
repetitions. Adding a new performance later should mean naming its ingredients
and their arrangement, not writing new coordination code.

Two timing concerns motivate the framework and are why the existing
timer-based `run_action_and_audio` is insufficient:

1. **Audio start can be gated on movement progress.** A movement may need to
   reach a physical state (e.g. the arm fully raised) before the dialog begins,
   so audio start is triggered by a movement condition rather than a fixed delay.
2. **Movement duration is driven by audio length.** The composed movements
   repeat until playback completes, so gesture duration follows the audio rather
   than a fixed count.

The **first concrete instance** exercising the framework is the **`brains`
routine**: a single-step performance running `menacing_reach` (arm) and
`look_around_random` (head) concurrently, played over `brains.wav` (~15.2 s).
The arm raises while the head begins scanning immediately; once the arm is fully
raised the dialog starts; the menace swing and head scan repeat until the audio
ends; then the figure returns to rest. `brains` is used throughout this document
as a worked example of the general framework, not as the scope of the work — the
requirements are written for the framework first and validated by this instance.

## Glossary

- **Performance_Framework**: The reusable system that composes Simple_Movements
  (concurrently and/or sequentially) over an Audio_Track — handling concurrency,
  sequencing, audio gating, looping for the audio duration, and return-to-rest —
  so that individual performances are declared rather than hand-coded. This is
  the primary deliverable.
- **Performance_Definition**: The declarative description of one performance: an
  ordered list of Performance_Steps, the Audio_Track, which movement (if any)
  supplies the Audio_Gate, and per-movement start timing. Adding a performance
  means writing a Performance_Definition, not new coordination logic.
- **Performance_Step**: One entry in a performance's ordered sequence. A step
  runs a Concurrent_Group and completes before the next step begins.
- **Concurrent_Group**: One or more Simple_Movements within a Performance_Step
  that run at the same time. A group of one is just a single movement.
- **Concurrent_Performance**: A running instance of a Performance_Definition —
  its Performance_Steps executed in order, each step's Concurrent_Group playing
  together, all looping for the Audio_Track's length.
- **Simple_Movement**: An existing `Movements` gesture that owns a known,
  disjoint set of servo channels (e.g. `menacing_reach` owns the arm channels;
  `look_around_random` owns the neck channels). Simple_Movements are the reusable
  building blocks the framework composes.
- **Performance_Phase**: One of the ordered stages of a movement within a step —
  typically **lead-in** (e.g. raise the arm), **loop body** (the repeatable
  motion), and **return** (e.g. lower the arm to rest).
- **Audio_Gate**: A condition that must be satisfied before dialog audio starts
  (e.g. "arm fully raised"). Signalled from movement code to the audio starter.
- **Audio_Track**: A single dialog WAV file played once via `AudioPlayer` in a
  background thread, spanning the whole performance; playback blocks that thread
  until the file ends.
- **Playback_Active**: The observable condition "audio is still playing",
  derived from the audio thread being alive / an explicit completion signal.
- **Loop_Until_Audio**: Repeating a movement's loop body while Playback_Active
  is true, checked between whole loop-body iterations.
- **Channel_Ownership**: The existing rule (see `movements.py`) that movements
  running concurrently MUST own disjoint servo channel sets.
- **brains_routine**: The first Concurrent_Performance instance, used as the
  worked example — a single step running `menacing_reach` + `look_around_random`
  concurrently over `brains.wav`.

## Requirements

### Requirement 1: Reusable performance framework (primary deliverable)

**User Story:** As a gesture author, I want a single reusable framework that
composes simple movements over a dialog track, so that I can build many
lifelike performances without re-implementing audio, sequencing, and looping
coordination each time.

#### Acceptance Criteria

1. The system SHALL provide one Performance_Framework that handles concurrency,
   sequencing, audio gating, Loop_Until_Audio, and return-to-rest for every
   performance.
2. The audio/loop/gate/sequencing coordination logic SHALL exist in exactly one
   place and SHALL NOT be duplicated per performance.
3. WHEN a new performance is added THEN it SHALL be expressible as a
   Performance_Definition (naming its Performance_Steps, Audio_Track, and gating)
   WITHOUT modifying the framework's coordination logic.
4. The framework SHALL treat the constituent Simple_Movements as opaque,
   reusable building blocks, making no assumptions specific to any one movement
   beyond its declared phases, owned channels, and optional gate signal.
5. WHERE the `brains_routine` is defined, it SHALL be one Performance_Definition
   consumed by the framework, demonstrating the general path rather than a
   bespoke code path.

### Requirement 2: Compose movements concurrently and sequentially

**User Story:** As a gesture author, I want to run movements together AND chain
movements one after another, so that I can build richer performances than a
single simultaneous group allows.

#### Acceptance Criteria

1. The framework SHALL support running two or more Simple_Movements concurrently
   within a Performance_Step (a Concurrent_Group).
2. The framework SHALL support arranging Performance_Steps in an ordered
   sequence, running each step to completion before starting the next
   (sequential chaining).
3. The framework SHALL support performances that combine both: a sequence of
   steps where any step may itself run a Concurrent_Group.
4. WHEN a Performance_Step completes THEN the framework SHALL begin the next step
   in order; WHEN the final step completes THEN the performance SHALL end.
5. WHERE a performance has a single step with a single Concurrent_Group (as
   `brains`), the framework SHALL run it as that one concurrent step.

### Requirement 3: Declaratively author a performance from its ingredients

**User Story:** As a gesture author, I want to declare a performance by naming
its steps, movements, audio, and gate, so that creating the next performance is
a small declaration rather than new coordination code.

#### Acceptance Criteria

1. A Performance_Definition SHALL specify: an ordered list of Performance_Steps
   (each a Concurrent_Group of Simple_Movements), the Audio_Track, which movement
   (if any) supplies the Audio_Gate, and each movement's start timing (time zero
   vs. gated).
2. The framework SHALL accept any Concurrent_Group whose members' owned channels
   are mutually disjoint (Channel_Ownership).
3. WHERE two or more performances reuse the same Simple_Movement, they SHALL
   share the one movement implementation rather than copies.
4. The set of Simple_Movements composable by the framework SHALL be open —
   adding a new Simple_Movement (with lead-in/loop/return phases and declared
   channels) SHALL make it usable in performances without framework changes.

### Requirement 4: Combine simple movements to run concurrently

**User Story:** As a gesture author, I want the movements in a concurrent group
to run at the same time, so that combined motion within a step looks lifelike.

#### Acceptance Criteria

1. WHEN a Performance_Step runs a Concurrent_Group THEN the framework SHALL play
   its Simple_Movements concurrently.
2. The framework SHALL require that a Concurrent_Group's Simple_Movements own
   disjoint servo channel sets (Channel_Ownership), and SHALL treat any overlap
   as an authoring error rather than driving a channel from two movements at once.
3. WHERE the `brains_routine` is run, the framework SHALL combine
   `menacing_reach` (arm channels) with `look_around_random` (neck channels) in
   one concurrent step.

### Requirement 5: Single dialog track spans the whole performance

**User Story:** As a gesture author, I want one dialog track to play across the
entire performance, so that the figure speaks a single line while its steps play
out underneath.

#### Acceptance Criteria

1. The framework SHALL play exactly one Audio_Track per performance, spanning all
   Performance_Steps from the performance's audio start to the track's end.
2. The framework SHALL NOT restart or switch the Audio_Track between
   Performance_Steps.
3. WHILE Playback_Active is true, the framework SHALL keep the performance's steps
   running (looping per Requirement 7) rather than ending early.
4. This spec SHALL NOT include per-step audio tracks; that is deferred to future
   work.

### Requirement 6: Gate dialog audio on movement progress

**User Story:** As a gesture author, I want the dialog to start only once a
movement reaches a chosen physical state, so that the audio lands at the right
dramatic moment.

#### Acceptance Criteria

1. WHEN a Performance_Definition specifies an Audio_Gate THEN the framework SHALL
   NOT begin Audio_Track playback until the Audio_Gate condition is satisfied.
2. WHEN the Audio_Gate condition is satisfied THEN the framework SHALL begin
   Audio_Track playback promptly (without an additional fixed idle delay).
3. WHERE a movement is not gated, the framework SHALL start it at the beginning
   of the performance (time zero), concurrently with any gated movement's
   lead-in.
4. WHERE a Performance_Definition specifies no Audio_Gate, the framework SHALL
   start the Audio_Track at time zero.
5. WHERE the `brains_routine` is run, the Audio_Gate SHALL be "the arm is fully
   raised" (the `menacing_reach` reach-out phase has completed), and the head
   scan (ungated) SHALL start at time zero.

### Requirement 7: Loop movements for the audio duration

**User Story:** As a gesture author, I want the composed movements to keep
repeating until the dialog finishes, so that the figure stays animated for the
whole line without me counting repetitions.

#### Acceptance Criteria

1. WHILE Playback_Active is true, the framework SHALL repeat each looping
   movement's loop body (Loop_Until_Audio).
2. The framework SHALL check Playback_Active between whole loop-body iterations,
   so an in-progress movement completes cleanly rather than being cut off
   mid-move.
3. WHEN Playback_Active becomes false THEN the framework SHALL stop starting new
   loop-body iterations and proceed to each movement's return phase.
4. WHERE a performance chains multiple Performance_Steps, the framework SHALL
   apply Loop_Until_Audio according to the Performance_Definition (e.g. looping a
   designated step for the audio duration) rather than assuming a single looping
   step.
5. WHERE the `brains_routine` is run, the arm's menace swing and the head's
   random scan SHALL each repeat until `brains.wav` finishes.

### Requirement 8: Return to rest after the performance

**User Story:** As an operator, I want the figure to return to a safe resting
pose after any performance, so that no servo is left under load and the figure
is ready for the next routine.

#### Acceptance Criteria

1. WHEN the loop phase ends THEN the framework SHALL run each movement's return
   phase to bring all driven channels to their resting positions.
2. IF a movement raised a `verified_pose_override` (as `menacing_reach` does for
   its sub-floor shoulder tilt) THEN the override SHALL be scoped so it is
   released once that movement's return phase completes.
3. IF a gesture coroutine raises an exception THEN the framework SHALL drive all
   servos to safe resting positions (preserving the existing `_safe_rest`
   recovery behavior).
4. WHERE the `brains_routine` is run, the framework SHALL retract the arm to rest
   AND return the neck to center after the audio completes.

### Requirement 9: Movement decomposition into reusable phases

**User Story:** As a developer, I want looping movements exposed as
lead-in / loop-body / return phases with no embedded audio logic, so that the
framework can time any movement generically.

#### Acceptance Criteria

1. A Simple_Movement usable in a performance SHALL expose its motion as
   separable Performance_Phases: an optional lead-in, a repeatable loop body,
   and a return, with a uniform shape the framework can drive generically.
2. Movement phase methods SHALL contain no audio-playback logic; audio
   coordination SHALL live only in the Performance_Framework.
3. The system SHALL expose `menacing_reach` as such phases (reach-out lead-in,
   single menace-swing loop body, retract return) while preserving its existing
   standalone behavior (reach, swing three times, retract).
4. The system SHALL expose `look_around_random`'s scanning as a repeatable loop
   body, reusing its existing per-glance random pan/tilt and dwell behavior.
5. WHERE a phase drives channels below a global `SAFE_LIMITS` floor that is
   operator-verified safe (e.g. `menacing_reach` shoulder tilt to 25), the phase
   SHALL apply the same `verified_pose_override` used by the standalone gesture.

### Requirement 10: Collision safety of the composed motion

**User Story:** As an operator, I want any composed motion — concurrent or
sequential — to remain collision-free, so that composing and repeating movements
cannot damage the figure.

#### Acceptance Criteria

1. Within a Concurrent_Group the framework SHALL rely on Channel_Ownership
   (disjoint channels) so that concurrent execution introduces no channel
   contention.
2. Combined-motion keyframes SHALL be validated collision-free using the existing
   kinematic collision model.
3. WHERE the `brains_routine` runs the neck and arm concurrently, validation
   SHALL confirm the combined extreme poses (arm out + neck at scan extremes) are
   SAFE.
4. WHERE steps run sequentially, the framework SHALL ensure each step ends in a
   pose from which the next step's lead-in is safe (e.g. returning driven
   channels to a known rest between steps as needed).

### Requirement 11: Routine registration and invocation

**User Story:** As an operator, I want to trigger performances the same way as
existing routines, so that new performances fit the current control surface.

#### Acceptance Criteria

1. The framework SHALL let a Performance_Definition be registered as a named
   action invokable through the existing `animatronic.py` action map.
2. WHERE the `brains_routine` is added, the system SHALL register it as a named
   action AND add `brains.wav` to the audio track list used by `animatronic.py`.
3. WHERE the custom dashboard lists routines, a new performance SHALL be
   registerable through the same mechanism as existing routines (no Node-RED).
4. Performances SHALL run under the existing root/servo-lock execution model used
   by other audio routines.
