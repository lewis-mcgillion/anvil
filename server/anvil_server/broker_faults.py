"""Represent simulated broker faults without a provider dispatch capability."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BrokerFault:
    kind: Literal["rate_limit", "disconnect", "truncated_stream", "delay"]
    retry_after_seconds: int = 0
    delay_ms: int = 0

    def __post_init__(self):
        if self.retry_after_seconds < 0 or self.delay_ms < 0:
            raise ValueError("Fault timing must be nonnegative")
        if self.kind == "rate_limit" and self.retry_after_seconds < 1:
            raise ValueError("Rate-limit faults require Retry-After")


@dataclass(frozen=True)
class BrokerFaultEvidence:
    kind: str
    pre_dispatch_calls: int
    dispatched_calls: int
    charged_calls: int
    retry_after_seconds: int
    simulated_delay_ms: int


def simulate_broker_fault(fault: BrokerFault, *, dispatched: bool) -> BrokerFaultEvidence:
    """Produce deterministic evidence; never contact a provider or sleep."""
    return BrokerFaultEvidence(
        kind=fault.kind, pre_dispatch_calls=1,
        dispatched_calls=int(dispatched), charged_calls=int(dispatched),
        retry_after_seconds=fault.retry_after_seconds,
        simulated_delay_ms=fault.delay_ms,
    )
