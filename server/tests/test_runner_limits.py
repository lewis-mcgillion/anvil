from dataclasses import replace

import pytest

from anvil_server.runner_limits import RunnerLimitDenied, RunnerLimits


def test_valid_isolated_runner_limits():
    RunnerLimits(500, 128 * 1024 * 1024, 16, 60).validate()


def test_runner_flags_deny_network_and_bound_resources():
    flags = RunnerLimits(500, 128 * 1024 * 1024, 16, 60).docker_flags()
    assert flags[:4] == ("--network=none", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges")
    assert flags[flags.index("--pids-limit") + 1] == "16"
    assert flags[flags.index("--cpu-quota") + 1] == "50000"
    assert flags[flags.index("--memory-swap") + 1] == "134217728"


def test_runner_flags_reject_invalid_limits_before_command_construction():
    with pytest.raises(RunnerLimitDenied):
        RunnerLimits(500, 128 * 1024 * 1024, 0, 60).docker_flags()


@pytest.mark.parametrize("limits,cpu_quota", [
    (RunnerLimits(100, 64 * 1024 * 1024, 1, 1), "10000"),
    (RunnerLimits(4000, 4 * 1024 * 1024 * 1024, 256, 120), "400000"),
])
def test_runner_limit_policy_accepts_exact_boundaries(limits, cpu_quota):
    flags = limits.docker_flags()
    assert flags[flags.index("--cpu-quota") + 1] == cpu_quota
    assert flags[flags.index("--pids-limit") + 1] == str(limits.process_count)
    assert flags[flags.index("--memory") + 1] == str(limits.memory_bytes)


@pytest.mark.parametrize("field,value", [
    ("cpu_millis", 0), ("cpu_millis", 5000), ("memory_bytes", 1),
    ("process_count", 0), ("process_count", 10000), ("wall_seconds", 121),
    ("wall_seconds", True), ("cpu_millis", True), ("memory_bytes", 4.0),
    ("process_count", False), ("wall_seconds", 0),
])
def test_resource_limit_bypass_fails_closed(field, value):
    with pytest.raises(RunnerLimitDenied):
        replace(RunnerLimits(500, 128 * 1024 * 1024, 16, 60), **{field: value}).validate()
