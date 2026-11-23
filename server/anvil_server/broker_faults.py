"""Represent simulated broker faults without a provider dispatch capability."""

import asyncio
from collections.abc import Awaitable, Callable
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


class InjectedBrokerFault(RuntimeError):
    def __init__(self, evidence: BrokerFaultEvidence):
        super().__init__("Simulated broker fault")
        self.evidence = evidence


async def execute_broker_fault(
    fault: BrokerFault, dispatch: Callable[[], Awaitable[object]],
    *, after_dispatch: bool = False, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[object, BrokerFaultEvidence]:
    """Inject a bounded fault around a fake-provider call without retries or fallbacks."""
    if fault.kind == "delay":
        await sleep(fault.delay_ms / 1000)
    if not after_dispatch and fault.kind != "delay":
        raise InjectedBrokerFault(simulate_broker_fault(fault, dispatched=False))
    result = await dispatch()
    if after_dispatch and fault.kind != "delay":
        raise InjectedBrokerFault(simulate_broker_fault(fault, dispatched=True))
    return result, simulate_broker_fault(fault, dispatched=True)
