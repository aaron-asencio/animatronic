---
inclusion: fileMatch
fileMatchPattern: "src/{animatronic,movements,performance,webapp}.py"
---

# Audio Sequencing — animatronic-v2

How to wire the AUDIO side of a Routine: when audio starts relative to the
gesture (gating), and how to play more than one clip (follow-on, two-phase).
Motion authoring itself is covered by `authoring-gestures.md`; this file is
only about audio timing and track order, the part that keeps getting
re-derived from existing routines.

Terms (`Routine`, `Gesture`, `Stream`) are defined in
`animation-vocabulary.md`. A Routine owns audio + the jaw motor; a Gesture
never touches audio. Everything below is about Routines.

## Pick the mechanism first

| You need… | Use | Reference routine |
|-----------|-----|-------------------|
| One gesture + one clip, fixed or no lead-in | `run_action_and_audio(method, file, audio_delay=…)` | `yawn`, `snuckUp`, `awaken` |
| Concurrent gestures and/or audio-synced looping motion | Performance Framework (`PerformanceDefinition` + `PerformanceRunner`) | `blah`, `brains`, `clearThroat` |
| Audio gated until a gesture reaches a specific pose | Performance Framework, `supplies_gate=True` | `clearThroat`, `coughLong` |
| A second clip back-to-back after the first | Performance Framework, `followup_audio_files` | `hypnotic`, `burp` |
| Audio at t=0 but the ARM MOTION delayed a beat | Performance Framework, `gate=None` + a delayed lead-in (`yawn_cover_lead_in_delayed`) | `burp` |
| A clip to finish BEFORE the gesture starts | Two-phase routine (blocking `AudioPlayer`, then a Performance) | `fart` |

Rule of thumb: reach for `run_action_and_audio` only for the simple
one-gesture / one-clip case. Anything with concurrency, looping-to-audio,
pose-based gating, or multiple tracks belongs in the Performance Framework —
do not try to bolt those onto `run_action_and_audio`.

## Gating: when does audio start?

"Gating" delays audio start so the motion leads. There are three shapes; pick
by what the sound should line up with.

### 1. Ungated — audio at t=0

Motion and audio both start immediately. Framework: `gate=None`. Simple runner:
`audio_delay=0.0` (the default).

Use when the clip should begin the instant the routine does (e.g. `brains`,
`snuckUp`).

### 2. Fixed-delay gate — audio N seconds in

Audio starts a fixed offset after the routine begins, so a short lead-in of
motion plays first.

- **Simple runner**: `run_action_and_audio(method, file, audio_delay=0.3)` —
  the audio thread sleeps `audio_delay` then plays (see `yawn`, gated 300ms).
- **Framework**: one movement sets `supplies_gate=True` and its `lead_in`
  sleeps the fixed delay before returning; the framework opens the gate the
  instant that lead-in completes. The 250ms convention is
  `Movements._SN_GATE_DELAY` / `_MC_GATE_DELAY` / `_YC_GATE_SHORT` (see `blah`,
  `moreCandy`).

Use when a brief motion beat should precede the sound but you do NOT need the
hand to have arrived anywhere specific.

### 2b. Ungated audio, DELAYED motion — sound leads, arm follows

The inverse of a fixed-delay gate: instead of holding the audio, start the
audio at t=0 (`gate=None`) and delay the ARM so the sound plays first and the
hand comes up a beat later. The lead-in sleeps a fixed delay with the arm at
rest, then moves — and does NOT supply the gate (no movement does, since
`gate=None`).

- Cover-mouth variant: `yawn_cover_lead_in_delayed` sleeps
  `Movements._YC_MOTION_DELAY` (currently 0.5s) at rest, then centers the head
  and folds the hand fully up (awaited). Used by `burp` — the burp sound starts
  immediately and the hand rises to cover the mouth ~0.5s in.

Use when the SOUND is the event and the motion is a reaction to it (a burp
happens, THEN you cover your mouth), rather than the motion leading the sound.

### 3. Gate-until-settled — audio when the gesture reaches a pose

Audio is held until the gesture's `lead_in` fully completes its move, so the
sound lines up with the hand ARRIVING at a position. Framework only:
`supplies_gate=True` and the `lead_in` AWAITS its motion to completion before
returning.

Only ONE movement in a group may set `supplies_gate=True`.

### Cover-mouth lead-in variants — the full set

All four reuse yawn_cover's operator-verified hand-to-mouth pose and its
`verified_pose_override` (`Movements._YAWN_COVER_OVERRIDE`), the same
`yawn_cover_loop_body` (hold) and `yawn_cover_return` (lower + release
override). They differ ONLY in WHEN the audio starts relative to the fold.
Pick by what the sound should line up with — do not re-derive this from the
code:

| Lead-in | Audio timing | Supplies gate? | Used by |
|---------|-------------|----------------|---------|
| `yawn_cover_lead_in` | gate opens `_YC_GATE_LEAD` (~0.5s) BEFORE the fold settles — sound leads the final settle | yes | `clearThroat` |
| `yawn_cover_lead_in_settled` | gate opens AFTER the fold fully settles — "audio when the hand reaches final position" | yes | `coughLong`, `coughMedium`, `fart` phase 2 |
| `yawn_cover_lead_in_gated` | gate opens a fixed `_YC_GATE_SHORT` (250ms) into the fold, fold continues underneath | yes | (none currently) |
| `yawn_cover_lead_in_delayed` | UNGATED (`gate=None`); arm waits `_YC_MOTION_DELAY` (0.5s) at rest then folds — the sound leads, the motion follows | no | `burp` |

Timing constants (all class attributes on `Movements`): `_YC_GATE_LEAD`,
`_YC_GATE_SHORT`, `_YC_MOTION_DELAY`. The cover pose values are the
`_YC_*_COVER` constants (elbow cover is 162); the documented pose is also in
`constants.ARM_DESTINATION_POSES["coverMouth"]`.

## Multi-track audio

### Back-to-back (follow-on) — one continuous performance

`PerformanceDefinition.followup_audio_files` plays extra clips immediately after
`audio_file`, in order, on the same audio thread with NO gap. The
`PlaybackController` stays `is_active()` across the whole chain and its duration
is the SUM of every track, so looping motion keeps running across all of them
and `stop_loop_lead_seconds` fires against the end of the LAST track.

```python
followup_audio_files=(self.music[32],)                 # bare filename, same options
followup_audio_files=((self.music[27], {"drive_jaw": True}),)  # per-track options
```

Use when the clips are one performance beat (e.g. `hypnotic`: `hypnotic.wav`
then `in_my_power.wav`; `burp`: `gurgle_burp.wav` then `excuseme_sb.wav`).

The gesture does NOT have to hold through the whole chain. A movement's
`stop_loop_lead_seconds` is measured against the SUM of all tracks, so you can
retract the hand partway through: `burp` sets `stop_loop_lead_seconds` to the
`excuseme_sb.wav` length (~3.45s) so the hand starts lowering the instant
`gurgle_burp.wav` ends, and the "excuse me" plays while the hand lowers. See
"Performance ends only after audio drains" below for why that does NOT cut off
the follow-on track.

### Two-phase — a clip finishes before the gesture

When a clip must fully play BEFORE any gesture (not concurrently), do NOT use
the framework's gate — the gate controls when the FIRST track starts, not a
pause between tracks. Instead run two sequential phases in the routine method:

1. Play the lead clip to completion with a blocking `AudioPlayer`, then
   `close()` it so its jaw/eye GPIO pins are released.
2. Run the gesture-with-audio phase (a Performance, or `run_action_and_audio`).

Because the two audio phases never overlap, there is no jaw-motor / audio-device
contention. See `fart`: `fart.wav` plays alone (phase 1), then the cover-mouth
Performance plays `excuseme_sb.wav` gated-until-settled (phase 2).

Phase 1 can disable the jaw/eyes for a non-mouth sound: `fart` builds its
phase-1 player as `AudioPlayer(drive_jaw=False, drive_eyes=False)` so the fart
plays as PURE audio — no jaw flap, no eye flash (a fart doesn't come out of the
mouth), and neither GPIO pin is claimed so phase 2's players get them cleanly.

## Performance ends only after audio drains

`PerformanceRunner.run()` does NOT return the moment the gesture finishes. On
normal completion — after the final step's `do_return` and the residual
return-to-rest — it waits for the audio chain to fully drain
(`await asyncio.to_thread(playback.wait_finished)`, guarded on
`playback.has_started()`) before printing "finished" and returning.

Why this matters: audio runs on a `daemon=True` thread. If `run()` returned
while audio was still playing, the `asyncio.run(...)` at the top of the routine
would unwind, the process/servo-lock context would tear down, and the daemon
audio thread would be killed MID-TRACK. That is exactly what cut off the tail
of `burp`'s `excuseme_sb.wav` once the hand lowered early (via
`stop_loop_lead_seconds`) and the gesture finished well before the audio did.

Consequences for authoring:
- You can freely retract the hand before audio ends (`stop_loop_lead_seconds`)
  without fear of truncating a follow-on track — the runner waits out the
  remaining audio.
- The wait is on the NORMAL path only. The exception/failure path drives servos
  to safe rest and re-raises immediately; it must not block waiting on audio.
- The join runs in a worker thread so it never stalls the event loop.

## Per-track player options (jaw / eyes)

`player_options` on the definition (and per-track options in
`followup_audio_files`) forward kwargs to `AudioPlayer`, e.g.
`{"drive_jaw": False, "drive_eyes": False}`. A fresh `AudioPlayer` is built per
track and the previous one is `close()`d first, so consecutive tracks can use
different jaw/eye settings without a gpiozero "pin already in use" clash.

Common cases:
- Silence the jaw for a non-speech lead track, turn it on for a spoken
  follow-on (`hypnotic` → `in_my_power.wav`).
- Disable `drive_eyes` when a separate ambient blinker owns `EYE_LIGHT_PIN`
  (`hypnotic`'s `_blink_eyes`), so the envelope driver does not fight it.

## Wiring checklist (audio side)

1. Add the clip filename(s) to `Animatronic.music` with an index comment.
2. Author the routine method using the mechanism chosen above.
3. Register the `camelCase` action in `action_map` in `animatronic.py`'s
   `main()` (the dispatch allowlist — never `getattr`/`eval` on raw `--action`).
4. Add the action to `ROUTINE_ACTIONS` in `webapp.py` if it should appear in
   the control panel.

(Steps 1–4 mirror the gesture wiring checklist in `authoring-gestures.md`;
this list is just the audio-facing view of the same steps.)
