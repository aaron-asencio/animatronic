# Removal of unused Routines & Gestures — with unrelated burp/cover-mouth rework bundled in

This change removes the unused Routines (`yodaFear`, `vaderFather`, `vaderBeaten`, `torture`) and the unused selectable Gestures (`lookUp`, `scan`, `patrol`, plus `reachOut`/`lookAround`/`waveAndSwivel` as *selectable* actions), deletes three audio files (`vader-beaten.wav`, `vader-father.wav`, `yoda-fear.wav`), keeps the torture audio (`spongebob-torture.wav`), and cleans up every reference across the dispatch map, allowlists, automation pools, controller docstring, and the Awake-mode web hint. The deletion half of the diff is correct, index-safe, and consistent across files. The problem is the other half: the same working tree also carries a full `burp`/`fart`/cover-mouth routine rework (gating → ungated motion delay, `drive_jaw`/`drive_eyes`, a new `coverMouth` pose, an elbow 165→162 retune, a new `PerformanceRunner` audio-drain join, and two rewritten steering docs) that has nothing to do with a cleanup task.

Watch for: unrelated behavioral changes bundled into a deletion diff (confirmed) — these alter physical servo behavior (elbow 165→162) and audio/gesture timing and should ship as their own change; the deletion itself is clean. No coder verification notes file exists on disk (confirmed) — only `cleanup-plan.md` is present in the task dir.

**Verdict**: APPROVED

## High-level view

The music list in `animatronic.py` was handled exactly as the plan's highest-risk item demanded: indices 5, 6, 10 were replaced with `None` + a `(removed: …)` comment, index 4 (`spongebob-torture.wav`) is untouched, and no element was spliced — the list still holds 34 entries (indices 0–33), so every other routine's numeric index (e.g. `waiting` → `self.music[7]`) still resolves to the same file.

The three deleted routines and the dead `_do_patrol` wrapper were removed from both the method bodies and `action_map`; `waiting`, `krusty`, `exorcist` and the rest are intact. The torture routine method and its `action_map` entry are gone while its audio stays, matching "keep audio for torture."

The gesture removals split into two kinds, and the coder correctly diverged from a literal reading of the plan here. `lookUp`, `scan`, and `patrol` have zero remaining callers and were fully deleted. `reach_out`, `look_around`, and `wave_and_swivel` are still used internally (by `come_and_look`/`reach_and_look` and by `evilLaugh` via `_do_wave_and_swivel`), so they were kept as coroutines but removed from the selectable action lists, with a comment on each explaining the retention. Deleting them outright — as plan step 6 literally said — would have broken those composites; keeping them is the right call.

Allowlists and pools in `webapp.py`, the `action_map` and docstring in `controller.py`, the `__main__` cases in `audio_player.py`, and the Awake-mode hint in `index.html` were all updated consistently, with no false-positive substring damage: `scan_timeout`, `Scan_Sweep`, `_run_scan_sweep`, `slow_scan`, `look_around_random`, `look_around_small`, `wave_and_swivel_smooth`, `reach_and_look` all survive untouched. The Tracking Mode's `--scan-timeout` flag and Scan_Sweep internals are entirely absent from the diff.

The remaining concern is scope. `performance.py`, `constants.py`, the audio/timing internals of `movements.py`, the `yawn`/`burp`/`fart` routines in `animatronic.py`, and both steering docs carry a cover-mouth/burp rework that is unrelated to removing dead code. It is self-consistent and compiles, but it does not belong in a cleanup diff and changes real hardware/audio behavior.

<details>
<summary>Issues (2)</summary>

1. **Unrelated burp/cover-mouth rework bundled in** — `performance.py` (audio-drain join), `constants.py` (`coverMouth` pose + elbow 165→162), `movements.py` (`yawn_cover_lead_in_delayed`, `_YC_MOTION_DELAY`, elbow retune), `animatronic.py` (`yawn`/`burp`/`fart` gating + `drive_jaw`/`drive_eyes`), and both `.kiro/steering/*.md` files are not part of the deletion task and change physical/audio behavior. Non-blocking for the deletion's correctness, but should be split into its own commit/PR before shipping. (confirmed)
2. **No coder verification-evidence file on disk** — the task dir contains only `cleanup-plan.md`; there is no notes file recording the py_compile/grep/pytest results the brief referenced. Verdict relies on the diff being self-evidently correct plus one spot-check compile. (confirmed)

</details>

<details>
<summary>Details</summary>

### Music list — index-safe, torture preserved

The single riskiest edit landed correctly:

```
'spongebob-torture.wav',   # 4      <- UNCHANGED (torture audio kept)
None,                      # 5  (removed: vaderBeaten)
None,                      # 6  (removed: vaderFather)
'were-waiting.wav',        # 7      <- unchanged
'yoda-900.wav',            # 8      <- unchanged
None,                      # 9  (removed: yoda / yoda-agent-evil.wav)
None,                      # 10 (removed: yodaFear)
```

No list element was spliced; indices 5/6/10 became `None` + comment matching the pre-existing placeholder style (index 9, 11–15, 18). The list still has 34 entries (0–33), confirmed by count. Index-based consumers elsewhere are therefore safe: `waiting` → `self.music[7]`, `exorcist` → `[0]`, `krusty` → `[2]`, `yawn`/`burp` → `[32]`, `fart` → `[33]` all still point at their intended files. `torture` was the only routine that referenced index 4, and its method was removed while the audio stayed — exactly "keep audio for torture."

### Routine + dead-wrapper removal

`vader_father`, `torture`, `vader_beaten`, `yoda_fear` methods and their `action_map` entries are gone; `_do_patrol` (whose only caller was `vader_beaten` and which called the now-deleted `mv.patrol()`) was also removed, so no method references a deleted coroutine. `waiting`, `krusty`, `exorcist` and the rest of `action_map` are intact. `python -m py_compile src/animatronic.py` exits 0 (spot-checked).

### Gesture removal split — deletion vs. de-listing

`lookUp`/`scan`/`patrol`: no callers remain anywhere, coroutines deleted. `reach_out`/`look_around`/`wave_and_swivel`: still called by `come_and_look`, `reach_and_look`, and `evilLaugh`'s `_do_wave_and_swivel`, so the coroutines were kept and only removed from `controller.py`/`webapp.py` selectable lists, each annotated as an internal building block. This diverges from plan step 6 (which said delete them) but is correct — a literal deletion would have broken the kept composites and `evilLaugh`.

### No false-positive substring damage

The sweep confirms every look-alike survives: `scan_timeout`, `Scan_Sweep`, `_run_scan_sweep`, `TRACKING_INTERRUPT_SCAN_TIMEOUT`, `_SCAN_STEP_DEG`, `slow_scan`, `look_around_random`, `look_around_small`, `wave_and_swivel_smooth`, `reach_and_look`, `lookAroundRandom`, `lookAroundSmall`, `waveAndSwivelSmooth`. The Tracking Mode's `--scan-timeout` flag and Scan_Sweep logic do not appear in the diff at all (`git diff` shows no tracking-scan lines changed), so that Mode is intact.

### Allowlist / pool / UI consistency

`webapp.py`: removed routines dropped from `ROUTINE_ACTIONS` and `vaderFather` from `ROUTINE_POOL`; removed gestures dropped from `MOVEMENT_ACTIONS` and `lookAround`/`scan` from `MOVEMENT_POOL` (keeping `lookAroundSmall`). `controller.py`: `action_map` entries and the docstring ARM/HEAD/COMPOSITE lists updated, and the `--help` example changed from `patrol` to `nod`. `audio_player.py`: the three deleted `__main__` cases and the three filenames removed from its local demo `music` list (torture was never in that list); the already-commented `# case "torture"` left as-is. `index.html`: the Awake-mode hint no longer names `patrol` (grep returns nothing). Buttons are rendered from the allowlists, so no button HTML needed editing.

### Audio files

Exactly the three target files are deleted (`git status` shows `deleted: vader-beaten.wav / vader-father.wav / yoda-fear.wav`) and `audio/spongebob-torture.wav` is still present.

### Out-of-scope rework (the scope concern)

`performance.py` gains a post-run `await asyncio.to_thread(playback.wait_finished)` audio-drain join. `constants.py` adds a `coverMouth` pose and retunes `yawnCover` elbow 165→162. `movements.py` adds `yawn_cover_lead_in_delayed` / `_YC_MOTION_DELAY` and retunes the yawn/`_YC_*` elbow landmarks 165→162. `animatronic.py`'s `yawn`/`burp`/`fart` routines move from a 250ms gate to ungated-with-motion-delay and build the fart player with `drive_jaw=False, drive_eyes=False` (the `AudioPlayer.__init__` signature supports both kwargs — confirmed, so the bundled change is not itself broken). Both `.kiro/steering/*.md` files are rewritten to document all of the above. None of this is "remove unused Routines/Gestures." It compiles and is internally consistent, but it alters physical servo travel and audio timing and should be its own change so the cleanup diff stays a pure deletion. Flagged as non-blocking because it does not break the deletion's correctness.

### Verification evidence

The brief referenced coder-recorded py_compile/grep/pytest results, but the task dir holds only `cleanup-plan.md` — no notes file. Per policy I did not run the suites; I ran one spot-check (`py_compile src/animatronic.py`, exit 0) against the articulable doubt that the music-index edits and the bundled routine rework both live in that file. The deletion is self-evidently correct from the diff and the reference sweeps, so this is noted rather than treated as blocking.

</details>

<details>
<summary>File map</summary>

- `src/animatronic.py` — removed 4 routine methods + `_do_patrol` + their `action_map` entries; music indices 5/6/10 → `None`; (bundled) `yawn`/`burp`/`fart` rework.
- `src/movements.py` — deleted `look_up`/`scan`/`patrol`; de-listed-but-kept `reach_out`/`look_around`/`wave_and_swivel` with retention comments; (bundled) `yawn_cover_lead_in_delayed`, `_YC_MOTION_DELAY`, elbow 165→162.
- `src/controller.py` — removed 6 `action_map` entries; updated docstring + `--help` example.
- `src/webapp.py` — pruned `ROUTINE_ACTIONS`/`ROUTINE_POOL`/`MOVEMENT_ACTIONS`/`MOVEMENT_POOL`.
- `src/audio_player.py` — removed 3 `__main__` cases + 3 demo-list filenames.
- `src/templates/index.html` — Awake-mode hint no longer names `patrol`.
- `src/constants.py` — (bundled) new `coverMouth` pose, `yawnCover` elbow 165→162.
- `src/performance.py` — (bundled) audio-drain join on normal completion.
- `.kiro/steering/audio-sequencing.md`, `.kiro/steering/authoring-gestures.md` — (bundled) cover-mouth/burp docs.
- `audio/vader-beaten.wav`, `audio/vader-father.wav`, `audio/yoda-fear.wav` — deleted.

Full diff: `git diff` in `/home/aaron/workspace/animatronic-v2`.

</details>
