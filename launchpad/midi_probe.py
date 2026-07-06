"""
MIDI Probe
==========
Prints every incoming MIDI message from a device, so you can press a pad/
button/knob and read off exactly what note or CC number it sends - useful
for filling in config values (e.g. alive_led) without guessing.

Usage:
    python -m launchpad.midi_probe "Launch Control"
    python -m launchpad.midi_probe "Launchpad"
"""

import sys

from launchpad.midi_utils import open_input


def main():
    if len(sys.argv) != 2:
        print('Usage: python -m launchpad.midi_probe "<device name substring>"')
        sys.exit(1)

    with open_input(sys.argv[1]) as inport:
        print(f"Listening on {inport.name} - press pads/buttons/knobs (Ctrl+C to stop)...")
        for msg in inport:
            print(msg)


if __name__ == "__main__":
    main()
