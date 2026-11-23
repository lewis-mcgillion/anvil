from dataclasses import replace
import asyncio
import socket

import pytest

from anvil_server.runner_policy import IsolationUnavailable, RunnerLaunch, container_arguments, supervise_container


def safe_launch():
    return RunnerLaunch("anvil/runner@sha256:" + "a" * 64, True, True, True, True, True, True)


def test_isolated_launch_policy_accepts_enforced_controls():
    safe_launch().validate()


@pytest.mark.parametrize("control", ["network_disabled", "read_only_root", "no_new_privileges",
                                           "drop_all_capabilities", "seccomp_enforced", "uds_only"])
def test_missing_isolation_control_fails_closed(control):
    with pytest.raises(IsolationUnavailable):
        replace(safe_launch(), **{control: False}).validate()


def test_mutable_image_mounts_and_credentials_are_rejected():
    for variant in (replace(safe_launch(), image="anvil/runner:latest"),
                    replace(safe_launch(), host_mounts=("/var/run/docker.sock",)),
                    replace(safe_launch(), inherited_secrets=("PRODUCTION_API_KEY",))):
        with pytest.raises(IsolationUnavailable):
            variant.validate()


def test_container_arguments_enforce_isolation(tmp_path):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text("{}")
    profile.chmod(0o600)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        arguments = container_arguments(safe_launch(), directory, profile)
        assert "--network=none" in arguments
        assert "--cap-drop=ALL" in arguments
        assert "--read-only" in arguments
        assert "--user=65534:65534" in arguments
        assert arguments[-1] == safe_launch().image


def test_container_arguments_reject_missing_socket_and_unsafe_paths(tmp_path):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text("{}")
    profile.chmod(0o600)
    with pytest.raises(IsolationUnavailable, match="socket"):
        container_arguments(safe_launch(), directory, profile)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        directory.chmod(0o777)
        with pytest.raises(IsolationUnavailable, match="writable"):
            container_arguments(safe_launch(), directory, profile)


@pytest.mark.asyncio
async def test_supervisor_rejects_invalid_budget_before_launch(tmp_path):
    with pytest.raises(IsolationUnavailable, match="timeout"):
        await supervise_container(safe_launch(), tmp_path, tmp_path, timeout_seconds=0)


@pytest.mark.asyncio
async def test_supervisor_fails_closed_without_runtime(tmp_path, monkeypatch):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text("{}")
    profile.chmod(0o600)
    async def missing_runtime(*args, **kwargs):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_runtime)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        with pytest.raises(IsolationUnavailable, match="runtime"):
            await supervise_container(safe_launch(), directory, profile)
