import logging
import threading
import time

from launchpad_obs import LaunchpadController, load_config as lp_config, RECONNECT_INTERVAL
from launch_control_obs import LaunchControlController, load_config as lc_config

# obsws-python logs every failed request with a full traceback via
# logger.exception() before re-raising.  We already catch those exceptions and
# print our own concise "not found" warnings, so silence the library's ERROR
# logging to keep the console readable (raising it to CRITICAL still lets truly
# fatal messages through).
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)


def _try_connect(controller, name):
    """
    Attempt to connect a controller, retrying until successful.

    Blocks until the device is found and connected, printing a message on
    each failed attempt.  This means the script will wait at startup if a
    device isn't plugged in yet, and proceed as soon as it is.
    """
    while True:
        try:
            controller.connect()
            print(f"{name}: connected.")
            return
        except Exception as e:
            print(f"{name}: not available ({e}), retrying in {RECONNECT_INTERVAL}s...")
            time.sleep(RECONNECT_INTERVAL)


def main():
    lp = LaunchpadController(lp_config("config_launchpad.yaml"))
    # lc = LaunchControlController(lc_config("config_launch_control.yaml"))

    # Connect in the main thread, waiting for each device to appear.
    # run_loop() handles all subsequent reconnections automatically.
    _try_connect(lp, "Launchpad")
    # _try_connect(lc, "Launch Control")

    threads = [
        threading.Thread(target=lp.run_loop, daemon=True, name="Launchpad"),
        # threading.Thread(target=lc.run_loop, daemon=True, name="LaunchControl"),
    ]

    for t in threads:
        t.start()

    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
        lp._reset_leds()
        # lc._cleanup()


if __name__ == "__main__":
    main()
