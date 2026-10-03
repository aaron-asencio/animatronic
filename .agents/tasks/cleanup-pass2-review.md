# Second deletion pass — remove `waiting` / `exorcist` / `evilLaugh` routines and the leftover beckon/look-around gesture helpers

This pass removes three routines (`waiting`, `exorcist`, `evilLaugh`) and the now-orphaned composite/helper gestures they depended on (`come_and_look`, `reach_and_look`, bare `look_around`, bare `wave_and_swivel`, `reach_out`), plus the `_do_*` wrappers and every allowlist / pool / docstring / README reference that named them. The user is keeping the audio for all three (they will redo `evilLaugh`, `exorcist`, and `waiting` later), so no audio files were deleted and the index-based `music[]` list keeps its slots. The deletions are clean and complete against the spec: no dangling references survive, every protected substring-neighbor (`*_smooth`, `*_small`, `*_random`, Tracking Mode `scan`/`slow_scan`) is intact, and the `music[]` indices the kept audio lives at (0/7/16) are byte-for-byte in place.

The working tree also carries an UNRELATED, undocumented behavioral rework of the `burp` / `fart` cover-mouth routines (new `yawn_cover_lead_in_delayed`, `_YC_MOTION_DELAY`, a `coverMouth` pose entry, an elbow angle change 165→162, a `PerformanceRunner` audio-drain wait, and matching steering docs). It is not part of the deletion spec, is not described in the coder's notes, and changes servo motion + audio timing. It does not touch any removed name, the `music[]` list, or any deleted audio, so it does not break the cleanup — but it is bundled into this diff and should be reviewed and committed on its own merits.

Watch for:
- **Unrelated burp/fart/yawn rework bundled in the same working tree** (confirmed) — servo-motion and audio-timing change undocumented by this pass's notes; separate it from the deletion commit.
- **`music[]` indices 5/6/10 are `None`, not their old filenames** (confirmed) — these are the PRIOR pass's `vaderBeaten`/`vaderFather`/`yodaFear` removals, left in place (not deleted/shifted). The spec's protected indices 0/7/16 are untouched. Not a pass-2 fault.
- **README still lists `vaderFather` / `torture` / `vaderBeaten` rows** (confirmed) — pass-1 leftovers, explicitly out of scope here; flagged only so they aren't forgotten.

**Verdict**: APPROVED

## High-level view

The three routines and their now-dead helpers are gone from `animatronic.py` and `movements.py`, and every by-name reference followed: `action_map`, both webapp allowlists (`ROUTINE_ACTIONS` / `MOVEMENT_ACTIONS`), both automation pools (`ROUTINE_POOL` / `MOVEMENT_POOL`), the `controller.py` gesture map + module docstring + help text, the README routine table and gesture line, and the argparse help example in `animatronic.py`. A repo-wide grep for every removed name (both camelCase action and snake_case method forms) returns nothing in `src/` or `tests/`.

The single highest-risk item — the index-based `music[]` list — is correct. Indices 0 (`beetel-exorcist.wav`), 7 (`were-waiting.wav`), and 16 (`evil-laugh.wav`) remain exactly where they were because the user is keeping that audio. The `None` entries at 5/6/10 are the earlier pass's vader/yoda removals, nulled in place so no index shifts; this pass added nothing to the list and removed nothing from it.

Survivors that share a substring with a removed name all remain and are wired correctly: `wave_and_swivel_smooth` (startParty), `reach_and_look_smooth` (vincentPrice), `look_around_random`, `look_around_small`, `_do_look_around_random`, `_do_hand_visor`, and the whole Tracking Mode `Scan_Sweep` / `_run_scan_sweep` / `scan_timeout` / `slow_scan` stack. The dangling docstring references in `wave_and_swivel_smooth` and `reach_and_look_smooth` that used to point at the deleted bare gestures were reworded rather than left broken.

The one thing a reviewer must not miss is that this working tree is not a pure deletion diff. A separate, substantive rework of the `burp` and `fart` cover-mouth routines rides along: a new ungated-delayed-motion lead-in, a new motion-delay constant, a `coverMouth` pose entry in `constants.py`, an elbow-cover angle change, a `PerformanceRunner` change that now waits for the audio chain to drain, and two steering docs updated to match. This is coherent on its own but has nothing to do with removing `waiting`/`exorcist`/`evilLaugh`.

<details>
<summary>Issues (3)</summary>

1. **Unrelated burp/fart/yawn rework bundled in** — `movements.py` (`yawn_cover_lead_in_delayed`, `_YC_MOTION_DELAY`), `animatronic.py` (burp ungated + `stop_loop_lead_seconds=3.45`, fart `drive_jaw/eyes=False`), `constants.py` (`coverMouth` pose, elbow 165→162), `performance.py` (audio-drain wait), and two steering docs change servo motion and audio timing. Not part of the deletion spec and absent from the coder's notes. Commit it separately and have the operator bench-verify the cover-mouth pose/timing on hardware. Non-blocking for the cleanup.
2. **README retains stale `vaderFather` / `torture` / `vaderBeaten` rows** — pass-1 removals whose rows (and, for vader*, deleted audio) are still in the routine table. Out of scope for pass-2, but worth cleaning when those names are finished with.
3. **`music[]` 5/6/10 nulled (prior pass)** — informational: indices 5/6/10 read `None` from the earlier vader/yoda removal. Confirmed not deleted/shifted and the protected 0/7/16 are intact; recorded only so the `None`s aren't mistaken for a pass-2 edit.

</details>

<details>
<summary>Details</summary>

### Routines and `action_map` (`animatronic.py`)

`def waiting`, `def exorcist`, and `def evil_laugh` are gone, along with their three `action_map` entries (`'waiting'`, `'exorcist'`, `'evilLaugh'`) and the now-empty `# --- Beckon routines ---` / `# --- New gesture routines ---` grouping that only held removed methods. The argparse `--action` help example that named `waiting` was repointed to `krusty`, a routine that still exists. `start_party` (music[3]), `krusty` (music[2]), `blah`, and `vincent_price` (music[17]) are untouched and still dispatchable.

### `music[]` list — the index-safety check

The list is still 34 entries, indices 0–33, with no splice. The three slots the kept audio occupies are exactly as before: `beetel-exorcist.wav` at 0, `were-waiting.wav` at 7, `evil-laugh.wav` at 16. The `None` entries at 5/6/10 carry `(removed: vaderBeaten/vaderFather/yodaFear)` comments and are the earlier pass's work, nulled in place so every surviving index still resolves to the same file. This is the single highest-risk item in the spec and it is correct.

### Orphaned `_do_*` wrappers

`_do_come_and_look`, `_do_wave_and_swivel`, and `_do_reach_and_look` are removed; each had no remaining caller once its routine was deleted. The `_smooth` siblings that real routines still use — `_do_wave_and_swivel_smooth` (startParty) and `_do_reach_and_look_smooth` (vincentPrice) — and the ambient-pool `_do_look_around_random` / `_do_hand_visor` are all present.

### `movements.py` deletions and reworded docstrings

The bare composites `come_and_look` and `reach_and_look` and the bare primitives `look_around`, `wave_and_swivel`, `reach_out` (and `look_up`, `scan`, `patrol` from the prior pass) are deleted. The eased survivors `wave_and_swivel_smooth` and `reach_and_look_smooth` remain; their docstrings and landmark comments that previously contrasted themselves against the now-deleted bare gestures were reworded to describe the eased motion directly instead of being left as dangling cross-references. `look_around_random` and `look_around_small` are intact.

### `controller.py`, `webapp.py`, README

`controller.py` drops the `reachOut` / `lookUp` / `lookAround` / `scan` / `waveAndSwivel` / `patrol` map entries and `comeAndLook` / `reachAndLook`, updates the ARM/HEAD/COMPOSITE docstring lists, and repoints the help example off a removed name; `waveAndSwivelSmooth` and `handVisor` stay. In `webapp.py`, `ROUTINE_ACTIONS` loses `waiting`/`exorcist`/`evilLaugh` and `ROUTINE_POOL` becomes `['blah', 'startParty', 'krusty']` — all three remaining pool entries exist as routines in `action_map`. `MOVEMENT_ACTIONS` / `MOVEMENT_POOL` lose `comeAndLook`/`reachAndLook` (and the prior pass's `lookAround`/`scan`). The README routine table drops the `waiting` and `exorcist` rows and the gesture line drops `lookAround`/`scan` while keeping `slowScan` and `lookAroundSmall`.

### Unrelated cover-mouth rework (surface, do not block)

Independent of any deletion, the diff reworks `burp` from a 250ms-gated lead-in to an ungated-at-t=0 design with the arm motion delayed by a new `_YC_MOTION_DELAY` (0.5s) via a new `yawn_cover_lead_in_delayed` adapter, and sets `stop_loop_lead_seconds=3.45` so the hand lowers as `gurgle_burp.wav` ends while `excuseme_sb.wav` plays out. `fart`'s phase-1 player is built with `drive_jaw=False, drive_eyes=False`. `constants.py` gains a `coverMouth` pose entry and lowers the elbow-cover angle 165→162 (also applied to the `yawnCover` pose and the `_YC_*_COVER` constants). `PerformanceRunner.run()` now awaits `playback.wait_finished` on the normal path so a daemon audio thread isn't killed mid-track. `audio-sequencing.md` and `authoring-gestures.md` are updated to document all of the above.

This is a servo-motion and audio-timing change, not a cleanup. Per the project's hardware-movement policy the sim collision/limit checks are intentionally not run here (the operator verifies on the robot), and the in-code `set_angle` / override / return-to-rest guards are preserved, so there is nothing to block on. But it does not belong in a deletion commit — it should be committed separately with its own description so the history and the operator's hardware check are scoped to it.

</details>

<details>
<summary>File map</summary>

- `src/animatronic.py` — removed `waiting`/`exorcist`/`evil_laugh` methods + `action_map` entries + orphan `_do_*` wrappers; help example repointed. (Also carries the unrelated burp/fart rework.)
- `src/movements.py` — deleted `come_and_look`/`reach_and_look`/`look_around`/`wave_and_swivel`/`reach_out`; reworded `*_smooth` docstrings. (Also the new `yawn_cover_lead_in_delayed`.)
- `src/controller.py` — removed `comeAndLook`/`reachAndLook` map entries + docstring/help mentions.
- `src/webapp.py` — pruned `ROUTINE_ACTIONS`/`ROUTINE_POOL`/`MOVEMENT_ACTIONS` of removed names.
- `README.md` — dropped `waiting`/`exorcist` rows and `lookAround`/`scan` from the gesture line.
- `src/audio_player.py` — prior-pass `__main__` audition pruning; `exorcist`/`waiting` cases remain (audio kept).
- `src/constants.py`, `src/performance.py`, `src/templates/index.html`, `.kiro/steering/*.md` — unrelated cover-mouth rework + its doc updates.

Full diff: `git diff` in `/home/aaron/workspace/animatronic-v2`.

</details>
