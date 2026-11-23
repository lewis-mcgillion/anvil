import os
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["projection", "verify"])
def test_documented_offline_playback_commands(command):
    if importlib.util.find_spec("typer") is None:
        pytest.skip("Install server CLI dependencies to execute the tutorial")
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("ANVIL_DATABASE_URL", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "server"),
        os.environ.get("PYTHONPATH", "")))
    result = subprocess.run(
        [sys.executable, "-m", "anvil_server.cli", "playback", command,
         "--recording", "fixtures/conformance/v1/retries.json"],
        cwd=root, env=environment,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
