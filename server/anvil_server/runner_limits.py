"""Resource policy arguments for a future isolated runner supervisor.

These arguments alone do not establish seccomp, filesystem or UDS isolation.
"""

from dataclasses import dataclass


class RunnerLimitDenied(ValueError):
    pass


@dataclass(frozen=True)
class RunnerLimits:
    cpu_millis: int
    memory_bytes: int
    process_count: int
    wall_seconds: int

    def docker_flags(self) -> tuple[str, ...]:
        self.validate()
        cpu_quota = self.cpu_millis * 100
        return ("--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit", str(self.process_count),
                "--memory", str(self.memory_bytes), "--memory-swap", str(self.memory_bytes),
                "--cpu-period", "100000", "--cpu-quota", str(cpu_quota),
                "--user", "65534:65534")

    def validate(self) -> None:
        bounds = ((self.cpu_millis, 100, 4000),
                  (self.memory_bytes, 64 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
                  (self.process_count, 1, 256),
                  (self.wall_seconds, 1, 120))
        if any(type(value) is not int or not minimum <= value <= maximum
               for value, minimum, maximum in bounds):
            raise RunnerLimitDenied("Runner resources exceed bounded isolation policy")
