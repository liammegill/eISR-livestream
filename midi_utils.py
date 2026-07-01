"""
MIDI Port Matching Helpers
==========================
Device names reported by mido/rtmidi vary by OS and even by connection
order — e.g. a Novation Launchpad S shows up as "Launchpad S" on macOS,
but as "Launchpad S 0" (input) / "Launchpad S 1" (output) on Windows.

Rather than requiring an exact match in config files, these helpers let
config values be a substring (e.g. "Launchpad") that's matched
case-insensitively against whatever ports are actually present. Input and
output ports are searched separately, so a single substring like
"Launchpad" cleanly resolves to the one matching input port and the one
matching output port even though their full names differ.
"""

import mido


def resolve_port_name(substring, names):
    """
    Return the single port name in `names` that contains `substring`
    (case-insensitive).

    Raises IOError with the list of available ports if there isn't
    exactly one match, so the user can immediately see what's actually
    connected and fix their config.
    """
    matches = [n for n in names if substring.lower() in n.lower()]

    if len(matches) == 1:
        return matches[0]

    if len(matches) == 0:
        raise IOError(
            f"No MIDI port matching '{substring}' was found.\n"
            f"Available ports: {names}"
        )

    raise IOError(
        f"'{substring}' matches multiple MIDI ports: {matches}\n"
        f"Use a more specific name in your config to pick one."
    )


def open_output(substring):
    """Open the output port whose name contains `substring`."""
    return mido.open_output(resolve_port_name(substring, mido.get_output_names()))


def open_input(substring):
    """Open the input port whose name contains `substring`."""
    return mido.open_input(resolve_port_name(substring, mido.get_input_names()))
