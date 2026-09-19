"""Print the current angle of every servo, with names and a paste-ready pose.

Reads the last-commanded angle of each configured servo channel from the shared
ServoKit and prints it with its human-readable name, plus a single-line JSON
pose object you can hand straight back for authoring a gesture.

NOTE: servos report the last angle COMMANDED over I2C, not a measured position
(hobby servos have no feedback). So after you physically pose the arm by hand,
this shows the last driven angle, which may differ from where you moved it. It
is most useful right after a gesture/calibration drives the joints, to capture
exactly where the software left them.

Usage (run as root for I2C):
    sudo PYTHONPATH=src /usr/bin/python3 -m positions
    # or, in the venv:
    sudo PYTHONPATH=src .venv/bin/python -m positions
"""

import json

import constants
from trunkcontroller import TrunkController


def read_positions():
    """Returns the current commanded angle of each configured servo channel.

    Returns:
        A dict mapping servo channel -> angle in degrees (int), or None for any
        channel that has not been commanded since power-on.
    """
    tc = TrunkController("positions")
    positions = {}
    for channel in sorted(constants.servos):
        angle = tc.kit.servo[channel].angle
        positions[channel] = int(round(angle)) if angle is not None else None
    return positions


def main():
    """Print each servo's current angle with its name, plus a JSON pose line."""
    positions = read_positions()

    print("\nCurrent servo positions:")
    print("  ch  name                    angle   SAFE_LIMITS")
    print("  --  ----                    -----   -----------")
    for channel in sorted(positions):
        name = constants.servos.get(channel, f"ch{channel}")
        angle = positions[channel]
        limits = constants.SAFE_LIMITS.get(channel, "-")
        shown = "None" if angle is None else f"{angle}"
        print(f"  {channel:>2}  {name:<22}  {shown:>5}   {limits}")

    # Paste-ready pose: channels as string keys (matches the CLI --pose format),
    # skipping any channel that has not been commanded yet.
    pose = {str(ch): a for ch, a in positions.items() if a is not None}
    print("\nPaste-ready pose (channel -> degrees):")
    print("  " + json.dumps(pose))
    print()


if __name__ == "__main__":
    main()
