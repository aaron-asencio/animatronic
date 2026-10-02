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
  `moreCandy`, `burp`).

Use when a brief motion beat should precede the sound but you do NOT need the
hand to have arrived anywhere specific (e.g. the burp starts as the hand is
still on its way to the mouth).

### 3. Gate-until-settled — audio when the gesture reaches a pose

Audio is held until the gesture's `lead_in` fully completes its move, so the
sound lines up with the hand ARRIVING at a position. Framework only:
`supplies_gate=True` and the `lead_in` AWAITS its motion to completion before
returning.

Cover-mouth has two ready-made lead-in variants for exactly this choice
(both reuse yawn_cover's operator-verified pose + `verified_pose_override`):

- `yawn_cover_lead_in_settled` — folds the hand fully up, THEN opens the gate
  ("gate audio until the hand reaches final position"). Used by `coughLong`,
  `coughMedium`, and `fart`'s phase 2.
- `yawn_cover_lead_in_gated` — opens the gate a fixed 250ms in while the fold
  continues underneath. Used by `burp`.
- `yawn_cover_lead_in` — opens the gate ~0.5s BEFORE the fold settles (the
  sound leads the final settle). Used by `clearThroat`.

Only ONE movement in a group may set `supplies_gate=True`.

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

Use when the clips are one performance beat and the gesture should hold through
all of them (e.g. `burp`: `gurgle_burp.wav` then `excuseme_sb.wav`, hand at the
mouth the whole time; `hypnotic`: `hypnotic.wav` then `in_my_power.wav`).

### Two-phase — a clip finishes before the gesture

When a clip must fully play BEFORE any gesture (not concurrently), do NOT use
the framework's gate — the gate controls when the FIRST track starts, not a
pause between tracks. Instead run two sequential phases in the routine method:

1. Play the lead clip to completion with a blocking `AudioPlayer`, then
   `close()` it so its jaw/eye GPIO pins are released.
2. Run the gesture-with-audio phase (a Performance, or `run_action_and_audio`).

Because the two audio phases never overlap, there is no jaw-motor / audio-device
contention. See `fart`: `fart.wav` plays alone, then the cover-mouth Performance
plays `excuseme_sb.wav` gated-until-settled.

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
