from dataclasses import replace
import asyncio
from types import SimpleNamespace
import socket

import pytest

from anvil_server.runner_policy import IsolationUnavailable, RunnerLaunch, container_arguments, supervise_container, verify_runner_image_config, verify_local_runner_image, prepare_runner_socket_directory


def safe_launch():
    return RunnerLaunch("anvil/runner@sha256:" + "a" * 64, True, True, True, True, True, True,
                        image_environment_verified=True)


def test_isolated_launch_policy_accepts_enforced_controls():
    safe_launch().validate()


def test_socket_directory_provisioning_fails_without_owner_privileges(tmp_path, monkeypatch):
    def denied(*args):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr("anvil_server.runner_policy.os.chown", denied)
    directory = tmp_path / "sandbox-uds"
    with pytest.raises(IsolationUnavailable, match="Cannot provision"):
        prepare_runner_socket_directory(directory)
    assert not directory.exists()


def test_socket_directory_provisioning_requires_new_path(tmp_path):
    with pytest.raises(IsolationUnavailable, match="new and absolute"):
        prepare_runner_socket_directory(tmp_path)


def test_runner_image_rejects_credentials_ports_and_volumes():
    assert verify_runner_image_config({"config": {"Env": ["PATH=/usr/bin", "HOME=/tmp"], "User": "65534:65534"}})
    for config in ({"config": {"Env": ["API_KEY=secret"]}},
                   {"config": {"Env": [], "ExposedPorts": {"443/tcp": {}}}},
                   {"config": {"Env": [], "Volumes": {"/host": {}}}},
                   {"config": {"Env": [], "User": "root"}},
                   {"config": {"Env": [], "User": "root:65534"}},
                   {"config": {"Env": [], "User": "1000:1000"}},
                   {"config": {"Env": [], "User": "65534"}}):
        with pytest.raises(IsolationUnavailable):
            verify_runner_image_config(config)


@pytest.mark.asyncio
async def test_local_image_verification_rejects_digest_mismatch(monkeypatch):
    class Inspected:
        returncode = 0

        async def communicate(self):
            return (b'[{"RepoDigests":["anvil/runner@sha256:' + b'b' * 64
                    + b'"],"Config":{"Env":[],"User":"65534:65534"}}]', b'')

    async def spawn(*args, **kwargs):
        return Inspected()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(IsolationUnavailable, match="digest"):
        await verify_local_runner_image(safe_launch())


@pytest.mark.asyncio
async def test_local_image_verification_fails_without_runtime(monkeypatch):
    async def spawn(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(IsolationUnavailable, match="unavailable"):
        await verify_local_runner_image(safe_launch())


@pytest.mark.parametrize("control", ["network_disabled", "read_only_root", "no_new_privileges",
                                           "drop_all_capabilities", "seccomp_enforced", "uds_only"])
def test_missing_isolation_control_fails_closed(control):
    with pytest.raises(IsolationUnavailable):
        replace(safe_launch(), **{control: False}).validate()


def test_mutable_image_mounts_and_credentials_are_rejected():
    for variant in (replace(safe_launch(), image="anvil/runner:latest"),
                    replace(safe_launch(), host_mounts=("/var/run/docker.sock",)),
                    replace(safe_launch(), inherited_secrets=("PRODUCTION_API_KEY",)),
                    replace(safe_launch(), image_environment_verified=False)):
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
        assert "--pull=never" in arguments
        assert "--cap-drop=ALL" in arguments
        assert "--read-only" in arguments
        assert "--user=65534:65534" in arguments
        assert "--pid=private" in arguments
        assert "--ipc=private" in arguments
        assert "--ulimit=core=0:0" in arguments
        assert "--network=none" in arguments
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


def test_seccomp_profile_rejects_privileged_syscall_allow_rules(tmp_path):
    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO","syscalls":['
                       '{"names":["mount"],"action":"SCMP_ACT_ALLOW"}]}')
    profile.chmod(0o600)
    with pytest.raises(IsolationUnavailable, match="privileged"):
        container_arguments(safe_launch(), directory, profile)


@pytest.mark.parametrize("names", [None, "mount", [3]])
def test_seccomp_profile_rejects_malformed_syscall_names(tmp_path, names):
    import json

    directory = tmp_path / "uds"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "seccomp.json"
    profile.write_text(json.dumps({"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [
        {"names": names, "action": "SCMP_ACT_ALLOW"},
    ]}))
    profile.chmod(0o600)
    with pytest.raises(IsolationUnavailable, match="privileged"):
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
