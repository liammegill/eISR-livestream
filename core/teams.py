from pathlib import Path

import yaml

from core.paths import DATA_DIR


def load_teams(path=None):
    """Return the `teams:` mapping from data/teams.yaml, keyed by team id."""
    path = Path(path or DATA_DIR / "teams.yaml")
    return yaml.safe_load(path.read_text())["teams"]
