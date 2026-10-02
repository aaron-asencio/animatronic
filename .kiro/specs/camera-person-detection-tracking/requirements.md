# Requirements Document

## Introduction

This feature adds vision-based perception to the animatronic: a Raspberry Pi Camera Module 3 Noir (NoIR) mounted in the head (nose), paired with an IR illuminator for night operation, feeds an object-detection pipeline that identifies people, animals, and objects. Because the NoIR sensor plus IR illumination can capture a usable image in darkness, person detection works both in ambient light and at night. Detected people drive directional head tracking (the neck pans/tilts to keep the person centered in the camera frame), and specific detections trigger audio-backed Routines (for example, a person accompanied by a dog triggers a "nice day to walk your dog" Routine). When the tracked person leaves the frame, the head performs a slow scanning sweep to reacquire a person before recentering. A live camera feed with detection overlays is surfaced in the existing Flask control panel for operator testing.

The work is grouped along six build steps so each layer is independently verifiable on hardware:

1. Camera capture working
2. Live feed displayed in the web control panel
3. Detection of people and objects
4. Motion/position detection of people
5. Translation of person position into neck movement (head tracking)
6. Translation of detections into triggered Routines

The feature also introduces **Tracking** as a fourth background **Mode** (alongside Mic stream, Sleep, and Awake) and changes the servo lock from a single whole-robot mutex to **per-channel-group locks** (a neck group and an arm group) so disjoint channel groups can be owned concurrently. The per-group lock change is safety-critical and is specified in detail below.

This document follows the project's established vocabulary (see the animation-vocabulary steering): **Gesture** = a `Movements` coroutine (no audio); **Routine**/**Act** = audio-backed `Animatronic` action; **Stream** = live mic passthrough; **Mode** = a continuous background loop.

### Resolved decisions (from requirements Q&A)

- **Camera placement**: head-mounted (nose). Tracking is a closed feedback loop — the neck is driven to reduce the person's offset from frame center.
- **Detection stack**: TensorFlow Lite with a COCO-trained SSD-MobileNet model (people + animals + objects). Edge TPU acceleration is optional/configurable, not required.
- **Lighting**: night operation is supported via the NoIR camera plus an IR illuminator; person detection must work in darkness with IR illumination.
- **Leave-frame behavior**: on losing the Target_Person, the head performs a slow scanning sweep to reacquire a person; after a configurable timeout with no detection it recenters and yields.

### Confirmed defaults

The following defaults were recommended during the requirements Q&A and have since been confirmed by the operator (no longer open assumptions):

- **Multi-person selection**: when multiple people are detected, the one with the largest bounding box (nearest proxy) is the tracking target.
- **Jitter control**: a center deadband plus command smoothing.
- **Detection→Routine mapping**: a configurable map (not a hardcoded fixed list), seeded with the `person → wave` and `person+dog → walk-your-dog` examples.
- **Process model**: the camera + detection pipeline runs as a separate long-lived service that does not require root; the web app and Tracking Mode query it. Root privilege stays confined to servo/GPIO processes.
- **Responsiveness**: detection runs at >= 5 frames per second and a neck tracking update is issued within 500 ms of a new detection (adjustable).

## Glossary

- **Camera_Service**: The separate long-lived process that owns the camera, runs the detection pipeline, and exposes the latest frame and detection results to other processes. Does not require root privilege.
- **IR_Illuminator**: The infrared illumination hardware paired with the NoIR camera that lights the scene in darkness so the camera can capture a usable image for detection when ambient light is insufficient.
- **Detector**: The component inside Camera_Service that runs the TFLite COCO model over a frame and returns a list of detections.
- **Detection**: A single recognized object: a class label (e.g. `person`, `dog`), a confidence score, and a bounding box in frame pixel coordinates.
- **Tracking_Controller**: The component that converts the selected person's bounding-box center into neck pan/tilt commands.
- **Tracking_Mode**: The fourth background Mode. A continuous loop (read detections → compute direction → drive neck) that runs until interrupted.
- **Target_Person**: The single Detection of class `person` currently chosen for head tracking.
- **Scan_Sweep**: The Tracking_Mode reacquisition behavior — a slow pan of the Neck_Group across the `NECK_PAN` safe range to find a person after the Target_Person is lost.
- **Frame_Center**: The pixel coordinate at the horizontal/vertical center of the camera frame.
- **Offset**: The signed pixel distance from the Target_Person bounding-box center to Frame_Center, in the horizontal (pan) and vertical (tilt) axes.
- **Deadband**: A center region of the frame within which Offset is treated as zero (no neck command issued).
- **Neck_Group**: Servo channel group for the head/neck — channels 0 (`NECK_PAN`) and 1 (`NECK_TILT`).
- **Arm_Group**: Servo channel group for the right arm — channels 4–7.
- **Group_Lock**: A per-channel-group cross-process advisory lock. One Group_Lock exists per channel group (Neck_Group, Arm_Group).
- **Servo_Writer**: Any process that commands servo angles (gesture scripts, routines, Tracking_Mode).
- **Control_Panel**: The Flask web control panel (`src/webapp.py`, port 8000).
- **Detection_Routine_Map**: The configurable mapping from a detection condition (e.g. "person present", "person and dog present") to a Routine action name in `action_map`.
- **Live_Feed**: The MJPEG (or equivalent) stream of camera frames with detection overlays, served to the browser.

## Requirements

### Requirement 1: Camera capture (Step 1)

**User Story:** As an operator, I want the head-mounted camera to initialize and produce frames, so that downstream detection and display have a video source.

#### Acceptance Criteria

1. WHEN Camera_Service starts, THE Camera_Service SHALL initialize the Raspberry Pi Camera Module 3 Noir and begin capturing frames within 10 seconds of process start.
2. WHILE Camera_Service is capturing, THE Camera_Service SHALL expose the most recent captured frame to other components, and SHALL replace it with each newly captured frame such that the exposed frame is no older than 1 captured frame interval.
3. THE Camera_Service SHALL capture frames at a configurable resolution between 320x240 and 1920x1080 pixels and a configurable frame rate between 5 and 30 frames per second, defaulting to a resolution and frame rate that sustain at least 5 captured frames per second measured over any 10-second window on the Raspberry Pi.
4. IF no component has requested the exposed frame yet, THEN THE Camera_Service SHALL continue capturing and overwriting the exposed frame without buffering more than 1 frame.
5. IF the camera device cannot be opened or initialized within 10 seconds of process start, THEN THE Camera_Service SHALL report an initialization error identifying the camera device as the failure source and exit with a non-zero status, while the Control_Panel continues running.
6. IF frame capture fails after successful initialization, THEN THE Camera_Service SHALL report a capture error identifying the failure and SHALL retain the last successfully captured frame as the exposed frame.
7. WHILE Camera_Service is capturing, THE Camera_Service SHALL run as a non-root process.

### Requirement 2: Live feed in the control panel (Step 2)

**User Story:** As an operator, I want to view the live camera feed in the web control panel, so that I can see what the robot sees during testing.

#### Acceptance Criteria

1. WHEN an operator opens the camera view in the Control_Panel, THE Control_Panel SHALL display the Live_Feed from Camera_Service within 2 seconds.
2. WHILE Camera_Service is producing frames, THE Control_Panel SHALL update the Live_Feed continuously at a configurable rate between 1 and 30 frames per second.
3. WHERE detection overlays are enabled, THE Control_Panel SHALL render each Detection as a bounding box labeled with the class name and a confidence score in the range 0.00 to 1.00 over the Live_Feed.
4. WHERE detection overlays are enabled AND no Detections are present for the current frame, THE Control_Panel SHALL display the Live_Feed with no bounding boxes.
5. IF Camera_Service is unavailable when the camera view is opened, THEN THE Control_Panel SHALL display a "camera unavailable" status indicator instead of a broken stream, and all other Control_Panel controls SHALL remain accessible.
6. IF the Live_Feed stops producing frames after the camera view is open, THEN THE Control_Panel SHALL display a stalled-feed status indicator within 5 seconds.
7. WHEN the Control_Panel serves any camera route, THE Control_Panel SHALL expose the Live_Feed as a read-only view, and THE camera route SHALL NOT issue any servo command.

### Requirement 3: Person and object detection (Step 3)

**User Story:** As an operator, I want the system to identify people, animals, and objects in the frame, so that the robot can react to what is present.

#### Acceptance Criteria

1. WHILE Camera_Service is capturing, THE Detector SHALL run the TFLite COCO SSD-MobileNet model over each captured frame and produce zero or more Detections per processed frame.
2. THE Detector SHALL classify each Detection with a COCO class label, a confidence score in the range 0.0 to 1.0, and a bounding box expressed as frame pixel coordinates bounded by the captured frame dimensions.
3. WHERE a detected object's confidence score is below a configurable confidence threshold (default 0.5, configurable within the range 0.0 to 1.0), THE Detector SHALL exclude that Detection from the reported results.
4. WHEN the Detector completes processing a frame, THE Detector SHALL make the resulting list of Detections for that frame available to both the Control_Panel overlay and Tracking_Mode.
5. WHERE Edge TPU acceleration is configured and available, THE Detector SHALL use the Edge TPU delegate for inference.
6. IF Edge TPU acceleration is configured but unavailable, THEN THE Detector SHALL perform inference using CPU and SHALL emit an indication that the Edge TPU fallback to CPU occurred.
7. WHERE a Detection's COCO class label is `person`, THE Detector SHALL mark that Detection as a person for use by Tracking_Mode.

### Requirement 4: Person position detection (Step 4)

**User Story:** As an operator, I want the system to compute where a tracked person is relative to the center of the frame, so that the neck can be aimed at that person.

#### Acceptance Criteria

1. WHEN one or more person Detections are present, THE Tracking_Controller SHALL select exactly one Target_Person as the Detection whose bounding box has the largest pixel area.
2. IF two or more Detections share the largest bounding-box area, THEN THE Tracking_Controller SHALL select the one whose bounding-box center is closest to Frame_Center as the Target_Person.
3. WHEN a Target_Person is selected, THE Tracking_Controller SHALL compute a horizontal Offset and a vertical Offset of the Target_Person bounding-box center relative to Frame_Center, where the horizontal Offset is positive when the center is right of Frame_Center and the vertical Offset is positive when the center is below Frame_Center.
4. WHILE both the horizontal and vertical distances from the Target_Person bounding-box center to Frame_Center are within the Deadband half-width (a configurable region centered on Frame_Center, default 5% of frame width horizontally and 5% of frame height vertically), THE Tracking_Controller SHALL report both the horizontal and vertical Offset as zero.
5. WHEN no person Detection is present, THE Tracking_Controller SHALL report that no Target_Person is available and report both Offsets as zero.

### Requirement 5: Head tracking (Step 5)

**User Story:** As an operator, I want the neck to turn so the head follows the tracked person, so that the robot appears to make eye contact.

#### Acceptance Criteria

1. WHEN the horizontal Offset is non-zero and indicates the Target_Person is to the animatronic's left, THE Tracking_Controller SHALL increase the `NECK_PAN` angle to turn the head toward the Target_Person.
2. WHEN the horizontal Offset is non-zero and indicates the Target_Person is to the animatronic's right, THE Tracking_Controller SHALL decrease the `NECK_PAN` angle to turn the head toward the Target_Person.
3. WHEN the vertical Offset is non-zero and indicates the Target_Person is below Frame_Center, THE Tracking_Controller SHALL increase the `NECK_TILT` angle to lower the head toward the Target_Person.
4. WHEN the vertical Offset is non-zero and indicates the Target_Person is above Frame_Center, THE Tracking_Controller SHALL decrease the `NECK_TILT` angle to raise the head toward the Target_Person.
5. THE Tracking_Controller SHALL issue every neck angle command through `TrunkController.set_angle`, which clamps the commanded angle to `constants.SAFE_LIMITS`.
6. THE Tracking_Controller SHALL apply smoothing so that each neck command changes the commanded angle by no more than a configurable maximum step per update (default 5 degrees, configurable within 1 to 30 degrees).
7. WHEN a new Target_Person Offset is computed, THE Tracking_Controller SHALL issue the corresponding neck command within 500 milliseconds.
8. WHILE tracking is active, THE Tracking_Controller SHALL write only to Neck_Group channels (0 and 1).
9. WHEN no Target_Person is available, THE Tracking_Controller SHALL issue no neck command and SHALL hold the current neck angle.

### Requirement 6: Tracking Mode as a background Mode

**User Story:** As an operator, I want tracking to run as a continuous background Mode that behaves like a Gesture toward the mic stream, so that it fits the existing Mode model and never fights audio-backed actions.

#### Acceptance Criteria

1. WHEN an operator starts Tracking_Mode from the Control_Panel, THE Tracking_Mode SHALL run a continuous loop that reads Detections, computes direction, and drives the Neck_Group until interrupted.
2. THE Tracking_Mode SHALL carry no audio and SHALL NOT drive the jaw motor.
3. WHILE a live mic Stream is active, THE Tracking_Mode SHALL continue running without interrupting the Stream.
4. WHEN a Routine or Act is requested, THE Tracking_Mode SHALL wind down and release the Neck_Group within 1 second so the Routine or Act can run.
5. WHEN an operator presses a Routine or Movement button in the Control_Panel, THE Tracking_Mode SHALL wind down and yield using the same cross-process stop-signal pattern used by Sleep and Awake modes (`nap_signal` equivalent).
6. WHERE an arm-only Gesture (channels 4-7, no audio) is requested while Tracking_Mode is active, THE Tracking_Mode SHALL continue running concurrently because the Gesture owns only disjoint Arm_Group channels.
7. WHEN the Target_Person leaves the frame, THE Tracking_Mode SHALL begin a Scan_Sweep that pans the Neck_Group across the `NECK_PAN` safe range to reacquire a person.
8. WHILE performing a Scan_Sweep, THE Tracking_Mode SHALL issue every neck command through `TrunkController.set_angle` so each commanded angle is clamped to `constants.SAFE_LIMITS`.
9. WHILE performing a Scan_Sweep, IF a person Detection reappears, THEN THE Tracking_Mode SHALL stop the Scan_Sweep and resume tracking that person as the Target_Person.
10. IF no person Detection is reacquired within a configurable scan timeout (default 10 seconds, configurable within 1 to 120 seconds) after the Scan_Sweep begins, THEN THE Tracking_Mode SHALL recenter the Neck_Group to `REST_POSITIONS` and yield to the previously active Mode.
11. THE Tracking_Mode SHALL issue every neck command through `TrunkController.set_angle` so each command is clamped to `constants.SAFE_LIMITS`.
12. WHERE Sleep mode or Awake mode is configured to be interrupted by a person-detection sensor, THE Camera_Service person Detection SHALL serve as that interrupting sensor signal.

### Requirement 7: Detection-triggered Routines (Step 6)

**User Story:** As an operator, I want specific detections to trigger matching Routines, so that the robot reacts contextually (waving at a person, commenting on a dog walker).

#### Acceptance Criteria

1. THE Detection_Routine_Map SHALL define a configurable mapping where each entry maps exactly one detection condition to exactly one Routine action name that is present in `action_map`.
2. WHEN a detection condition in the Detection_Routine_Map is satisfied and its cooldown is not active, THE System SHALL request the mapped Routine through the existing `action_map` allowlist dispatch within 1 second of the condition being satisfied.
3. THE Detection_Routine_Map SHALL include a default entry mapping the condition "a person is present" to a wave Routine.
4. THE Detection_Routine_Map SHALL include a default entry mapping the condition "a person and a dog are present" to a walk-your-dog Routine.
5. IF two or more detection conditions in the Detection_Routine_Map are simultaneously satisfied, THEN THE System SHALL select the condition matching the greatest number of detected object classes, and SHALL select the condition defined earliest in the Detection_Routine_Map when the counts are equal.
6. IF a detection condition maps to an action name that is not present in the `action_map` allowlist, THEN THE System SHALL reject the request, SHALL NOT execute any subprocess or servo command for that name, and SHALL emit an indication identifying the rejected action name.
7. WHEN a detection-triggered Routine is requested, THE System SHALL wind down Tracking_Mode and release the Neck_Group before the Routine begins driving the jaw/audio path, so that the Routine and Tracking_Mode never own the Neck_Group simultaneously.
8. WHERE a detection condition has triggered its Routine, THE System SHALL block that same condition from triggering that same Routine again until a configurable cooldown of 1 to 600 seconds (default 30 seconds) has elapsed since the Routine completed.

### Requirement 8: Per-channel-group servo locking (safety-critical)

**User Story:** As a developer, I want the servo lock to be per channel group instead of one whole-robot mutex, so that disjoint channel groups (neck vs arm) can be owned by independent processes at the same time without ever letting two writers command the same channel.

#### Acceptance Criteria

1. THE servo lock module SHALL provide one Group_Lock per channel group: a Neck_Group lock (channels 0-1) and an Arm_Group lock (channels 4-7).
2. WHEN a Servo_Writer intends to write a channel, THE Servo_Writer SHALL hold the Group_Lock for every channel group that contains a channel it will write before issuing any write to that channel.
3. THE Servo_Writer SHALL NOT write any channel that belongs to a channel group whose Group_Lock the Servo_Writer does not hold.
4. IF a requested Group_Lock is already held by another process and the request is non-waiting, THEN THE servo lock module SHALL raise a busy error for that group, SHALL NOT grant the lock, and the requesting CLI entry point SHALL exit with status code 3.
5. WHERE a Group_Lock is requested in waiting mode, THE servo lock module SHALL block until that group's lock becomes available.
6. WHEN a Servo_Writer acquires more than one Group_Lock and any one of the requested Group_Locks is unavailable in non-waiting mode, THEN THE servo lock module SHALL grant none of the requested Group_Locks.
7. WHEN a Servo_Writer releases a Group_Lock or exits on error, THE Servo_Writer SHALL return the channels of each released group to `REST_POSITIONS` for that group only and SHALL NOT write any channel outside the released groups.
8. WHEN a Servo_Writer process holding a Group_Lock terminates for any reason, THE operating system SHALL release that Group_Lock so a subsequent requester can acquire it.
9. WHEN a Routine or Act requires both neck and arm motion, THE Routine or Act SHALL acquire both the Neck_Group lock and the Arm_Group lock before writing either group.
10. WHEN a Routine or Act requests the Neck_Group lock while Tracking_Mode holds it, THE Tracking_Mode SHALL release the Neck_Group lock within 1 second, and IF Tracking_Mode does not release it within that interval THEN the Routine or Act request SHALL fail with a busy error rather than writing the Neck_Group.
11. THE servo lock module SHALL ensure that no two processes simultaneously hold the Group_Lock for the same channel group, such that a second non-waiting acquirer of an already-held group always receives a busy error.
12. WHILE two independent processes hold disjoint Group_Locks, THE servo lock module SHALL allow both to run concurrently.
13. THE servo lock module SHALL anchor each Group_Lock lockfile in the user-owned project directory so that both root-run gesture scripts and the user-run Control_Panel can open it.
14. THE servo lock module SHALL provide a non-destructive per-group status probe reporting whether each Group_Lock is currently held, usable by the Control_Panel.
15. IF a per-group status probe cannot open a lockfile, THEN THE status probe SHALL report that group as not held rather than raising an error.

### Requirement 9: Security and privilege boundaries

**User Story:** As a maintainer, I want the camera, web, and tracking additions to preserve the project's security boundaries, so that adding network-facing vision code does not weaken hardware safety.

#### Acceptance Criteria

1. WHEN the Control_Panel receives a camera-related or tracking-related action request, THE Control_Panel SHALL verify the action name against an explicit allowlist (action_map) before launching any subprocess.
2. IF a requested action name is not present in the allowlist, THEN THE Control_Panel SHALL reject the request, SHALL NOT launch any subprocess, and SHALL return a response indicating the action is not permitted.
3. THE System SHALL NOT pass any externally supplied action name to `getattr`, `eval`, `exec`, or a shell command without first validating it against the allowlist.
4. WHEN a web route receives a value that becomes a filesystem path, THE route SHALL validate the value against an explicit allowlist of permitted names or confirm the resolved path remains within the designated base directory before use.
5. IF a web-route-supplied path value fails validation or resolves outside the designated base directory, THEN THE route SHALL reject the request, SHALL NOT read or write the targeted path, and SHALL return a response indicating the value is invalid.
6. THE Camera_Service SHALL run as a non-root process, confining root privilege to the servo/GPIO Servo_Writers.
7. THE System SHALL NOT transmit camera frames or detection data to any network endpoint outside the local device.

### Requirement 10: Night operation with IR illumination

**User Story:** As an operator, I want the camera to detect people in darkness using IR illumination, so that the robot stays interactive at night.

#### Acceptance Criteria

1. WHERE ambient light is insufficient for detection, THE System SHALL enable the IR_Illuminator so the NoIR camera can capture a usable image.
2. WHILE the IR_Illuminator is active, THE Detector SHALL continue to detect people from the IR-illuminated frames.
3. THE System SHALL provide a configurable control to enable, disable, or auto-switch the IR_Illuminator based on ambient light.
4. WHERE the IR_Illuminator is configured to auto-switch, THE System SHALL enable the IR_Illuminator when the measured ambient light level falls below a configurable ambient-light threshold and SHALL disable the IR_Illuminator when the measured ambient light level rises above that threshold.
5. WHERE the IR_Illuminator hardware is absent or fails, THE System SHALL continue operating in available light and SHALL report that IR illumination is unavailable.
6. WHEN the IR_Illuminator state changes between enabled and disabled, THE System SHALL report the new IR_Illuminator state to the Control_Panel.
