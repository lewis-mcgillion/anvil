import pytest

from anvil_server.broker_faults import BrokerFault, simulate_broker_fault


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
