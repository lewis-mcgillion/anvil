from dataclasses import replace

import pytest

from anvil_server.runner_policy import IsolationUnavailable, RunnerLaunch


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
