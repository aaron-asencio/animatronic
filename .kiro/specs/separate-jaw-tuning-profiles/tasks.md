# Implementation Plan: separate-jaw-tuning-profiles

## Overview

Introduce a single new leaf module `config_store.py` (stdlib `os`/`json` only) that owns two independent jaw-tuning profiles — File_Profile and Mic_Profile — persisted to one shared JSON file with deterministic path resolution. Wire it into `AudioPlayer` (file profile), `AudioStreamer` (mic profile), and `micwebcontroller.py` (profile-aware endpoints), preserving the existing `talk()` jaw-mapping math byte-for-byte. Follow project conventions: `print()` debug output and Google-style docstrings with `Args:` sections. Property tests use `hypothesis`; example/edge tests use `pytest`, run with `pytest -q --maxfail=1`.

## Tasks

- [x] 1. Create the `config_store.py` module with defaults and path resolution
  - [x] 1.0 Create the module scaffold, constants, and path resolution
    - Create `config_store.py` importing only `os` and `json`
    - Define module constants: `DEFAULT_SENSITIVITY` (500), `DEFAULT_NOISE_FLOOR` (600), `DEFAULT_DROP_THRESHOLD` (0.20), `PROFILE_FILE` ("file"), `PROFILE_MIC` ("mic"), `ALLOWED_PROFILES` tuple, `CONFIG_FILENAME` (".animatronic_jaw_config.json"), `CONFIG_PATH_OVERRIDE_ENV` ("ANIMATRONIC_JAW_CONFIG")
    - Implement `_default_profile()` returning a fresh mutable default dict
    - Implement `ConfigStore.__init__(config_path=None)` and `ConfigStore.resolve_config_path()` static method mirroring `Animatronic._resolve_audio_dir` precedence: env override → `SUDO_USER` home → `expanduser('~')`
    - Use `print()` for debug output and Google-style docstrings with `Args:` sections on every public function
    - _Requirements: 1.1, 6.4, 7.1, 7.2, 7.3, 7.4, 8.3, 8.4_

  - [x]* 1.1 Write property test for deterministic path resolution
    - **Property 7: Path resolution is deterministic**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    - Generate random combinations of `ANIMATRONIC_JAW_CONFIG`, `SUDO_USER`, and home; assert repeated `resolve_config_path()` calls return the identical string

  - [x]* 1.2 Write unit tests for path-resolution branches
    - Override set → override value; no override + `SUDO_USER` → `/home/<sudo_user>/...`; neither → `expanduser('~')` path
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 2. Implement profile read logic in `ConfigStore`
  - [x] 2.1 Implement `load_profiles()` and `load_profile(name)`
    - `load_profiles()` starts from a default pair, opens the Config_File, and fills per-field from stored JSON when present
    - Catch `FileNotFoundError` (print notice, return defaults) and `json.JSONDecodeError`/`ValueError`/`OSError` (print notice, return defaults) so it never raises for missing/corrupt files
    - Fill any missing profile or missing field from defaults during the merge
    - `load_profile(name)` validates `name` against `ALLOWED_PROFILES` (raise `ValueError` otherwise) and returns a mutable copy of that profile
    - _Requirements: 1.1, 1.2, 1.3, 6.4, 6.5_

  - [x]* 2.2 Write property test for corrupt or missing config yielding defaults
    - **Property 6: Corrupt or missing config yields defaults**
    - **Validates: Requirements 6.4, 6.5**
    - Generate arbitrary text/bytes (malformed JSON and wrong shapes) into a temp file; assert `load_profiles` returns the default pair and does not raise

  - [x]* 2.3 Write unit tests for loaded config shape and missing-file defaults
    - Loaded config exposes both `file` and `mic` profiles, each with the three fields
    - Missing file → both profiles equal defaults (500 / 600 / 0.20)
    - _Requirements: 1.1, 1.2, 1.3, 6.4_

- [x] 3. Implement profile write and update logic in `ConfigStore`
  - [x] 3.1 Implement `save_profiles(profiles)`, `update_profile(name, updates)`, and module-level wrappers
    - `save_profiles` writes a single JSON object `{"version": 1, "profiles": {"file": {...}, "mic": {...}}}`, always writing both profiles; print the written path
    - `update_profile` validates `name` against the allowlist, loads both profiles, merges `updates` into the named profile only, persists both, and returns the updated pair
    - Add module-level `_default_store = ConfigStore()` and `load_profile(name)` wrapper
    - _Requirements: 1.4, 1.5, 4.5, 6.1, 6.2, 6.3_

  - [x]* 3.2 Write property test for profile update isolation
    - **Property 2: Profile updates are isolated**
    - **Validates: Requirements 1.4, 1.5, 4.3**
    - Generate a random profile pair and a valid single-profile update; assert the untouched profile is byte-for-byte identical

  - [x]* 3.3 Write property test for persistence round-trip
    - **Property 3: Persistence round-trip**
    - **Validates: Requirements 4.5, 6.2, 6.3**
    - Generate a random valid profile pair, `save_profiles` to a temp path, `load_profiles`, assert equality

  - [x]* 3.4 Write unit test for single-file both-profile persistence
    - After a write, the single JSON file on disk contains both `file` and `mic` profiles
    - _Requirements: 6.1_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Wire the store into `audio_player.py`
  - [x] 5.1 Load File_Profile in `AudioPlayer.__init__`
    - Import `load_profile, PROFILE_FILE` from `config_store`
    - Change constructor signature to `sensitivity=None, noise_floor=None, drop_threshold=None`; load the File_Profile and assign the three attributes, letting explicit args override
    - Leave `talk()` and the jaw-mapping math unchanged
    - Update the docstring to Google-style with an `Args:` section describing the optional overrides
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 6. Wire the store into `audio_streamer.py`
  - [x] 6.1 Load Mic_Profile in `AudioStreamer.__init__`
    - Import `load_profile, PROFILE_MIC` from `config_store`
    - Change constructor signature to `sensitivity=None, noise_floor=None, drop_threshold=None`; load the Mic_Profile and assign the three attributes, letting explicit args override
    - Leave `talk()` and the effects methods unchanged
    - Update the docstring to Google-style with an `Args:` section describing the optional overrides
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x]* 6.2 Write property test for the shared jaw-value mapping
    - **Property 1: Jaw-value mapping preserves the gate → scale → snap semantics**
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.5, 3.2, 3.3, 3.4, 3.5**
    - Test the pure `(peak, previous, profile) → jaw_value` mapping over generated inputs: 0.0 when peak < noise_floor; else `min(100, peak / sensitivity * 100)`; snapped to 0.0 when falling and ratio to previous is below drop_threshold. Covers both consumers since the logic is shared

- [x] 7. Make `micwebcontroller.py` profile-aware
  - [x] 7.1 Replace `jaw_config` with a `ConfigStore` instance and profile pair
    - Import `ConfigStore, ALLOWED_PROFILES, PROFILE_MIC` from `config_store`
    - Instantiate `config_store = ConfigStore()` and `jaw_profiles = config_store.load_profiles()` at startup
    - Update the live-mic `talk()` lookups to read `jaw_profiles[PROFILE_MIC][...]` for the three fields; leave gate/scale/drop branches otherwise unchanged
    - _Requirements: 1.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 7.2 Add profile selector and validation to `POST /config`
    - Read the `profile` selector and check it against `ALLOWED_PROFILES` (400 on miss) before any mutation
    - Validate each supplied field: sensitivity > 0, noise_floor >= 0, drop_threshold in [0.0, 1.0]; return 400 on any violation before mutating
    - Only after full validation, call `config_store.update_profile(profile_name, updates)`, reassign the global `jaw_profiles`, print the update, and return success with echoed fields and both profiles
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 5.1, 5.2, 5.3, 5.4, 8.1, 8.2, 8.3_

  - [x] 7.3 Update `GET /status` to return both profiles
    - Return `jaw_profiles` (both `file` and `mic`) alongside streaming status and effects config
    - _Requirements: 4.4_

  - [x]* 7.4 Write property test for out-of-bounds rejection without mutation
    - **Property 4: Out-of-bounds values are rejected without mutation**
    - **Validates: Requirements 5.1, 5.2, 5.3, 8.1**
    - Using the Flask test client with the store pointed at a temp file, generate updates with at least one out-of-bounds field; assert an error response and unchanged stored state

  - [x]* 7.5 Write property test for unknown-profile rejection without mutation
    - **Property 5: Unknown profile names are rejected without mutation**
    - **Validates: Requirements 5.4, 8.2**
    - Generate random strings excluded from the allowlist; assert an error response and unchanged stored state

  - [x]* 7.6 Write unit tests for valid updates and status echo
    - `POST /config` with a valid file update and with a valid mic update each return 200 with echoed fields
    - `GET /status` returns both profiles with the currently stored values
    - _Requirements: 4.1, 4.2, 4.4_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP.
- Each task references specific requirements (granular sub-requirement clauses) for traceability.
- Checkpoints ensure incremental validation; run tests with `pytest -q --maxfail=1`.
- Property tests use `hypothesis` (minimum 100 iterations each); tag format `Feature: separate-jaw-tuning-profiles, Property {n}: {text}`.
- Hardware-dependent construction (`gpiozero`, `pyaudio`) is out of scope for tests — the value-loading logic is verified by pointing the store at a temp file and asserting the three attributes match the loaded profile.
- Convention checks (`print` not `logging`, Google-style docstrings with `Args:` sections) are review-enforced rather than property tests (Requirements 8.3, 8.4).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.0"] },
    { "id": 1, "tasks": ["1.1", "1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "5.1", "6.1"] },
    { "id": 4, "tasks": ["6.2", "7.1"] },
    { "id": 5, "tasks": ["7.2"] },
    { "id": 6, "tasks": ["7.3"] },
    { "id": 7, "tasks": ["7.4", "7.5", "7.6"] }
  ]
}
```
