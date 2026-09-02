# Requirements Document

## Introduction

The animatronic's jaw motor is driven from audio amplitude by two independent code paths: `AudioPlayer` (WAV file playback during full routines) and `AudioStreamer`/`micwebcontroller` (live microphone passthrough). Both paths currently use the same three jaw-tuning parameters (`sensitivity`, `noise_floor`, `drop_threshold`) with identical defaults (500, 600, 0.20). A single shared set of values cannot serve both cases well: pre-recorded files and live mic input have different amplitude characteristics and noise profiles, so tuning that produces natural jaw movement for one source degrades the other.

This feature splits jaw tuning into two independent profiles — one for file playback and one for live mic streaming — each with its own `sensitivity`, `noise_floor`, and `drop_threshold`. Both profiles are adjustable live through the existing web UI, are persisted to a single shared JSON configuration file, and are loaded by the respective audio components at startup so that tuning survives process restarts. The configuration file location is resolved deterministically so it is the same file regardless of whether a script runs as the invoking user or as root via `sudo`.

## Glossary

- **Jaw_Motor**: The mouth motor driven from audio amplitude via `MOUTH_MOTOR_PIN`.
- **Jaw_Tuning_Profile**: A named set of the three jaw-tuning parameters (`sensitivity`, `noise_floor`, `drop_threshold`) applied when converting audio amplitude to jaw motor value.
- **File_Profile**: The Jaw_Tuning_Profile used for WAV file playback (consumed by `AudioPlayer`).
- **Mic_Profile**: The Jaw_Tuning_Profile used for live microphone streaming (consumed by the mic streaming path in `micwebcontroller`/`AudioStreamer`).
- **sensitivity**: Peak amplitude divisor used to scale peak to a 0–100 jaw value; lower values increase sensitivity. Valid range: greater than 0.
- **noise_floor**: Absolute peak value below which the Jaw_Motor is held closed. Valid range: greater than or equal to 0.
- **drop_threshold**: Ratio below which a falling jaw value snaps the Jaw_Motor closed. Valid range: 0.0 to 1.0 inclusive.
- **Config_File**: The single shared JSON file that stores both the File_Profile and the Mic_Profile.
- **Config_Store**: The component responsible for resolving the Config_File path, reading it, and writing it.
- **Audio_Player**: The `AudioPlayer` component in `audio_player.py` that plays WAV files and drives the Jaw_Motor.
- **Audio_Streamer**: The live-mic streaming path (`micwebcontroller`/`AudioStreamer`) that drives the Jaw_Motor from microphone input.
- **Web_Controller**: The Flask web UI in `micwebcontroller.py` that exposes jaw-tuning and effects endpoints.
- **Config_Path_Override**: The environment variable that, when set, supplies the Config_File path explicitly.

## Requirements

### Requirement 1

**User Story:** As an operator, I want file playback and live mic streaming to have independent jaw-tuning parameters, so that I can tune each audio source for natural jaw movement without one degrading the other.

#### Acceptance Criteria

1. THE Config_Store SHALL represent jaw tuning as two independent profiles: a File_Profile and a Mic_Profile.
2. THE File_Profile SHALL contain an independent sensitivity value, noise_floor value, and drop_threshold value.
3. THE Mic_Profile SHALL contain an independent sensitivity value, noise_floor value, and drop_threshold value.
4. WHEN a value in the File_Profile is changed, THE Config_Store SHALL leave the Mic_Profile values unchanged.
5. WHEN a value in the Mic_Profile is changed, THE Config_Store SHALL leave the File_Profile values unchanged.

### Requirement 2

**User Story:** As an operator, I want file playback to use the File_Profile when driving the jaw, so that pre-recorded routines produce natural mouth movement.

#### Acceptance Criteria

1. WHEN the Audio_Player initializes for playback, THE Audio_Player SHALL load the File_Profile from the Config_Store.
2. WHILE the Audio_Player drives the Jaw_Motor, THE Audio_Player SHALL apply the File_Profile sensitivity, noise_floor, and drop_threshold.
3. WHILE the Audio_Player processes an audio frame whose peak is below the File_Profile noise_floor, THE Audio_Player SHALL set the Jaw_Motor value to 0.0.
4. WHILE the Audio_Player processes an audio frame whose peak is greater than or equal to the File_Profile noise_floor, THE Audio_Player SHALL set the jaw value to the minimum of 100 and (peak divided by the File_Profile sensitivity multiplied by 100).
5. WHILE the Audio_Player processes a falling jaw value whose ratio to the previous jaw value is below the File_Profile drop_threshold, THE Audio_Player SHALL set the jaw value to 0.0.

### Requirement 3

**User Story:** As an operator, I want live mic streaming to use the Mic_Profile when driving the jaw, so that live passthrough produces natural mouth movement.

#### Acceptance Criteria

1. WHEN the Audio_Streamer initializes for streaming, THE Audio_Streamer SHALL load the Mic_Profile from the Config_Store.
2. WHILE the Audio_Streamer drives the Jaw_Motor, THE Audio_Streamer SHALL apply the Mic_Profile sensitivity, noise_floor, and drop_threshold.
3. WHILE the Audio_Streamer processes an audio frame whose peak is below the Mic_Profile noise_floor, THE Audio_Streamer SHALL set the Jaw_Motor value to 0.0.
4. WHILE the Audio_Streamer processes an audio frame whose peak is greater than or equal to the Mic_Profile noise_floor, THE Audio_Streamer SHALL set the jaw value to the minimum of 100 and (peak divided by the Mic_Profile sensitivity multiplied by 100).
5. WHILE the Audio_Streamer processes a falling jaw value whose ratio to the previous jaw value is below the Mic_Profile drop_threshold, THE Audio_Streamer SHALL set the jaw value to 0.0.

### Requirement 4

**User Story:** As an operator, I want to adjust both the file-playback profile and the mic profile live through the web UI, so that I can tune each source without editing code or restarting.

#### Acceptance Criteria

1. THE Web_Controller SHALL accept requests that update the File_Profile sensitivity, noise_floor, or drop_threshold.
2. THE Web_Controller SHALL accept requests that update the Mic_Profile sensitivity, noise_floor, or drop_threshold.
3. WHEN the Web_Controller receives a request that identifies which profile to update, THE Web_Controller SHALL apply the changes to the identified profile only.
4. WHEN the Web_Controller receives a status request, THE Web_Controller SHALL return the current File_Profile values and the current Mic_Profile values.
5. WHEN the Web_Controller applies a valid profile update, THE Web_Controller SHALL persist both profiles to the Config_File through the Config_Store.

### Requirement 5

**User Story:** As an operator, I want the web UI to reject invalid tuning values, so that the jaw logic always receives values within safe operating bounds.

#### Acceptance Criteria

1. IF a request supplies a sensitivity value that is less than or equal to 0, THEN THE Web_Controller SHALL reject the request with an error response and leave the stored profiles unchanged.
2. IF a request supplies a noise_floor value that is less than 0, THEN THE Web_Controller SHALL reject the request with an error response and leave the stored profiles unchanged.
3. IF a request supplies a drop_threshold value that is outside the range 0.0 to 1.0 inclusive, THEN THE Web_Controller SHALL reject the request with an error response and leave the stored profiles unchanged.
4. IF a request identifies a profile name that is not File_Profile or Mic_Profile, THEN THE Web_Controller SHALL reject the request with an error response and leave the stored profiles unchanged.

### Requirement 6

**User Story:** As an operator, I want both profiles persisted to a single shared configuration file, so that my tuning survives process restarts and is shared across all components.

#### Acceptance Criteria

1. THE Config_Store SHALL persist the File_Profile and the Mic_Profile to a single JSON Config_File.
2. WHEN the Web_Controller persists an update, THE Config_Store SHALL write both the File_Profile and the Mic_Profile to the Config_File.
3. WHEN the Audio_Player or the Audio_Streamer starts after the Config_File has been written, THE Config_Store SHALL provide the values that were last persisted to the Config_File.
4. IF the Config_File does not exist when a profile is requested, THEN THE Config_Store SHALL provide the default profile values (sensitivity 500, noise_floor 600, drop_threshold 0.20) for both the File_Profile and the Mic_Profile.
5. IF the Config_File exists but cannot be parsed as valid JSON, THEN THE Config_Store SHALL provide the default profile values and continue operation.

### Requirement 7

**User Story:** As an operator running scripts both directly and as root via sudo, I want the configuration file location resolved deterministically, so that every component reads and writes the same file regardless of the executing user.

#### Acceptance Criteria

1. WHERE the Config_Path_Override environment variable is set, THE Config_Store SHALL use the Config_Path_Override value as the Config_File path.
2. WHERE the Config_Path_Override environment variable is not set AND the SUDO_USER environment variable is set, THE Config_Store SHALL resolve the Config_File path within the SUDO_USER home directory.
3. WHERE the Config_Path_Override environment variable is not set AND the SUDO_USER environment variable is not set, THE Config_Store SHALL resolve the Config_File path within the invoking user home directory.
4. THE Config_Store SHALL resolve the Config_File to the same path for the Audio_Player, the Audio_Streamer, and the Web_Controller when the same environment conditions apply.

### Requirement 8

**User Story:** As a developer maintaining this embedded codebase, I want the new profile handling to follow existing project conventions, so that the change stays consistent with the rest of the code.

#### Acceptance Criteria

1. THE Config_Store SHALL preserve the existing validation bounds for sensitivity (greater than 0), noise_floor (greater than or equal to 0), and drop_threshold (0.0 to 1.0 inclusive).
2. THE Web_Controller SHALL dispatch profile identifiers through an explicit allowlist that permits only the File_Profile and the Mic_Profile identifiers.
3. THE Config_Store and the Web_Controller SHALL emit debug output using print statements rather than a logging framework.
4. THE Config_Store SHALL document each public function using Google-style docstrings with an Args section.
