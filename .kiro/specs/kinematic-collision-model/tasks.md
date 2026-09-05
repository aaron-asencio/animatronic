# Implementation Plan: Kinematic Collision Model

## Overview

This plan builds the hardware-free `src/kinematics/` package incrementally, following the module dependency order in the design: dependencies + scaffold, then the calibration transform, the FK engine, collision proxies, analytic collision detection, the `CollisionModel` facade, the CLI, the optional preview, and finally the separate `src/validate_hardware.py` driver. Each step builds on the previous and ends by wiring components into the facade and CLI. Property tests reference the 14 Correctness Properties in the design by number; unit/example tests cover the specific scenarios and error conditions. All code uses `snake_case`, Google-style docstrings with `Args:` sections, and `print()`-style debug output. The real `src/config/maximus.urdf` is used as the test fixture.

## Tasks

- [ ] 1. Dependencies and package scaffold
  - Add pinned `yourdfpy` and `trimesh` to `requirements.txt`; confirm `numpy` and `hypothesis` are present and pinned; add optional preview extras (`matplotlib`) with a comment noting they are optional/headless-safe.
  - Create `src/kinematics/__init__.py` exporting `CollisionModel` and `PoseResult` (import wiring stubbed until later tasks land).
  - Do not add or import any hardware libraries in the new package.
  - _Requirements: 11.1, 11.2, 11.3_

- [ ] 2. Calibration transform and editable JSON store
  - [ ] 2.1 Implement `calibration.py` core
    - Define `JointCalibration` frozen dataclass (`urdf_joint`, `servo_channel`, `sign`, `offset_deg`, `scale`) and `CalibrationError`.
    - Implement `Calibration_Store.seed_defaults()` returning the six provisional seed calibrations derived from the `constants.py` AXIS DIRECTION REFERENCE: `yawl_neck_joint` (ch 0, off 90), `pitch_neck_joint` (ch 1, off 90), `shoulder_yaw_joint` (ch 6, off 135), `shoulder_pitch_joint` (ch 7, off 0, scale ≈0.6296), `elbow_yaw_joint` (ch 4, off 150), `elbow_pitch_joint` (ch 5, off 5); all signs +1 provisional; scales seeded 1.0 for the non-geared joints and ≈0.6296 for the geared shoulder rotator, with every scale a provisional seed (1.0 is an unverified assumption expected near 1 for direct drive but pending hardware measurement).
    - Implement `__init__(path)` that loads the JSON, seeding and writing the file when absent, and validates each entry (missing/non-numeric field raises `CalibrationError` naming joint + field).
    - Implement `to_radians` (`sign * (servo_deg - offset_deg) * (pi/180) * scale`), `to_servo_deg` (inverse), and `joint_radians(servo_angles)` mapping channels to URDF-joint radians.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5_

  - [ ]* 2.2 Write property test for the calibration transform round-trip
    - **Property 1: Calibration transform round-trips**
    - **Validates: Requirements 2.1**

  - [ ]* 2.3 Write property test for the offset zero-crossing
    - **Property 2: Offset is the zero-crossing**
    - **Validates: Requirements 2.5**

  - [ ]* 2.4 Write property test for the calibration JSON round-trip
    - **Property 3: Calibration JSON serialization round-trips**
    - **Validates: Requirements 3.1**

  - [ ]* 2.5 Write unit/edge tests for the calibration store
    - Missing file seeds and creates the JSON; changed value re-applies on re-init (no code change).
    - Missing-field and non-numeric entries raise `CalibrationError` naming joint + field.
    - _Requirements: 3.3, 3.4, 3.5_

- [ ] 3. Checkpoint - calibration tests pass
  - Ensure all calibration tests pass, ask the user if questions arise.

- [ ] 4. Kinematics engine (URDF load + forward kinematics)
  - [ ] 4.1 Implement `kinematics.py`
    - Define `KinematicsError`. Implement `Kinematics_Engine.__init__(urdf_path)` loading `src/config/maximus.urdf` via `yourdfpy`; raise `KinematicsError` naming the path and parse failure on missing/unparseable URDF.
    - Implement `link_transforms(joint_radians)` returning each link's 4x4 world transform relative to `base_link` (identity), defaulting unspecified revolute joints to 0 and moving fixed-joint children rigidly.
    - Implement `adjacency()` (single-joint link pairs, including fixed joints), `joints_between(link_a, link_b)` (revolute joints on the tree path), and `visual_geometry()` returning per-link `VisualPrimitive`.
    - Ignore `<limit>` values entirely; compute FK for any angle. Use `<visual>` as the geometric source.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 4.2 Write property test for forward kinematics rigidity
    - **Property 4: Forward kinematics yields valid rigid transforms**
    - **Validates: Requirements 1.3, 1.4**

  - [ ]* 4.3 Write unit/edge tests for the engine
    - Missing/unparseable URDF raises `KinematicsError` naming the path.
    - A `<limit>`-exceeding angle still computes FK without clamping.
    - _Requirements: 1.2, 1.5_

- [ ] 5. Collision proxies
  - [ ] 5.1 Implement `proxies.py`
    - Define `VisualPrimitive`, `Capsule`, and `Sphere` frozen dataclasses.
    - Implement `make_proxy(primitive, margin)` converting box/cylinder/sphere into a conservative capsule or sphere inflated by `margin`: sphere→Sphere(r+margin); cylinder→Capsule along axis, radius r+margin; box→Capsule along longest axis (or Sphere when near-cubic) fully enclosing the box + margin.
    - Reject negative margin with `ValueError`; apply margin ≥ 0 to every proxy.
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [ ]* 5.2 Write property test for conservative enclosure
    - **Property 5: Proxies conservatively enclose their geometry**
    - **Validates: Requirements 4.2**

  - [ ]* 5.3 Write property test for margin monotonicity
    - **Property 6: Proxy inflation is monotonic in margin**
    - **Validates: Requirements 4.4**

  - [ ]* 5.4 Write unit/edge tests for proxy generation
    - Negative margin raises `ValueError`; thin arm cylinders become thin capsules; head sphere and box links map per the design rules.
    - _Requirements: 4.1, 4.3_

- [ ] 6. Collision detection and joint mapping
  - [ ] 6.1 Implement analytic distances in `collision.py`
    - Implement `capsule_capsule_distance` (clamped segment-segment closest points minus radii), `capsule_sphere_distance` (point-segment minus radii), and `sphere_sphere_distance` (center distance minus radii), all in `numpy`; gap ≤ 0 means intersection.
    - _Requirements: 5.2_

  - [ ] 6.2 Implement `Collision_Detector`
    - `__init__(engine, proxies)` precomputes the adjacency exclusion set from the joint graph.
    - `check(link_transforms)` places each proxy in world coordinates (transform capsule endpoints / sphere center), tests all non-adjacent link pairs, and returns `CollisionPair`s. Map each colliding pair to offending revolute joints by walking the tree path between the links (servo channel + URDF joint name).
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 6.3 Write property test for distance detection correctness/symmetry
    - **Property 7: Proxy intersection detection is correct and symmetric**
    - **Validates: Requirements 5.2**

  - [ ]* 6.4 Write property test for adjacency exclusion
    - **Property 8: Adjacent link pairs are never reported**
    - **Validates: Requirements 5.4**

  - [ ]* 6.5 Write property test for offending-joint mapping
    - **Property 9: Offending joints equal the revolute joints between the links**
    - **Validates: Requirements 5.3, 5.5**

- [ ] 7. Checkpoint - geometry and detection tests pass
  - Ensure all kinematics, proxy, and collision tests pass, ask the user if questions arise.

- [ ] 8. CollisionModel facade and data models
  - [ ] 8.1 Implement `model.py`
    - Define `OffendingJoint`, `CollisionPair`, `PoseResult`, `SequenceResult` frozen dataclasses and `PoseInputError`.
    - Implement `CollisionModel.__init__(urdf_path, calibration_path, inflation_margin)` wiring `Calibration_Store`, `Kinematics_Engine`, `make_proxy`, and `Collision_Detector`; no hardware imports.
    - Implement `is_pose_safe(servo_angles)`: validate the six required channels are present and numeric (raise `PoseInputError` naming the channel), convert to radians, run FK + detection, return a `PoseResult` where `ok == (colliding_pairs is empty)`.
    - Implement `check_sequence(poses)` returning a `SequenceResult` with per-pose results in order and `unsafe` true iff any pose is unsafe.
    - Expose no servo-write or gating interface; leave `TrunkController` unchanged.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 10.1, 10.2, 10.3, 11.3_

  - [ ]* 8.2 Write property test for determinism
    - **Property 10: is_pose_safe is deterministic**
    - **Validates: Requirements 6.1**

  - [ ]* 8.3 Write property test for the ok⇔empty invariant
    - **Property 11: ok flag equals the emptiness of the colliding-pair list**
    - **Validates: Requirements 6.2, 6.3**

  - [ ]* 8.4 Write property test for sequence aggregation
    - **Property 12: Sequence is unsafe iff any pose is unsafe**
    - **Validates: Requirements 7.2, 7.5**

  - [ ]* 8.5 Write example/edge tests for the facade
    - Rest pose `{0:90, 1:90, 4:150, 5:5, 6:55, 7:0}` classifies SAFE.
    - Elbow-flexion-behind-back / arm-into-body pose classifies COLLISION with the arm-vs-body pair and shoulder offending joints.
    - Missing channel / non-numeric angle raises `PoseInputError` naming the channel.
    - Import-scan test: `src/kinematics` imports none of `adafruit_servokit`, `gpiozero`, `pyaudio`.
    - _Requirements: 5.5, 6.4, 6.5, 11.3_

- [ ] 9. Command-line authoring aid
  - [ ] 9.1 Implement `cli.py`
    - `argparse` entry point (`python -m src.kinematics.cli`) with `--pose` (repeatable inline JSON channel→deg), `--sequence` (JSON file of pose objects), and `--preview` (lazy import of `preview.py`).
    - Print `SAFE` or `COLLISION: <link_a> <-> <link_b> (joints: <servo_name>/<urdf_joint>, ...)` per pose in order; overall non-zero exit when any pose collides.
    - On unparseable input, print an expected-format message (channel→degrees JSON object or a JSON list of such objects) and exit non-zero.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 9.2 Write unit tests for the CLI
    - Rest pose prints SAFE; known collision prints the named pair + joints; malformed JSON prints the format message with a non-zero exit; sequence with one collision reports unsafe.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

- [ ] 10. Optional 3D preview
  - [ ] 10.1 Implement `preview.py`
    - Build a `trimesh` scene (or `matplotlib` 3D fallback) of the placed link proxies for a pose and highlight links in any colliding pair.
    - Keep the module importable only via the CLI `--preview` path (lazy import) so verdicts work headless; raise a clear message only when preview is requested and extras/display are unavailable.
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ]* 10.2 Write unit test for preview laziness
    - Verdict path computes `PoseResult`s without importing `preview`; `--preview` disabled produces no rendering.
    - _Requirements: 8.3_

- [ ] 11. Hardware validation driver (separate, outside the package)
  - [ ] 11.1 Implement `src/validate_hardware.py`
    - Implement `position_difference(predicted, measured)` (per-link Euclidean distance), `compare_verdict(model_ok, measured_collision)` ("agree"/"disagree"), `boundary_probe_report(first_collision_index, contact_index)` (True iff flagged at/before contact), and `log_disagreement(path, servo_angles, model_ok, measured_collision)` (append to a log file).
    - Import `CollisionModel` for predictions; perform no hardware imports in the model package (driving lives here, outside `src/kinematics/`).
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ]* 11.2 Write property test for the position-difference metric
    - **Property 13: Position difference is a well-formed metric**
    - **Validates: Requirements 9.1**

  - [ ]* 11.3 Write property test for the boundary probe report
    - **Property 14: Boundary probe reports flagged-before-contact correctly**
    - **Validates: Requirements 9.3**

  - [ ]* 11.4 Write unit tests for verdict comparison and logging
    - `compare_verdict` returns "disagree" on mismatch; `log_disagreement` appends servo angles + both verdicts.
    - _Requirements: 9.2, 9.4_

- [ ] 12. Final checkpoint - ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation sub-tasks are never optional.
- Property tests use `hypothesis` with a minimum of 100 iterations and are tagged **Feature: kinematic-collision-model, Property {n}: {property text}**, each referencing the design property it validates.
- Run tests with `pytest -q --maxfail=1` (minimal verbosity, stop on first failure), without hardware.
- The real `src/config/maximus.urdf` is the test fixture for FK, proxy, and end-to-end collision tests; calibration store tests use a temporary path (`tmp_path`) for isolation.
- The seed calibration **signs, offsets, AND scales are all PROVISIONAL** pending hardware validation — none is trusted until proven on the physical robot. The `1.0` scales for the non-geared joints are an unverified assumption (expected near 1 for direct drive, but must be measured), and the geared shoulder-rotator scale (~170/270) is an observation to reconfirm. Transform magnitude and zero-crossing hold regardless, but direction of motion depends on the sign and physical-arc magnitude depends on the scale; the hardware validation workflow confirms or corrects each value by editing the JSON in place (no code change).
- The model is an offline authoring aid only: it must **not** gate live servo writes and leaves the `TrunkController` write path unchanged (Requirement 10).
- The `src/kinematics` package imports no hardware libraries (`adafruit_servokit`, `gpiozero`, `pyaudio`); an import-scan test enforces this.
- Each task references specific requirement clauses for traceability; checkpoints ensure incremental validation.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "4.2", "4.3", "5.2", "5.3", "5.4", "6.1"] },
    { "id": 3, "tasks": ["6.2"] },
    { "id": 4, "tasks": ["6.3", "6.4", "6.5", "8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3", "8.4", "8.5", "9.1", "11.1"] },
    { "id": 6, "tasks": ["9.2", "10.1", "11.2", "11.3", "11.4"] },
    { "id": 7, "tasks": ["10.2"] }
  ]
}
```
