# Gesture-action cleanup: remove `come`, remove `no`, rename `nod`→`yes`

Three mechanical action-name edits across the controller CLI, the Flask control
panel, the movements module, and the README. `come` is removed as a gesture
action and its coroutine deleted; the redundant `no` head-shake action is
removed in favor of the surviving `shakeHead`; and the `nod` action key is
renamed to `yes` while its backing coroutine keeps the name `nod`. The diff is
confined to exactly the four files the task touches (README.md, controller.py,
movements.py, webapp.py) with +15/−60 lines, and the substring-safety carve-outs
are all honored.

Watch for: nothing blocking. One cosmetic note — the README still lists `comein`
on the gesture-actions line, which is pre-existing stale doc text (never a real
action_map key) and explicitly out of scope for this pass (confirmed).

**Verdict**: APPROVED

## High-level view

EDIT 1 removes `come` end to end: the action_map entry in controller.py, the
docstring ARM-gesture list, the webapp `MOVEMENT_ACTIONS`/`MOVEMENT_POOL`
membership, the README actions line, and the `async def come` coroutine itself.
The coroutine deletion is justified by a caller grep recorded in the notes
showing no surviving `.come(`/`mv.come`/`self.come` reference — only a
commented-out `__main__` demo line, also removed. The distinct `come_here`/
`comeHere` gesture is left intact, which the diff confirms.

EDIT 2 removes the `no` action (`'no': mv.shake_no`) from the action_map and from
the webapp action set and pool, and drops `shakeNo` from the controller
docstring. Critically, the `shake_no` coroutine is NOT deleted — the diff's only
movements.py deletion is `come` — so the `blah` Performance that depends on it
and its collision test remain intact. `smno`/`small_shake_no` and
`shakeHead`/`shake_head` are kept.

EDIT 3 renames the action key `'nod'` to `'yes'` while the value stays `mv.nod`,
so the coroutine is untouched. The rename is propagated to the controller
docstring, the argparse `--action` help example, the webapp action set, the
movement pool, and the Node-RED history comment, which now accurately describes
`yes` and the removal of the redundant `no`.

Verification evidence is present in the notes: py_compile over the three changed
Python files passed, the `blah` collision suite passed (27 tests), and the
reference sweep for the removed/renamed action names came back clean. Per the
testing steering, the SERVO_SIM angle/collision checks were correctly skipped —
this is a name-only change with no motion edits and the in-code safety guards are
untouched.

<details>
<summary>Issues (1)</summary>

1. **Stale `comein` in README (non-blocking)** — the gesture-actions line still
   lists `comein`, which is not an action_map key. Pre-existing and out of scope
   for this pass; flagged only so it is not mistaken for a regression. No action
   required for this task.

</details>

<details>
<summary>Details</summary>

### `come` removal is complete and the coroutine deletion is safe

The diff removes `'come': mv.come,` from the controller action_map, strikes
`come` from the ARM-gesture docstring line (leaving `wave, beckon, comeHere,
menacingReach, yawnCover, facePalm`), drops `'come'` from both
`MOVEMENT_ACTIONS` and `MOVEMENT_POOL` in webapp.py, and removes `come` from the
README actions line. The `async def come` coroutine and its commented-out
`__main__` demo invocation are deleted from movements.py.

The one risk in this edit — deleting a coroutine that still has a caller — is
addressed by the recorded caller grep (`\.come\(|mv\.come|self\.come|async def
come`), which found only the action_map entry and the definition/commented demo.
The composite that used to call it (`come_and_look`) was removed in a prior pass.
The surviving `comeHere`/`come_here` gesture is confirmed present in the diff
(`'comeHere': mv.come_here,` is retained in the action_map), so the substring
carve-out holds. (confirmed)

### `no` action removed without touching `shake_no`

The action_map loses `'no': mv.shake_no,` and the webapp loses `'no'` from the
action set and the movement pool; the docstring drops the `shakeNo` label. The
movements.py side of the diff deletes only the `come` coroutine — `shake_no` is
not in the deletion hunk — so the `blah` Performance building block and its
test coverage survive. `smno`/`small_shake_no` and `shakeHead`/`shake_head`
remain mapped. The `WEBAPP_DEV` env parsing that keys on the strings
`'no'/'false'/'off'` does not appear anywhere in the diff, so that unrelated
`'no'` literal was correctly left alone. (confirmed)

### `nod`→`yes` rename stays at the action-key layer

`'nod': mv.nod,` becomes `'yes': mv.nod,` — the value, and therefore the
coroutine name, is unchanged. The rename flows to the docstring HEAD list
(`nod`→`yes`), the argparse help example (`wave, nod, swivelHead` →
`wave, yes, swivelHead`), the webapp `MOVEMENT_ACTIONS` and `MOVEMENT_POOL`, and
the Node-RED comment, which previously claimed the canonical name was `nod` and
now correctly states the action is `yes` with the redundant `no` shake removed in
favor of `shakeHead`. The README actions line already carried `yes` and now reads
sensibly after `no`/`come` were struck. (confirmed)

### Scope and safety

The diff touches only the four in-scope files, +15/−60, with no new/backup/
duplicate files and no commit. There is no sign the prior uncommitted work or the
burp/cover-mouth rework was reverted — those files are not in the diff at all.
Verification evidence (py_compile OK; `test_blah_collision.py` 27 passed; clean
reference sweep) is recorded in the notes, and the SERVO_SIM collision/limit
simulation was appropriately skipped for this name-only change per the testing
steering, with in-code `SAFE_LIMITS` guards left intact. (confirmed)

</details>

<details>
<summary>File map</summary>

- `src/controller.py` — action_map: removed `come` and `no`, renamed `nod`→`yes`;
  docstring ARM/HEAD lists and argparse help example updated.
- `src/movements.py` — deleted the `async def come` coroutine and its
  commented-out `__main__` demo line; no other coroutine touched.
- `src/webapp.py` — `MOVEMENT_ACTIONS` and `MOVEMENT_POOL`: dropped `come`/`no`,
  renamed `nod`→`yes`; refreshed the Node-RED history comment.
- `README.md` — gesture-actions line: removed `come` and `no`, kept `yes`/`smno`.

Full diff: `git diff` in /home/aaron/workspace/animatronic-v2.

</details>
