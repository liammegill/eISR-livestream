"""
Launchpad S OBS Controller
===========================
Bridges a Novation Launchpad S to OBS Studio via WebSocket (v5).

Button layout (note numbers, USB port at top):

  Top row:   0   1   2   3   4   5   6   7  | side:   8
  Row 2:    16  17  18  19  20  21  22  23  | side:  24
  Row 3:    32  33  34  35  36  37  38  39  | side:  40
  ...

This controller drives OBS in Studio Mode: scene buttons load a scene into
the PREVIEW (staging the next shot) and the trigger_transition button pushes
that preview to PROGRAM (live).  Studio Mode is enabled automatically on
connect so the preview workflow always works.

Supported actions (configured via config_launchpad.yaml):
  scene                — load a scene into the OBS preview (Studio Mode)
  trigger_transition   — push the previewed scene to program (go live)
  toggle_source        — show/hide a scene item (overlay)
  start_stop_streaming — start or stop the OBS stream
  start_stop_recording — start or stop OBS recording

LED colour convention:
  green (solid)  = available / pressable but not live (scene you can stage,
                   overlay that's off, idle record/stream, the Go button)
  red   (solid)  = live in program (on-air scene, active overlay, recording,
                   streaming)
  amber (solid)  = the scene staged in preview (next up, not yet live)
  yellow (solid) = configured but not found in OBS (error)
"""

import sys
import time
import mido
import obsws_python as obs
import yaml

from midi_utils import open_input, open_output

RECONNECT_INTERVAL = 5  # seconds between reconnection attempts


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
        self.outport = open_output(self.config["devices"]["launchpad"])
        self._ensure_studio_mode()
        self._reset_leds()
        self._resolve_source_ids()
        self._validate_buttons()
        self._sync_toggle_states()

    def _ensure_studio_mode(self):
        """
        Enable OBS Studio Mode so the preview workflow works.

        Scene buttons stage into the preview via set_current_preview_scene(),
        which requires Studio Mode to be active.  Enabling an already-enabled
        Studio Mode is a harmless no-op.
        """
        try:
            self.obs_client.set_studio_mode_enabled(True)
        except Exception as e:
            print(f"Warning: could not enable Studio Mode ({e})")

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
        refer to a missing scene or source.

        Marked notes are added to self._not_found_notes; update_all_lights()
        lights these solid yellow and handle_button() silently ignores them.
        """
        try:
            scene_names = {s["sceneName"] for s in self.obs_client.get_scene_list().scenes}
        except Exception:
            scene_names = None

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
            elif action == "start_stop_streaming":
                try:
                    resp = self.obs_client.get_stream_status()
                    self.toggle_states[note] = resp.output_active
                except Exception:
                    self.toggle_states[note] = False

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

        Colour convention (see module docstring):
          red    = live in program (on-air scene, active overlay, recording,
                   streaming, selected transition)
          amber  = the scene staged in preview (next up)
          green  = available / pressable but not live
          yellow = configured but not found in OBS

        Both the program and preview scene names are queried once per call so
        all scene buttons update atomically.  Program takes priority over
        preview: a scene that is both live and previewed shows red.

        Hardware flash mode (CC 0 = 40) is enabled only if a button needs to
        blink; nothing currently flashes, so simple mode (CC 0 = 32) is used.
        """
        try:
            program_scene = (
                self.obs_client.get_current_program_scene().current_program_scene_name
            )
        except Exception:
            program_scene = None

        try:
            preview_scene = (
                self.obs_client.get_current_preview_scene().current_preview_scene_name
            )
        except Exception:
            preview_scene = None

        display = {}
        for note, btn in self.config.get("launchpad_buttons", {}).items():
            if note in self._not_found_notes:
                display[note] = ("yellow", False)
                continue

            action = btn.get("action")

            if action == "scene":
                target = btn.get("target")
                if target == program_scene:
                    display[note] = ("red", False)      # live on air
                elif target == preview_scene:
                    display[note] = ("amber", False)    # staged next
                else:
                    display[note] = ("green", False)    # available to stage

            elif action == "trigger_transition":
                display[note] = ("green", False)         # always ready to press

            elif action == "toggle_source":
                on = self.toggle_states.get(note, False)
                display[note] = ("red", False) if on else ("green", False)

            elif action == "toggle_mute":
                muted = self.toggle_states.get(note, False)
                # Live (unmuted) audio is on air -> red; muted is available.
                display[note] = ("green", False) if muted else ("red", False)

            elif action == "start_stop_recording":
                recording = self.toggle_states.get(note, False)
                display[note] = ("red", False) if recording else ("green", False)

            elif action == "start_stop_streaming":
                streaming = self.toggle_states.get(note, False)
                display[note] = ("red", False) if streaming else ("green", False)

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
                self.obs_client.set_current_preview_scene(btn["target"])
                print(f"Preview -> {btn['target']}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error setting preview scene: {e}")

        elif action == "trigger_transition":
            try:
                # OBS completes the program<->preview swap only when the
                # transition animation finishes, so read the transition's
                # duration up front and wait for it before refreshing the
                # LEDs; an immediate refresh would show the pre-swap state.
                try:
                    duration_ms = (
                        self.obs_client.get_current_scene_transition().transition_duration
                        or 0
                    )
                except Exception:
                    duration_ms = 0
                self.obs_client.trigger_studio_mode_transition()
                print("Transition -> program (live)")
                time.sleep(duration_ms / 1000.0 + 0.1)
                self.update_all_lights()
            except Exception as e:
                print(f"Error triggering transition: {e}")

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

        elif action == "start_stop_streaming":
            streaming = self.toggle_states.get(note, False)
            try:
                if streaming:
                    self.obs_client.stop_stream()
                    self.toggle_states[note] = False
                    print("Streaming stopped")
                else:
                    self.obs_client.start_stream()
                    self.toggle_states[note] = True
                    print("Streaming started")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling streaming: {e}")

    def _reconnect(self):
        """
        Re-open MIDI ports and resync all state after a disconnection.

        Clears previously cached state (source IDs, not-found notes) so that
        a full re-validation runs against the current OBS state.  The OBS
        WebSocket connection is not re-established here — if OBS itself has
        gone away that will surface as errors in update_all_lights() or
        handle_button() and can be handled separately.
        """
        self.source_item_ids = {}
        self._not_found_notes = set()
        self.outport = open_output(self.config["devices"]["launchpad"])
        self._ensure_studio_mode()
        self._reset_leds()
        self._resolve_source_ids()
        self._validate_buttons()
        self._sync_toggle_states()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run_loop(self):
        """
        Main MIDI event loop with automatic reconnection.

        Opens the input port and processes messages in an inner loop.  If the
        port raises an exception (e.g. USB unplug) or closes cleanly, the
        outer loop waits RECONNECT_INTERVAL seconds and attempts to reopen
        both ports and resync state before listening again.  This repeats
        indefinitely until the thread is stopped (e.g. process exit).
        """
        device = self.config["devices"]["launchpad"]
        while True:
            try:
                self.update_all_lights()
                with open_input(device) as inport:
                    print("Launchpad: Listening...")
                    for msg in inport:
                        if msg.type == "note_on" and msg.velocity > 0:
                            self.handle_button(msg.note)
                # Loop ended without exception — device closed cleanly.
                print("Launchpad: disconnected.")
            except Exception as e:
                print(f"Launchpad: connection lost ({e})")

            print(f"Launchpad: reconnecting in {RECONNECT_INTERVAL}s...")
            time.sleep(RECONNECT_INTERVAL)
            try:
                self._reconnect()
                print("Launchpad: reconnected.")
            except Exception as e:
                print(f"Launchpad: reconnect failed ({e}), will retry...")

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
