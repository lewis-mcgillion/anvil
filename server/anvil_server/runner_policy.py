"""Fail-closed requirements for an isolated re-execution container."""

from __future__ import annotations

import re
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
    socket_path = socket_directory / "supervisor.sock"
    if socket_path.is_symlink() or not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise IsolationUnavailable("Runner supervisor socket is missing")
    return (
        "docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", f"--security-opt=seccomp={seccomp_profile}",
        "--pids-limit=64", "--memory=512m", "--cpus=1", "--user=65534:65534",
        "--env=HOME=/tmp", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        f"--mount=type=bind,src={socket_directory},dst=/run/anvil,readonly",
        launch.image,
    )
