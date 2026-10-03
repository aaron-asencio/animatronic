# Cleanup Pass 2 — Verification Notes

Second deletion pass, building on the prior uncommitted cleanup already in the
working tree. All changes left UNCOMMITTED in the working tree. No audio files
deleted; `music[]` list untouched.

## Per-file summary of what was removed

### src/animatronic.py
- Removed routine method `waiting` (was `run_action_and_audio("_do_come_and_look", self.music[7])`).
- Removed routine method `exorcist` (was `run_action_and_audio("_do_come_and_look", self.music[0])`).
- Removed routine method `evil_laugh` (was `run_action_and_audio("_do_wave_and_swivel", self.music[16])`).
- Removed `action_map` entries: `'waiting'`, `'exorcist'`, `'evilLaugh'` (and the now-empty "Beckon routines" comment).
- Removed orphaned `_do_*` wrappers: `_do_come_and_look`, `_do_wave_and_swivel`, `_do_reach_and_look`.
- Updated `--action` argparse help example (`waiting` -> `krusty`).
- KEPT: `_do_wave_and_swivel_smooth` (startParty), `_do_reach_and_look_smooth` (vincentPrice), and the entire `music[]` list byte-for-byte (indices 0 beetel-exorcist.wav, 7 were-waiting.wav, 16 evil-laugh.wav all intact).

### src/movements.py
- Deleted coroutine `reach_out` + its "Internal helper: retained..." comment.
- Deleted coroutine `look_around` + its retention comment (KEPT `look_around_random`, `look_around_small`).
- Deleted coroutine `wave_and_swivel` + its retention comment (KEPT `wave_and_swivel_smooth`).
- Deleted composite coroutines `come_and_look` and `reach_and_look`.
- Reworded docstring references in `wave_and_swivel_smooth` and `reach_and_look_smooth` that pointed at the now-deleted `wave_and_swivel` / `reach_and_look`, and the `reach_and_look_smooth` landmark comment that referenced `reach_out`. No behavior change; `wave_and_swivel_smooth` itself untouched.

### src/controller.py
- Removed `action_map` entries `'comeAndLook'` and `'reachAndLook'`.
- Removed `comeAndLook, reachAndLook` from the COMPOSITE gestures module docstring.
- Updated `--action` help example (removed `comeAndLook`).
- KEPT `'waveAndSwivelSmooth'`, `'handVisor'`.

### src/webapp.py
- `ROUTINE_ACTIONS`: removed `'waiting'`, `'exorcist'`, `'evilLaugh'`.
- `ROUTINE_POOL`: `['blah', 'exorcist', 'startParty', 'waiting', 'krusty']` -> `['blah', 'startParty', 'krusty']` (all remaining verified to still exist as routines).
- `MOVEMENT_ACTIONS`: removed `'comeAndLook'` and `'reachAndLook'`.
- `MOVEMENT_POOL`: no removed names present (left as-is).
- KEPT Tracking Mode internals (`run_tracking`, `--scan-timeout`, `scan_timeout`).

### README.md
- Removed `waiting` and `exorcist` rows from the routines table (`evilLaugh` was not listed).
- Removed `lookAround` and `scan` from the "Available gesture actions" line (kept `slowScan`, `lookAroundSmall`). No reachAndLook/comeAndLook were listed.
- Left pre-existing stale rows (vaderFather/torture/vaderBeaten) untouched — out of scope for this pass.

### src/audio_player.py — intentionally NOT changed
- Its `__main__` match/case has `exorcist` -> beetel-exorcist.wav and `waiting` -> were-waiting.wav test cases. Audio is KEPT, so these audition cases were left as-is per spec.

## Audio files
- No audio files deleted in this pass. (The `audio/vader-*.wav` / `yoda-fear.wav` deletions shown by `git status` are from the PRIOR uncommitted pass, not this one.)

## Verification results

### Compile checks (`python -m py_compile`)
    src/animatronic.py src/movements.py src/controller.py src/webapp.py  ->  COMPILE OK

### Grep — no dangling references
- `_do_come_and_look`, `_do_wave_and_swivel`, `_do_reach_and_look`, `come_and_look`, `reach_and_look`, bare `wave_and_swivel`, bare `look_around`, `reach_out`, `comeAndLook`, `reachAndLook`, `evil_laugh`, `evilLaugh`: NO matches in src/ or tests/.
- Remaining `waiting`/`exorcist` matches in code are all safe: the kept audio strings (`were-waiting.wav`, `beetel-exorcist.wav`), the `audio_player.py` audition cases, and English-word "waiting" in servo_lock/performance/webapp/tracking prose.
- `action_map` / `ROUTINE_ACTIONS` / `ROUTINE_POOL` / `MOVEMENT_ACTIONS` no longer reference any removed name.

### Survivors confirmed intact
`wave_and_swivel_smooth`, `reach_and_look_smooth`, `look_around_random`, `look_around_small`, `start_party`, `vincent_price`, `krusty`, `blah`, `_do_wave_and_swivel_smooth`, `_do_reach_and_look_smooth`, and Tracking Mode `Scan_Sweep`/`_run_scan_sweep`/`--scan-timeout`/`scan_timeout`/`slow_scan`.

### music[] list
Byte-for-byte unchanged by this pass (verified via `git diff HEAD` — indices 0/7/16 are context lines, not edits).

### Tests (`pytest -q`, SERVO_SIM=1)
- `tests/test_cli.py tests/test_proxies.py`: 12 passed.
- Full suite: 248 passed in ~96s. No servo sim / collision-limit verification was run (per project safety steering).
