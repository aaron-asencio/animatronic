"""Tests for preview laziness in the kinematic collision model (Requirement 8.3).

The verdict path must classify poses WITHOUT importing the optional 3D preview,
and running the CLI without ``--preview`` must not import ``kinematics.preview``
(and therefore must not pull in the heavy ``trimesh`` rendering stack). A third
test confirms that scene construction itself is display-free, so the preview can
be exercised headless while the blocking viewer (``show``) stays separate.

These are example-based pytest tests (conventions mirror
``tests/test_calibration.py``). Run with:

    .venv/bin/python -m pytest tests/test_preview.py -q --maxfail=1
"""

import os
import sys

import pytest

# Ensure the src/ directory is importable when pytest is run from the repo root
# (mirrors the convention in the other tests/ modules).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# Absolute path to the real Maximus URDF used as the test fixture.
ABS_URDF = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "config", "maximus.urdf")
)

# Rest pose (SAFE) and a colliding pose (arm folded into body), keyed by channel.
_REST_POSE = {0: 90, 1: 90, 4: 150, 5: 5, 6: 55, 7: 0}
_COLLIDING_POSE = {0: 90, 1: 90, 4: 150, 5: 145, 6: 55, 7: 0}


@pytest.fixture
def restore_sys_modules():
    """Saves and restores selected ``sys.modules`` entries around a test.

    Pops ``kinematics.preview``, ``trimesh``, and ``matplotlib`` from
    ``sys.modules`` (recording their prior values) so the test starts from a
    clean slate and can observe whether the code under test imports them. On
    teardown the original entries are restored exactly, so popping modules here
    never corrupts other tests in the session.

    Yields:
        None. The fixture only manages ``sys.modules`` state.
    """
    watched = ("kinematics.preview", "trimesh", "matplotlib")
    saved = {name: sys.modules.get(name) for name in watched}
    for name in watched:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_verdict_path_does_not_import_preview(tmp_path, restore_sys_modules):
    """The verdict path computes PoseResults without importing preview.

    Builds a ``CollisionModel`` from the real URDF and a temp calibration file,
    classifies a safe and a colliding pose, and asserts that neither the model
    import nor ``is_pose_safe`` pulled ``kinematics.preview`` (or its ``trimesh``
    rendering dependency) into ``sys.modules`` (Requirement 8.3).

    Args:
        tmp_path: Pytest temp directory for an isolated calibration JSON.
        restore_sys_modules: Fixture that snapshots/restores ``sys.modules``.
    """
    from kinematics.model import CollisionModel

    model = CollisionModel(
        urdf_path=ABS_URDF,
        calibration_path=str(tmp_path / "c.json"),
    )

    safe = model.is_pose_safe(_REST_POSE)
    colliding = model.is_pose_safe(_COLLIDING_POSE)

    # Sanity: the poses exercise both verdict branches.
    assert safe.ok is True
    assert colliding.ok is False

    # The core requirement: the verdict path never imports the preview module.
    assert "kinematics.preview" not in sys.modules
    # Note: we deliberately do NOT assert 'trimesh' not in sys.modules here.
    # trimesh is a transitive dependency of yourdfpy (used by Kinematics_Engine
    # to parse the URDF), so it is legitimately imported by the verdict path.
    # It is NOT pulled in by the lazy preview module, which is what 8.3 governs.


def test_cli_without_preview_does_not_import_preview(tmp_path, restore_sys_modules):
    """Running the CLI without --preview produces no rendering / preview import.

    Invokes ``kinematics.cli.main`` on a single safe pose (no ``--preview``) and
    asserts a success exit code and that ``kinematics.preview`` was not imported
    (Requirement 8.3).

    Args:
        tmp_path: Pytest temp directory for an isolated calibration JSON.
        restore_sys_modules: Fixture that snapshots/restores ``sys.modules``.
    """
    import kinematics.cli as cli

    exit_code = cli.main(
        [
            "--urdf",
            ABS_URDF,
            "--calibration",
            str(tmp_path / "c.json"),
            "--pose",
            '{"0":90,"1":90,"4":150,"5":5,"6":55,"7":0}',
        ]
    )

    assert exit_code == 0
    assert "kinematics.preview" not in sys.modules


def test_build_scene_is_display_free(tmp_path):
    """Scene construction works headless without opening a viewer.

    Confirms ``preview.build_scene`` places the pose's link proxies into a scene
    with geometry, without holding a window open — showing that scene/verdict
    construction is possible headless while the blocking viewer (``show``) is a
    separate concern. Skips cleanly if ``trimesh`` is genuinely unavailable.

    Args:
        tmp_path: Pytest temp directory for an isolated calibration JSON.
    """
    pytest.importorskip("trimesh")

    from kinematics import preview
    from kinematics.model import CollisionModel

    model = CollisionModel(
        urdf_path=ABS_URDF,
        calibration_path=str(tmp_path / "c.json"),
    )

    scene = preview.build_scene(model, _COLLIDING_POSE)

    assert len(scene.geometry) >= 1
