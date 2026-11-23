"""Opt-in real-container isolation probes for a locally pinned Python runner image."""

import os
import shutil
import subprocess

import pytest


IMAGE = os.getenv("ANVIL_RUNNER_PROBE_IMAGE", "")


@pytest.mark.skipif(not IMAGE or not shutil.which("docker"), reason="Docker probe image/runtime unavailable")
def test_runner_container_denies_network_and_host_access(tmp_path):
    if "@sha256:" not in IMAGE:
        pytest.fail("ANVIL_RUNNER_PROBE_IMAGE must reference an immutable digest")
    script = """
import os, socket

def denied(operation):
    try:
        operation()
    except (OSError, PermissionError):
        return
    raise AssertionError('isolation probe unexpectedly succeeded')

denied(lambda: socket.getaddrinfo('example.com', 443))
denied(lambda: socket.create_connection(('1.1.1.1', 80), timeout=2))
denied(lambda: socket.create_connection(('169.254.169.254', 80), timeout=2))
denied(lambda: open('/etc/shadow').read())
denied(lambda: open('/var/run/docker.sock', 'rb').read(1))
denied(lambda: os.setuid(0))
assert os.getuid() == 65534
assert not any(key.lower().endswith(('token', 'secret', 'key')) for key in os.environ)
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
         "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
         "--pids-limit=64", "--memory=512m", "--cpus=1", IMAGE,
         "python", "-c", script],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
