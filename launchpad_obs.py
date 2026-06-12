"""
Launchpad S OBS Controller
===========================
Bridges a Novation Launchpad S to OBS Studio via WebSocket (v5).

Button layout (note numbers, USB port at top):

  Top row:   0   1   2   3   4   5   6   7  | side:   8
  Row 2:    16  17  18  19  20  21  22  23  | side:  24
  Row 3:    32  33  34  35  36  37  38  39  | side:  40
  ...

Supported actions (configured via config_launchpad.yaml):
  scene                — switch OBS program scene
  toggle_source        — show/hide a scene item (overlay)
  set_transition       — change the active scene transition
  start_stop_recording — start or stop OBS recording

LED colour convention:
  green (solid)  = active / on / live
  amber (solid)  = available but inactive
  red   (solid)  = configured but not found in OBS
  red   (flash)  = currently recording  (hardware 280ms double-buffer blink)
"""

import sys
import mido
import obsws_python as obs
import yaml


def load_config(path="config_launchpad.yaml"):
    """Load and return the YAML configuration file."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class LaunchpadController:
    """
    Manages the Novation Launchpad S MIDI device for OBS scene control.

    Call connect() from the main thread before handing run_loop() to a
    daemon thread.  This keeps MIDI port ownership in the main thread so
    _reset_leds() can safely clear the hardware on Ctrl+C without racing
    the event loop.
    """

    def __init__(self, config):
        """Initialise the controller from the parsed YAML config dict."""
        self.config = config
        self.obs_client = None
        self.outport = None
        self.toggle_states = {}       # note -> bool
        self.source_item_ids = {}     # (scene_name, source_name) -> OBS item ID
        self._not_found_notes = set() # notes whose OBS counterpart doesn't exist
        self._current_transition = None

    # ── Startup ──────────────────────────────────────────────────────────────

    def connect(self):
        """
        Open the OBS WebSocket connection and the Launchpad MIDI output port,
        then reset LEDs and sync all state from OBS.

        Must be called from the main thread.
        """
        cfg = self.config["obs"]
        self.obs_client = obs.ReqClient(
            host=cfg["host"], port=cfg["port"], password=cfg["password"]
        )
        self.outport = mido.open_output(self.config["devices"]["launchpad"])
        self._reset_leds()
        self._resolve_source_ids()
        self._validate_buttons()
        self._sync_toggle_states()
        self._sync_transition()

    def _reset_leds(self):
        """
        Turn off all Launchpad LEDs and revert to power-on state.
        Sends CC 0 = 0, which is the Launchpad S reset command. 
        """
        self.outport.send(mido.Message("control_change", control=0, value=0))

    def _resolve_source_ids(self):
        """
        Pre-fetch the OBS scene item ID for every toggle_source button.

        OBS requires an integer scene item ID (not the source name string) for
        get/set_scene_item_enabled.  IDs are resolved once at startup and
        cached in self.source_item_ids.  Failures are silently deferred to
        _validate_buttons() which will mark the button as not-found.
        """
        for _, btn in self.config.get("launchpad_buttons", {}).items():
            if btn.get("action") != "toggle_source":
                continue
            scene, source = btn["scene"], btn["source"]
            key = (scene, source)
            if key in self.source_item_ids:
                continue
            try:
                resp = self.obs_client.get_scene_item_id(
                    scene_name=scene, source_name=source
                )
                self.source_item_ids[key] = resp.scene_item_id
            except Exception:
                pass  # reported in _validate_buttons

    def _validate_buttons(self):
        """
        Check each configured button against live OBS state and mark any that
        refer to a missing scene, source, or transition.

        Marked notes are added to self._not_found_notes; update_all_lights()
        lights these solid red and handle_button() silently ignores them.
        """
        try:
            scene_names = {s["sceneName"] for s in self.obs_client.get_scene_list().scenes}
        except Exception:
            scene_names = None

        try:
            transition_names = {
                t["transitionName"]
                for t in self.obs_client.get_scene_transition_list().transitions
            }
        except Exception:
            transition_names = None

        for note, btn in self.config.get("launchpad_buttons", {}).items():
            action = btn.get("action")

            if action == "scene":
                if scene_names is not None and btn.get("target") not in scene_names:
                    self._not_found_notes.add(note)
                    print(f"Warning: scene '{btn['target']}' not found in OBS")

            elif action == "toggle_source":
                scene, source = btn["scene"], btn["source"]
                if (scene, source) not in self.source_item_ids:
                    self._not_found_notes.add(note)
                    print(f"Warning: source '{source}' not found in scene '{scene}'")

            elif action == "set_transition":
                if transition_names is not None and btn.get("transition") not in transition_names:
                    self._not_found_notes.add(note)
                    print(f"Warning: transition '{btn['transition']}' not found in OBS")

            elif action == "toggle_mute":
                try:
                    self.obs_client.get_input_mute(name=btn["input"])
                except Exception:
                    self._not_found_notes.add(note)
                    print(f"Warning: audio input '{btn['input']}' not found in OBS")

    def _sync_toggle_states(self):
        """
        Read current OBS state for all stateful buttons so LEDs reflect
        reality at startup rather than defaulting to off.

        Covers toggle_source (scene item visibility), toggle_mute (input mute
        state), and start_stop_recording (record status).
        """
        for note, btn in self.config.get("launchpad_buttons", {}).items():
            if note in self._not_found_notes:
                continue
            action = btn.get("action")
            if action == "toggle_source":
                scene, source = btn["scene"], btn["source"]
                item_id = self.source_item_ids.get((scene, source))
                if item_id is not None:
                    try:
                        resp = self.obs_client.get_scene_item_enabled(
                            scene_name=scene, item_id=item_id
                        )
                        self.toggle_states[note] = resp.scene_item_enabled
                    except Exception:
                        self.toggle_states[note] = False
            elif action == "toggle_mute":
                try:
                    resp = self.obs_client.get_input_mute(name=btn["input"])
                    self.toggle_states[note] = resp.input_muted
                except Exception:
                    self.toggle_states[note] = False
            elif action == "start_stop_recording":
                try:
                    resp = self.obs_client.get_record_status()
                    self.toggle_states[note] = resp.output_active
                except Exception:
                    self.toggle_states[note] = False

    def _sync_transition(self):
        """Read the currently active OBS transition into self._current_transition."""
        try:
            resp = self.obs_client.get_current_scene_transition()
            self._current_transition = resp.transition_name
        except Exception:
            self._current_transition = None

    # ── LED helpers ──────────────────────────────────────────────────────────

    def _color_velocity(self, color_name):
        """Return the MIDI velocity that produces the named LED colour."""
        return self.config.get("colors", {}).get(color_name, 0)

    def light_pad(self, note, color_name):
        """Send a note_on message to set a pad LED to a solid colour."""
        self.outport.send(
            mido.Message("note_on", note=note, velocity=self._color_velocity(color_name))
        )

    def _light_pad_flash(self, note, color_name):
        """
        Set a pad LED in Launchpad S "clear mode" so it flashes via hardware.

        The Launchpad S double-buffer system alternates between two frame
        buffers every 280ms.  Copy-mode LEDs (velocity bits 3:2 = 11) are
        solid; clear-mode LEDs (bits 3:2 = 10, i.e. velocity - 4) are written
        to buffer 0 only, so they appear to blink as the hardware swaps.

        Flash mode must be active (CC 0 = 40) for this to take effect; see
        update_all_lights().
        """
        velocity = self._color_velocity(color_name) - 4
        self.outport.send(mido.Message("note_on", note=note, velocity=velocity))

    def update_all_lights(self):
        """
        Recompute all LED states from current OBS state and send MIDI.

        Queries the active program scene once per call so all scene buttons
        update atomically.  Enables hardware flash mode (CC 0 = 40) only when
        at least one button needs to blink (i.e. recording is active); otherwise
        uses simple mode (CC 0 = 32) to avoid unintended flicker.
        """
        try:
            current_scene = (
                self.obs_client.get_current_program_scene().current_program_scene_name
            )
        except Exception:
            current_scene = None

        display = {}
        for note, btn in self.config.get("launchpad_buttons", {}).items():
            if note in self._not_found_notes:
                display[note] = ("red", False)
                continue

            action = btn.get("action")

            if action == "scene":
                active = btn.get("target") == current_scene
                display[note] = ("green", False) if active else ("amber", False)

            elif action == "toggle_source":
                on = self.toggle_states.get(note, False)
                display[note] = ("green", False) if on else ("amber", False)

            elif action == "toggle_mute":
                muted = self.toggle_states.get(note, False)
                display[note] = ("amber", False) if muted else ("green", False)

            elif action == "set_transition":
                active = btn.get("transition") == self._current_transition
                display[note] = ("green", False) if active else ("amber", False)

            elif action == "start_stop_recording":
                recording = self.toggle_states.get(note, False)
                display[note] = ("red", True) if recording else ("amber", False)

        needs_flash = any(flash for _, flash in display.values())
        self.outport.send(mido.Message("control_change", control=0, value=40 if needs_flash else 32))

        for note, (color, should_flash) in display.items():
            if should_flash:
                self._light_pad_flash(note, color)
            else:
                self.light_pad(note, color)

    # ── Event handler ─────────────────────────────────────────────────────────

    def handle_button(self, note):
        """
        Dispatch a pad press to the appropriate OBS action.

        Silently ignores presses on notes not in the config or marked
        not-found.  After any successful state change, update_all_lights()
        is called so all LEDs immediately reflect the new OBS state.
        """
        btn = self.config.get("launchpad_buttons", {}).get(note)
        if not btn:
            return
        action = btn.get("action")
        label = btn.get("label", f"pad {note}")

        if note in self._not_found_notes:
            print(f"Button not available in OBS: {label}")
            return

        if action == "scene":
            try:
                self.obs_client.set_current_program_scene(btn["target"])
                print(f"Scene -> {btn['target']}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error switching scene: {e}")

        elif action == "toggle_source":
            scene, source = btn["scene"], btn["source"]
            item_id = self.source_item_ids.get((scene, source))
            if item_id is None:
                print(f"Unknown source: {label}")
                return
            new_state = not self.toggle_states.get(note, False)
            try:
                self.obs_client.set_scene_item_enabled(
                    scene_name=scene, item_id=item_id, enabled=new_state
                )
                self.toggle_states[note] = new_state
                print(f"Overlay {'on' if new_state else 'off'}: {label}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling overlay: {e}")

        elif action == "toggle_mute":
            new_state = not self.toggle_states.get(note, False)
            try:
                self.obs_client.set_input_mute(name=btn["input"], muted=new_state)
                self.toggle_states[note] = new_state
                print(f"{'Muted' if new_state else 'Unmuted'}: {label}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling mute: {e}")

        elif action == "set_transition":
            try:
                self.obs_client.set_current_scene_transition(btn["transition"])
                self._current_transition = btn["transition"]
                print(f"Transition -> {btn['transition']}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error setting transition: {e}")

        elif action == "start_stop_recording":
            recording = self.toggle_states.get(note, False)
            try:
                if recording:
                    self.obs_client.stop_record()
                    self.toggle_states[note] = False
                    print("Recording stopped")
                else:
                    self.obs_client.start_record()
                    self.toggle_states[note] = True
                    print("Recording started")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling recording: {e}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run_loop(self):
        """
        Main MIDI event loop.  Blocks until the input port is closed.

        Only note_on messages with velocity > 0 are acted on; note_off and
        zero-velocity note_on (button release) are ignored.
        """
        self.update_all_lights()
        device = self.config["devices"]["launchpad"]
        with mido.open_input(device) as inport:
            print("Launchpad: Listening...")
            for msg in inport:
                if msg.type == "note_on" and msg.velocity > 0:
                    self.handle_button(msg.note)

    def run(self):
        """
        Standalone entry point (used when running this file directly).

        Connects, runs the event loop, and resets all LEDs on exit.
        For multi-device use, call connect() and run_loop() separately —
        see main.py.
        """
        self.connect()
        try:
            self.run_loop()
        finally:
            self._reset_leds()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(config_path)
    controller = LaunchpadController(config)
    controller.run()
