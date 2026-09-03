"""Shared tuning config store.

This leaf module owns the shared TUNING config file for the project — jaw
profiles today (File_Profile and Mic_Profile) and servo limits later — persisted
as a single JSON file inside the repo `config/` directory so it is
version-controlled and shared identically across all executing users
(sudo/pi/aaron). It imports only the standard library and never imports the
audio components or web controller, matching the "higher layers call lower ones,
never the reverse" rule.

Debug output uses print() (no logging framework), consistent with the rest of
the codebase.
"""

import os
import json

DEFAULT_SENSITIVITY = 500
DEFAULT_NOISE_FLOOR = 600
DEFAULT_DROP_THRESHOLD = 0.20

PROFILE_FILE = "file"
PROFILE_MIC = "mic"
ALLOWED_PROFILES = (PROFILE_FILE, PROFILE_MIC)

CONFIG_FILENAME = "tuning.json"
CONFIG_DIRNAME = "config"
CONFIG_PATH_OVERRIDE_ENV_PRIMARY = "ANIMATRONIC_TUNING_CONFIG"
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
    """Resolves, reads, and writes the shared TUNING Config_File.

    The store owns the shared tuning config file (jaw profiles today, servo
    limits later) persisted in the application's ``config/`` directory. It holds
    two independent jaw profiles keyed by "file" (File_Profile) and "mic"
    (Mic_Profile). It resolves a single deterministic path — inside the app's
    ``config/`` directory — so every component reads and writes the same file
    regardless of the executing user (identical under sudo/pi/aaron).
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

        The file lives in the application's ``config/`` directory
        (version-controlled and shared identically across all executing users,
        e.g. sudo/pi/aaron), so resolution does not depend on the invoking
        user's home directory.

        Precedence:
            1. ANIMATRONIC_TUNING_CONFIG env override (primary), used verbatim.
            2. ANIMATRONIC_JAW_CONFIG env override (legacy fallback), used
               verbatim.
            3. ``<app_root>/config/tuning.json`` where app_root is the directory
               containing this module.

        Returns:
            The absolute path string to the Config_File.
        """
        primary = os.environ.get(CONFIG_PATH_OVERRIDE_ENV_PRIMARY)
        if primary:
            return primary
        legacy = os.environ.get(CONFIG_PATH_OVERRIDE_ENV)
        if legacy:
            return legacy
        app_root = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(app_root, CONFIG_DIRNAME, CONFIG_FILENAME)

    def load_profiles(self):
        """Load both profiles from the Config_File, falling back to defaults.

        Missing file, unparseable JSON, or content that cannot be decoded as
        UTF-8 all yield a full default pair; the method never raises for those
        cases. An unreadable file (permission denied) is warned about clearly
        and also falls back to defaults so operation continues. Any profile or
        field absent from an otherwise-valid file is filled from the defaults.

        Returns:
            A dict {"file": <profile>, "mic": <profile>} of mutable copies.
        """
        profiles = {PROFILE_FILE: _default_profile(), PROFILE_MIC: _default_profile()}
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            print(f"Tuning config not found at {self._config_path}; using defaults")
            return profiles
        except PermissionError as e:
            print(f"WARNING: Tuning config at {self._config_path} is not readable "
                  f"by the current user ({e}). Tuning changes will NOT persist until "
                  f"this is fixed. Check file ownership/permissions (it may be owned "
                  f"by root from a sudo run); it should be mode 644.")
            return profiles
        except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError) as e:
            print(f"Tuning config unreadable ({e}); using defaults")
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

        Both profiles are always written so the file stays complete and
        consistent regardless of which one changed. Any other top-level
        sections already present in the file (e.g. a future "servo_limits"
        section) are preserved: the existing raw JSON is loaded best-effort and
        only the "version" and "profiles" keys are overwritten. The target
        directory is created if it does not yet exist.

        Args:
            profiles: A dict {"file": <profile>, "mic": <profile>}. Both
                      profiles are always written so the file stays complete.
        """
        # Best-effort load of existing raw JSON so unknown top-level sections
        # survive; on any error treat the existing content as empty.
        existing = {}
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError):
                existing = {}

        payload = dict(existing)
        payload["version"] = 1
        payload["profiles"] = {
            PROFILE_FILE: dict(profiles[PROFILE_FILE]),
            PROFILE_MIC: dict(profiles[PROFILE_MIC]),
        }

        config_dir = os.path.dirname(self._config_path)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)

        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        # Make the tuning file world-readable (0644). It holds no secrets, and this
        # prevents a root-written (umask 077) file from being unreadable by a
        # non-root reader, which would otherwise silently fall back to defaults.
        try:
            os.chmod(self._config_path, 0o644)
        except OSError as e:
            print(f"Could not chmod tuning config ({e}); leaving existing permissions")
        print(f"Tuning config written to {self._config_path}")

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
