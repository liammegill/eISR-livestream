import csv
from datetime import datetime
from pathlib import Path

from core.paths import ROOT

PHASES = ["toSet", "toStart", "start", "gate1", "gate2", "finish", "abort"]
FIELDNAMES = ["run", "team", *PHASES]


class RaceLog:
    """Logs race-phase timestamps to a CSV file, one row per run.

    The file is fully rewritten after every phase press (UI or Launchpad),
    so times are never lost if the app is closed mid-run. Existing rows are
    loaded on startup so the file accumulates across the whole event.
    """

    def __init__(self, path=None):
        self.path = Path(path or ROOT / "output" / "race_times.csv")
        self._finished_runs = []
        self._current_run = {}
        self._load_existing()

    def _load_existing(self):
        if self.path.exists():
            with self.path.open(newline="") as f:
                self._finished_runs = list(csv.DictReader(f))
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write_file(self):
        with self.path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(self._finished_runs)
            if self._current_run:
                writer.writerow(self._current_run)

    def record(self, phase, team=None, run_number=None):
        """Timestamp `phase` for the run in progress and persist to disk."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        if team is not None:
            self._current_run.setdefault("team", team)
        if run_number is not None:
            self._current_run.setdefault("run", run_number)
        self._current_run[phase] = timestamp
        self._write_file()
        return timestamp

    def get_current_run(self):
        """Return a copy of the run currently in progress."""
        return dict(self._current_run)

    def start_new_run(self):
        """Finalize the current run as a row and reset for the next one.

        Returns the finished run's dict (empty if nothing was recorded).
        """
        finished = self._current_run
        if finished:
            self._finished_runs.append(finished)
        self._current_run = {}
        self._write_file()
        return finished
