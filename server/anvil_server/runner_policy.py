"""Fail-closed requirements for an isolated re-execution container."""

from __future__ import annotations

import re
import asyncio
import os
import signal
import tempfile
import json
from pathlib import Path
import stat
from dataclasses import dataclass


class IsolationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RunnerLaunch:
    image: str
    network_disabled: bool
    read_only_root: bool
    no_new_privileges: bool
    drop_all_capabilities: bool
    seccomp_enforced: bool
    uds_only: bool
    host_mounts: tuple[str, ...] = ()
    inherited_secrets: tuple[str, ...] = ()

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}", self.image):
            raise IsolationUnavailable("Runner requires an immutable image digest")
        if not all((self.network_disabled, self.read_only_root, self.no_new_privileges,
                    self.drop_all_capabilities, self.seccomp_enforced, self.uds_only)):
            raise IsolationUnavailable("Runner isolation controls are unavailable")
        if self.host_mounts or self.inherited_secrets:
            raise IsolationUnavailable("Runner must not inherit host mounts or secrets")


def container_arguments(launch: RunnerLaunch, socket_directory: Path, seccomp_profile: Path) -> tuple[str, ...]:
    launch.validate()
    for path in (socket_directory, seccomp_profile):
        if not path.is_absolute() or path.is_symlink():
            raise IsolationUnavailable("Runner isolation paths must be absolute and non-symlinked")
    if not socket_directory.is_dir() or not seccomp_profile.is_file():
        raise IsolationUnavailable("Runner socket directory and seccomp profile must exist")
    if socket_directory.stat().st_mode & 0o022 or seccomp_profile.stat().st_mode & 0o022:
        raise IsolationUnavailable("Runner isolation paths must not be group/world writable")
    try:
        profile = json.loads(seccomp_profile.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise IsolationUnavailable("Runner seccomp policy is unreadable or invalid") from error
    if not isinstance(profile, dict) or profile.get("defaultAction") not in ("SCMP_ACT_ERRNO", "SCMP_ACT_KILL", "SCMP_ACT_KILL_PROCESS", "SCMP_ACT_KILL_THREAD"):
        raise IsolationUnavailable("Runner seccomp policy must deny syscalls by default")
    rules = profile.get("syscalls", [])
    if not isinstance(rules, list) or any(
        not isinstance(rule, dict)
        or not isinstance(rule.get("names"), list)
        or not all(isinstance(name, str) for name in rule["names"])
        or rule.get("action") == "SCMP_ACT_ALLOW" and any(
            name in {"mount", "umount2", "ptrace", "bpf", "setns", "unshare", "keyctl"}
            for name in rule["names"]
        ) for rule in rules
    ):
        raise IsolationUnavailable("Runner seccomp profile allows forbidden privileged syscalls")
    socket_path = socket_directory / "supervisor.sock"
    if socket_path.is_symlink() or not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise IsolationUnavailable("Runner supervisor socket is missing")
    if socket_directory.stat().st_uid != 65534 or socket_path.stat().st_uid != 65534:
        raise IsolationUnavailable("Runner socket must be owned by the sandbox identity")
    if not socket_directory.stat().st_mode & stat.S_IXUSR or not socket_path.stat().st_mode & stat.S_IWUSR:
        raise IsolationUnavailable("Runner socket must be accessible to the sandbox identity")
    return (
        "docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
        "--ipc=private", "--pid=private", "--uts=private", "--cgroupns=private",
        "--env=PATH=/usr/bin:/bin",
        "--security-opt=no-new-privileges", f"--security-opt=seccomp={seccomp_profile}",
        "--pids-limit=64", "--memory=512m", "--cpus=1", "--user=65534:65534",
        "--ulimit=nofile=64:64", "--ulimit=core=0:0",
        "--env=HOME=/tmp", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        f"--mount=type=bind,src={socket_directory},dst=/run/anvil,readonly",
        launch.image,
    )


async def supervise_container(
    launch: RunnerLaunch, socket_directory: Path, seccomp_profile: Path, *, timeout_seconds: float = 60
) -> int:
    if timeout_seconds <= 0:
        raise IsolationUnavailable("Runner timeout must be positive")
    arguments = container_arguments(launch, socket_directory, seccomp_profile)
    with tempfile.TemporaryDirectory(prefix="anvil-runner-") as temporary_directory:
        container_id_file = Path(temporary_directory) / "container-id"
        command = (*arguments[:2], f"--cidfile={container_id_file}", *arguments[2:])
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env={"PATH": os.defpath}, start_new_session=True,
            )
        except OSError as error:
            raise IsolationUnavailable("Container runtime is unavailable") from error
        try:
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout_seconds)
                if return_code != 0:
                    raise IsolationUnavailable(f"Runner container exited unsuccessfully ({return_code})")
                return return_code
            except asyncio.TimeoutError as error:
                raise IsolationUnavailable("Runner execution timed out") from error
        finally:
            cleanup_task = asyncio.create_task(
                asyncio.wait_for(_reclaim_container(process, container_id_file), timeout=10)
            )
            try:
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        if cleanup_task.done():
                            break
                await cleanup_task
            except asyncio.TimeoutError as error:
                raise IsolationUnavailable("Runner container cleanup exceeded 10 seconds") from error


async def _reclaim_container(process: asyncio.subprocess.Process, container_id_file: Path) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
    if not container_id_file.exists():
        return
    container_id = container_id_file.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise IsolationUnavailable("Runner container ID is invalid; cleanup cannot be verified")
    try:
        cleanup = await asyncio.create_subprocess_exec(
            "docker", "rm", "--force", container_id,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env={"PATH": os.defpath},
        )
        await cleanup.wait()
    except OSError as error:
        raise IsolationUnavailable("Runner container cleanup unavailable") from error
    if cleanup.returncode != 0:
        raise IsolationUnavailable("Runner container cleanup failed")
