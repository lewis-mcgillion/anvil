from dataclasses import replace
import asyncio
from types import SimpleNamespace
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
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')
    profile.chmod(0o600)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        with pytest.MonkeyPatch.context() as monkeypatch:
            original_stat = type(directory).stat

            def stat_as_sandbox_identity(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == directory or path == directory / "supervisor.sock":
                    return SimpleNamespace(st_mode=result.st_mode, st_uid=65534)
                return result

            monkeypatch.setattr(type(directory), "stat", stat_as_sandbox_identity)
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
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')
    profile.chmod(0o600)
    with pytest.raises(IsolationUnavailable, match="socket"):
        container_arguments(safe_launch(), directory, profile)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        directory.chmod(0o777)
        with pytest.raises(IsolationUnavailable, match="writable"):
            container_arguments(safe_launch(), directory, profile)


def test_container_arguments_reject_host_owned_socket(tmp_path):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')
    profile.chmod(0o600)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        if directory.stat().st_uid != 65534:
            with pytest.raises(IsolationUnavailable, match="sandbox identity"):
                container_arguments(safe_launch(), directory, profile)


def test_seccomp_profile_must_deny_by_default(tmp_path):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ALLOW"}')
    profile.chmod(0o600)
    with pytest.raises(IsolationUnavailable, match="deny syscalls"):
        container_arguments(safe_launch(), directory, profile)


@pytest.mark.asyncio
async def test_supervisor_rejects_invalid_budget_before_launch(tmp_path):
    with pytest.raises(IsolationUnavailable, match="timeout"):
        await supervise_container(safe_launch(), tmp_path, tmp_path, timeout_seconds=0)


@pytest.mark.asyncio
async def test_supervisor_fails_closed_without_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("anvil_server.runner_policy.container_arguments", lambda *args: ("docker", "run", "image"))
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')
    profile.chmod(0o600)
    async def missing_runtime(*args, **kwargs):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_runtime)
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        with pytest.raises(IsolationUnavailable, match="runtime"):
            await supervise_container(safe_launch(), directory, profile)


@pytest.mark.asyncio
async def test_supervisor_kills_timed_out_runner(tmp_path, monkeypatch):
    monkeypatch.setattr("anvil_server.runner_policy.container_arguments", lambda *args: ("docker", "run", "image"))
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')
    profile.chmod(0o600)
    killed = []

    class HungProcess:
        pid = 4242
        returncode = None

        async def wait(self):
            if not killed:
                await asyncio.sleep(10)
            self.returncode = -9
            return self.returncode

    async def fake_spawn(*args, **kwargs):
        return HungProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("anvil_server.runner_policy.os.killpg", lambda pid, signal: killed.append(pid))
    with socket.socket(socket.AF_UNIX) as supervisor:
        supervisor.bind(str(directory / "supervisor.sock"))
        with pytest.raises(IsolationUnavailable, match="timed out"):
            await supervise_container(safe_launch(), directory, profile, timeout_seconds=0.001)
    assert killed == [4242]


@pytest.mark.asyncio
async def test_supervisor_rejects_nonzero_container_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("anvil_server.runner_policy.container_arguments", lambda *args: ("docker", "run", "image"))

    class FailedProcess:
        returncode = 125

        async def wait(self):
            return self.returncode

    async def fake_spawn(*args, **kwargs):
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    with pytest.raises(IsolationUnavailable, match="125"):
        await supervise_container(safe_launch(), tmp_path, tmp_path)


@pytest.mark.asyncio
async def test_supervisor_cancellation_waits_for_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("anvil_server.runner_policy.container_arguments", lambda *args: ("docker", "run", "image"))
    started = asyncio.Event()
    killed = asyncio.Event()

    class RunningProcess:
        pid = 4242
        returncode = None

        async def wait(self):
            started.set()
            await killed.wait()
            self.returncode = -9
            return -9

    async def fake_spawn(*args, **kwargs):
        return RunningProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("anvil_server.runner_policy.os.killpg", lambda *args: killed.set())
    task = asyncio.create_task(supervise_container(safe_launch(), tmp_path, tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert killed.is_set()


@pytest.mark.asyncio
async def test_supervisor_reports_cleanup_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("anvil_server.runner_policy.container_arguments", lambda *args: ("docker", "run", "image"))

    class DoneProcess:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        return DoneProcess()

    async def stalled_cleanup(*args, **kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("anvil_server.runner_policy._reclaim_container", stalled_cleanup)
    with pytest.raises(IsolationUnavailable, match="cleanup exceeded"):
        await supervise_container(safe_launch(), tmp_path, tmp_path)
