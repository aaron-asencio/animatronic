# Voice-FX Style-Change Stream Crash — Bugfix Design
# Voice-FX Style-Change Stream Crash — Bugfix Design

## Overview

The live microphone path in `src/micwebcontroller.py` goes silent whenever a Voice-FX **style**
is selected before or during an active mic session. The root cause is a **data race on shared DSP
state** between two threads that touch the same objects with **no lock**:

- the **PortAudio callback thread** (`audio_callback` inside `stream_mic()`), which runs
  `talk()` + `apply_effects()` on *every* audio frame, and
- the **Flask request thread** (`threaded=True`), which handles `POST /effects` and, for the
  style branch, calls `_apply_chain_to_live()` then `_reset_audio_dsp_state()`.

The stream-killing fault is in `_fx_echo`: it iterates `audio_state['echo_buffer']`
(`for i, old in enumerate(audio_state['echo_buffer'])`) while the request thread's
`_reset_audio_dsp_state()` **rebinds** `audio_state['echo_buffer']` to a brand-new `deque`. A
Python `deque` that is replaced/mutated mid-iteration raises `RuntimeError: deque mutated during
iteration` **inside the callback**; PortAudio treats an unhandled callback exception as fatal and
stops the stream, so the mic is silent for the rest of the session.

The fix is **synchronization only** — the DSP math is untouched. This design evaluates a lock vs.
a lock-free snapshot/atomic-rebind approach and selects a **combination**: a short
`threading.Lock` serialising the callback's whole-frame DSP access against the request thread's
rare mutators, *plus* a whole-chain atomic rebind of `effects_config` so the callback can never
observe a half-updated chain. The fix stays confined to `src/micwebcontroller.py`;
`audio_player.py` and `performance.py` are uninvolved.

## Glossary

- **Bug_Condition (C)** — a style/effects mutation on the Flask request thread
  (`_apply_chain_to_live` + `_reset_audio_dsp_state`, or an in-place `effects_config[name]` edit)
  runs **concurrently** with an in-flight PortAudio callback that is reading/iterating the same
  shared DSP state inside `apply_effects` / `_fx_echo` / `_fx_reverb` / `_fx_tremolo` /
  `_fx_ring_mod`, with no lock serialising the two threads.
- **Property (P)** — for buggy inputs, no `RuntimeError` escapes the callback, the stream keeps
  producing output, and no torn `effects_config` read occurs.
- **Preservation** — for all non-racing behaviour (a fully-applied chain processing audio, the
  `/effects`/`/effects/save`/`/effects/revert`/`/config` endpoints, routine audio,
  start/stop + GPIO release), the fixed module behaves identically to the original.
- **Callback thread** — PortAudio's own thread, which invokes `audio_callback` (defined inside
  `stream_mic()`) once per `CHUNK` (1024 frames). It calls `talk()` then `apply_effects()`.
- **Request thread** — a Flask worker thread (`app.run(..., threaded=True)`) handling an HTTP
  request such as `POST /effects`.
- **Shared DSP state** — the module/process globals the two threads both touch:
  `audio_state['echo_buffer']` (a `deque`, `maxlen=3`), `audio_state['reverb_buffer']` /
  `audio_state['reverb_pos']`, the `effects_config` dict, and the `effect_state` phase dict.
- **`_reset_audio_dsp_state()`** — request-thread helper that clears transient DSP energy/phase:
  rebinds `echo_buffer` to a fresh zero-filled `deque`, rebinds `reverb_buffer` to a fresh zero
  buffer with `reverb_pos = 0`, zeroes the tremolo/ring phases, and resets the jaw envelope
  globals.
- **`_apply_chain_to_live(chain)`** — request-thread helper that overwrites `effects_config`
  **in place**, key by key (`effects_config[name] = dict(params)`).

## Bug Details

### Bug Condition

The bug manifests when the Flask request thread mutates the shared DSP state (style branch:
`_apply_chain_to_live` + `_reset_audio_dsp_state`; or single-effect branch: in-place
`effects_config[name]` edit) at the same instant the PortAudio callback thread is inside
`apply_effects` reading/iterating that same state. There is currently **no lock** anywhere in the
module (confirmed: the only `threading.` use is `threading.Thread` in `start_streaming`).

Two distinct hazards fall under this condition:

1. **Deque mutated-during-iteration (the crash — bugfix 1.1, 1.2, 1.3).** `_fx_echo` runs
   `for i, old in enumerate(audio_state['echo_buffer'])` and then `.append(...)` to the same
   deque. `_reset_audio_dsp_state()` rebinds `audio_state['echo_buffer']` to a *new* deque on the
   request thread. If the rebind lands while the callback is mid-iteration, Python raises
   `RuntimeError: deque mutated during iteration` inside the callback → PortAudio stops the stream
   → silence.
2. **Torn `effects_config` read (secondary non-crash hazard — bugfix 1.5).** `apply_effects` reads
   `cfg = effects_config` and then dereferences `cfg['echo']['enabled']` / `['amount']` etc. field
   by field across the chain. `_apply_chain_to_live` overwrites keys in place one at a time, so the
   callback can observe a chain that is half old / half new (e.g. an effect flipped `enabled=True`
   while its `amount` is still the old value, or a mix of two presets). This does not crash but can
   momentarily apply an inconsistent chain.

Note on reverb: `_reset_audio_dsp_state()` rebinds `reverb_buffer` to a **length-stable** 9600-sample
array and `_fx_reverb` indexes `buf[pos] / pos = (pos + 1) % blen`, so a concurrent swap cannot
`IndexError` (the length never changes). It is still an **unsynchronized read/write** of shared
state (the callback could read from the old buffer and write `reverb_pos` that then applies to a
freshly-swapped buffer), so it is covered by the same synchronization fix even though it is not the
crash trigger.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT:  input describing concurrent access to the shared DSP state
  OUTPUT: boolean

  RETURN  input.callback_in_flight                 // a PortAudio callback is running
          AND input.callback_reads_shared_dsp      //   apply_effects reading echo_buffer /
                                                   //   reverb_buffer / effects_config / effect_state
          AND input.request_mutates_shared_dsp     // /effects style or single-effect branch
                                                   //   mutating that same state on the request
                                                   //   thread, with NO lock serialising the two
END FUNCTION
```

### Examples

- **Mid-session style change (crash).** Mic streaming with `echo` enabled; operator selects
  "ghost". The request thread runs `_reset_audio_dsp_state()` and rebinds `echo_buffer` exactly as
  the callback is in `for i, old in enumerate(audio_state['echo_buffer'])`.
  Actual: `RuntimeError: deque mutated during iteration` kills the stream; mic silent.
  Expected: new chain applies on the next frames; audio keeps flowing.
- **Style change just before start (crash).** Style selected, then stream started; the first
  callbacks race the just-reset state.
  Actual: same `RuntimeError`. Expected: clean start, audio flows.
- **Torn config read (secondary).** Operator switches "natural" → "possessed" mid-frame; the
  callback reads `pitch.enabled=True` (new) but `distortion.amount` still old for one frame.
  Actual: one inconsistent-chain frame. Expected: the callback sees either the whole old chain or
  the whole new chain, never a mix.
- **Reverb-only, mid-session reset (no crash, still a race).** `reverb` enabled; style change swaps
  `reverb_buffer`. Actual: no `IndexError` (length-stable), but an unsynchronized read/write.
  Expected: synchronized swap.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors (must continue to work exactly as before):**
- DSP output for a fully-applied chain with **no** concurrent style change is byte-for-byte
  identical to the current code (same input + same seeded state → same output). The fix is
  synchronization, not effect math (bugfix 3.1).
- `/effects` (style branch, single-effect branch), `/effects/save`, `/effects/revert`, and
  `/config` return the same responses and persist the same `ConfigStore` state as before
  (bugfix 3.2).
- Routine audio (`audio_player.py` / `performance.py`) is untouched — it does not use the shared
  effects engine, `effects_config`, or the echo/reverb buffers (bugfix 3.3).
- `start_streaming` / `stop_streaming` / `_release_gpio` behave identically, including the
  `mic-gpio-line-release-leak` pin-factory release and the `is_streaming` callback short-circuit
  (bugfix 3.4).
- `_reset_audio_dsp_state()` still clears stale echo/reverb energy and effect phase (bugfix 2.4) —
  only *how* it swaps becomes thread-safe.

**Scope:** Any execution in which the request thread is **not** mutating DSP state while a callback
is reading it (`NOT isBugCondition`) must be completely unaffected: single-threaded frame
processing, idle streams, config/profile endpoints, and the stream lifecycle.

## Hypothesized Root Cause

This root cause is **confirmed by code inspection**, not hypothetical:

1. **No synchronization exists.** A repository-wide check shows the module's only `threading.`
   reference is `threading.Thread` in `start_streaming`; there is no `Lock`/`RLock`. The callback
   thread and request thread share `audio_state`, `effects_config`, and `effect_state` with nothing
   serialising them.

2. **Deque rebind vs. iteration (the stream killer).** `_fx_echo` iterates
   `audio_state['echo_buffer']`; `_reset_audio_dsp_state()` rebinds it to a new `deque`. Replacing
   the object a `for` loop is iterating raises `RuntimeError: deque mutated during iteration`. The
   `/effects` style branch calls `_reset_audio_dsp_state()` on every style selection, so every
   style change during an active stream can trigger it.

3. **Unhandled callback exception stops PortAudio.** `audio_callback` has no `try/except`; a raised
   `RuntimeError` propagates out of the callback, which PortAudio treats as fatal — the stream stops
   and never resumes (observed silence).

4. **In-place `effects_config` edits (secondary).** `_apply_chain_to_live` and the single-effect
   branch mutate `effects_config` in place while `apply_effects` reads it field by field, allowing a
   torn/half-updated chain read.

## Correctness Properties

Property 1: Bug Condition — Style change never crashes or stalls the live callback

_For any_ input where the bug condition holds (`isBugCondition` returns true) — a style/effects
mutation on the request thread concurrent with an in-flight callback reading the shared DSP state —
the fixed module SHALL ensure no `RuntimeError` (notably "deque mutated during iteration") escapes
the callback, the stream keeps producing output, and the callback never observes a torn
(half-updated) `effects_config` chain.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation — Non-racing behaviour is unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns false) — no
concurrent request-thread mutation while a callback reads DSP state — the fixed module SHALL produce
the same result as the original: identical DSP output for a fully-applied chain, identical
`/effects`/`/effects/save`/`/effects/revert`/`/config` responses and persisted `ConfigStore` state,
untouched routine audio, and unchanged start/stop + GPIO-release behaviour. `_reset_audio_dsp_state`
still clears stale echo/reverb energy and effect phase.

**Validates: Requirements 2.4, 3.1, 3.2, 3.3, 3.4**

## Fix Design — Options Evaluated and Decision

### Option A — Lock around all shared DSP access

Introduce one module-level `threading.Lock` (`_dsp_lock`). The callback holds it for the whole
`apply_effects` (and the `effect_state`/buffer touches inside it); the request-thread mutators
(`_apply_chain_to_live`, `_reset_audio_dsp_state`, the single-effect `effects_config[name]` edit)
hold it for their (tiny) critical sections.

- **Pro:** simplest to reason about; one lock closes every hazard (deque, reverb, torn config,
  phase) at once. Correct for one consumer (callback, every frame) + one rare producer (request).
- **Real-time constraint:** the callback runs under PortAudio timing, so the critical section must
  be short and bounded. The request-thread critical sections are *tiny* — a dict overwrite plus two
  buffer reallocations — so the callback can never block more than a few microseconds waiting on
  them, well within one `CHUNK` period (1024/48000 ≈ 21 ms). Holding the lock for the whole
  `apply_effects` is therefore acceptable: the only contender is the rare, fast request mutator, and
  `apply_effects` does no blocking I/O.
- **Con:** the lock spans the DSP math even though the math itself is not the hazard. Acceptable
  given the tiny opposing critical section, but slightly coarser than necessary.

### Option B — Lock-free snapshot + atomic rebind

Make the callback snapshot the mutable containers at frame start
(`buf = audio_state['echo_buffer']`, `cfg = effects_config`, iterate/read the locals) and make
mutators build a **new** object and rebind the shared slot in a single assignment (atomic under the
GIL).

- **Deque:** snapshotting + rebind-on-reset closes the iteration-vs-rebind race *because* `_fx_echo`
  both iterates and appends to **its own local reference** for the frame; `_reset` only swaps the
  shared slot `audio_state['echo_buffer']`, which the next frame picks up. The appended-to deque is
  the callback's current reference, so iteration and append stay consistent within a frame.
- **`effects_config`:** change `_apply_chain_to_live` to build a whole new dict and rebind
  `effects_config` in one assignment (whole-chain swap) instead of per-key in-place edits, so the
  callback's `cfg = effects_config` snapshot is always a complete chain — no torn read.
- **Pro:** no lock in the hot path; the callback never blocks.
- **Con:** more invasive and subtler to prove — the single-effect `/effects` branch also edits
  `effects_config` in place and would likewise need to become a rebuild-and-rebind; `effect_state`
  phases are read-modify-written by the callback itself, so "atomic rebind" does not naturally apply
  to them (they are owned by the callback and only *reset* by the request thread). Correctness
  depends on every mutation site following the discipline exactly.

### Decision — Option A (a short lock), with Option B's whole-chain `effects_config` swap folded in

**Use a single module-level `threading.Lock` serialising (a) the callback's whole-frame DSP access
and (b) every request-thread DSP mutator.** This is the simplest provably-correct design for one
frequent consumer + one rare producer, and the real-time constraint is satisfied because the
request-thread critical sections are tiny and non-blocking. On top of the lock, convert
`effects_config` mutations to **whole-chain rebinds under the lock** so the structure is swapped
atomically rather than edited field-by-field — this keeps the critical sections minimal and removes
any dependency on per-key edit ordering, directly addressing the torn-read hazard (bugfix 1.5 /
2.2). The lock makes the design robust even where the pure snapshot discipline is awkward
(`effect_state` phases, the reverb read/write pair).

Rationale for not going pure lock-free: the callback's only lock contender is the rare style/effects
request, and the request's critical section is a handful of assignments — so lock contention is
negligible and the lock buys a single, auditable correctness argument instead of a per-site
discipline that is easy to break in future edits.

### Changes Required

**File:** `src/micwebcontroller.py` (only).

1. **Add a module-level lock.** Define `_dsp_lock = threading.Lock()` near the other module globals.
   Document that it guards all shared DSP state (`audio_state` echo/reverb entries, `effects_config`,
   `effect_state`) across the callback thread and the request thread.

2. **Guard the callback's DSP work.** In `audio_callback`, keep the existing `is_streaming`
   short-circuit *first* (unchanged — bugfix 3.4), then hold `_dsp_lock` around the
   `talk()` + `apply_effects()` call (the state-reading work). The critical section stays short
   (pure CPU DSP, no I/O). The `is_recording` append of the already-computed bytes may stay outside
   the lock (it touches only `record_frames`, not DSP state).
   - `talk()` reads `jaw_profiles[PROFILE_MIC]` and the jaw-envelope globals; the request thread's
     `_reset_audio_dsp_state()` also writes those envelope globals, so including `talk()` in the
     locked region keeps the envelope reset race-free too. (`/config` reassigns the `jaw_profiles`
     dict reference atomically; `talk()` reads it once — unchanged behaviour.)

3. **Guard the request-thread mutators** with the same `_dsp_lock`:
   - `_reset_audio_dsp_state()` builds the fresh `deque` / `reverb_buffer` first, then takes the
     lock to perform the rebinds + phase/envelope zeroing (build-new-then-atomic-rebind). Its DSP
     **intent is preserved** (still a fresh zero-filled echo deque, fresh zero reverb buffer at
     pos 0, zeroed tremolo/ring phase, reset envelope) — only the write is now serialised.
   - `_apply_chain_to_live(chain)` builds a complete new `effects_config` dict from the chain and
     rebinds the module global in one assignment, under the lock (whole-chain swap). This replaces
     the current per-key in-place loop and removes the torn-read window.
   - The `/effects` **single-effect** branch (toggle `enabled` / set `amount`) builds an updated
     copy of the affected effect (and the dict) and rebinds under the lock, so even a single-field
     change is atomic from the callback's perspective. (Simplest consistent rule: *all*
     `effects_config` writes go through a rebind under the lock.)
   - The `/effects` style branch keeps its current order — `_apply_chain_to_live(...)` then
     `_reset_audio_dsp_state()` — each now internally lock-guarded; `current_style` / logging
     unchanged.

4. **Decide on belt-and-suspenders callback guard — RECOMMEND INCLUDING a minimal one.** Wrap the
   locked `talk()`+`apply_effects()` in a `try/except Exception` that, on an unexpected error, logs
   via `print(...)` (project convention — no `logging`) and returns passthrough
   `(in_data, pyaudio.paContinue)` so a *future* unforeseen error can never silently kill the
   stream. This is **defense-in-depth, explicitly secondary** to the synchronization fix and MUST
   NOT be relied on to hide the real race — the lock is what makes the deque/`effects_config` races
   impossible; the `try/except` only prevents a *different, unknown* future exception from being
   fatal. (If the reviewer prefers the smallest possible diff, the `try/except` can be dropped
   without weakening Property 1, since the lock already closes the known races. Recommendation:
   include it — it is cheap, matches the hardware-safety "never silently die" posture, and does not
   mask the primary fix because the concurrency tests still assert on the *locked* behaviour.)

5. **Keep everything else byte-identical:** no change to any `_fx_*` math, `apply_effects` ordering,
   `start_streaming` / `stop_streaming` / `_release_gpio` logic, the GPIO line-release fix, the
   `/config` / `/effects/save` / `/effects/revert` handlers' persistence, or the in-code GPIO safety
   (writes still through gpiozero devices in `talk`; pins still released in `_release_gpio`).

## Testing Strategy

### Validation Approach

Two-phase: first surface the crash on **unfixed** code with a *thread-faithful* concurrency test,
then verify the fix stops the crash while preserving DSP output and endpoint behaviour. Because the
defect is a race, **a single-threaded in-process test cannot surface it** — the exploration test
MUST run two real threads.

### Exploratory Bug Condition Checking (THREAD-FAITHFUL — critical)

**Goal:** reproduce `RuntimeError: deque mutated during iteration` (and/or a torn-config read) on
the UNFIXED module, confirming the root cause.

**Test Plan — concurrency harness:**
- Import `micwebcontroller` with the existing hardware stubs (see "Stub note" below).
- Enable a chain that iterates the echo buffer: set `effects_config['echo']['enabled'] = True`
  (and a non-zero amount). Keep the chain otherwise simple so the loop is tight and fast.
- **Consumer thread:** call `micwebcontroller.apply_effects(frame)` in a tight bounded loop
  (e.g. up to N iterations or until a shared `stop`/`error` flag is set), with a fixed small
  int16 `frame` of length `CHUNK`. This is the callback-equivalent (it runs the same `_fx_echo`
  iterate+append over `audio_state['echo_buffer']`).
- **Producer thread:** repeatedly call `micwebcontroller._reset_audio_dsp_state()` (the style-change
  path's reset) in a tight bounded loop — this is what rebinds `echo_buffer`.
- Run both threads concurrently; capture any exception raised in the consumer into a shared slot.
- **Assertion:** over the whole run, **no `RuntimeError` escapes** the consumer and it keeps
  producing output (every `apply_effects` call returns an int16 array of length `CHUNK`).

**Reliable reproduction on unfixed code while staying CI-friendly:**
- Keep both threads **busy and overlapping** (both in tight loops) to maximise the interleaving that
  triggers the deque fault; a few thousand iterations per thread reproduces it reliably in practice.
- Bound the run with an **iteration cap and a wall-clock timeout** (e.g. join threads with a
  timeout, then stop) so the test is fast and deterministic enough for CI and never hangs.
- On UNFIXED code the consumer thread raises `RuntimeError: deque mutated during iteration` → the
  shared error slot is set → **test FAILS** (confirms the bug).
- On FIXED code the lock serialises the two threads → no error, output keeps flowing → **test
  PASSES**.
- The same harness, with `_apply_chain_to_live` switching between two different presets on the
  producer thread and the consumer asserting it only ever reads a *complete* preset, covers the
  torn-config hazard (bugfix 1.5 / 2.2).

### Fix Checking

**Goal:** for all inputs where the bug condition holds, the fixed module produces the expected
behaviour.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := run_concurrent(apply_effects_loop, reset_or_chain_swap_loop)
  ASSERT no RuntimeError escaped the consumer
         AND consumer kept returning int16[CHUNK] output
         AND every observed effects_config read was a complete chain
END FOR
```

### Preservation Checking

**Goal:** for all non-buggy inputs, the fixed module equals the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT apply_effects_fixed(input, seeded_state) == apply_effects_original(input, seeded_state)
  ASSERT endpoint_response_fixed(input) == endpoint_response_original(input)
END FOR
```

**Testing Approach:** property-based testing (hypothesis, ≥100 examples) is used for the universal
preservation claims (DSP equality across a range of inputs; endpoint acceptance/rejection across
valid/invalid bodies), because it generates many cases and catches edge cases a few unit tests would
miss.

**Test Plan — observe-then-assert:**
1. **DSP output equality (single-threaded).** With a *fixed, fully-applied* chain and a **seeded**
   starting DSP state (seed the echo deque / reverb buffer / phases to known values, or
   `_reset_audio_dsp_state()` first for a clean slate), feed a fixed input sequence through
   `apply_effects` and record the exact output bytes. Assert the fixed module produces identical
   bytes to the recorded baseline (bugfix 3.1). Property-based variant: for random valid chains +
   random int16 input, assert output length is `CHUNK` and dtype int16, and that running the same
   seeded state twice is deterministic.
2. **Endpoint behaviour (Flask test-client + temp-store).** Reuse the fixture pattern from
   `tests/test_micwebcontroller.py` (hardware stubs installed before import; per-test temp
   `ConfigStore`). Assert `/effects` style/single-effect, `/effects/save`, `/effects/revert`, and
   `/config` return the same statuses/payloads and persist the same `ConfigStore` state as before
   the fix (bugfix 3.2). Property-based variant over valid effect names/amounts and valid style
   names (≥100 examples).
3. **Lifecycle unaffected.** Assert the lock/rebind changes do not alter
   `start_streaming` / `stop_streaming` / `_release_gpio` observable behaviour (bugfix 3.4) — these
   paths are exercised with the stubs and must behave as today.

### Unit Tests

- `apply_effects` output equality against a recorded baseline for a fixed seeded chain (3.1).
- `_reset_audio_dsp_state` still yields a fresh zero echo deque (len 3, maxlen 3), fresh zero reverb
  buffer (len 9600) at pos 0, zeroed phases, reset envelope (2.4).
- `_apply_chain_to_live` whole-chain swap leaves `effects_config` a complete, correct chain.
- `/effects`, `/effects/save`, `/effects/revert`, `/config` happy-path + rejection responses (3.2).

### Property-Based Tests (hypothesis, ≥100 examples)

- Random valid effect chains + random int16 input → `apply_effects` returns int16[CHUNK],
  deterministic for a fixed seeded state.
- Random valid `/effects` and `/config` bodies → accepted; invalid bodies → rejected without
  mutation (mirrors existing Property 4 / 5 patterns).

### Integration / Concurrency Tests

- The thread-faithful harness above (consumer `apply_effects` loop vs. producer
  `_reset_audio_dsp_state` / chain-swap loop): FAILS on unfixed, PASSES on fixed (Property 1).
- A combined run: start/alternate a style change while the consumer loop runs, asserting continuous
  output and no exception (end-to-end of the live-session scenario).

### Tooling, Stubs, and Safety

- **Compile check:** `python -m py_compile src/micwebcontroller.py` before running anything.
- **Run tests:** `.venv/bin/python -m pytest -q` (quiet; `--maxfail=1` for focused runs per the
  testing standard).
- **No `SERVO_SIM`** — this fix touches no servo/movement code, so no collision/limit simulation is
  involved. In-code GPIO safety stays intact (writes through gpiozero devices in `talk`; pins
  released in `_release_gpio`).
- **Stub note (gpiozero `Device`):** the effects-engine/concurrency tests import
  `micwebcontroller` with the same hardware stubs used in `tests/test_micwebcontroller.py`. That
  stub already provides `Device` (`_gpiozero_stub.Device = MagicMock(...)`), required because
  `micwebcontroller` imports `from gpiozero import Device` for the GPIO line-release fix. Any new
  test module that installs its own gpiozero stub MUST likewise set a `Device` attribute (and
  `PWMLED`, `LED`, `DigitalOutputDevice`) so the import succeeds.

## Requirements → Design → Verification Traceability

| Requirement (bugfix.md) | Design element | How verified |
|---|---|---|
| 1.1 style change swaps `echo_buffer` concurrently with `_fx_echo` iteration → `RuntimeError` | Root cause §Bug Details(1), §Hypothesized Root Cause(2) | Exploration concurrency test raises/exposes the `RuntimeError` on unfixed code (FAILS) |
| 1.2 `RuntimeError` in callback stops the stream (silence) | §Hypothesized Root Cause(3); fix removes the raise via `_dsp_lock` (+ optional `try/except` belt) | Fix Checking: consumer keeps producing output after fix |
| 1.3 fails both before-start and mid-session | §Bug Details examples (both cases); lock guards all callbacks incl. the first | Concurrency test (racing resets vs. loop) + integration run covering both orders |
| 1.4 Routine audio unaffected | §Overview / Preservation; fix confined to `micwebcontroller.py` | Preservation: `audio_player.py`/`performance.py` untouched (no code change) |
| 1.5 torn `effects_config` read (secondary) | Whole-chain atomic rebind of `effects_config` under `_dsp_lock` (Fix Changes 3) | Concurrency test asserts only complete chains are observed; unit test on whole-chain swap |
| 2.1 keep stream producing audio across style change | `_dsp_lock` serialises callback vs. mutators (Fix Changes 2–3) | Fix Checking: no error, continuous output |
| 2.2 apply new chain mid-stream, no crash / no torn read | Lock + whole-chain swap | Concurrency tests (deque + torn-read variants) PASS on fixed |
| 2.3 serialise/snapshot shared DSP access | Decision: single `_dsp_lock` across callback + mutators | Concurrency test is the direct check; code review of all access sites |
| 2.4 still clear stale echo/reverb energy + phase, race-free | `_reset_audio_dsp_state` build-new-then-rebind under lock, intent preserved | Unit test: fresh zero deque/reverb/phases after reset |
| 3.1 identical DSP output for fully-applied chain | No `_fx_*`/`apply_effects` math change; lock only | Preservation: byte-equality vs. recorded baseline; PBT determinism |
| 3.2 `/effects`,`/effects/save`,`/effects/revert`,`/config` + ConfigStore unchanged | Mutators rebind but keep responses/persistence identical | Flask test-client + temp-store tests; PBT over valid/invalid bodies |
| 3.3 Routine audio unchanged | Fix confined to `micwebcontroller.py` | No change to `audio_player.py`/`performance.py` |
| 3.4 start/stop + GPIO line-release unchanged | `is_streaming` short-circuit kept first; `_release_gpio` untouched | Lifecycle tests; code review that lock/rebind don't alter start/stop/release |
