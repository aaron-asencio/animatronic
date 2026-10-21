# Requirements Document

## Introduction

This feature adds Camera_Service person detection as a selectable sensor trigger for Awake mode, alongside the existing HC-SR04 ultrasonic proximity sensor. Awake mode currently reacts to a single proximity sensor (the HC-SR04 approach detector, consumed through `_setup_nap_sensor` / `_poll_nap_sensor` in `src/animatronic.py`). This feature lets the operator choose, in the Flask web control panel's Awake Config section, which of the two sensor sources Awake mode uses as its interrupt trigger.

The chosen source is a persisted default: it is saved to `tuning.json` through the existing `config_store` pattern and is read when Awake mode next starts (`animatronic.py --action=awake`). The camera source (Camera_Service person detection) is the default: when no source has been persisted, or when an invalid value is saved, the camera source is used. The selection does not affect an already-running Awake session. When the camera source is selected, only a detected person triggers the reaction; other detected object classes (for example dogs) are ignored. When the camera source is selected but Camera_Service is unavailable at launch, Awake mode silently falls back to the HC-SR04 range sensor so that Awake mode always has a working sensor interrupt.

All existing Awake mode behaviors — the timeout interrupt, the cross-process web stop (`nap_signal`), and the ambient routine/gesture loop — are preserved unchanged. All hardware safety rules continue to apply: every servo write goes through the clamping `set_angle` / `move_to` path against `SAFE_LIMITS`, servos return to rest after routines and errors, and action dispatch stays behind the existing allowlist.

## Glossary

- **Awake_Mode**: The continuous background Mode launched via `animatronic.py --action=awake`, implemented by `Animatronic.awake(...)`, that runs ambient Routines/Gestures until interrupted by its timeout, a sensor trigger, or an external stop.
- **Sensor_Source**: The configured source of Awake_Mode's sensor interrupt trigger. One of exactly two values: `range` (HC-SR04 ultrasonic proximity sensor) or `camera` (Camera_Service person detection).
- **Range_Sensor**: The HC-SR04 ultrasonic proximity sensor path, consumed via `ApproachDetector` + `PublishedReadingSensor` in `Animatronic._setup_nap_sensor` and polled by `_poll_nap_sensor`.
- **Camera_Service**: The non-root person-detection service (`src/camera_service.py`) that publishes `Detection` records consumed by Tracking mode (`tracking_controller.py` / `vision_models.Detection`).
- **Person_Detection**: A `Detection` published by Camera_Service whose object class is a person.
- **Config_Store**: The persisted tuning store (`src/config_store.py`), backed by `src/config/tuning.json`, exposing paired load/save module-level helpers.
- **Awake_Config_Section**: The Awake configuration area of the Flask web control panel rendered by `src/webapp.py`.
- **Web_Control_Panel**: The Flask control panel (`src/webapp.py`, port 8000) that renders configuration and launches `animatronic.py` subprocesses through an action allowlist.
- **Awake_Launch**: The act of starting Awake_Mode via `run_awake(...)` → `animatronic.py --action=awake`, at which point the persisted Sensor_Source is read and applied.

## Requirements

### Requirement 1: Operator selects the Awake sensor source

**User Story:** As an operator, I want to choose whether Awake mode is triggered by the proximity sensor or by camera person detection, so that I can match the figure's wake behavior to the sensor available in the installation.

#### Acceptance Criteria

1. THE Awake_Config_Section SHALL present a Sensor_Source selection with exactly two selectable values: Range_Sensor and Camera_Service.
2. WHEN the Web_Control_Panel renders the Awake_Config_Section, THE Web_Control_Panel SHALL pre-select the value of the Sensor_Source currently persisted in Config_Store.
3. WHEN the operator submits a Sensor_Source selection in the Awake_Config_Section, THE Web_Control_Panel SHALL persist the selected value to Config_Store.
4. IF a submitted Sensor_Source value is neither Range_Sensor nor Camera_Service, THEN THE Web_Control_Panel SHALL reject the submission and leave the persisted Sensor_Source unchanged.

### Requirement 2: Persist the sensor source to tuning.json

**User Story:** As an operator, I want the sensor source choice stored in tuning.json using the same pattern as the other Awake settings, so that the choice survives restarts and stays consistent with existing configuration.

#### Acceptance Criteria

1. THE Config_Store SHALL provide a load helper that returns the persisted Sensor_Source, following the existing module-level helper pattern used by `load_awake_timeout` and `load_awake_pools`.
2. THE Config_Store SHALL provide a save helper that persists a Sensor_Source value to `tuning.json`, following the existing module-level helper pattern used by `save_awake_timeout` and `save_awake_pools`.
3. WHEN the save helper receives a value that is neither Range_Sensor nor Camera_Service, THE Config_Store SHALL persist Camera_Service as the Sensor_Source.
4. WHEN the load helper is called and no Sensor_Source value is present in `tuning.json`, THE Config_Store SHALL return Camera_Service as the Sensor_Source.
5. THE Config_Store SHALL return the persisted Sensor_Source value from the save helper.

### Requirement 3: Apply the sensor source at next Awake launch

**User Story:** As an operator, I want my sensor source choice applied the next time Awake mode starts, so that the figure uses my configured trigger without me passing it manually.

#### Acceptance Criteria

1. WHEN Awake_Launch occurs, THE Awake_Mode SHALL read the persisted Sensor_Source from Config_Store and use that value as its sensor interrupt source for the session.
2. WHILE an Awake_Mode session is running, THE Awake_Mode SHALL continue using the Sensor_Source value read at Awake_Launch for the remainder of that session.
3. WHEN the operator changes the persisted Sensor_Source while an Awake_Mode session is running, THE Web_Control_Panel SHALL leave the running session's active sensor source unchanged.
4. WHERE the Sensor_Source value is passed from the Web_Control_Panel to the Awake_Mode subprocess, THE Web_Control_Panel SHALL pass the value through the existing action allowlist dispatch path.

### Requirement 4: Camera source triggers on people only

**User Story:** As an operator, I want camera-triggered Awake mode to react only to people, so that animals and other detected objects do not wake the figure.

#### Acceptance Criteria

1. WHILE the active Sensor_Source is Camera_Service, WHEN a Person_Detection is reported by Camera_Service, THE Awake_Mode SHALL treat the detection as a sensor trigger.
2. WHILE the active Sensor_Source is Camera_Service, WHEN Camera_Service reports a detection whose object class is not a person, THE Awake_Mode SHALL ignore the detection.
3. WHILE the active Sensor_Source is Range_Sensor, THE Awake_Mode SHALL use the Range_Sensor approach-detection trigger and SHALL disregard Camera_Service detections.

### Requirement 5: Fall back to the range sensor when the camera is unavailable

**User Story:** As an operator, I want Awake mode to keep working even when the camera service is down, so that the figure always has a functioning wake trigger.

#### Acceptance Criteria

1. IF the active Sensor_Source is Camera_Service AND Camera_Service is unavailable at Awake_Launch, THEN THE Awake_Mode SHALL use Range_Sensor as its sensor interrupt source for the session.
2. WHERE the Awake_Mode falls back to Range_Sensor because Camera_Service is unavailable at Awake_Launch, THE Awake_Mode SHALL complete Awake_Launch without surfacing an error to the operator.
3. THE Awake_Mode SHALL have exactly one active sensor interrupt source for each running session.

### Requirement 6: Preserve existing Awake mode behaviors

**User Story:** As an operator, I want all existing Awake mode behaviors to keep working unchanged, so that adding the camera source does not regress timeout, stop, or the ambient loop.

#### Acceptance Criteria

1. THE Awake_Mode SHALL continue to end a session when its configured timeout elapses, regardless of the active Sensor_Source.
2. WHEN the Web_Control_Panel issues a cross-process stop request via `nap_signal`, THE Awake_Mode SHALL wind down the running session, regardless of the active Sensor_Source.
3. THE Awake_Mode SHALL continue running its ambient routine and gesture loop between sensor triggers, regardless of the active Sensor_Source.
4. WHEN a sensor trigger fires from the active Sensor_Source, THE Awake_Mode SHALL run its approach reaction using the existing reaction behavior.

### Requirement 7: Preserve hardware safety guarantees

**User Story:** As an operator, I want the camera-triggered Awake mode to respect the figure's hardware safety guarantees, so that no movement can damage the mechanism.

#### Acceptance Criteria

1. WHEN the Awake_Mode runs a reaction in response to any Sensor_Source trigger, THE Awake_Mode SHALL issue every servo write through the clamping movement path bounded by `SAFE_LIMITS`.
2. WHEN an Awake_Mode reaction completes or raises an error, THE Awake_Mode SHALL return the servos to their rest positions.
3. WHEN the Awake_Mode dispatches a reaction action, THE Awake_Mode SHALL validate the action name against the existing dispatch allowlist before dispatch.
