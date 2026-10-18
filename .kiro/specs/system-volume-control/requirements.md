# Requirements Document

## Introduction

This feature adds a system volume control to the web control panel UI (`src/webapp.py`, port 8000) so the operator can set the Raspberry Pi's default audio sink level without opening a shell. Today the operator must run a command such as `wpctl set-volume @DEFAULT_AUDIO_SINK@ 100%` manually. The feature adds a 0–100% slider with a live percentage readout, placed to the right of the existing live-mic and record controls in the `.mic-bar` area of `src/templates/index.html`. A new backend route on the main control-panel app applies the requested level and reads back the real sink level on page load. The volume value is validated and clamped to the integer range [0, 100], and the underlying `wpctl` command is invoked with `subprocess` using `shell=False` and a fixed argument list, never interpolating the UI value into a shell string.

## Glossary

- **Volume_Control**: The combined backend route and front-end UI component that reads and sets the system audio sink level from the web control panel.
- **Set_Volume_Route**: The backend HTTP route (`POST /volume`) on the Control_Panel_App that validates a requested level and applies it to the default audio sink.
- **Get_Volume_Route**: The backend HTTP route (`GET /volume`) on the Control_Panel_App that reads back the current default audio sink level.
- **Control_Panel_App**: The Flask application defined in `src/webapp.py` serving the control panel on port 8000.
- **Volume_Slider**: The front-end 0–100 range slider with a live percentage readout, rendered in the `.mic-bar` area of `src/templates/index.html`.
- **Default_Audio_Sink**: The system default audio output referenced by `wpctl` as `@DEFAULT_AUDIO_SINK@`.
- **Volume_Level**: An integer percentage in the range [0, 100] representing the Default_Audio_Sink output level.
- **wpctl**: The WirePlumber command-line utility used to get and set the Default_Audio_Sink level.

## Requirements

### Requirement 1: Volume control placement in the UI

**User Story:** As an operator, I want a volume control next to the live-mic and record controls, so that I can adjust system volume without leaving the control panel.

#### Acceptance Criteria

1. THE Volume_Slider SHALL render in the `.mic-bar` area of `src/templates/index.html` to the right of the live-mic and record controls.
2. THE Volume_Slider SHALL present an integer range slider spanning Volume_Level values 0 through 100.
3. THE Volume_Slider SHALL display a live percentage readout of the slider's current position.

### Requirement 2: Initial value read-back on page load

**User Story:** As an operator, I want the slider to reflect the actual current system volume when the page loads, so that the control matches reality before I touch it.

#### Acceptance Criteria

1. WHEN the control panel page loads, THE Volume_Slider SHALL request the current Volume_Level from the Get_Volume_Route.
2. WHEN the Get_Volume_Route receives a request, THE Get_Volume_Route SHALL read the Default_Audio_Sink level by invoking `wpctl get-volume @DEFAULT_AUDIO_SINK@`.
3. WHEN the Get_Volume_Route reads the Default_Audio_Sink level, THE Get_Volume_Route SHALL parse the `wpctl` output's floating-point gain value and convert it to an integer Volume_Level by multiplying by 100 and rounding to the nearest integer.
4. WHEN the Get_Volume_Route returns a Volume_Level, THE Volume_Slider SHALL set the slider position and percentage readout to the returned Volume_Level.
5. IF the Get_Volume_Route cannot read or parse the Default_Audio_Sink level, THEN THE Get_Volume_Route SHALL return an error status and an HTTP 500 response without raising an unhandled exception.
6. IF the Volume_Slider receives an error response from the Get_Volume_Route, THEN THE Volume_Slider SHALL remain usable and leave the slider at its default markup position.

### Requirement 3: Applying a volume change

**User Story:** As an operator, I want my slider adjustment to change the system volume when I release the slider, so that the audio output level updates without flooding the backend while I drag.

#### Acceptance Criteria

1. WHEN the operator releases the Volume_Slider after changing its position, THE Volume_Slider SHALL send the selected Volume_Level to the Set_Volume_Route.
2. WHILE the operator is dragging the Volume_Slider, THE Volume_Slider SHALL update only the percentage readout and SHALL NOT send a request to the Set_Volume_Route.
3. WHEN the Set_Volume_Route applies a Volume_Level, THE Set_Volume_Route SHALL invoke `wpctl set-volume @DEFAULT_AUDIO_SINK@ <N>%` where `<N>` is the validated integer Volume_Level.
4. WHEN the Set_Volume_Route completes successfully, THE Set_Volume_Route SHALL return a success status and the applied Volume_Level.
5. WHEN the Volume_Slider receives a success response, THE Volume_Slider SHALL display the applied Volume_Level in the percentage readout.

### Requirement 4: Validation and clamping

**User Story:** As an operator, I want out-of-range or malformed volume values to be rejected or corrected, so that I cannot drive the audio sink to an invalid level.

#### Acceptance Criteria

1. WHEN the Set_Volume_Route receives a requested Volume_Level, THE Set_Volume_Route SHALL coerce the requested value to an integer.
2. IF the requested Volume_Level is less than 0, THEN THE Set_Volume_Route SHALL clamp the applied Volume_Level to 0.
3. IF the requested Volume_Level is greater than 100, THEN THE Set_Volume_Route SHALL clamp the applied Volume_Level to 100.
4. IF the requested Volume_Level is missing or cannot be coerced to an integer, THEN THE Set_Volume_Route SHALL reject the request with an HTTP 400 response and apply no change to the Default_Audio_Sink.
5. WHEN the Volume_Slider computes a Volume_Level to send, THE Volume_Slider SHALL clamp the value to the integer range [0, 100] before sending.

### Requirement 5: Safe subprocess invocation

**User Story:** As the project maintainer, I want the volume command invoked without a shell and without string interpolation of the UI value, so that the root-privileged control panel is not exposed to command injection.

#### Acceptance Criteria

1. THE Set_Volume_Route SHALL invoke `wpctl` using `subprocess` with `shell=False` and a list of arguments.
2. THE Get_Volume_Route SHALL invoke `wpctl` using `subprocess` with `shell=False` and a list of arguments.
3. THE Set_Volume_Route SHALL pass the Volume_Level only as a validated integer formatted into a single `<N>%` argument element, and SHALL NOT interpolate the requested value into a shell command string.
4. IF the `wpctl` process exits with a non-zero status, THEN THE Set_Volume_Route SHALL return an error status and an HTTP 500 response.
