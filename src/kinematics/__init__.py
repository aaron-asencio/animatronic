"""Kinematic collision model for the "Maximus" animatronic.

Offline gesture-authoring aid: forward kinematics from servo angles + self-
collision detection. Plain Python (yourdfpy + trimesh + numpy); imports NO
hardware libraries so it runs and tests without a Raspberry Pi.

Public API (CollisionModel, PoseResult) is exported once model.py lands
(see .kiro/specs/kinematic-collision-model). This scaffold intentionally
imports no submodules so the package imports cleanly during incremental build.
"""
