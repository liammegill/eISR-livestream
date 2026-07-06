PHASES = [
    ("To Set", "toSet"),
    ("To Start", "toStart"),
    ("Start", "start"),
    ("Gate 1", "gate1"),
    ("Gate 2", "gate2"),
    ("Finish", "finish"),
    ("Abort", "abort"),
]

CLEAR_ACTION = ("Clear / New", "clear")


class RaceController:
    """Single entry point for loading a team and triggering race-phase actions.

    Both the control UI and the Launch Control call into the same instance,
    so OBS, the timing log, and the run-times display stay in sync no matter
    which input triggered the action. Each input also registers its own
    add_listener() callback so it can react to phases triggered by the
    *other* input too (e.g. the Launch Control's button LEDs update to
    reflect a phase pressed from the control UI, and vice versa).
    """

    def __init__(self, obs_client, race_log):
        self.obs = obs_client
        self.race_log = race_log
        self.current_team = None       # human-readable label of the loaded team
        self.current_run_number = "-"  # "-" for a test run, else an integer string
        self.listeners = []            # callback(run: dict, action: str), fired after every phase

    def add_listener(self, callback):
        """Register `callback(run, action)` to be called after every trigger_phase().

        Multiple inputs (control UI, Launch Control, ...) can each add their
        own listener so they all stay in sync no matter which one triggered
        the action.
        """
        self.listeners.append(callback)

    def set_team(self, team_id, team_label=None):
        self.current_team = team_label or team_id
        self.obs.load_team(team_id)

    def set_run_number(self, value):
        self.current_run_number = value

    def trigger_phase(self, action):
        self.obs.race_action(action)
        if action == "clear":
            # start_new_run() finalizes the just-completed run; show that
            # snapshot until the next toSet starts filling in a fresh one.
            run_to_show = self.race_log.start_new_run()
        else:
            team = self.current_team if action == "toSet" else None
            run_number = (self.current_run_number or "-") if action == "toSet" else None
            self.race_log.record(action, team=team, run_number=run_number)
            run_to_show = self.race_log.get_current_run()

        for listener in self.listeners:
            listener(run_to_show, action)
