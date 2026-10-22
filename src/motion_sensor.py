"""motion_sensor.py

Reusable HC-SR501 PIR motion sensor helper.

Wraps gpiozero's ``MotionSensor`` for the sensor wired to
``constants.PIR_MOTION_PIN`` and exposes the one thing the rest of the system
needs:

- ``motion_detected()`` — True while the PIR OUT line is HIGH (motion sensed).

The class is a context manager so callers can guarantee the GPIO pin is
released::

    from motion_sensor import PirMotionSensor

    with PirMotionSensor() as sensor:
        while True:
            if sensor.motion_detected():
                # something moved in front of the sensor
                ...
            time.sleep(0.2)

Wiring note: unlike the HC-SR04 ECHO line, the HC-SR501 OUT is a clean 3.3V
push-pull logic signal (HIGH = motion). It needs NO voltage divider or level
shifter — it wires straight to the Pi GPIO input.

Runs on the Raspberry Pi with GPIO access; hardware scripts using it may need
root.
"""

from gpiozero import MotionSensor

from constants import PIR_MOTION_PIN


class PirMotionSensor:
    """HC-SR501 PIR motion sensor on the configured GPIO pin.

    Thin wrapper over gpiozero's ``MotionSensor`` that centralizes pin
    selection (from ``constants``). Owns the underlying gpiozero device; call
    ``close()`` (or use it as a context manager) to release the GPIO pin.
    """

    def __init__(self, pin=PIR_MOTION_PIN):
        """Open the PIR motion sensor and configure its pin.

        Args:
            pin: BCM pin reading the HC-SR501 OUT (input). The OUT line is a
                 clean 3.3V push-pull signal (HIGH = motion), so no voltage
                 divider is needed. Defaults to ``constants.PIR_MOTION_PIN``.
        """
        self.pin = pin
        self._sensor = MotionSensor(pin)

    def motion_detected(self):
        """Return True while the PIR OUT line is HIGH (motion sensed).

        Returns:
            True if the sensor currently reports motion, else False.
        """
        return bool(self._sensor.motion_detected)

    def close(self):
        """Release the sensor's GPIO pin. Safe to call more than once."""
        self._sensor.close()

    def __enter__(self):
        """Enter the context manager, returning this sensor."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager, always releasing the GPIO pin."""
        self.close()
        return False
