"""Fail-closed resource limits for a future isolated runner supervisor."""

from dataclasses import dataclass


class RunnerLimitDenied(ValueError):
    pass


@dataclass(frozen=True)
class RunnerLimits:
    cpu_millis: int
    memory_bytes: int
    process_count: int
    wall_seconds: int

    def validate(self) -> None:
        bounds = ((self.cpu_millis, 100, 4000),
                  (self.memory_bytes, 64 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
                  (self.process_count, 1, 256),
                  (self.wall_seconds, 1, 120))
        if any(type(value) is not int or not minimum <= value <= maximum
               for value, minimum, maximum in bounds):
            raise RunnerLimitDenied("Runner resources exceed bounded isolation policy")
