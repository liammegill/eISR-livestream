"""
OBS Input Probe
================
Lists every input (source) currently configured in OBS, with its kind.

Use this to find the exact source name for config/cameras.yaml's `input:`
field. That must be the name of the underlying Media Source itself, not the
name of the scene it's placed into - GetInputSettings (and SetInputSettings,
used by the toggle_camera Launchpad action) only operates on inputs, not
scenes, so a scene name there fails with "The specified source is not an
input".

Usage:
    python -m core.obs_probe
"""

import obsws_python as obs
import yaml

from core.paths import CONFIG_DIR


def main():
    cfg = yaml.safe_load((CONFIG_DIR / "obs.yaml").read_text())
    client = obs.ReqClient(host=cfg["host"], port=cfg["port"], password=cfg["password"])

    for item in client.get_input_list().inputs:
        print(f"{item['inputName']}  (kind: {item['inputKind']})")


if __name__ == "__main__":
    main()
