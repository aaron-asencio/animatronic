# Design Document: Kinematic Collision Model

## Overview

This feature adds an offline gesture-authoring aid that predicts self-collisions for the "Maximus" animatronic before a pose is ever driven to hardware. It computes forward kinematics from servo angles and detects when non-adjacent links intersect, naming the colliding link pair and the offending joints (servo channel + URDF joint name).

The system is delivered as a new plain-Python package, `src/kinematics/`, that imports **no** hardware libraries (`adafruit_servokit`, `gpiozero`, `pyaudio`). It builds on `yourdfpy` (URDF parsing + forward kinematics) and `numpy` (analytic geometry), with `trimesh`/`matplotlib` used only for an optional, lazily-imported 3D preview.

The central design tension is reconciling two coordinate conventions:

- **Servos** are commanded in degrees (0–270) with empirically reseated, non-zero centers (see `SAFE_LIMITS` / `REST_POSITIONS` / the AXIS DIRECTION REFERENCE in `src/constants.py`).
- **The URDF** (`src/config/maximus.urdf`) is in radians, zero-centered, with per-joint axis signs.

Each of the six mapped joints therefore carries a calibratable transform, stored in an editable JSON file so signs, offsets, and scales can be refined against physical measurements without code changes. Every joint's `scale` (its servo-degree → physical-arc ratio) is an empirical unknown: no value is trusted until proven on hardware. `RT_SHOULDER_ROTATOR` is the only joint observed strongly geared so far — its electrical 0–270° range maps to a ~170° physical arc, so its `scale` is well below 1. The remaining direct-drive joints are *expected* near 1.0, but that is a hypothesis to confirm against the physical robot, not an assumed fact.

Hardware validation is kept strictly **outside** the model package: a separate `validate_hardware.py` driver imports the model for predictions but performs any servo driving elsewhere, so the model stays importable and testable without a Pi.

This phase is explicitly an authoring aid. It does **not** gate live servo writes and leaves the existing `TrunkController` write path unchanged.

## Grounded Facts (from the actual URDF and constants)

### Kinematic tree (from `src/config/maximus.urdf`)

```
base_link (box 0.265 x 0.038 x 0.55)
├── [yawl_neck_joint  revolute z]  origin (0,0,0.275)   → lower_neck_link  (cyl l=0.046 r=0.056)
│       └── [lower_neck_joint FIXED] → middle_neck_link (cyl l=0.014 r=0.056)
│               └── [pitch_neck_joint revolute y] → upper_neck_link (cyl l=0.03 r=0.056)
│                       └── [upper_neck_joint FIXED] origin (0,0,0.17) → head_link (sphere r=0.105)
└── [shoulder_yaw_joint revolute x]  origin (0.1325,0,0.250) → shoulder_link (cyl l=0.065 r=0.056)
        └── [shoulder_pitch_joint revolute y] origin (0.03,0,0) → upper_arm_link (cyl l=0.19 r=0.015)
                └── [elbow_yaw_joint revolute z] → elbow_link (cyl l=0.055 r=0.056)
                        └── [elbow_pitch_joint revolute x] origin (0,0,0.285) → lower_arm_link (cyl l=0.17 r=0.015)
                                └── [wrist_joint FIXED] origin (0,0,0.17) → hand_link (box 0.12 x 0.10 x 0.02)
```

Fixed joints (`lower_neck_joint`, `upper_neck_joint`, `wrist_joint`) do not move independently — `middle_neck_link`/`head_link`/`hand_link` move rigidly with their parent chain.

### Joint → servo mapping (confirmed in the URDF header comment)

Note the shoulder yaw/pitch URDF names are **swapped** relative to the tilt/rotator servo naming.

| URDF joint | Axis | Servo constant | Channel |
|---|---|---|---|
| `yawl_neck_joint` | z | `NECK_PAN` | 0 |
| `pitch_neck_joint` | y | `NECK_TILT` | 1 |
| `shoulder_yaw_joint` | x | `RT_SHOULDER_TILT` | 6 |
| `shoulder_pitch_joint` | y | `RT_SHOULDER_ROTATOR` | 7 |
| `elbow_yaw_joint` | z | `RT_ELBOW_ROTATOR` | 4 |
| `elbow_pitch_joint` | x | `RT_ELBOW_TILT` | 5 |

### Landmark data driving calibration seeds (from the AXIS DIRECTION REFERENCE)

- `NECK_PAN` center = 90 (forward)
- `NECK_TILT` center = 90 (level)
- `RT_ELBOW_ROTATOR` center = 150 (hand parallel to side)
- `RT_ELBOW_TILT` = 5 straight (145 = right angle, 210 = full flexion)
- `RT_SHOULDER_TILT` rest = 55, 135 = arm straight out horizontally
- `RT_SHOULDER_ROTATOR` rest = 0 (arm at side), electrical 0–270 → ~170° physical arc (gearing)

## Architecture

### Module layout

```
src/kinematics/                     ← new package, NO hardware imports
├── __init__.py                     ← exports CollisionModel, PoseResult
├── calibration.py                  ← Calibration_Store: servo° <-> URDF rad transform + JSON store
├── kinematics.py                   ← Kinematics_Engine: yourdfpy load + per-link FK
├── proxies.py                      ← Collision_Proxy generation (capsule/sphere + inflation)
├── collision.py                    ← Collision_Detector: analytic proxy intersection + joint mapping
├── model.py                        ← CollisionModel facade + PoseResult / dataclasses
├── preview.py                      ← optional 3D preview (trimesh/matplotlib, lazily imported)
└── cli.py                          ← command-line entry point (python -m src.kinematics.cli)

src/validate_hardware.py            ← SEPARATE hardware-validation driver (imports model, drives servos elsewhere)

src/config/calibration.json         ← editable calibration store (auto-created if missing)

tests/
├── test_calibration.py
├── test_kinematics.py
├── test_proxies.py
├── test_collision.py
├── test_model.py
└── test_cli.py
```

### Module diagram

```
                         ┌───────────────────────────┐
                         │        cli.py             │  parse pose/sequence JSON,
                         │  (python -m ...cli)       │  print SAFE/COLLISION,
                         └─────────────┬─────────────┘  optional --preview (lazy)
                                       │
                         ┌─────────────▼─────────────┐
                         │        model.py           │  CollisionModel facade
                         │  is_pose_safe / check_seq │  input validation, PoseResult
                         └──┬──────────┬─────────┬───┘
             ┌──────────────┘          │         └───────────────┐
   ┌─────────▼────────┐   ┌────────────▼──────┐      ┌───────────▼─────────┐
   │ calibration.py   │   │  kinematics.py    │      │   collision.py      │
   │ Calibration_Store│   │  Kinematics_Engine│      │  Collision_Detector │
   │ servo°→rad, JSON │   │  yourdfpy FK      │      │  analytic distance, │
   └────────┬─────────┘   └─────────┬─────────┘      │  adjacency, joint    │
            │                       │                │  mapping            │
            │                       │                └──────────┬──────────┘
            │             ┌─────────▼─────────┐                 │
            │             │    proxies.py     │◄────────────────┘
            │             │ Collision_Proxy   │  capsules/spheres from <visual>
            │             └───────────────────┘
      seeds from                                        preview.py (lazy import,
      constants.py                                      only when --preview)
```

### Data flow (single pose)

```
servo_angles: dict[int channel, float deg]
      │
      ▼  (1) CollisionModel validates channels + numeric
Calibration_Store.to_radians(channel, deg)
      │        urdf_rad = sign * (servo_deg - offset_deg) * (pi/180) * scale
      ▼  (2) dict[urdf_joint_name, radians]
Kinematics_Engine.link_transforms(joint_radians)
      │        yourdfpy FK → per-link 4x4 world transform (relative to base_link)
      ▼  (3) dict[link_name, np.ndarray(4,4)]
Collision_Detector.check(link_transforms, proxies)
      │        place each Collision_Proxy in world, test non-adjacent pairs
      │        analytic capsule/sphere min-distance vs summed radii
      ▼  (4) list[colliding link pair]
map each pair → offending joints (walk kinematic chain between links)
      │
      ▼  (5)
PoseResult(ok, colliding_pairs=[(linkA, linkB, offending_joints)])
```

## Components and Interfaces

### `calibration.py` — Calibration_Store

Owns the per-joint transform and the editable JSON store. Depends only on `json`, `math`, and constants derived at module load.

```python
# src/kinematics/calibration.py
import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JointCalibration:
    """Per-joint servo-degree to URDF-radian calibration.

    Attributes:
        urdf_joint: URDF revolute joint name (e.g. "yawl_neck_joint").
        servo_channel: PCA9685 channel number from constants.py.
        sign: +1 or -1, direction reconciliation between servo and URDF axis.
        offset_deg: Servo angle (deg) that maps to 0 radians in the URDF.
        scale: Multiplier mapping a servo-degree delta to the physical/URDF-radian
            delta. PROVISIONAL until hardware-measured — expected near 1.0 for
            direct-drive joints and ~170/270 for the geared shoulder rotator, but
            every joint's scale is an empirical unknown that must be validated
            against the physical robot.
    """
    urdf_joint: str
    servo_channel: int
    sign: float
    offset_deg: float
    scale: float


class Calibration_Store:
    """Loads, seeds, validates, and applies per-joint calibration transforms."""

    def __init__(self, path: str | Path):
        """Loads calibration from ``path``, seeding the file if it is absent.

        Args:
            path: Filesystem path to the editable calibration JSON file.
        """

    @staticmethod
    def seed_defaults() -> dict[str, JointCalibration]:
        """Returns provisional seed calibrations derived from constants.py."""

    def to_radians(self, servo_channel: int, servo_deg: float) -> float:
        """Converts a servo angle in degrees to URDF radians.

        Args:
            servo_channel: PCA9685 channel number.
            servo_deg: Commanded servo angle in degrees.

        Returns:
            The URDF joint angle in radians:
            ``sign * (servo_deg - offset_deg) * (pi/180) * scale``.
        """

    def to_servo_deg(self, servo_channel: int, urdf_rad: float) -> float:
        """Inverse of ``to_radians`` (used for round-trip validation)."""

    def joint_radians(self, servo_angles: dict[int, float]) -> dict[str, float]:
        """Maps a servo-channel pose to a URDF-joint-name → radians dict."""
```

Notes:
- `to_radians` implements the transform of Requirement 2.1. When `servo_deg == offset_deg`, the result is exactly 0 rad (Requirement 2.5) regardless of sign/scale.
- Missing file → seed and write (Requirement 3.3). Missing/non-numeric field → `CalibrationError` naming the joint and field (Requirement 3.4).
- Editing the JSON and re-initializing changes behavior with no code change (Requirement 3.5).

### `kinematics.py` — Kinematics_Engine

```python
# src/kinematics/kinematics.py
import numpy as np


class Kinematics_Engine:
    """Loads the Maximus URDF and computes per-link forward kinematics."""

    def __init__(self, urdf_path: str):
        """Loads the URDF via yourdfpy.

        Args:
            urdf_path: Path to src/config/maximus.urdf.

        Raises:
            KinematicsError: If the URDF is missing or fails to parse; the
                message names the path and the underlying parse failure.
        """

    def link_transforms(self, joint_radians: dict[str, float]) -> dict[str, np.ndarray]:
        """Computes each link's 4x4 world transform relative to base_link.

        Args:
            joint_radians: URDF-joint-name → angle in radians. Unspecified
                revolute joints default to 0. Fixed joints are ignored (their
                child links move rigidly with the parent).

        Returns:
            link_name → 4x4 homogeneous transform (numpy array), base_link
            being the identity.
        """

    def adjacency(self) -> set[frozenset[str]]:
        """Returns the set of directly-connected link pairs from the joint graph."""

    def joints_between(self, link_a: str, link_b: str) -> list[str]:
        """Returns the revolute URDF joints on the tree path between two links."""

    def visual_geometry(self) -> dict[str, "VisualPrimitive"]:
        """Returns each link's <visual> primitive (box/cylinder/sphere + origin)."""
```

Notes:
- Uses `yourdfpy` FK; `<visual>` is the geometric source (Requirement 1.4) since the URDF has no `<collision>` tags.
- `<limit>` values are ignored for range enforcement (Requirement 1.5) — the engine computes FK for any angle.
- The adjacency set is built from the parent/child joint graph including fixed joints, so rigid pairs (e.g. `lower_arm_link`/`hand_link`) are treated as adjacent.

### `proxies.py` — Collision_Proxy generation

Converts each link's visual primitive into a conservative bounding **capsule** (segment + radius) or **sphere**, inflated by a margin.

```python
# src/kinematics/proxies.py
import numpy as np
from dataclasses import dataclass


@dataclass(frozen=True)
class Capsule:
    """A capsule proxy in a link's local frame.

    Attributes:
        p0: Segment start point, shape (3,).
        p1: Segment end point, shape (3,).
        radius: Capsule radius (already includes the inflation margin).
    """
    p0: np.ndarray
    p1: np.ndarray
    radius: float


@dataclass(frozen=True)
class Sphere:
    """A sphere proxy in a link's local frame.

    Attributes:
        center: Sphere center, shape (3,).
        radius: Sphere radius (already includes the inflation margin).
    """
    center: np.ndarray
    radius: float


@dataclass(frozen=True)
class Box:
    """An oriented bounding box (OBB) proxy in a link's local frame.

    Attributes:
        center: Box center, shape (3,).
        axes: Orthonormal local axes as column vectors, shape (3, 3).
        half_extents: Half-sizes along each local axis, shape (3,) (already
            include the inflation margin).
    """
    center: np.ndarray
    axes: np.ndarray
    half_extents: np.ndarray


def make_proxy(primitive: "VisualPrimitive", margin: float) -> Capsule | Sphere | Box:
    """Builds a conservative proxy fully enclosing ``primitive`` plus ``margin``.

    Mapping rules:
        - sphere(r)          → Sphere(center, r + margin)
        - cylinder(l, r)     → Capsule along the cylinder axis, endpoints at
                               ±l/2, radius = r + margin. The spherical caps
                               guarantee the flat cylinder ends are enclosed.
        - box(x, y, z)       → an oriented bounding Box (OBB) that faithfully
                               encloses the slab: center = origin translation,
                               axes = the box's local axes (the rotation block of
                               the visual origin), half_extents = (x/2, y/2,
                               z/2) + margin. This replaces the old
                               longest-axis-capsule / near-cubic-sphere box rule,
                               which over-enclosed flat torso/hand slabs.

    Args:
        primitive: The link's visual primitive with its local origin.
        margin: Non-negative inflation distance (raises ValueError if < 0).

    Returns:
        A Capsule, Sphere, or Box expressed in the link's local frame.
    """
```

Notes:
- Every proxy fully encloses the raw geometry expanded by the margin (Requirement 4.2), and is never smaller than the raw geometry (conservative).
- Increasing the margin never shrinks the enclosed volume (Requirement 4.4).
- Negative margin is rejected; margin ≥ 0 is applied to every proxy (Requirement 4.3).
- Thin arm cylinders (`upper_arm_link`, `lower_arm_link`, r=0.015) become thin capsules; `head_link` becomes a sphere; the base and hand boxes become oriented bounding **Box** (OBB) proxies oriented by the visual origin, so flat slabs are enclosed faithfully rather than swollen into a fat capsule/sphere.

### `collision.py` — Collision_Detector

```python
# src/kinematics/collision.py
import numpy as np


def capsule_capsule_distance(a: "Capsule", b: "Capsule") -> float:
    """Returns the minimum distance between two capsule *segments* minus radii.

    Uses the analytic closest-points-between-two-segments algorithm (clamped
    parametric solution) to get the segment-segment distance ``d``; the surface
    gap is ``d - a.radius - b.radius``. A value <= 0 means intersection.

    Args:
        a: First capsule (world frame).
        b: Second capsule (world frame).
    """


def capsule_sphere_distance(c: "Capsule", s: "Sphere") -> float:
    """Point-to-segment distance from the sphere center to the capsule segment,
    minus the two radii. <= 0 means intersection."""


def sphere_sphere_distance(a: "Sphere", b: "Sphere") -> float:
    """Center distance minus the two radii. <= 0 means intersection."""


def sphere_box_distance(s: "Sphere", b: "Box") -> float:
    """Closest point on the OBB to the sphere center (via clamping) minus the
    sphere radius. Exact. <= 0 means intersection."""


def capsule_box_distance(c: "Capsule", b: "Box") -> float:
    """Conservative closest-feature approximation of the segment-OBB distance
    (min of each endpoint-to-box distance and the box-center-to-segment
    distance) minus the capsule radius. <= 0 means intersection."""


def box_box_distance(a: "Box", b: "Box") -> float:
    """OBB-OBB overlap via the Separating Axis Theorem (SAT): tests the 15
    candidate axes (3+3 face normals + 9 edge cross products). Exact overlap
    test; <= 0 means intersection, and a positive return is a conservative
    lower bound on the true gap."""


class Collision_Detector:
    """Detects intersecting non-adjacent proxy pairs and maps them to joints."""

    def __init__(self, engine: "Kinematics_Engine", proxies: dict):
        """Precomputes the adjacency exclusion set from the joint graph."""

    def check(self, link_transforms: dict[str, np.ndarray]) -> list["CollisionPair"]:
        """Places every proxy in world coordinates and tests all non-adjacent
        link pairs, returning the intersecting pairs with offending joints.

        Args:
            link_transforms: link_name → 4x4 world transform.

        Returns:
            A list of CollisionPair(link_a, link_b, offending_joints).
        """
```

Notes:
- Proxies are transformed into world coordinates using the FK link transforms (Requirement 5.1). A capsule transforms by moving its two endpoints; a sphere by moving its center; a box by moving its center and rotating its axes (half-extents unchanged, transforms being rigid).
- Box pairs use the Separating Axis Theorem for an exact overlap test; sphere-box is exact and capsule-box is a conservative closest-feature approximation.
- Only **non-adjacent** pairs are tested; directly-connected pairs are excluded (Requirement 5.4).
- Analytic capsule/sphere distance tests use `numpy` (cheap, no meshing). `trimesh` is available if a mesh-level test is ever needed but is not used on the verdict path.
- `offending_joints` are computed by walking the kinematic tree path between the two links and collecting the revolute joints on that path, each carrying its servo channel + URDF joint name (Requirements 5.3, 5.5).

### `model.py` — CollisionModel facade + data models

```python
# src/kinematics/model.py
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OffendingJoint:
    """A joint implicated in a colliding link pair.

    Attributes:
        urdf_joint: URDF joint name (e.g. "shoulder_yaw_joint").
        servo_channel: PCA9685 channel number.
        servo_name: Human-readable name from constants.servos.
    """
    urdf_joint: str
    servo_channel: int
    servo_name: str


@dataclass(frozen=True)
class CollisionPair:
    """A reported colliding link pair.

    Attributes:
        link_a: First link name.
        link_b: Second link name.
        offending_joints: Joints on the chain between the two links.
    """
    link_a: str
    link_b: str
    offending_joints: list[OffendingJoint]


@dataclass(frozen=True)
class PoseResult:
    """The verdict for a single pose.

    Attributes:
        ok: True when no colliding pairs were found.
        colliding_pairs: The colliding link pairs (empty when ok is True).
    """
    ok: bool
    colliding_pairs: list[CollisionPair] = field(default_factory=list)


@dataclass(frozen=True)
class SequenceResult:
    """The verdict for a pose sequence.

    Attributes:
        per_pose: PoseResult for each input pose, in order.
        unsafe: True when any pose in the sequence is unsafe.
    """
    per_pose: list[PoseResult]
    unsafe: bool


class CollisionModel:
    """Facade tying calibration, kinematics, proxies, and detection together."""

    def __init__(
        self,
        urdf_path: str = "src/config/maximus.urdf",
        calibration_path: str = "src/config/calibration.json",
        inflation_margin: float = 0.01,
    ):
        """Initializes the model, loading the URDF, calibration, and proxies.

        Args:
            urdf_path: Path to the Maximus URDF.
            calibration_path: Path to the editable calibration JSON.
            inflation_margin: Non-negative proxy inflation distance (meters).
        """

    def is_pose_safe(self, servo_angles: dict[int, float]) -> PoseResult:
        """Classifies a single servo pose as safe or colliding.

        Args:
            servo_angles: PCA9685 channel → angle in degrees. Must contain the
                six mapped channels with numeric values.

        Returns:
            A PoseResult. ``ok`` is True iff ``colliding_pairs`` is empty.

        Raises:
            PoseInputError: If a required channel is missing or an angle is
                non-numeric; the message names the offending channel.
        """

    def check_sequence(self, poses: list[dict[int, float]]) -> SequenceResult:
        """Classifies an ordered sequence of poses.

        Args:
            poses: Ordered list of servo poses.

        Returns:
            A SequenceResult with a per-pose result list and an overall
            ``unsafe`` flag that is True iff any pose is unsafe.
        """
```

Notes:
- `is_pose_safe` is pure/deterministic given fixed calibration and URDF (Requirement 6.1) and imports no hardware libraries (Requirements 6.5, 11.3). `ok == (colliding_pairs is empty)` always holds (Requirements 6.2, 6.3).
- Input validation raises `PoseInputError` naming the channel (Requirement 6.4).
- The facade exposes no servo-write or gating interface (Requirements 10.1–10.3).

### `cli.py` — command-line authoring aid

Invoked as `python -m src.kinematics.cli`. Accepts a single pose or a sequence.

```
# Single pose, inline JSON of channel→deg
python -m src.kinematics.cli --pose '{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}'

# Repeated poses (sequence)
python -m src.kinematics.cli --pose '{...}' --pose '{...}'

# Sequence from a JSON file (list of channel→deg objects)
python -m src.kinematics.cli --sequence poses.json

# Optional 3D preview (lazy import of trimesh/matplotlib)
python -m src.kinematics.cli --pose '{...}' --preview
```

Output per pose:
- `SAFE` when no collision.
- `COLLISION: <link_a> <-> <link_b> (joints: <servo_name>/<urdf_joint>, ...)` when colliding (Requirement 7.3).
- Sequence: one verdict line per pose in order (Requirement 7.2); overall exit is unsafe if any pose collides (Requirement 7.5).
- Parse failure: a message describing the expected format (channel→degrees JSON object, or a JSON list of such objects) and a non-zero exit (Requirement 7.4).

The `--preview` path imports `preview.py` lazily so verdicts still work headless / without a display (Requirements 8.1–8.3).

### `preview.py` — optional 3D preview

Builds a `trimesh` scene (or `matplotlib` 3D fallback) of the placed link proxies for a pose and highlights the links in any colliding pair. Imported only when `--preview` is used, so a missing display or missing extras never blocks verdicts.

### `validate_hardware.py` — hardware validation (SEPARATE, outside the model)

Lives at `src/validate_hardware.py`, outside `src/kinematics/`. It imports `CollisionModel` for predictions but performs servo driving through the existing hardware layer elsewhere, keeping the model package hardware-free (Requirements 9.5, 11.3). Besides comparing predicted vs measured link positions and verdicts, it measures each joint's actual physical arc versus its commanded servo-degree range so the provisional per-joint `scale` (and `sign` and `offset_deg`) can be confirmed or corrected (Requirement 9.6).

```python
# src/validate_hardware.py
def position_difference(predicted: dict[str, np.ndarray],
                        measured: dict[str, np.ndarray]) -> dict[str, float]:
    """Per-link Euclidean distance between predicted and measured link origins.

    Args:
        predicted: link_name → predicted world position (3,).
        measured: link_name → measured world position (3,).

    Returns:
        link_name → distance in meters (0 when identical).
    """


def measured_scale(commanded_deg_range: tuple[float, float],
                   measured_arc_deg: float) -> float:
    """Estimates a joint's physical scale from a commanded sweep.

    Divides the measured physical arc (degrees) by the commanded servo-degree
    range so the per-joint ``scale`` seed can be confirmed or corrected against
    the physical robot (e.g. a 0→270 sweep producing a ~170° arc yields
    ~0.63). Direct-drive joints are expected near 1.0 but must be measured.

    Args:
        commanded_deg_range: (start_deg, end_deg) commanded servo sweep.
        measured_arc_deg: Observed physical arc in degrees over that sweep.

    Returns:
        The empirical scale = measured_arc_deg / |end_deg - start_deg|.
    """


def compare_verdict(model_ok: bool, measured_collision: bool) -> str:
    """Compares model verdict to measured outcome → "agree" or "disagree"."""


def boundary_probe_report(first_collision_index: int | None,
                          contact_index: int) -> bool:
    """Reports whether the model flagged a collision at or before contact.

    Args:
        first_collision_index: Step index where the model first flags a
            collision along the probe, or None if it never does.
        contact_index: Step index where physical contact was observed.

    Returns:
        True iff the model flagged before/at physical contact.
    """


def log_disagreement(path: str, servo_angles: dict[int, float],
                     model_ok: bool, measured_collision: bool) -> None:
    """Appends servo angles and both verdicts to a disagreement log file."""
```

## Data Models

### Calibration JSON schema (`src/config/calibration.json`)

```json
{
  "yawl_neck_joint":     {"servo_channel": 0, "sign": 1.0,  "offset_deg": 90.0, "scale": 1.0},
  "pitch_neck_joint":    {"servo_channel": 1, "sign": 1.0,  "offset_deg": 90.0, "scale": 1.0},
  "shoulder_yaw_joint":  {"servo_channel": 6, "sign": 1.0,  "offset_deg": 135.0, "scale": 1.0},
  "shoulder_pitch_joint":{"servo_channel": 7, "sign": 1.0,  "offset_deg": 0.0,  "scale": 0.6296},
  "elbow_yaw_joint":     {"servo_channel": 4, "sign": 1.0,  "offset_deg": 150.0, "scale": 1.0},
  "elbow_pitch_joint":   {"servo_channel": 5, "sign": 1.0,  "offset_deg": 5.0,  "scale": 1.0}
}
```

### Seed calibration values — **PROVISIONAL until hardware-validated**

Every value below is a starting *estimate* derived from the AXIS DIRECTION REFERENCE landmarks in `constants.py`. **All three of `sign`, `offset_deg`, and `scale` are provisional for every joint** — none is trusted until proven against the physical robot; the seeds exist only to give the hardware validation loop something to confirm or correct. Specifically:

- **Offsets** are best estimates from documented landmark centers, still pending confirmation.
- **Signs** are provisional guesses and MUST be confirmed by the hardware validation workflow (a wrong sign drives the link the opposite way).
- **Scales** are empirical unknowns for *every* joint. The `1.0` values for the non-geared joints are an **UNVERIFIED ASSUMPTION** — direct-drive joints are *expected* near 1.0 (e.g. neck pan sweeping 0→180 covers nearly 180 physical degrees, so its scale is *probably* near 1), but this must be measured, not assumed. Only `RT_SHOULDER_ROTATOR` has been *observed* strongly geared (~170°/270°), and even that seed is subject to correction.

| URDF joint | Servo | Channel | sign (provisional) | offset_deg (provisional) | scale (provisional) | Rationale |
|---|---|---|---|---|---|---|
| `yawl_neck_joint` | NECK_PAN | 0 | +1 | 90 | 1.0* | 90 = head faces forward (0 rad). |
| `pitch_neck_joint` | NECK_TILT | 1 | +1 | 90 | 1.0* | 90 = head level (0 rad). |
| `shoulder_yaw_joint` | RT_SHOULDER_TILT | 6 | +1 | 135 | 1.0* | 135 = arm straight out horizontally (chosen as the 0-rad landmark). |
| `shoulder_pitch_joint` | RT_SHOULDER_ROTATOR | 7 | +1 | 0 | ≈0.6296 (170/270) | Geared: electrical 0–270 → ~170° arc (observed); rest 0 = 0 rad. |
| `elbow_yaw_joint` | RT_ELBOW_ROTATOR | 4 | +1 | 150 | 1.0* | 150 = hand parallel to side (0 rad). |
| `elbow_pitch_joint` | RT_ELBOW_TILT | 5 | +1 | 5 | 1.0* | 5 = elbow straight (0 rad); 145 ≈ right angle. |

> \* **Scale unverified** — expected ~1 for direct drive, but must be measured on the physical robot before it is trusted. The geared shoulder-rotator seed (~170/270) is itself an observation to reconfirm.

> **Provisional-calibration note:** Every seed above (sign, offset, and scale) is a starting estimate only. The transform's correctness for magnitude and zero-crossing does not depend on the sign being right, but the *direction* of motion does, and the physical-arc magnitude depends on the scale being right. The hardware validation workflow compares predicted vs measured link positions and measures each joint's actual physical arc versus its commanded servo-degree range to confirm or correct each sign, offset, and scale, then the JSON is edited in place (no code change per Requirement 3.5).

### Visual primitive model

```python
@dataclass(frozen=True)
class VisualPrimitive:
    """A link's <visual> geometry expressed in the link frame.

    Attributes:
        kind: "box" | "cylinder" | "sphere".
        dims: box (x, y, z); cylinder (length, radius); sphere (radius,).
        origin: 4x4 local transform of the visual origin (xyz + rpy).
    """
    kind: str
    dims: tuple[float, ...]
    origin: np.ndarray
```

## Capsule distance math

The verdict path uses cheap analytic distances (all in `numpy`), never meshing.

**Segment–segment distance (capsule–capsule).** For segments `P(s) = p0 + s*(p1-p0)` and `Q(t) = q0 + t*(q1-q0)` with `s, t ∈ [0, 1]`, minimize `|P(s) - Q(t)|²`. Solve the 2×2 system from setting the partial derivatives to zero, then **clamp** `s` and `t` to `[0, 1]` (re-solving the other parameter after clamping) to handle the parallel/endpoint cases. The surface gap is:

```
gap = segment_distance(seg_a, seg_b) - radius_a - radius_b
intersect  ⇔  gap <= 0
```

**Point–segment distance (capsule–sphere).** Project the sphere center onto the capsule segment, clamp the projection parameter to `[0, 1]`, take the distance to the clamped point:

```
gap = point_segment_distance(center, seg) - capsule_radius - sphere_radius
```

**Center distance (sphere–sphere).**

```
gap = ||center_a - center_b|| - radius_a - radius_b
```

Because proxies are conservative (never smaller than the raw geometry plus a non-negative margin), `gap <= 0` for the proxies is a sufficient (over-approximating) condition for a real collision — the model prefers false positives over misses (Requirement 4's intent).

## Adjacency exclusion

The adjacency set is the set of link pairs joined by a **single** joint (revolute or fixed) in the URDF graph. Directly-connected pairs are never tested (Requirement 5.4). From the actual tree, excluded pairs include:

```
base_link–lower_neck_link, lower_neck_link–middle_neck_link,
middle_neck_link–upper_neck_link, upper_neck_link–head_link,
base_link–shoulder_link, shoulder_link–upper_arm_link,
upper_arm_link–elbow_link, elbow_link–lower_arm_link,
lower_arm_link–hand_link
```

All other pairs (e.g. `upper_arm_link`–`base_link`, `lower_arm_link`–`head_link`, `hand_link`–`base_link`) are candidates for collision testing. The arm-vs-body collision that per-axis `SAFE_LIMITS` cannot catch — shoulder tilt + rotator driving the upper/lower arm into `base_link` — falls squarely in the tested set.

## Offending-joint mapping

For a reported colliding pair `(link_a, link_b)`, walk the kinematic tree to find the unique path between the two links (via their lowest common ancestor). Collect every **revolute** joint on that path; each maps to an `OffendingJoint(urdf_joint, servo_channel, servo_name)` using the mapping table. Fixed joints on the path contribute no offending joint (they cannot change the configuration). This is how an arm-vs-body pair is reported together with the shoulder joints (Requirement 5.5).

## Error Handling

| Condition | Behavior | Requirement |
|---|---|---|
| URDF missing/unparseable | Raise `KinematicsError` naming path + parse failure | 1.2 |
| Calibration file missing | Create it seeded with defaults; proceed | 3.3 |
| Calibration entry missing field / non-numeric | Raise `CalibrationError` naming joint + field | 3.4 |
| Negative inflation margin | Raise `ValueError` | 4.3 |
| Pose missing required channel | Raise `PoseInputError` naming the channel | 6.4 |
| Pose angle non-numeric | Raise `PoseInputError` naming the channel | 6.4 |
| CLI unparseable input | Print expected-format message, non-zero exit | 7.4 |
| Preview extras/display unavailable | Verdicts still computed; preview raises a clear message only when `--preview` requested | 8.3 |

Debug output uses `print()` (no logging framework), consistent with the codebase (Requirement 11.5).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Calibration transform round-trips

For any joint calibration (sign ∈ {+1, −1}, finite offset_deg, non-zero finite scale) and any finite servo angle, converting servo degrees to radians and back with the inverse transform returns the original servo angle within numerical tolerance.

**Validates: Requirements 2.1**

### Property 2: Offset is the zero-crossing

For any joint calibration, transforming a servo angle equal to that joint's `offset_deg` produces exactly 0 radians, regardless of sign and scale.

**Validates: Requirements 2.5**

### Property 3: Calibration JSON serialization round-trips

For any valid calibration mapping over the six joints, writing it to the JSON store and loading it back yields an equal mapping.

**Validates: Requirements 3.1**

### Property 4: Forward kinematics yields valid rigid transforms

For any dict of URDF-joint angles in radians, every link transform returned by the Kinematics_Engine is a valid homogeneous rigid transform (orthonormal rotation block, bottom row `[0, 0, 0, 1]`), `base_link` is the identity, and each fixed-joint child link equals its parent transform composed with the fixed origin (rigid motion with the parent).

**Validates: Requirements 1.3, 1.4**

### Property 5: Proxies conservatively enclose their geometry

For any visual primitive and any non-negative margin, every sampled point on the raw primitive's surface lies inside the generated proxy, and the proxy's radius/extent is never smaller than the raw geometry's extent plus the margin.

**Validates: Requirements 4.2**

### Property 6: Proxy inflation is monotonic in margin

For any visual primitive and any two margins `m1 <= m2`, the enclosed volume of the proxy generated with `m1` is less than or equal to the enclosed volume generated with `m2`.

**Validates: Requirements 4.4**

### Property 7: Proxy intersection detection is correct and symmetric

For any pair of proxies (capsule or sphere), the detector reports an intersection if and only if the analytic surface gap is ≤ 0, and the result is unchanged when the two proxies are swapped.

**Validates: Requirements 5.2**

### Property 8: Adjacent link pairs are never reported

For any pose, no directly-connected (single-joint) link pair from the URDF graph appears in the reported colliding pairs.

**Validates: Requirements 5.4**

### Property 9: Offending joints equal the revolute joints between the links

For any reported colliding link pair, the offending joints are exactly the revolute URDF joints on the kinematic-tree path between the two links, each carrying its servo channel and URDF joint name.

**Validates: Requirements 5.3, 5.5**

### Property 10: is_pose_safe is deterministic

For any valid pose, calling `is_pose_safe` twice on the same model instance returns equal PoseResults.

**Validates: Requirements 6.1**

### Property 11: ok flag equals the emptiness of the colliding-pair list

For any valid pose, the PoseResult's `ok` flag is True if and only if its `colliding_pairs` list is empty.

**Validates: Requirements 6.2, 6.3**

### Property 12: Sequence is unsafe iff any pose is unsafe

For any pose sequence, the SequenceResult's `unsafe` flag is True if and only if at least one per-pose result has `ok` False, and the per-pose results preserve the input order.

**Validates: Requirements 7.2, 7.5**

### Property 13: Position difference is a well-formed metric

For any two sets of link positions, the per-link difference is zero exactly when the positions are identical and is symmetric under swapping predicted and measured.

**Validates: Requirements 9.1**

### Property 14: Boundary probe reports flagged-before-contact correctly

For any probe with a designated contact step, the boundary-probe report is True if and only if the model's first-collision step index is at or before the contact step index.

**Validates: Requirements 9.3**

## Testing Strategy

Tests use `pytest` + `hypothesis`, run entirely without hardware (Requirement 11.4). Property tests use a minimum of 100 iterations. No hardware libraries are imported by the package or its tests (Requirements 6.5, 11.3).

### Dual approach

- **Property tests** cover the universal properties above — the calibration transform, calibration JSON round-trip, FK rigidity, proxy enclosure/monotonicity, capsule/sphere distance detection, adjacency exclusion, offending-joint mapping, determinism, the ok⇔empty invariant, sequence aggregation, and the validation helpers.
- **Example / edge-case tests** cover specific scenarios and error conditions:
  - **Known SAFE pose** — the rest pose (`{0:90, 1:90, 4:150, 5:5, 6:55, 7:0}` from `REST_POSITIONS`) classifies as SAFE.
  - **Known COLLISION pose** — an elbow-flexion-behind-back / arm-into-body pose (e.g. shoulder tilt near the low end with a rotator value plus elbow flexion) classifies as COLLISION with the arm-vs-body pair and shoulder offending joints.
  - Missing/unparseable URDF raises `KinematicsError` naming the path.
  - Missing-field and non-numeric calibration entries raise `CalibrationError` naming joint + field.
  - Missing channel / non-numeric angle raises `PoseInputError` naming the channel.
  - `<limit>`-exceeding angle still computes FK (no clamping).
  - CLI: rest pose prints SAFE; known collision prints the named pair + joints; malformed JSON prints the format message with a non-zero exit.
  - Import scan: the `src/kinematics` package imports none of `adafruit_servokit`, `gpiozero`, `pyaudio`.
  - Preview disabled: verdicts computed with no display; `preview` module not imported on the verdict path.

### Fixtures

- Use the **real** `src/config/maximus.urdf` for FK, proxy, and end-to-end collision tests (it is the source of truth).
- Use a temporary calibration path (`tmp_path`) for calibration store tests so seeding/creation is isolated.
- A small in-memory `VisualPrimitive` generator (hypothesis strategies over box/cylinder/sphere dims and margins) drives the proxy and distance property tests.

### Property test tagging

Each property test is tagged: **Feature: kinematic-collision-model, Property {n}: {property text}**, and references the design property it validates.

## Dependencies (to pin in `requirements.txt`)

- `yourdfpy` — URDF parsing + forward kinematics (new).
- `trimesh` — optional mesh support / preview (new; not on the verdict path).
- `numpy` — analytic geometry (already present).
- `hypothesis` — property-based testing (dev/test).
- Preview extras: `matplotlib` and/or `pyglet` for `trimesh` scene viewing — declared as optional extras so headless installs work.

All runtime deps are pinned to specific versions (Requirement 11.1). The `src/kinematics` package imports no hardware libraries (Requirements 6.5, 9.5, 11.3), mirroring the existing stub-friendly pattern so it installs and tests reproducibly without a Raspberry Pi.
