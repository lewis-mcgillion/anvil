"""Fail-closed requirements for an isolated re-execution container."""

from __future__ import annotations

import re
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
