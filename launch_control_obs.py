"""
Launch Control OBS Controller
==============================
Bridges a Novation Launch Control to OBS Studio via WebSocket (v5).

Each column on the Launch Control maps to one OBS audio input:
  - Top knob (CC 21-28): sets volume in dB over a configured range
  - Corresponding button (notes 9-12 / 25-28): toggles mute

LED colour convention: red = muted, green = live.

All configured inputs are muted on startup so the LED state always matches
reality from the first moment.

Configuration is loaded from config_launch_control.yaml.
"""

import sys
import time

import mido
import obsws_python as obs
import yaml

from midi_utils import open_input, open_output

RECONNECT_INTERVAL = 5  # seconds between reconnection attempts


def load_config(path="config_launch_control.yaml"):
    """Load and return the YAML configuration file."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class LaunchControlController:
    """
    Manages the Novation Launch Control MIDI device for OBS audio control.

    Call connect() from the main thread before handing run_loop() to a
    daemon thread.  This keeps MIDI port ownership in the main thread so
    _cleanup() can safely send LED-off messages on Ctrl+C without racing
    the event loop.
    """

    def __init__(self, config):
        """Initialise the controller from the parsed YAML config dict."""
        self.config = config
        self.obs_client = None
        self.outport = None
        self.mute_states = {}         # note -> bool (True = muted)
        self._not_found_notes = set() # button notes whose OBS input is missing
        self._not_found_knobs = set() # knob CCs whose OBS input is missing

    # ── Startup ──────────────────────────────────────────────────────────────

    def connect(self):
        """
        Open the OBS WebSocket connection and the Launch Control MIDI output
        port, then validate config and sync initial state.

        Must be called from the main thread.
        """
        cfg = self.config["obs"]
        self.obs_client = obs.ReqClient(
            host=cfg["host"], port=cfg["port"], password=cfg["password"]
        )
        self.outport = open_output(self.config["devices"]["launch_control"])
        self._validate_and_sync()

    def _list_obs_inputs(self):
        """
        Print all available OBS audio input names to stdout.

        Called automatically when a configured input name isn't found, so the
        user can identify the correct name to put in the config file.
        """
        try:
            inputs = self.obs_client.get_input_list().inputs
            names = [i.get("inputName", i) if isinstance(i, dict) else str(i) for i in inputs]
            print(f"  Available OBS inputs: {names}")
        except Exception:
            print("  (could not retrieve input list)")

    def _validate_and_sync(self):
        """
        Verify that every configured OBS input exists, mute each one, and
        mark any missing inputs so their buttons and knobs are silently ignored
        during the event loop.

        Inputs are force-muted so the LED state (red on boot) always reflects
        the true OBS state from the first frame.
        """
        warned = False
        for note, btn in self.config.get("buttons", {}).items():
            try:
                self.obs_client.get_input_mute(name=btn["input"])
                self.obs_client.set_input_mute(name=btn["input"], muted=True)
                self.mute_states[note] = True
            except Exception:
                self._not_found_notes.add(note)
                print(f"Warning: audio input '{btn['input']}' not found in OBS")
                warned = True

        for cc, knob in self.config.get("knobs", {}).items():
            try:
                self.obs_client.get_input_volume(name=knob["input"])
            except Exception:
                self._not_found_knobs.add(cc)
                print(f"Warning: audio input '{knob['input']}' not found in OBS")
                warned = True

        if warned:
            self._list_obs_inputs()

    # ── LED helpers ──────────────────────────────────────────────────────────

    def _color_velocity(self, color_name):
        """Return the MIDI velocity that produces the named LED colour."""
        return self.config.get("colors", {}).get(color_name, 0)

    def light_button(self, note, color_name):
        """Send a note_on message to set a button LED to the given colour."""
        self.outport.send(
            mido.Message("note_on", note=note, velocity=self._color_velocity(color_name))
        )

    def update_all_lights(self):
        """
        Redraw all configured button LEDs based on current mute state.

        Buttons whose OBS input was not found are turned off rather than lit
        in a potentially misleading colour.
        """
        for note in self.config.get("buttons", {}):
            if note in self._not_found_notes:
                self.light_button(note, "off")
                continue
            muted = self.mute_states.get(note, False)
            self.light_button(note, "red" if muted else "green")

    # ── Event handlers ───────────────────────────────────────────────────────

    def handle_knob(self, control, value):
        """
        Handle a CC message from a volume knob.

        Maps the raw 0-100 knob value linearly onto the [min_db, max_db] range
        configured for that knob, then sends SetInputVolume to OBS.
        """
        knob = self.config.get("knobs", {}).get(control)
        if not knob or control in self._not_found_knobs:
            return

        if knob.get("action") == "set_volume":
            min_db = float(knob.get("min_db", -60.0))
            max_db = float(knob.get("max_db", 0.0))
            db = min_db + (value / 100.0) * (max_db - min_db)
            try:
                self.obs_client.set_input_volume(
                    name=knob["input"], vol_db=db
                )
            except Exception as e:
                print(f"Error setting volume '{knob['label']}': {e}")

    def handle_button(self, note):
        """
        Handle a note_on message from a mute button.

        Toggles the OBS mute state for the associated input and updates the
        button LED to reflect the new state.
        """
        btn = self.config.get("buttons", {}).get(note)
        if not btn or note in self._not_found_notes:
            return

        if btn.get("action") == "toggle_mute":
            new_state = not self.mute_states.get(note, False)
            try:
                self.obs_client.set_input_mute(
                    name=btn["input"], muted=new_state
                )
                self.mute_states[note] = new_state
                print(f"{'Muted' if new_state else 'Unmuted'}: {btn.get('label', btn['input'])}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling mute: {e}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _cleanup(self):
        """
        Turn off all button LEDs.

        Called from the main thread on shutdown (Ctrl+C) so the hardware is
        left in a clean state.
        """
        for note in self.config.get("buttons", {}).keys():
            self.light_button(note, "off")

    def _reconnect(self):
        """
        Re-open MIDI ports and resync all state after a disconnection.

        Clears previously cached not-found sets so that a full re-validation
        runs against the current OBS state on reconnect.
        """
        self._not_found_notes = set()
        self._not_found_knobs = set()
        self.outport = open_output(self.config["devices"]["launch_control"])
        self._validate_and_sync()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def run_loop(self):
        """
        Main MIDI event loop with automatic reconnection.

        Opens the input port and processes messages in an inner loop.  If the
        port raises an exception (e.g. USB unplug) or closes cleanly, the
        outer loop waits RECONNECT_INTERVAL seconds and attempts to reopen
        both ports and resync state before listening again.  This repeats
        indefinitely until the thread is stopped (e.g. process exit).
        """
        device = self.config["devices"]["launch_control"]
        while True:
            try:
                self.update_all_lights()
                with open_input(device) as inport:
                    print("Launch Control: Listening...")
                    for msg in inport:
                        if msg.type == "control_change":
                            self.handle_knob(msg.control, msg.value)
                        elif msg.type == "note_on" and msg.velocity > 0:
                            self.handle_button(msg.note)
                # Loop ended without exception — device closed cleanly.
                print("Launch Control: disconnected.")
            except Exception as e:
                print(f"Launch Control: connection lost ({e})")

            print(f"Launch Control: reconnecting in {RECONNECT_INTERVAL}s...")
            time.sleep(RECONNECT_INTERVAL)
            try:
                self._reconnect()
                print("Launch Control: reconnected.")
            except Exception as e:
                print(f"Launch Control: reconnect failed ({e}), will retry...")

    def run(self):
        """
        Standalone entry point (used when running this file directly).

        Connects, runs the event loop, and ensures LED cleanup on exit.
        For multi-device use, call connect() and run_loop() separately —
        see main.py.
        """
        self.connect()
        try:
            self.run_loop()
        finally:
            self._cleanup()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config_launch_control.yaml"
    config = load_config(config_path)
    controller = LaunchControlController(config)
    controller.run()
