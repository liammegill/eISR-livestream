"""
Launch Control Race-Control Bridge
====================================
Maps Novation Launch Control button presses to race-phase actions (see
core/race_controller.py) using the note -> action mapping in
config/launch_control.yaml.

LED colour convention:
  green (solid) = phase not yet pressed this run - ready to press next
  amber (solid) = phase already pressed this run
  off           = Clear/New - always unlit, since it's a momentary reset
                  rather than a step in the sequence

Pressing Clear/New resets every phase button back to green for the next
run. LED state reacts to RaceController.trigger_phase() regardless of which
input called it (this device or the control UI), via
RaceController.add_listener(), so both stay in sync no matter where a phase
is pressed.

This is a first, minimal pass: no mute/volume knobs or other AV control yet
- see ../eISR-livestream/launch_control_obs.py for the fuller AV-system
controller (mute/volume via knobs+buttons), which can be folded in here
once the timing button mapping is confirmed on this rig. The "alive"
heartbeat LED remains unconfigured for now (see config/launch_control.yaml's
alive_led section) until we know a lightable button/note not already used
by the buttons below.
"""

import threading
import time

import mido
import yaml

from core.paths import CONFIG_DIR
from launchpad.midi_utils import AliveLed, open_input, open_output

RECONNECT_INTERVAL = 5  # seconds between reconnection attempts


def load_config(path=None):
    """Load and return the YAML configuration file."""
    path = path or CONFIG_DIR / "launch_control.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class LaunchControlController:
    """Listens for Launch Control button presses and forwards them to a RaceController."""

    def __init__(self, race_controller, config=None):
        self.race_controller = race_controller
        self.config = config or load_config()
        self.buttons = self.config.get("buttons", {})
        self.outport = None
        self.pressed = set()  # notes pressed since the last Clear/New
        self.alive_led = AliveLed(
            self.config.get("alive_led"), self.config.get("colors"), lambda: self.outport
        )
        self.race_controller.add_listener(self._on_phase_update)

    def _color_velocity(self, color_name):
        return self.config.get("colors", {}).get(color_name, 0)

    def light_pad(self, note, color_name):
        """Send a note_on message to set a button LED to a solid colour."""
        if not self.outport:
            return
        self.outport.send(mido.Message("note_on", note=note, velocity=self._color_velocity(color_name)))

    def _note_for_action(self, action):
        for note, btn in self.buttons.items():
            if btn.get("action") == action:
                return note
        return None

    def _paint_all(self):
        """
        Repaint every configured button from self.pressed: green if still to
        be pressed this run, amber if already pressed. Clear/New always
        stays unlit - it's a momentary reset, not a step in the sequence.
        """
        for note, btn in self.buttons.items():
            if btn.get("action") == "clear":
                self.light_pad(note, "off")
            else:
                self.light_pad(note, "amber" if note in self.pressed else "green")

    def _on_phase_update(self, _run, action):
        """RaceController listener: repaint LEDs for a phase triggered by any
        input (this device or the control UI), so both stay in sync."""
        if action == "clear":
            self.pressed.clear()
            self._paint_all()
            return
        note = self._note_for_action(action)
        if note is None:
            return
        self.pressed.add(note)
        self.light_pad(note, "amber")

    def _handle_note(self, note):
        btn = self.buttons.get(note)
        if not btn or "action" not in btn:
            return
        print(f"Launch Control: {btn.get('label', note)}")
        self.race_controller.trigger_phase(btn["action"])

    def shutdown(self):
        """Turn off every configured button LED (and the alive LED), best-effort.

        Unlike the Launchpad S, the Launch Control has no single "reset all
        LEDs" command, so each one is switched off individually.
        """
        if not self.outport:
            return
        off = self.config.get("colors", {}).get("off", 0)

        for note in self.buttons:
            try:
                self.outport.send(mido.Message("note_on", note=note, velocity=off))
            except Exception:
                pass

        alive_cfg = self.config.get("alive_led") or {}
        if "number" in alive_cfg:
            try:
                if alive_cfg.get("type") == "note":
                    msg = mido.Message("note_on", note=alive_cfg["number"], velocity=off)
                else:
                    msg = mido.Message("control_change", control=alive_cfg["number"], value=off)
                self.outport.send(msg)
            except Exception:
                pass

    def run_loop(self):
        """
        Main MIDI event loop with automatic reconnection.

        Runs forever: if the Launch Control isn't plugged in, or is
        unplugged mid-session, this just retries every RECONNECT_INTERVAL
        seconds rather than crashing the rest of the app.
        """
        device = self.config["devices"]["launch_control"]
        threading.Thread(target=self.alive_led.run_forever, daemon=True, name="LaunchControlAliveLed").start()

        while True:
            try:
                self.outport = open_output(device)
                self._paint_all()
            except Exception:
                self.outport = None

            try:
                with open_input(device) as inport:
                    print("Launch Control: listening...")
                    for msg in inport:
                        if msg.type == "note_on" and msg.velocity > 0:
                            self._handle_note(msg.note)
                print("Launch Control: disconnected.")
            except Exception as e:
                print(f"Launch Control: connection lost ({e})")

            self.outport = None
            time.sleep(RECONNECT_INTERVAL)
