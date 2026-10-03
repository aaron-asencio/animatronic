# Cleanup Plan — remove unused Routines & Gestures (animatronic-v2)

Pure code-deletion task. Remove unused Routines (`yodaFear`, `vaderFather`,
`vaderBeaten`, `torture`) and Gestures (`reachOut`, `lookAround`, `comeAndSwivel`,
`lookUp`, `waveAndSwivel`, `scan`, `patrol`), delete three audio files, and clean
up every reference (web UI, automation pools, tracking/dispatch wiring).

## Safety / verification policy

- Do NOT run `SERVO_SIM` collision/limit/simulate-and-inspect passes. This is
  code deletion only.
- Keep all in-code servo safety intact (`set_angle`/`move_to` clamping,
  `SAFE_LIMITS`, `FORBIDDEN_COMBINATIONS`, return-to-rest) — we only delete
  methods/entries, never touch those guards.
- Verification for every edited `.py` file: `python -m py_compile <file>` must
  exit 0 (catches syntax/import errors). Audio deletion verified by `ls`.

## Key findings from investigation (read before editing)

1. **`comeAndSwivel` / `come_and_swivel` does NOT exist anywhere** in the repo
   (grep returned no matches in `controller.py`, `movements.py`, or elsewhere).
   → Nothing to remove for it. Noted and skipped per instructions.
2. **Music list is index-based** (`src/animatronic.py` lines 190–224). It already
   uses `None` placeholders (indices 9, 11–15, 18). Do NOT splice elements —
   replace removed filenames with `None` + comment. Confirmed indices:
   - index 4 = `'spongebob-torture.wav'` → **KEEP unchanged** (torture audio stays).
   - index 5 = `'vader-beaten.wav'` → replace with `None` + comment.
   - index 6 = `'vader-father.wav'` → replace with `None` + comment.
   - index 10 = `'yoda-fear.wav'` → replace with `None` + comment.
3. **Extra dead-code finding:** `Animatronic._do_patrol` (`src/animatronic.py`
   ~lines 2549–2552) calls `mv.patrol()`. Its only caller is `vader_beaten`
   (being deleted) and `mv.patrol` (being deleted from `movements.py`). So
   `_do_patrol` must be removed too, or `py_compile` passes but the method would
   reference a now-deleted coroutine. (No other `_do_*` wrapper is orphaned:
   `_do_come_and_look` is still used by `waiting`/`exorcist`; `_do_reach_and_look`
   uses `reach_and_look`, a KEPT gesture.)
4. **Tracking mode is independent and must be preserved.** The Tracking Mode's
   `Scan_Sweep` (`_run_scan_sweep`, `scan_timeout`/`--scan-timeout`,
   `TRACKING_INTERRUPT_SCAN_TIMEOUT = "scan-timeout"`, `_SCAN_STEP_DEG`,
   `TrunkController.slow_scan`) pans `NECK_PAN` directly via `set_angle` and
   NEVER dispatches the removed `scan`/`patrol`/`lookAround` gesture coroutines
   by name. Do NOT touch any of it. (grep confirmed `tracking_controller.py`
   has no gesture dispatch; animatronic.py's scan references are all Scan_Sweep.)
5. **Detection map is clean:** `src/detection_routine_map.py` only references
   `wave` and `walkYourDog` — no removed names.
6. **Test false-positives (do NOT touch):** `tests/test_brains_collision.py` and
   `tests/test_performance.py` reference `look_around_random` (KEPT), not
   `look_around`. No test references any removed name.
7. **Substring false-positives to avoid everywhere:** `look_around_random`,
   `look_around_small`, `wave_and_swivel_smooth`, `reach_and_look`,
   `lookAroundRandom`, `lookAroundSmall`, `waveAndSwivelSmooth`, `scan_timeout`,
   `Scan_Sweep`, `slow_scan`, generic "lookup"/"looking around" prose. Only
   remove the EXACT names listed.
8. Web UI buttons are rendered dynamically from the `routines`/`movements`
   template vars (sourced from the webapp allowlists), so removing allowlist
   entries removes the buttons automatically. The ONLY hardcoded by-name mention
   in `src/templates/index.html` is the Awake-mode hint naming `patrol`.

---

# Implementation Plan

- [ ] 1. Remove the three deleted routines' methods and the dead `_do_patrol`
      wrapper in `src/animatronic.py`. Delete the methods `vader_father`
      (~lines 409–411), `torture` (~lines 413–415), `vader_beaten`
      (~lines 423–425), `yoda_fear` (~lines 499–501), and `_do_patrol`
      (~lines 2549–2552, the `async def _do_patrol` wrapper that calls
      `mv.patrol()`). Keep `waiting`, `exorcist`, `krusty`, `start_party`,
      `blah` and all other routines/wrappers intact. Remove any now-empty
      section comment only if it leaves a dangling header (e.g. the
      `# --- Patrol / ambient routines ---` comment can stay above `krusty`).
      Files: src/animatronic.py
      Verify: `python -m py_compile src/animatronic.py` exits 0.

- [ ] 2. Remove the deleted routines from `action_map` in
      `src/animatronic.py` (the dict returned by `build_action_map`, ~lines
      2473–2500). Delete the entries `'vaderFather': self.vader_father,`,
      `'torture': self.torture,`, `'yodaFear': self.yoda_fear,`, and
      `'vaderBeaten': self.vader_beaten,`. Keep `'krusty'`, `'waiting'`,
      `'exorcist'`, `'tracking'`, and all others.
      Files: src/animatronic.py
      Verify: `python -m py_compile src/animatronic.py` exits 0.

- [ ] 3. Replace the removed audio entries in the index-based `self.music`
      list in `src/animatronic.py` (~lines 190–224) with `None` + a comment,
      matching the existing `None` placeholder pattern. Make exactly these
      changes (do NOT delete/splice list elements — indices must not shift):
      - index 5: `'vader-beaten.wav',        # 5` → `None,                      # 5  (removed: vaderBeaten)`
      - index 6: `'vader-father.wav',        # 6` → `None,                      # 6  (removed: vaderFather)`
      - index 10: `'yoda-fear.wav',           # 10` → `None,                      # 10 (removed: yodaFear)`
      Leave index 4 `'spongebob-torture.wav',   # 4` UNCHANGED (torture audio is
      kept). Match the surrounding comment-alignment style.
      Files: src/animatronic.py
      Verify: `python -m py_compile src/animatronic.py` exits 0; confirm the
      list still has the same number of elements (34 entries, indices 0–33) and
      that routines referencing other indices (e.g. `self.music[7]` in
      `waiting`) are unchanged.

- [ ] 4. Remove the deleted routines from the `ROUTINE_ACTIONS` allowlist and
      `ROUTINE_POOL` in `src/webapp.py`. In `ROUTINE_ACTIONS` (~lines 79–84)
      remove `'vaderFather'`, `'torture'`, `'vaderBeaten'`, `'yodaFear'`
      (keep `'startParty'`, `'blah'`, `'krusty'`, `'waiting'`, `'exorcist'`
      and all others). In `ROUTINE_POOL` (~line 113,
      `['blah','exorcist','startParty','waiting','krusty','vaderFather']`)
      remove `'vaderFather'` so it becomes
      `['blah', 'exorcist', 'startParty', 'waiting', 'krusty']`.
      Files: src/webapp.py
      Verify: `python -m py_compile src/webapp.py` exits 0.

- [ ] 5. Remove the three deleted audio cases from the `__main__` match/case in
      `src/audio_player.py` (~lines 220–248). Delete the cases
      `case "vaderBeaten": ...vader-beaten.wav`,
      `case "vaderFather": ...vader-father.wav`, and
      `case "yoda-fear": ...yoda-fear.wav` (their audio files are being
      deleted). KEEP the `"exorcist"`, `"blah"`, `"krusty"`, `"waiting"` cases
      and the `case _` fallthrough. LEAVE the already-commented-out
      `# case "torture":` block as-is.
      Files: src/audio_player.py
      Verify: `python -m py_compile src/audio_player.py` exits 0.

- [ ] 6. Delete the three unused gesture coroutines in `src/movements.py`. Remove
      ONLY these exact methods (confirmed line ranges — verify the `async def`
      header before deleting, and keep the surrounding kept methods and the two
      section-separator comment blocks noted):
      - `reach_out` (~lines 1735–1782). Keep the `# === HEAD gestures ===`
        separator block that follows (~1784–1786) and `nod` after it.
      - `look_up` (~lines 1807–1820). Keep `nod` before and `look_around_random`
        chain after.
      - `look_around` (~lines 1822–1832). KEEP `look_around_random` (starts
        ~1833) — do NOT remove it.
      - `scan` (~lines 2085–2108). KEEP `shake_head` (starts ~2110).
      - `wave_and_swivel` (~lines 2528–2539). KEEP the
        `# --- wave_and_swivel_smooth landmarks ---` comment block and the
        `_WSS_*` constants and `wave_and_swivel_smooth` that follow (~2541+).
      - `patrol` (~lines 2743–2750). KEEP the `# === more_candy ===` separator
        block that follows (~2752+).
      Do NOT touch any kept gesture (e.g. `look_around_small`,
      `look_around_random`, `wave_and_swivel_smooth`, `come_and_look`,
      `reach_and_look`, `swivel_head`, `neck_ellipse`, `wave`, `come`,
      `beckon`, `come_here`, `menacing_reach`, `yawn_cover`, `face_palm`,
      `nod`, `shake_head`, `shake_no`, `small_shake_no`, `hand_visor`).
      Files: src/movements.py
      Verify: `python -m py_compile src/movements.py` exits 0.

- [ ] 7. Remove the deleted gestures from `src/controller.py`'s `action_map`
      dict (~lines 50–83). Delete the entries `'reachOut': mv.reach_out,`,
      `'lookUp': mv.look_up,`, `'lookAround': mv.look_around,`,
      `'scan': mv.scan,`, `'waveAndSwivel': mv.wave_and_swivel,`, and
      `'patrol': mv.patrol,`. KEEP `'lookAroundSmall'`, `'lookAroundRandom'`,
      `'waveAndSwivelSmooth'`, `'comeAndLook'`, `'reachAndLook'`, and all
      others. (`comeAndSwivel` is not present — nothing to remove.)
      Files: src/controller.py
      Verify: `python -m py_compile src/controller.py` exits 0.

- [ ] 8. Update the module docstring gesture lists at the top of
      `src/controller.py` (~lines 13–21) to drop the removed names:
      - ARM line (~13): remove `reachOut` from
        `wave, come, beckon, comeHere, reachOut, menacingReach, yawnCover, facePalm`.
      - HEAD lines (~16–18): remove `lookUp` and `lookAround` and `scan`,
        keeping `lookAroundSmall`, `lookAroundRandom`, `neckEllipse`,
        `swivelHead`, `shakeHead`, `shakeNo`, `smallShakeNo`.
      - COMPOSITE lines (~20–21): remove `waveAndSwivel` (keep
        `waveAndSwivelSmooth`) and remove `patrol`, keeping `comeAndLook`,
        `reachAndLook`.
      Also update the `--help` example string if it names a removed gesture:
      line ~115 `help='Gesture to perform (e.g. wave, comeAndLook, patrol).'`
      → replace `patrol` with a kept gesture (e.g. `comeAndLook`), e.g.
      `help='Gesture to perform (e.g. wave, comeAndLook, nod).'`.
      Files: src/controller.py
      Verify: `python -m py_compile src/controller.py` exits 0.

- [ ] 9. Remove the deleted gestures from `MOVEMENT_ACTIONS` and `MOVEMENT_POOL`
      in `src/webapp.py`. In `MOVEMENT_ACTIONS` (~lines 86–92) remove
      `'reachOut'`, `'lookUp'`, `'lookAround'`, `'scan'`, `'waveAndSwivel'`,
      `'patrol'`. KEEP `'lookAroundSmall'`, `'lookAroundRandom'`,
      `'comeAndLook'`, `'reachAndLook'`, `'handVisor'`, and all others. In
      `MOVEMENT_POOL` (~lines 116–118,
      `['no','lookAround','lookAroundSmall','scan','neckEllipse','swivelHead','come','wave']`)
      remove `'lookAround'` and `'scan'`; KEEP `'lookAroundSmall'`. Result:
      `['no', 'lookAroundSmall', 'neckEllipse', 'swivelHead', 'come', 'wave']`.
      Files: src/webapp.py
      Verify: `python -m py_compile src/webapp.py` exits 0.

- [ ] 10. Reword the hardcoded Awake-mode hint in `src/templates/index.html`
      (~lines 475–478) so it no longer names `patrol`. Change
      "Performs ambient routines (patrol and others) on a loop as filler..."
      to e.g. "Performs ambient routines on a loop as filler until the timeout
      elapses or you stop it." Leave the rest of the paragraph intact. No other
      by-name mentions exist in the template (grep confirmed).
      Files: src/templates/index.html
      Verify: template is static HTML (no py_compile); visually confirm the word
      `patrol` no longer appears via `grep -n patrol src/templates/index.html`
      returning nothing. (The dynamic routine/movement buttons update
      automatically from the webapp allowlists — no button HTML edits needed.)

- [ ] 11. Delete exactly these three audio files from the local `audio/`
      directory (source of truth). Do NOT delete any other audio.
      - audio/vader-beaten.wav
      - audio/vader-father.wav
      - audio/yoda-fear.wav
      Keep `audio/spongebob-torture.wav` (torture audio retained),
      `audio/krusty-laugh.wav`, `audio/waiting.wav`, `audio/were-waiting.wav`,
      and all others.
      Files: audio/vader-beaten.wav, audio/vader-father.wav, audio/yoda-fear.wav
      Verify: `ls audio/ | grep -E 'vader-beaten|vader-father|yoda-fear'`
      returns nothing; `ls audio/spongebob-torture.wav` still exists.

- [ ] 12. Final repo-wide reference sweep. Grep `src/` and `tests/` for every
      removed name and confirm only intended edits remain and no stragglers
      reference a deleted method/action:
      - Action/camelCase: `torture`, `yodaFear`, `vaderFather`, `vaderBeaten`,
        `reachOut`, `lookAround`, `lookUp`, `waveAndSwivel`, `patrol`, and
        `scan` (as a standalone action/method, not `scan_timeout`/`Scan_Sweep`/
        `slow_scan`).
      - Python method: `yoda_fear`, `vader_father`, `vader_beaten`, `reach_out`,
        `look_around` (not `look_around_random`/`look_around_small`), `look_up`,
        `wave_and_swivel` (not `wave_and_swivel_smooth`), `mv.patrol`.
      Expected remaining `scan`/`patrol` hits are ONLY the Tracking Mode
      Scan_Sweep internals in `src/animatronic.py` (`_run_scan_sweep`,
      `scan_timeout`, `TRACKING_INTERRUPT_SCAN_TIMEOUT`, `_SCAN_STEP_DEG`,
      `slow_scan`) and the awake-pool prose `lookAroundRandom` — all legitimate
      and must stay. Do NOT touch `detection_routine_map.py` (only `wave`/
      `walkYourDog`). If any NEW breaking reference surfaces, remove it and note
      it here.
      Files: (sweep only — no edits unless a straggler is found)
      Verify: `python -m py_compile src/animatronic.py src/webapp.py
      src/controller.py src/movements.py src/audio_player.py` all exit 0.

## Final verification (run after all steps)

Compile every edited Python file in one pass:

```
python -m py_compile src/animatronic.py src/webapp.py src/controller.py \
    src/movements.py src/audio_player.py
```

All must exit 0. Then confirm the three audio files are gone and
`spongebob-torture.wav` remains. Do NOT run the pytest suite or any
`SERVO_SIM` pass for this deletion task.

## Notes / flags

- `comeAndSwivel`/`come_and_swivel` does not exist in the repo — skipped (no code
  to remove), per the task's contingency instruction.
- Extra deletion beyond the brief: `Animatronic._do_patrol` must be removed
  (step 1) because its only caller `vader_beaten` is deleted and it calls the
  now-deleted `mv.patrol()` coroutine. Flagged here for the reviewer.
- Tracking Mode `Scan_Sweep` / `--scan-timeout` / `slow_scan` are preserved
  untouched; they are independent of the removed `scan` gesture.
