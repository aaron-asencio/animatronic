# Requirements Document

## Introduction

This feature adds a 3D kinematic collision model for the animatronic figure "Maximus" to accelerate gesture development. The model computes forward kinematics from servo angles and detects self-collisions (arm versus body/head, and multi-axis combinations that per-axis `SAFE_LIMITS` cannot catch).

In this phase the model is an **offline gesture-authoring aid**. It provides a CLI and an importable Python API that classify a servo pose (or pose sequence) as SAFE or COLLISION, naming the colliding link pair and offending joints. It is built on `yourdfpy` (URDF parsing + forward kinematics) and `trimesh` (collision), in plain Python — no ROS, no pybullet — and must be importable and testable **without hardware** (no hardware library imports at all, mirroring the existing gpiozero/pyaudio stub pattern).

A central concern is reconciling servo semantics with the URDF: servos are commanded in degrees (0–270) with empirically reseated non-zero centers, while the URDF is in radians, zero-centered, with per-joint axis signs. Each joint therefore needs a calibratable transform whose `sign`, `offset_deg`, and `scale` are all provisional estimates that MUST be validated against the physical robot — no calibration value is trusted until proven on hardware. The degrees-to-physical-arc ratio (`scale`) is an empirical unknown for every joint: `RT_SHOULDER_ROTATOR` is the one joint observed so far to be strongly geared (electrical 0–270 servo degrees mapping to a ~170° physical arc, so its scale is well below 1), while the direct-drive joints are expected to be near 1.0 but that expectation is a hypothesis to confirm, not an assumed fact. These transforms are seeded from the AXIS DIRECTION REFERENCE landmarks in `src/constants.py` as starting estimates and stored in an editable JSON calibration file so offsets, signs, and scales can be refined against physical measurements without code changes.

The model MUST be validatable against the real robot: model-predicted link positions and collision verdicts can be compared to physical reality through a hardware validation workflow that supports probing toward predicted collision boundaries (to confirm the model flags a collision slightly before physical contact) and logs servo angles where model and reality disagree.

This phase explicitly does **not** gate live servo writes. Live-gating is a later, separate effort.

## Glossary

- **Collision_Model**: The overall software system delivered by this feature — URDF loading, forward kinematics, calibration, collision-proxy generation, self-collision detection, the `is_pose_safe` API, the CLI, the optional 3D preview, and the hardware validation workflow.
- **Kinematics_Engine**: The component that loads the URDF and computes forward kinematics (per-link 3D transforms) from joint angles in radians, using `yourdfpy`.
- **Calibration_Transform**: The per-joint mapping from servo degrees to URDF radians, of the form `urdf_rad = sign * (servo_deg - offset_deg) * (pi/180) * scale`.
- **Calibration_Store**: The editable JSON file, and the code that loads and validates it, holding per-joint `sign`, `offset_deg`, and `scale`.
- **Collision_Proxy**: A conservative bounding volume (bounding capsule — cylinder with spherical end caps — or sphere) that fully encloses a URDF link's visual geometry with an inflation margin.
- **Collision_Detector**: The component that, given per-link poses and collision proxies, detects intersecting proxy pairs and reports colliding link pairs.
- **Pose**: A set of commanded servo angles in degrees keyed by servo channel (as defined in `src/constants.py`), representing a single robot configuration.
- **Pose_Sequence**: An ordered list of poses representing successive robot configurations.
- **Pose_Result**: The verdict object returned by the API for a single pose, containing an `ok` flag, the list of colliding link pairs, and the offending joints for each pair.
- **Offending_Joints**: The joints (identified by servo channel and/or URDF joint name) whose configuration contributes to a detected colliding link pair.
- **Servo_Channel**: A PCA9685 channel number defined in `src/constants.py` (e.g. `NECK_PAN = 0`).
- **URDF_Joint**: A revolute joint defined in `src/config/maximus.urdf`, mapped to a servo channel per the mapping table in that file.
- **Geared_Joint**: A joint whose servo-degree range does not map one-to-one onto its physical arc, requiring a `scale` other than 1. Every joint's `scale` is empirical, but `RT_SHOULDER_ROTATOR` (channel 7) is the only joint observed strongly geared so far — its electrical 0–270° range maps to a ~170° physical arc.
- **Inflation_Margin**: A non-negative distance added to each collision proxy's extent so proxies conservatively over-enclose their link.
- **Hardware_Validation_Workflow**: The offline-driven procedure that compares model-predicted link positions and collision verdicts to measured physical reality, probes toward predicted collision boundaries, and records disagreements.
- **Boundary_Probe**: A validation step that advances joints toward a model-predicted collision boundary to confirm the model flags a collision slightly before physical contact.
- **SAFE_LIMITS**: The per-channel hard angle bounds (degrees) defined in `src/constants.py`.
- **AXIS_DIRECTION_REFERENCE**: The documented mapping in `src/constants.py` describing how commanded servo angles map to physical motion and landmark angles.

## Requirements

### Requirement 1: URDF loading and forward kinematics

**User Story:** As a gesture author, I want the model to load the Maximus URDF and compute each link's 3D position from joint angles, so that I can reason about the physical configuration of a pose.

#### Acceptance Criteria

1. WHEN the Collision_Model is initialized, THE Kinematics_Engine SHALL load the URDF from `src/config/maximus.urdf` using `yourdfpy`.
2. IF the URDF file cannot be found or fails to parse, THEN THE Kinematics_Engine SHALL raise an error identifying the URDF path and the parse failure.
3. WHEN provided a mapping of URDF_Joint names to angles in radians, THE Kinematics_Engine SHALL compute the 3D transform of every link relative to `base_link`.
4. THE Kinematics_Engine SHALL treat the URDF `<visual>` geometry as the geometric source for each link, because the URDF contains no `<collision>` tags.
5. THE Kinematics_Engine SHALL ignore the URDF `<limit>` values for joint range enforcement, because the reseated servos make those values non-credible.

### Requirement 2: Per-joint servo-degree to URDF-radian calibration transform

**User Story:** As a developer reconciling servo and URDF conventions, I want each joint to have an explicit degrees-to-radians transform, so that servo poses drive the URDF model correctly.

#### Acceptance Criteria

1. THE Calibration_Transform SHALL convert a servo angle in degrees to a URDF angle in radians using `urdf_rad = sign * (servo_deg - offset_deg) * (pi/180) * scale`.
2. THE Collision_Model SHALL define a Calibration_Transform for each of the six mapped joints: `NECK_PAN`→`yawl_neck_joint`, `NECK_TILT`→`pitch_neck_joint`, `RT_SHOULDER_TILT`→`shoulder_yaw_joint`, `RT_SHOULDER_ROTATOR`→`shoulder_pitch_joint`, `RT_ELBOW_ROTATOR`→`elbow_yaw_joint`, and `RT_ELBOW_TILT`→`elbow_pitch_joint`.
3. THE Collision_Model SHALL apply the joint-to-servo channel mapping such that the swapped shoulder yaw/pitch URDF names resolve to `RT_SHOULDER_TILT` (channel 6) and `RT_SHOULDER_ROTATOR` (channel 7) respectively.
4. WHERE a joint is the Geared_Joint (`RT_SHOULDER_ROTATOR`), THE Calibration_Transform SHALL use a provisional `scale` seed of approximately 170/270 that maps the electrical 0–270° range onto the observed ~170° physical arc, subject to confirmation or correction by hardware validation.
5. WHEN a servo angle equal to a joint's `offset_deg` is transformed, THE Calibration_Transform SHALL produce 0 radians for that joint.
6. THE Collision_Model SHALL seed each joint's initial `sign`, `offset_deg`, and `scale` from the AXIS_DIRECTION_REFERENCE landmarks and centers in `src/constants.py`.
7. THE Collision_Model SHALL treat every per-joint `sign`, `offset_deg`, and `scale` as a provisional seed requiring hardware validation; no non-geared joint's `scale` SHALL be assumed correct without measurement (direct-drive joints are expected near 1.0 but MUST be confirmed).

### Requirement 3: Editable JSON calibration file

**User Story:** As a technician calibrating against the physical build, I want per-joint offsets, signs, and scales stored in an editable file, so that I can refine calibration from measurements without changing code.

#### Acceptance Criteria

1. THE Calibration_Store SHALL persist per-joint `sign`, `offset_deg`, and `scale` values in a JSON file.
2. WHEN the Collision_Model is initialized, THE Calibration_Store SHALL load the calibration values from the JSON file.
3. IF the calibration JSON file is missing, THEN THE Calibration_Store SHALL create the file seeded with the values derived from the AXIS_DIRECTION_REFERENCE in `src/constants.py`.
4. IF a loaded calibration entry is missing a required field or is not numeric, THEN THE Calibration_Store SHALL raise an error identifying the offending joint and field.
5. WHEN a calibration value in the JSON file is changed and the Collision_Model is re-initialized, THE Collision_Model SHALL apply the updated value without any code change.

### Requirement 4: Conservative collision-proxy generation

**User Story:** As a gesture author, I want each link represented by a conservative bounding volume, so that the model prefers false-positive collision reports over missing a real collision.

#### Acceptance Criteria

1. WHEN the Collision_Model is initialized, THE Collision_Model SHALL generate a Collision_Proxy for each link that has visual geometry, using a bounding capsule or a sphere.
2. THE Collision_Proxy SHALL fully enclose the link's visual geometry expanded by the configured Inflation_Margin.
3. THE Collision_Model SHALL apply an Inflation_Margin greater than or equal to zero to every Collision_Proxy.
4. WHEN the Inflation_Margin is increased, THE Collision_Model SHALL produce Collision_Proxies whose enclosed volume is greater than or equal to the volume produced with a smaller margin.

### Requirement 5: Self-collision detection

**User Story:** As a gesture author, I want the model to detect when links intersect in a given pose and tell me which links and joints are involved, so that I can fix an unsafe gesture.

#### Acceptance Criteria

1. WHEN evaluating a Pose, THE Collision_Detector SHALL position every Collision_Proxy using the Kinematics_Engine link transforms for that Pose.
2. WHEN two Collision_Proxies of non-adjacent links intersect, THE Collision_Detector SHALL report the corresponding colliding link pair.
3. WHEN reporting a colliding link pair, THE Collision_Detector SHALL include the Offending_Joints associated with that pair, identified by servo channel and URDF_Joint name.
4. WHILE two links are directly connected by a single joint, THE Collision_Detector SHALL exclude that adjacent pair from collision reporting.
5. IF the arm and the body links intersect for a given shoulder tilt and shoulder rotator combination, THEN THE Collision_Detector SHALL report the arm-versus-body colliding pair together with the shoulder Offending_Joints.

### Requirement 6: `is_pose_safe` Python API

**User Story:** As a developer writing tests and scripts, I want an importable function that classifies a servo pose, so that I can check pose safety programmatically.

#### Acceptance Criteria

1. WHEN `is_pose_safe(servo_angles)` is called with a Pose, THE Collision_Model SHALL return a Pose_Result containing an `ok` flag and the list of colliding link pairs with their Offending_Joints.
2. WHEN a Pose contains no colliding link pairs, THE Collision_Model SHALL return a Pose_Result with `ok` set to true and an empty list of colliding pairs.
3. WHEN a Pose contains at least one colliding link pair, THE Collision_Model SHALL return a Pose_Result with `ok` set to false and the non-empty list of colliding pairs.
4. IF a Pose omits a required Servo_Channel or contains a non-numeric angle, THEN THE Collision_Model SHALL raise an error identifying the offending channel.
5. THE Collision_Model SHALL import no hardware libraries, so that `is_pose_safe` is callable in an environment without hardware.

### Requirement 7: Command-line authoring aid

**User Story:** As a gesture author, I want a CLI that reports whether a pose or a sequence of poses is safe, so that I can validate gestures during authoring.

#### Acceptance Criteria

1. WHEN the CLI is invoked with a single Pose, THE Collision_Model SHALL print a SAFE or COLLISION verdict for that Pose.
2. WHEN the CLI is invoked with a Pose_Sequence, THE Collision_Model SHALL print a SAFE or COLLISION verdict for each Pose in order.
3. WHEN the CLI reports a COLLISION verdict, THE Collision_Model SHALL name the colliding link pair and the Offending_Joints.
4. IF the CLI receives an input that cannot be parsed as a Pose or Pose_Sequence, THEN THE Collision_Model SHALL print an error message describing the expected input format.
5. WHEN a Pose_Sequence contains at least one Pose with a COLLISION verdict, THE Collision_Model SHALL report the sequence result as unsafe.

### Requirement 8: Optional 3D preview

**User Story:** As a gesture author, I want an optional rendered 3D preview highlighting colliding links, so that I can visually understand a collision.

#### Acceptance Criteria

1. WHERE the 3D preview option is enabled for a Pose, THE Collision_Model SHALL render the links in 3D using `trimesh` or `matplotlib`.
2. WHERE the 3D preview is enabled and a Pose has colliding link pairs, THE Collision_Model SHALL visually highlight the colliding links.
3. WHERE the 3D preview option is not enabled, THE Collision_Model SHALL compute Pose_Results without producing any rendering.

### Requirement 9: Hardware validation workflow

**User Story:** As a technician, I want to compare model predictions against the physical robot, so that I can confirm and refine the model's accuracy and conservative margins.

#### Acceptance Criteria

1. WHEN a set of measured physical link positions is provided for a Pose, THE Hardware_Validation_Workflow SHALL compute the difference between model-predicted link positions and the measured positions for that Pose.
2. WHEN a measured physical collision outcome is provided for a Pose, THE Hardware_Validation_Workflow SHALL compare the model's collision verdict to the measured outcome for that Pose.
3. WHEN performing a Boundary_Probe toward a model-predicted collision boundary, THE Hardware_Validation_Workflow SHALL report whether the model flagged a collision before physical contact occurred.
4. IF the model verdict and the measured outcome disagree for a Pose, THEN THE Hardware_Validation_Workflow SHALL log the servo angles of that Pose along with both verdicts.
5. THE Hardware_Validation_Workflow SHALL keep hardware interaction outside the Collision_Model module, so that the Collision_Model remains importable without hardware.
6. THE Hardware_Validation_Workflow SHALL support measuring each joint's actual physical arc versus its commanded servo-degree range, so that the per-joint `scale` (and `sign` and `offset_deg`) can be confirmed or corrected against the physical robot.

### Requirement 10: Non-goal — no live servo gating

**User Story:** As a maintainer, I want it documented and enforced that this phase does not gate live servo writes, so that scope stays limited to offline authoring.

#### Acceptance Criteria

1. THE Collision_Model SHALL operate as an offline gesture-authoring aid that returns Pose_Results.
2. THE Collision_Model SHALL expose no interface that intercepts or blocks live servo write commands in this phase.
3. THE Collision_Model SHALL leave the existing servo write path in `TrunkController` and related modules unchanged.

### Requirement 11: Dependencies and no-hardware-import constraints

**User Story:** As a developer, I want pinned dependencies and a hardware-free module, so that the model installs reproducibly and runs and tests without a Raspberry Pi.

#### Acceptance Criteria

1. THE Collision_Model SHALL declare `yourdfpy`, `trimesh`, and any required transitive dependencies (including `numpy`) with pinned versions in `requirements.txt`.
2. THE Collision_Model SHALL reside under `src/` (for example `src/collision_model.py` or a `src/kinematics/` package), with tests under `tests/`.
3. THE Collision_Model SHALL import no hardware libraries (for example `adafruit_servokit`, `gpiozero`, or `pyaudio`).
4. WHEN the automated tests are run in an environment without hardware, THE Collision_Model tests SHALL execute using `pytest` and `hypothesis` without requiring hardware.
5. THE Collision_Model SHALL use `snake_case` naming and Google-style docstrings with `Args:` sections, and use `print()`-style debug output rather than a logging framework.
