"""Unit tests for ``AudioPlayer``'s ``drive_jaw`` / ``drive_eyes`` flags.

``AudioPlayer.__init__`` normally constructs real gpiozero devices
(``LED(EYE_LIGHT_PIN)`` and ``DigitalOutputDevice(MOUTH_MOTOR_PIN)``). To test
the flag logic WITHOUT touching real hardware, we monkeypatch the two names the
module imports at top -- ``audio_player.LED`` and
``audio_player.DigitalOutputDevice`` -- with fakes that record their on()/off()
calls. This lets us assert:

  - With ``drive_jaw=False`` the jaw device is never constructed
    (``player.jaw_motor is None``) and ``talk()`` issues no jaw on()/off().
  - With ``drive_eyes=False`` the eye LED is never constructed
    (``player.led_eye_light is None``) -- so ``EYE_LIGHT_PIN`` is left free for a
    separate blinker -- and ``talk()`` issues no eye on()/off().
  - With both defaults (``True``) both devices are constructed and driven from
    the envelope, preserving today's behaviour.

No real ``pyaudio`` stream is opened and no real GPIO pin is claimed. Run with:

    SERVO_SIM=1 .venv/bin/python -m pytest tests/test_audio_player_flags.py -q --maxfail=1
"""

import os
import sys

import numpy as np
import pytest

# Ensure src/ is importable when pytest runs from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import audio_player  # noqa: E402


class FakeDevice:
    """Records on()/off() calls in place of a real gpiozero device.

    Stands in for both ``LED`` and ``DigitalOutputDevice`` so the flag tests can
    confirm exactly which devices are driven without any GPIO access.

    Attributes:
        pin: The pin the fake was "constructed" for (for debugging).
        on_calls: Number of ``on()`` calls.
        off_calls: Number of ``off()`` calls.
    """

    def __init__(self, pin):
        self.pin = pin
        self.on_calls = 0
        self.off_calls = 0

    def on(self):
        """Record an ``on()`` actuation."""
        self.on_calls += 1

    def off(self):
        """Record an ``off()`` actuation."""
        self.off_calls += 1


@pytest.fixture
def fake_gpio(monkeypatch):
    """Patch ``audio_player.LED`` / ``DigitalOutputDevice`` with recording fakes.

    Yields nothing; tests read the constructed fakes off the ``AudioPlayer``
    instance (``player.led_eye_light`` / ``player.jaw_motor``), which are the
    fakes when the corresponding flag is enabled and ``None`` when disabled.
    """
    monkeypatch.setattr(audio_player, "LED", FakeDevice)
    monkeypatch.setattr(audio_player, "DigitalOutputDevice", FakeDevice)
    yield


def _prime_chunk():
    """Return a quiet-but-above-floor chunk to seed the running average low.

    The envelope seeds ``_avg`` to the first real level, so a single loud frame
    can't out-swell its own seed. Feeding a low (but above ``silence_floor``)
    frame first seeds ``_avg`` near ~700, so the following loud frame clears the
    ``open_ratio * avg`` threshold and opens the jaw.

    Returns:
        Raw bytes of a low-amplitude mono int16 buffer.
    """
    return (np.ones(1024, dtype=np.int16) * 700).tobytes()


def _loud_chunk():
    """Return a loud int16 PCM chunk that pushes the jaw OPEN.

    A large-amplitude signal makes the per-window RMS exceed both the silence
    floor and the adaptive open threshold, so a driven device gets an ``on()``.

    Returns:
        Raw bytes of a loud mono int16 buffer.
    """
    return (np.ones(1024, dtype=np.int16) * 12000).tobytes()


def _open_jaw(player):
    """Drive ``player`` through prime + loud frames so the jaw opens.

    Args:
        player: The ``AudioPlayer`` under test.
    """
    player.talk(_prime_chunk(), start_time=0.0)
    player.talk(_loud_chunk(), start_time=0.0)


def _silent_chunk():
    """Return a silent int16 PCM chunk that keeps the jaw closed.

    Returns:
        Raw bytes of a zero-amplitude mono int16 buffer.
    """
    return np.zeros(1024, dtype=np.int16).tobytes()


def test_defaults_construct_and_drive_both_devices(fake_gpio):
    """Both flags default True: jaw + eye devices constructed and actuated.

    Confirms the existing behaviour is preserved -- a loud chunk drives both the
    jaw motor and the eye LED ``on()``.
    """
    player = audio_player.AudioPlayer()

    assert isinstance(player.jaw_motor, FakeDevice)
    assert isinstance(player.led_eye_light, FakeDevice)

    _open_jaw(player)

    assert player.jaw_open is True
    assert player.jaw_motor.on_calls >= 1
    assert player.led_eye_light.on_calls >= 1


def test_drive_jaw_false_never_constructs_or_drives_jaw(fake_gpio):
    """``drive_jaw=False``: jaw device is None and never actuated.

    The eye LED is still constructed and driven (drive_eyes defaults True), but
    the jaw motor is never built and ``talk()`` makes no jaw on()/off() calls on
    a loud OR silent chunk.
    """
    player = audio_player.AudioPlayer(drive_jaw=False)

    assert player.jaw_motor is None
    assert isinstance(player.led_eye_light, FakeDevice)

    _open_jaw(player)
    player.talk(_silent_chunk(), start_time=0.0)

    # No jaw device exists, so nothing could have been actuated on it.
    # The eye LED, still enabled, was driven by the loud chunk.
    assert player.led_eye_light.on_calls >= 1


def test_drive_eyes_false_frees_eye_pin_and_never_drives_eyes(fake_gpio):
    """``drive_eyes=False``: eye LED is None (pin freed) and never actuated.

    This is the hypnotic case: the eye LED is NOT constructed, so
    ``EYE_LIGHT_PIN`` is left free for a separate blinker to own. The jaw motor
    (drive_jaw defaults True) is still constructed and driven.
    """
    player = audio_player.AudioPlayer(drive_eyes=False)

    assert player.led_eye_light is None
    assert isinstance(player.jaw_motor, FakeDevice)

    _open_jaw(player)

    # Jaw still driven; no eye device exists to actuate.
    assert player.jaw_motor.on_calls >= 1


def test_both_disabled_construct_neither_and_drive_nothing(fake_gpio):
    """Both flags False (hypnotic): neither device constructed nor driven.

    ``talk()`` still runs the envelope math (so ``jaw_open`` may flip) but makes
    no hardware calls because both devices are ``None``.
    """
    player = audio_player.AudioPlayer(drive_jaw=False, drive_eyes=False)

    assert player.jaw_motor is None
    assert player.led_eye_light is None

    # Must not raise despite both devices being None -- the guards protect it.
    _open_jaw(player)
    player.talk(_silent_chunk(), start_time=0.0)
