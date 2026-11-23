import pytest

from anvil_server.broker_faults import BrokerFault, InjectedBrokerFault, execute_broker_fault, retry_pre_dispatch_rate_limit, simulate_broker_fault


@pytest.mark.parametrize("kind", ["rate_limit", "disconnect", "truncated_stream", "delay"])
def test_pre_dispatch_broker_fault_has_no_paid_call(kind):
    fault = BrokerFault(kind, retry_after_seconds=5 if kind == "rate_limit" else 0)
    evidence = simulate_broker_fault(fault, dispatched=False)
    assert evidence.pre_dispatch_calls == 1
    assert evidence.dispatched_calls == evidence.charged_calls == 0
    assert evidence.retry_after_seconds == (5 if kind == "rate_limit" else 0)


def test_post_dispatch_failure_preserves_paid_call_provenance():
    evidence = simulate_broker_fault(BrokerFault("disconnect"), dispatched=True)
    assert evidence.charged_calls == evidence.dispatched_calls == 1


def test_rate_limit_requires_explicit_retry_after():
    with pytest.raises(ValueError):
        BrokerFault("rate_limit")


@pytest.mark.asyncio
async def test_fake_provider_is_never_called_for_pre_dispatch_rate_limit():
    calls = []

    async def provider():
        calls.append("paid")

    with pytest.raises(InjectedBrokerFault) as raised:
        await execute_broker_fault(BrokerFault("rate_limit", retry_after_seconds=7), provider)
    assert calls == []
    assert raised.value.evidence.charged_calls == 0
    assert raised.value.evidence.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_fake_provider_post_dispatch_fault_charges_once_without_retry():
    calls = []

    async def provider():
        calls.append("paid")
        return {"response": "unused"}

    with pytest.raises(InjectedBrokerFault) as raised:
        await execute_broker_fault(BrokerFault("disconnect"), provider, after_dispatch=True)
    assert calls == ["paid"]
    assert raised.value.evidence.dispatched_calls == raised.value.evidence.charged_calls == 1


@pytest.mark.asyncio
async def test_fake_provider_delay_is_bounded_and_observable():
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    async def provider():
        return "response"

    response, evidence = await execute_broker_fault(BrokerFault("delay", delay_ms=250), provider, sleep=sleep)
    assert response == "response"
    assert delays == [0.25]
    assert evidence.charged_calls == 1


@pytest.mark.asyncio
async def test_retry_after_waits_before_single_paid_dispatch():
    events = []

    async def sleep(seconds):
        events.append(("wait", seconds))

    async def provider():
        events.append("dispatch")
        return "ok"

    result, evidence = await retry_pre_dispatch_rate_limit(
        BrokerFault("rate_limit", retry_after_seconds=12), provider, sleep=sleep,
    )
    assert result == "ok"
    assert events == [("wait", 12), "dispatch"]
    assert (evidence.pre_dispatch_calls, evidence.dispatched_calls, evidence.charged_calls) == (2, 1, 1)
