import threading

from core.obs_client import OBSClient
from core.teams import load_teams
from core.race_log import RaceLog
from core.race_controller import RaceController
from control_ui.app import run as run_control_ui
from launchpad.launch_control_obs import LaunchControlController
from launchpad.launchpad_obs import LaunchpadController


def main():
    obs_client = OBSClient()
    teams = load_teams()
    race_log = RaceLog()
    race_controller = RaceController(obs_client, race_log)

    launch_control = LaunchControlController(race_controller)
    launchpad = LaunchpadController()
    threading.Thread(target=launch_control.run_loop, daemon=True, name="LaunchControl").start()
    threading.Thread(target=launchpad.run_loop, daemon=True, name="Launchpad").start()

    def shutdown():
        print("Shutting down - turning off controller LEDs...")
        launchpad.shutdown()
        launch_control.shutdown()

    run_control_ui(race_controller, teams, on_exit=shutdown)


if __name__ == "__main__":
    main()
