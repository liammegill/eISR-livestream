"""
Launchpad AV Controller
========================
Bridges a Novation Launchpad S to OBS Studio via WebSocket (v5), driving
camera switching, overlay visibility, streaming, recording and transitions.
Configured via config/launchpad.yaml.

This controller drives OBS in Studio Mode: scene buttons load a scene into
the PREVIEW (staging the next shot) and the trigger_transition button pushes
that preview to PROGRAM (live). Studio Mode is enabled automatically on
connect so the preview workflow always works.

Supported actions:
  scene                - load a scene into the OBS preview (Studio Mode)
  trigger_transition   - push the previewed scene to program (go live)
  toggle_source        - show/hide a scene item (overlay). All overlays live
                         in "Overlay Scene", nested into every camera scene,
                         so toggling it here shows/hides it everywhere at once.
                         Turning one on hides every other toggle_source overlay,
                         since only one is meant to be shown at a time.
  clear_overlays       - hide every toggle_source overlay. Always unlit -
                         it's a momentary action, not a toggle with its own state.
  toggle_camera        - remove/restore the RTSP URL (config/cameras.yaml) on
                         a camera's OBS Media Source, fully stopping/restarting
                         that incoming stream rather than just hiding it.
  start_stop_streaming - start or stop the OBS stream
  start_stop_recording - start or stop OBS recording

LED colour convention:
  green (solid)  = available / pressable but not live (scene you can stage,
                   overlay that's off, idle record/stream, camera stream off)
  red   (solid)  = live in program (on-air scene, active overlay, recording,
                   streaming, camera stream on)
  amber (solid)  = the scene staged in preview (next up, not yet live)
  yellow (solid) = configured but not found in OBS (error)
  yellow (blink) = a scene's linked camera was just switched back on and is
                   still starting up (see RECONNECT_BLINK_SECONDS)
  off            = a scene's linked camera is currently switched off

Race-phase timing has moved to the Launch Control (see
launchpad/launch_control_obs.py) - this controller no longer touches
RaceController.
"""

import threading
import time

import mido
import obsws_python as obs
import yaml

from core.paths import CONFIG_DIR
from launchpad.midi_utils import AliveLed, open_input, open_output

RECONNECT_INTERVAL = 5      # seconds between MIDI/OBS reconnection attempts
RECONNECT_BLINK_SECONDS = 5  # how long a just-reconnected camera's pad blinks yellow


def load_config(path=None):
    """Load and return the YAML configuration file."""
    path = path or CONFIG_DIR / "launchpad.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_obs_config(path=None):
    path = path or CONFIG_DIR / "obs.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_cameras_config(path=None):
    path = path or CONFIG_DIR / "cameras.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class LaunchpadController:
    """Manages the Novation Launchpad S MIDI device for OBS AV control.

    Opens its own OBS websocket connection (separate from RaceController's),
    since its concerns - scenes, streaming, recording - are unrelated to
    race timing.
    """

    def __init__(self, config=None, obs_config=None, cameras_config=None):
        self.config = config or load_config()
        self.obs_config = obs_config or _load_obs_config()
        self.cameras = (cameras_config or _load_cameras_config()).get("cameras", {})
        self.obs_client = None
        self.outport = None
        self.toggle_states = {}       # note -> bool
        self.source_item_ids = {}     # (scene_name, source_name) -> OBS item ID
        self.camera_enabled = {}      # camera key (config/cameras.yaml) -> bool
        self.camera_reconnect_until = {}  # camera key -> monotonic() deadline for the yellow blink
        self._not_found_notes = set()  # notes whose OBS counterpart doesn't exist
        self.alive_led = AliveLed(
            self.config.get("alive_led"), self.config.get("colors"), lambda: self.outport
        )

    # ---- Startup --------------------------------------------------------

    def _connect_obs(self):
        cfg = self.obs_config
        self.obs_client = obs.ReqClient(host=cfg["host"], port=cfg["port"], password=cfg["password"])

    def _ensure_studio_mode(self):
        try:
            self.obs_client.set_studio_mode_enabled(True)
        except Exception as e:
            print(f"Warning: could not enable Studio Mode ({e})")

    def _reset_leds(self):
        """Turn off all Launchpad LEDs (CC 0 = 0 is the Launchpad S reset)."""
        self.outport.send(mido.Message("control_change", control=0, value=0))

    def _resolve_source_ids(self):
        """
        Pre-fetch the OBS scene item ID of every toggle_source button, for
        every scene it appears in.

        Failures are silently deferred to _validate_buttons(), which marks
        the button as not-found.
        """
        for _, btn in self.config.get("launchpad_buttons", {}).items():
            if btn.get("action") != "toggle_source":
                continue
            scene, source = btn["scene"], btn["source"]
            key = (scene, source)
            if key in self.source_item_ids:
                continue
            try:
                resp = self.obs_client.get_scene_item_id(scene_name=scene, source_name=source)
                self.source_item_ids[key] = resp.scene_item_id
            except Exception:
                pass  # reported in _validate_buttons

    def _validate_buttons(self):
        """
        Check each configured button against live OBS state and mark any
        that refer to a missing scene or source.

        Marked notes are lit yellow by update_all_lights() and ignored by
        _handle_note().
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

            elif action == "toggle_camera":
                if btn.get("camera") not in self.cameras:
                    self._not_found_notes.add(note)
                    print(f"Warning: camera '{btn.get('camera')}' not found in cameras.yaml")

    def _sync_camera_states(self):
        """
        Read each configured camera's current OBS input settings so pads
        reflect reality (on/off) at startup rather than defaulting to on.

        A camera counts as "on" if its Media Source's "input" (URL) field is
        currently non-empty. Resets any in-progress reconnect blink, since a
        fresh sync means we don't actually know how long it's been running.
        """
        self.camera_reconnect_until = {}
        for camera_key, camera_cfg in self.cameras.items():
            try:
                resp = self.obs_client.get_input_settings(name=camera_cfg["input"])
                self.camera_enabled[camera_key] = bool(resp.input_settings.get("input"))
            except Exception:
                self.camera_enabled[camera_key] = False

    def _sync_toggle_states(self):
        """
        Read current OBS state for all stateful buttons so LEDs reflect
        reality at startup rather than defaulting to off.
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
                        resp = self.obs_client.get_scene_item_enabled(scene_name=scene, item_id=item_id)
                        self.toggle_states[note] = resp.scene_item_enabled
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

    # ---- LED helpers ------------------------------------------------------

    def _color_velocity(self, color_name):
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
        buffers every 280ms. Copy-mode LEDs (velocity bits 3:2 = 11) are
        solid; clear-mode LEDs (bits 3:2 = 10, i.e. velocity - 4) are written
        to buffer 0 only, so they appear to blink as the hardware swaps.
        Flash mode must be active (CC 0 = 40) for this to take effect; see
        update_all_lights().
        """
        self.outport.send(
            mido.Message("note_on", note=note, velocity=self._color_velocity(color_name) - 4)
        )

    def update_all_lights(self):
        """
        Recompute all LED states from current OBS state and send MIDI.

        Both the program and preview scene names are queried once per call
        so all scene buttons update atomically. Program takes priority over
        preview: a scene that is both live and previewed shows red.

        Hardware flash mode (CC 0 = 40) is enabled only if a pad needs to
        blink (a camera mid-reconnect); simple mode (CC 0 = 32) otherwise.
        """
        try:
            program_scene = self.obs_client.get_current_program_scene().current_program_scene_name
        except Exception:
            program_scene = None

        try:
            preview_scene = self.obs_client.get_current_preview_scene().current_preview_scene_name
        except Exception:
            preview_scene = None

        now = time.monotonic()
        display = {}
        for note, btn in self.config.get("launchpad_buttons", {}).items():
            if note in self._not_found_notes:
                display[note] = ("yellow", False)
                continue

            action = btn.get("action")
            camera_key = btn.get("camera")

            if action == "scene":
                if camera_key and not self.camera_enabled.get(camera_key, True):
                    display[note] = ("off", False)              # stream switched off
                elif camera_key and now < self.camera_reconnect_until.get(camera_key, 0):
                    display[note] = ("yellow", True)             # just switched back on
                else:
                    target = btn.get("target")
                    if target == program_scene:
                        display[note] = ("red", False)           # live on air
                    elif target == preview_scene:
                        display[note] = ("amber", False)         # staged next
                    else:
                        display[note] = ("green", False)         # available to stage

            elif action == "trigger_transition":
                display[note] = ("green", False)                 # always ready to press

            elif action == "toggle_source":
                display[note] = ("red", False) if self.toggle_states.get(note, False) else ("green", False)

            elif action == "toggle_camera":
                display[note] = ("red", False) if self.camera_enabled.get(camera_key, False) else ("green", False)

            elif action == "clear_overlays":
                display[note] = ("off", False)

            elif action == "start_stop_recording":
                display[note] = ("red", False) if self.toggle_states.get(note, False) else ("green", False)

            elif action == "start_stop_streaming":
                display[note] = ("red", False) if self.toggle_states.get(note, False) else ("green", False)

        needs_flash = any(flash for _, flash in display.values())
        self.outport.send(mido.Message("control_change", control=0, value=40 if needs_flash else 32))

        for note, (color, should_flash) in display.items():
            if should_flash:
                self._light_pad_flash(note, color)
            else:
                self.light_pad(note, color)

    # ---- Overlay helpers ------------------------------------------------------

    def _hide_other_overlays(self, except_note=None):
        """
        Hide every toggle_source overlay other than `except_note` (or all of
        them, if except_note is None/doesn't match any overlay button).

        Used both to enforce "only one overlay visible at a time" when one
        is turned on, and to implement the clear_overlays button.
        """
        for other_note, other_btn in self.config.get("launchpad_buttons", {}).items():
            if other_note == except_note or other_btn.get("action") != "toggle_source":
                continue
            if not self.toggle_states.get(other_note, False):
                continue
            scene, source = other_btn["scene"], other_btn["source"]
            item_id = self.source_item_ids.get((scene, source))
            if item_id is None:
                continue
            try:
                self.obs_client.set_scene_item_enabled(scene_name=scene, item_id=item_id, enabled=False)
                self.toggle_states[other_note] = False
            except Exception as e:
                print(f"Error hiding overlay '{other_btn.get('label', other_note)}': {e}")

    # ---- Event handler ------------------------------------------------------

    def _handle_note(self, note):
        """
        Dispatch a pad press to the appropriate OBS action.

        Silently ignores presses on notes not in the config or marked
        not-found. After any successful state change, update_all_lights()
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
                    duration_ms = self.obs_client.get_current_scene_transition().transition_duration or 0
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
                if new_state:
                    self._hide_other_overlays(except_note=note)
                self.obs_client.set_scene_item_enabled(scene_name=scene, item_id=item_id, enabled=new_state)
                self.toggle_states[note] = new_state
                print(f"Overlay {'on' if new_state else 'off'}: {label}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling overlay: {e}")

        elif action == "clear_overlays":
            self._hide_other_overlays()
            print(f"{label}: all overlays hidden")
            self.update_all_lights()

        elif action == "toggle_camera":
            camera_key = btn.get("camera")
            camera_cfg = self.cameras.get(camera_key)
            if not camera_cfg:
                print(f"Unknown camera: {label}")
                return
            new_state = not self.camera_enabled.get(camera_key, False)
            try:
                url = camera_cfg["url"] if new_state else ""
                self.obs_client.set_input_settings(name=camera_cfg["input"], settings={"input": url}, overlay=True)
                self.camera_enabled[camera_key] = new_state
                if new_state:
                    self.camera_reconnect_until[camera_key] = time.monotonic() + RECONNECT_BLINK_SECONDS
                    timer = threading.Timer(RECONNECT_BLINK_SECONDS, self.update_all_lights)
                    timer.daemon = True
                    timer.start()
                    print(f"Camera on: {label} (starting up...)")
                else:
                    self.camera_reconnect_until.pop(camera_key, None)
                    print(f"Camera off: {label}")
                self.update_all_lights()
            except Exception as e:
                print(f"Error toggling camera '{label}': {e}")

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

    # ---- Lifecycle ------------------------------------------------------

    def _connect_and_sync(self):
        self.outport = open_output(self.config["devices"]["launchpad"])
        self._ensure_studio_mode()
        self._reset_leds()
        self.source_item_ids = {}
        self._not_found_notes = set()
        self._resolve_source_ids()
        self._validate_buttons()
        self._sync_toggle_states()
        self._sync_camera_states()

    def shutdown(self):
        """Turn off all Launchpad LEDs, best-effort, for a clean exit."""
        if not self.outport:
            return
        try:
            self._reset_leds()
        except Exception:
            pass

    def run_loop(self):
        """
        Main MIDI event loop with automatic reconnection.

        Opens the input port and processes messages in an inner loop. If
        the port raises an exception (e.g. USB unplug) or closes cleanly,
        the outer loop waits RECONNECT_INTERVAL seconds and attempts to
        reopen both ports and resync state before listening again. This
        repeats indefinitely until the thread is stopped (e.g. process exit).
        """
        device = self.config["devices"]["launchpad"]
        self._connect_obs()
        threading.Thread(target=self.alive_led.run_forever, daemon=True, name="LaunchpadAliveLed").start()

        while True:
            try:
                self._connect_and_sync()
                self.update_all_lights()
                with open_input(device) as inport:
                    print("Launchpad: listening...")
                    for msg in inport:
                        if msg.type == "note_on" and msg.velocity > 0:
                            self._handle_note(msg.note)
                print("Launchpad: disconnected.")
            except Exception as e:
                print(f"Launchpad: connection lost ({e})")

            self.outport = None
            time.sleep(RECONNECT_INTERVAL)
