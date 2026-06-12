import threading

from launchpad_obs import LaunchpadController, load_config as lp_config
from launch_control_obs import LaunchControlController, load_config as lc_config


def main():
    lp = LaunchpadController(lp_config("config_launchpad.yaml"))
    lc = LaunchControlController(lc_config("config_launch_control.yaml"))

    # Connect in the main thread so outport is owned here and can be
    # reset on Ctrl+C without depending on daemon thread cleanup.
    lp.connect()
    lc.connect()

    t_lp = threading.Thread(target=lp.run_loop, daemon=True, name="Launchpad")
    t_lc = threading.Thread(target=lc.run_loop, daemon=True, name="LaunchControl")

    t_lp.start()
    t_lc.start()

    try:
        while t_lp.is_alive() or t_lc.is_alive():
            t_lp.join(timeout=0.5)
            t_lc.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
        lp._reset_leds()
        lc._cleanup()


if __name__ == "__main__":
    main()
