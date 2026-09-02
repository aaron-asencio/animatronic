# Design Document

## Overview

Today the three jaw-tuning parameters (`sensitivity`, `noise_floor`, `drop_threshold`) are duplicated in three places, each with identical hardcoded defaults:

- `AudioPlayer.__init__` in `audio_player.py` (constructor kwargs `500 / 600 / 0.20`, stored as `self.sensitivity` etc.).
- `AudioStreamer.__init__` in `audio_streamer.py` (same constructor kwargs, same instance attributes).
- The module-level `jaw_config` dict in `micwebcontroller.py`, mutated live by `POST /config`.

There is no persistence: `POST /config` mutates the in-process dict only, so tuning is lost on restart, and file playback and live mic streaming are forced to share one set of values even though they have different amplitude and noise characteristics.

This feature introduces a single new module, `config_store.py`, that owns two named jaw-tuning profiles — **File_Profile** and **Mic_Profile** — persisted to one shared JSON file. `AudioPlayer` loads the file profile at init, `AudioStreamer` loads the mic profile at init, and `micwebcontroller` reads and writes both profiles through the store. The file path is resolved deterministically using the same env → `SUDO_USER` → invoking-user precedence already established by `Animatronic._resolve_audio_dir()` in `animatronic.py`, so every component (whether run directly or under `sudo`) reads and writes the same file.

The `talk()` jaw-mapping logic in `AudioPlayer` and `AudioStreamer` is **not** changed in behavior — only the source of the three parameters changes. The existing effects handling in `micwebcontroller` (`/effects`, style presets) is untouched.

## Design Goals and Constraints

- **Minimal footprint.** Reuse existing structure. The jaw-mapping math in both `talk()` methods stays byte-for-byte equivalent; only where the parameters come from changes.
- **Deterministic path resolution.** Mirror `_resolve_audio_dir()` exactly so behavior under `sudo` is predictable and shared.
- **Fail open to defaults.** A missing or corrupt config file must never crash a routine mid-show. The store returns defaults and continues.
- **Allowlist dispatch.** Per the project security convention, profile selection in the web layer goes through an explicit `{file, mic}` allowlist — never `getattr`, `eval`, or raw dict-key indexing on unvalidated input.
- **Project conventions.** `print()` for debug (no `logging`), Google-style docstrings with `Args:` sections, `snake_case`, f-strings.

## Architecture

```
                          ┌────────────────────────┐
                          │  Config_File (JSON)     │
                          │  ~/.animatronic_jaw...  │
                          └────────────┬───────────┘
                                       │ read / write
                          ┌────────────▼───────────┐
                          │  config_store.py         │
                          │  ── ConfigStore ──       │
                          │  resolve_config_path()   │
                          │  load_profiles()         │
                          │  load_profile(name)      │
                          │  save_profiles(profiles) │
                          │  update_profile(name,..) │
                          └───┬───────────┬────────┬─┘
                              │           │        │
             load File_Profile│           │        │load both / write both
                              │           │load Mic_Profile
                   ┌──────────▼──┐   ┌─────▼──────┐   ┌────▼─────────────┐
                   │ AudioPlayer │   │AudioStreamer│  │ micwebcontroller │
                   │ (file WAV)  │   │ (live mic) │   │  Flask web UI    │
                   └─────────────┘   └────────────┘   └──────────────────┘
```

`config_store.py` is a leaf module: it imports only the standard library (`os`, `json`). It sits below the audio components and the web controller in the dependency graph and never imports them, matching the "higher layers call lower ones, never the reverse" rule.

### Where the profile values are applied

The jaw-mapping computation lives in each consumer's `talk()` method and is unchanged. The only edit is the *source* of `self.sensitivity`, `self.noise_floor`, `self.drop_threshold`:

- `AudioPlayer.__init__` — instead of defaulting to literals, load the File_Profile from the store and assign the three attributes.
- `AudioStreamer.__init__` — same, but load the Mic_Profile.
- `micwebcontroller` — replace the single module-level `jaw_config` dict with two profile dicts sourced from the store, and route updates through it.

## Data Models

### Jaw_Tuning_Profile

A profile is a plain `dict` with exactly three numeric fields. Using a dict (rather than a class) keeps it JSON-serializable with no conversion layer and matches the existing `jaw_config` shape in `micwebcontroller`.

```python
# A single profile
{
    "sensitivity":    500,     # float/int, > 0      — peak amplitude divisor
    "noise_floor":    600,     # float/int, >= 0     — gate threshold
    "drop_threshold": 0.20,    # float, 0.0 <= x <= 1.0 — snap-shut ratio
}
```

### Config_File JSON schema

A single JSON object with two named profiles. `version` is included so the schema can evolve without ambiguity.

```json
{
  "version": 1,
  "profiles": {
    "file": {
      "sensitivity": 500,
      "noise_floor": 600,
      "drop_threshold": 0.20
    },
    "mic": {
      "sensitivity": 500,
      "noise_floor": 600,
      "drop_threshold": 0.20
    }
  }
}
```

The two allowed profile keys are `"file"` (File_Profile) and `"mic"` (Mic_Profile). These are the identifiers the web-layer allowlist permits.

### Defaults

Defined once as module constants in `config_store.py`, matching the current hardcoded literals:

```python
DEFAULT_SENSITIVITY = 500
DEFAULT_NOISE_FLOOR = 600
DEFAULT_DROP_THRESHOLD = 0.20

PROFILE_FILE = "file"
PROFILE_MIC = "mic"
ALLOWED_PROFILES = (PROFILE_FILE, PROFILE_MIC)
```

## Components and Interfaces

### `config_store.py` — new module

A `ConfigStore` class encapsulates path resolution, read, and write. A module-level default instance and thin function wrappers are provided so consumers can call `config_store.load_profile("file")` without managing an instance, mirroring how `_resolve_audio_dir` is a simple static call.

```python
import os
import json

DEFAULT_SENSITIVITY = 500
DEFAULT_NOISE_FLOOR = 600
DEFAULT_DROP_THRESHOLD = 0.20

PROFILE_FILE = "file"
PROFILE_MIC = "mic"
ALLOWED_PROFILES = (PROFILE_FILE, PROFILE_MIC)

CONFIG_FILENAME = ".animatronic_jaw_config.json"
CONFIG_PATH_OVERRIDE_ENV = "ANIMATRONIC_JAW_CONFIG"


def _default_profile():
    """Return a fresh copy of the default jaw-tuning profile.

    Returns:
        A new dict with the default sensitivity, noise_floor, and
        drop_threshold values, safe to mutate without affecting other callers.
    """
    return {
        "sensitivity": DEFAULT_SENSITIVITY,
        "noise_floor": DEFAULT_NOISE_FLOOR,
        "drop_threshold": DEFAULT_DROP_THRESHOLD,
    }


class ConfigStore:
    """Resolves, reads, and writes the shared jaw-tuning Config_File.

    The store holds two independent profiles keyed by "file" (File_Profile)
    and "mic" (Mic_Profile). It resolves a single deterministic path so every
    component reads and writes the same file whether run directly or via sudo.
    """

    def __init__(self, config_path=None):
        """Initialise the store.

        Args:
            config_path: Optional explicit path to the Config_File. When None,
                         the path is resolved from the environment using
                         resolve_config_path().
        """
        self._config_path = config_path or self.resolve_config_path()

    @staticmethod
    def resolve_config_path():
        """Resolve the Config_File path deterministically.

        Precedence mirrors Animatronic._resolve_audio_dir():
            1. ANIMATRONIC_JAW_CONFIG env override, used verbatim.
            2. SUDO_USER home directory (so sudo runs share the invoker's file).
            3. The invoking user's home directory.

        Returns:
            The absolute path string to the Config_File.
        """
        override = os.environ.get(CONFIG_PATH_OVERRIDE_ENV)
        if override:
            return override
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            return os.path.join("/home", sudo_user, CONFIG_FILENAME)
        return os.path.join(os.path.expanduser("~"), CONFIG_FILENAME)

    def load_profiles(self):
        """Load both profiles from the Config_File, falling back to defaults.

        Missing file or unparseable JSON both yield a full default pair; the
        method never raises for those cases. Any profile or field absent from
        an otherwise-valid file is filled from the defaults.

        Returns:
            A dict {"file": <profile>, "mic": <profile>} of mutable copies.
        """
        profiles = {PROFILE_FILE: _default_profile(), PROFILE_MIC: _default_profile()}
        try:
            with open(self._config_path, "r") as f:
                raw = json.load(f)
        except FileNotFoundError:
            print(f"Jaw config not found at {self._config_path}; using defaults")
            return profiles
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"Jaw config unreadable ({e}); using defaults")
            return profiles

        stored = raw.get("profiles", {}) if isinstance(raw, dict) else {}
        for name in ALLOWED_PROFILES:
            entry = stored.get(name, {})
            if isinstance(entry, dict):
                for key in profiles[name]:
                    if key in entry:
                        profiles[name][key] = entry[key]
        return profiles

    def load_profile(self, name):
        """Load a single profile by name.

        Args:
            name: The profile identifier, one of ALLOWED_PROFILES
                  ("file" or "mic").

        Returns:
            A mutable dict copy of the requested profile.

        Raises:
            ValueError: If name is not an allowed profile identifier.
        """
        if name not in ALLOWED_PROFILES:
            raise ValueError(f"Unknown profile '{name}'; allowed: {ALLOWED_PROFILES}")
        return self.load_profiles()[name]

    def save_profiles(self, profiles):
        """Write both profiles to the Config_File as a single JSON object.

        Args:
            profiles: A dict {"file": <profile>, "mic": <profile>}. Both
                      profiles are always written so the file stays complete.
        """
        payload = {
            "version": 1,
            "profiles": {
                PROFILE_FILE: dict(profiles[PROFILE_FILE]),
                PROFILE_MIC: dict(profiles[PROFILE_MIC]),
            },
        }
        with open(self._config_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Jaw config written to {self._config_path}")

    def update_profile(self, name, updates):
        """Apply validated field updates to one profile and persist both.

        The other profile is loaded and re-written unchanged so the file
        always contains a complete, consistent pair.

        Args:
            name:    The profile identifier to update ("file" or "mic").
            updates: A dict of already-validated field/value pairs to merge
                     into the named profile.

        Returns:
            The updated dict {"file": <profile>, "mic": <profile>}.

        Raises:
            ValueError: If name is not an allowed profile identifier.
        """
        if name not in ALLOWED_PROFILES:
            raise ValueError(f"Unknown profile '{name}'; allowed: {ALLOWED_PROFILES}")
        profiles = self.load_profiles()
        profiles[name].update(updates)
        self.save_profiles(profiles)
        return profiles


# Module-level default instance + thin wrappers for simple call sites.
_default_store = ConfigStore()


def load_profile(name):
    """Load a single profile via the default store.

    Args:
        name: The profile identifier ("file" or "mic").

    Returns:
        A mutable dict copy of the requested profile.
    """
    return _default_store.load_profile(name)
```

### `audio_player.py` — minimal change

The constructor keeps its signature for backward compatibility (callers like `Animatronic.run_action_and_audio` construct `AudioPlayer()` with no args), but now loads the File_Profile as the source of defaults.

```python
from config_store import load_profile, PROFILE_FILE

class AudioPlayer:
    def __init__(self, sensitivity=None, noise_floor=None, drop_threshold=None):
        """Initialise the audio player and jaw motor.

        Jaw-tuning parameters default to the persisted File_Profile from the
        Config_Store. Explicit arguments, when given, override the profile.

        Args:
            sensitivity:    Optional override for the peak amplitude divisor.
            noise_floor:    Optional override for the noise-floor gate.
            drop_threshold: Optional override for the snap-shut ratio.
        """
        # ... existing gpiozero / pyaudio setup unchanged ...
        profile = load_profile(PROFILE_FILE)
        self.sensitivity = sensitivity if sensitivity is not None else profile["sensitivity"]
        self.noise_floor = noise_floor if noise_floor is not None else profile["noise_floor"]
        self.drop_threshold = drop_threshold if drop_threshold is not None else profile["drop_threshold"]
        self.previous_jaw_value = None
```

`talk()` is unchanged — it already reads `self.sensitivity`, `self.noise_floor`, `self.drop_threshold`.

### `audio_streamer.py` — minimal change

Identical treatment, loading `PROFILE_MIC`:

```python
from config_store import load_profile, PROFILE_MIC

class AudioStreamer:
    def __init__(self, sensitivity=None, noise_floor=None, drop_threshold=None):
        """Initialise the mic streamer and jaw motor.

        Jaw-tuning parameters default to the persisted Mic_Profile from the
        Config_Store. Explicit arguments, when given, override the profile.

        Args:
            sensitivity:    Optional override for the peak amplitude divisor.
            noise_floor:    Optional override for the noise-floor gate.
            drop_threshold: Optional override for the snap-shut ratio.
        """
        # ... existing pyaudio / buffer setup unchanged ...
        profile = load_profile(PROFILE_MIC)
        self.sensitivity = sensitivity if sensitivity is not None else profile["sensitivity"]
        self.noise_floor = noise_floor if noise_floor is not None else profile["noise_floor"]
        self.drop_threshold = drop_threshold if drop_threshold is not None else profile["drop_threshold"]
        self.previous_jaw_value = None
```

`talk()` and the effects methods are unchanged.

### `micwebcontroller.py` — profile-aware endpoints

The single module-level `jaw_config` dict is replaced by a store instance and two in-memory profiles loaded at startup. The live-mic `talk()` reads from the mic profile (mic is the only profile this process drives).

```python
from config_store import ConfigStore, ALLOWED_PROFILES, PROFILE_MIC

config_store = ConfigStore()
jaw_profiles = config_store.load_profiles()   # {"file": {...}, "mic": {...}}
```

`talk()` changes only its lookups from `jaw_config['sensitivity']` to `jaw_profiles[PROFILE_MIC]['sensitivity']` (and the other two fields). The gate / scale / drop-threshold branches are otherwise unchanged.

`POST /config` gains a profile selector and validates before mutating:

```python
@app.route('/config', methods=['POST'])
def set_config():
    """Update jaw tuning for one profile at runtime and persist both.

    Accepts JSON with a "profile" selector ("file" or "mic") plus any
    combination of sensitivity, noise_floor, drop_threshold. The selector is
    checked against an explicit allowlist; unknown profiles are rejected.

    Args:
        (request body) profile:        "file" or "mic".
        (request body) sensitivity:    optional, > 0.
        (request body) noise_floor:    optional, >= 0.
        (request body) drop_threshold: optional, 0.0–1.0.
    """
    data = request.json or {}

    # Allowlist dispatch — never index jaw_profiles with unvalidated input.
    profile_name = data.get('profile')
    if profile_name not in ALLOWED_PROFILES:
        return jsonify({'status': 'error',
                        'message': f'profile must be one of {list(ALLOWED_PROFILES)}'}), 400

    updates = {}
    if 'sensitivity' in data:
        value = float(data['sensitivity'])
        if value <= 0:
            return jsonify({'status': 'error', 'message': 'sensitivity must be > 0'}), 400
        updates['sensitivity'] = value
    if 'noise_floor' in data:
        value = float(data['noise_floor'])
        if value < 0:
            return jsonify({'status': 'error', 'message': 'noise_floor must be >= 0'}), 400
        updates['noise_floor'] = value
    if 'drop_threshold' in data:
        value = float(data['drop_threshold'])
        if not (0.0 <= value <= 1.0):
            return jsonify({'status': 'error', 'message': 'drop_threshold must be 0.0–1.0'}), 400
        updates['drop_threshold'] = value

    # Validation fully passed before any mutation → invalid requests leave
    # stored profiles unchanged.
    global jaw_profiles
    jaw_profiles = config_store.update_profile(profile_name, updates)
    print(f"Jaw profile '{profile_name}' updated: {jaw_profiles[profile_name]}")
    return jsonify({'status': 'success', 'profile': profile_name,
                    'updated': updates, 'profiles': jaw_profiles})
```

`GET /status` returns both profiles:

```python
@app.route('/status', methods=['GET'])
def get_status():
    """Return streaming status, both jaw profiles, and voice effects config."""
    return jsonify({
        'is_streaming': audio_state['is_streaming'],
        'profiles': jaw_profiles,   # {"file": {...}, "mic": {...}}
        'effects': effects_config,
        'styles': list(STYLE_PRESETS.keys()),
    })
```

**Validation ordering guarantee:** all field validation and the allowlist check run *before* any call to `update_profile`. On any invalid input the handler returns early, so neither the in-memory `jaw_profiles` nor the Config_File is mutated. This is what satisfies "leave the stored profiles unchanged" for every rejection path.

## Error Handling

| Condition | Handling | Requirement |
|-----------|----------|-------------|
| Config_File does not exist | `load_profiles` catches `FileNotFoundError`, returns default pair, prints a notice | 6.4 |
| Config_File is not valid JSON | `load_profiles` catches `JSONDecodeError`/`ValueError`, returns default pair, continues | 6.5 |
| Config_File readable but missing a profile/field | Missing pieces filled from defaults during merge | 6.4 |
| `POST /config` with unknown profile name | Allowlist check returns 400 before mutation | 5.4, 8.2 |
| `POST /config` with out-of-bounds value | Field validation returns 400 before mutation | 5.1–5.3, 8.1 |
| `save_profiles` I/O error | Propagates (a write failure is a genuine fault worth surfacing); `load` failures stay silent-with-default because reads happen on every startup and must never block a show | 6.2 |

Debug output uses `print()` throughout, consistent with the rest of the codebase (no `logging`).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

The prework identified overlapping criteria that were consolidated:

- **Jaw mapping (2.2–2.5 and 3.2–3.5)** — `AudioPlayer.talk()` and `AudioStreamer.talk()` share identical jaw-value computation, so both consumers are covered by one property over the pure `(peak, previous, profile) → jaw_value` mapping.
- **Profile isolation (1.4, 1.5)** — the two isolation directions are one invariant: updating any profile leaves the other untouched.
- **Persistence round-trip (4.5, 6.2, 6.3)** — write-through, write-both, and read-back-on-start are all the save→load identity.
- **Value validation (5.1–5.3, 8.1)** — the three per-field bound checks are one rejection-and-no-mutation invariant.
- **Allowlist (5.4, 8.2)** — one rejection invariant for non-allowed profile names.

### Property 1: Jaw-value mapping preserves the gate → scale → snap semantics

*For any* jaw-tuning profile (sensitivity > 0, noise_floor >= 0, drop_threshold in [0.0, 1.0]), any audio peak >= 0, and any previous jaw value, the computed jaw value SHALL be: 0.0 when peak < noise_floor; otherwise `min(100, peak / sensitivity * 100)`, further set to 0.0 when the value is falling and its ratio to the previous value is below drop_threshold.

**Validates: Requirements 2.2, 2.3, 2.4, 2.5, 3.2, 3.3, 3.4, 3.5**

### Property 2: Profile updates are isolated

*For any* pair of stored profiles and any valid update applied to one profile, the other profile SHALL remain byte-for-byte unchanged after the update.

**Validates: Requirements 1.4, 1.5, 4.3**

### Property 3: Persistence round-trip

*For any* valid pair of File_Profile and Mic_Profile values, saving them through the Config_Store and then loading from the same Config_File SHALL return an equivalent pair.

**Validates: Requirements 4.5, 6.2, 6.3**

### Property 4: Out-of-bounds values are rejected without mutation

*For any* update request in which sensitivity <= 0, noise_floor < 0, or drop_threshold falls outside [0.0, 1.0], the Web_Controller SHALL return an error response and leave both stored profiles unchanged.

**Validates: Requirements 5.1, 5.2, 5.3, 8.1**

### Property 5: Unknown profile names are rejected without mutation

*For any* profile identifier that is not in the allowlist {file, mic}, the Web_Controller SHALL return an error response and leave both stored profiles unchanged.

**Validates: Requirements 5.4, 8.2**

### Property 6: Corrupt or missing config yields defaults

*For any* Config_File content that is absent or cannot be parsed as valid config JSON, loading profiles SHALL return the default profile pair (sensitivity 500, noise_floor 600, drop_threshold 0.20) without raising.

**Validates: Requirements 6.4, 6.5**

### Property 7: Path resolution is deterministic

*For any* fixed environment (Config_Path_Override, SUDO_USER, and home directory held constant), resolving the Config_File path repeatedly SHALL yield the identical path string.

**Validates: Requirements 7.1, 7.2, 7.3, 7.4**

## Testing Strategy

**Dual approach.** Property tests (via `hypothesis`) cover the universal properties above; example-based unit tests (via `pytest`) cover shape, path-resolution branches, and fixed-outcome cases.

**Property tests** (minimum 100 iterations each; tag format `Feature: separate-jaw-tuning-profiles, Property {n}: {text}`):

- Property 1 — extract the jaw-value computation into a testable pure function (or test it against generated `(peak, previous, profile)` inputs) and assert the three-branch outcome. Covers both consumers since the logic is shared.
- Property 2 — generate a random profile pair and a random valid single-profile update; assert the untouched profile is identical.
- Property 3 — generate a random profile pair, `save_profiles` to a temp path, `load_profiles`, assert equality.
- Property 4 — generate updates with at least one out-of-bounds field; assert error response and unchanged stored state (use Flask test client with the store pointed at a temp file).
- Property 5 — generate random strings excluded from the allowlist; assert error and unchanged state.
- Property 6 — generate arbitrary byte/text content (including malformed JSON and wrong shapes) into a temp file; assert `load_profiles` returns defaults and does not raise.
- Property 7 — generate random combinations of the three env variables; assert repeated `resolve_config_path()` calls return the same string.

**Example / edge-case unit tests:**

- Loaded config exposes both `file` and `mic` profiles, each with the three fields (1.1, 1.2, 1.3).
- `POST /config` with a valid file update and a valid mic update each return 200 with echoed fields (4.1, 4.2).
- `GET /status` returns both profiles with the currently stored values (4.4).
- Single JSON file on disk contains both profiles after a write (6.1).
- Missing file → both profiles equal defaults (6.4).
- Path resolution: override set → override value; no override + `SUDO_USER` → `/home/<sudo_user>/...`; neither → `expanduser('~')` path (7.1, 7.2, 7.3).

**Convention checks (review-enforced, not property tests):** `config_store.py` uses `print` not `logging` (8.3) and all public functions carry Google-style docstrings with `Args:` sections (8.4).

Run tests quietly and stop on first failure during iteration:

```bash
pytest -q --maxfail=1
```

Hardware-dependent construction (`gpiozero`, `pyaudio` in the audio classes) is out of scope for these tests; the value-loading logic is verified by pointing the store at a temp file and asserting the three attributes match the loaded profile.
