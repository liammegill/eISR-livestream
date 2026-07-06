from pathlib import Path

import yaml
import obsws_python as obs

from core.paths import CONFIG_DIR


class OBSClient:
    """Thin wrapper around the OBS websocket connection.

    Any module (control UI, launchpad, future overlays) that needs to talk
    to OBS should go through an instance of this class rather than opening
    its own connection.
    """

    def __init__(self, config_path=None):
        config_path = Path(config_path or CONFIG_DIR / "obs.yaml")
        cfg = yaml.safe_load(config_path.read_text())
        self._client = obs.ReqClient(
            host=cfg["host"], port=cfg["port"], password=cfg["password"]
        )

    def emit_event(self, event_name, event_data):
        """Emit a custom browser-source event, picked up by an overlay's
        `window.addEventListener(event_name, ...)`."""
        self._client.call_vendor_request(
            vendor_name="obs-browser",
            request_type="emit_event",
            request_data={"event_name": event_name, "event_data": event_data},
        )

    # --- team_overlay.html convenience methods -----------------------------

    def load_team(self, team_id):
        self.emit_event("eisrTeamOverlay", {"loadTeam": team_id})

    def race_action(self, action):
        self.emit_event("eisrTeamOverlay", {"raceAction": action})

    def update_overlay(self, data):
        self.emit_event("eisrTeamOverlay", {"updateOverlay": data})
