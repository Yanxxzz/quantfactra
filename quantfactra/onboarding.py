"""Create a local, editable integration rehearsal without accounts or services."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import sys


STARTER_FILES = (
    "README.md", "config.json", "my_backtest.py", "my_planner.py", "worker.py",
    "run_adapter.py", "run_ai.py", "pyproject.toml", "model.example.json",
)


def initialize(output):
    destination = Path(output).resolve()
    source = files("quantfactra").joinpath("templates")
    # Read packaged inputs first: a broken installation must not leave an empty
    # workspace that prevents the user from retrying after repair.
    contents = {name: source.joinpath(name).read_bytes() for name in STARTER_FILES}
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    return {"status": "CREATED", "workspace": str(destination), "files": list(STARTER_FILES),
            "commands": [[sys.executable, str(destination / name)]
                         for name in ("run_adapter.py", "run_ai.py")],
            "scope": "Editable offline examples; no credentials or existing research copied"}
