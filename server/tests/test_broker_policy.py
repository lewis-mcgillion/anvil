from dataclasses import replace

import pytest

from anvil_server.broker_policy import BrokerDenied, BudgetLedger, LiveBudget, admit_live_model


def permitted(**changes):
    values = {"mode": "live", "approved": True,
              "profile_endpoint": "https://models.example.com/v1/responses",
              "requested_endpoint": "https://models.example.com/v1/responses",
              "budget": LiveBudget()}
    values.update(changes)
    return admit_live_model(**values)


def test_approved_budgeted_endpoint_is_admitted():
    permitted()


@pytest.mark.parametrize("changes", [
    {"mode": "recorded"}, {"approved": False},
    {"requested_endpoint": "https://metadata.example.com/v1/responses"},
    {"profile_endpoint": "http://models.example.com/v1/responses"},
    {"profile_endpoint": "https://user:password@models.example.com/v1/responses"},
    {"profile_endpoint": "https://models.example.com.local/v1/responses"},
    {"profile_endpoint": "https://localhost/v1/responses", "requested_endpoint": "https://localhost/v1/responses"},
    {"profile_endpoint": "https://127.0.0.1/v1/responses", "requested_endpoint": "https://127.0.0.1/v1/responses"},
    {"profile_endpoint": "https://[::1]/v1/responses", "requested_endpoint": "https://[::1]/v1/responses"},
    {"profile_endpoint": "https://models.example.com:8443/v1/responses", "requested_endpoint": "https://models.example.com:8443/v1/responses"},
    {"profile_endpoint": "https://metadata.google.internal/v1/responses", "requested_endpoint": "https://metadata.google.internal/v1/responses"},
    {"budget": replace(LiveBudget(), model_calls=0)},
    {"budget": replace(LiveBudget(), estimated_usd=2.0)},
])
def test_disallowed_endpoint_opt_in_or_budget_fails_closed(changes):
    with pytest.raises(BrokerDenied):
        permitted(**changes)


def test_broker_budget_ledger_accounts_and_rejects_overrun():
    ledger = BudgetLedger(LiveBudget(model_calls=1, input_tokens=10, estimated_usd=0.5))
    ledger.charge(model_calls=1, input_tokens=10, micro_usd=500_000)
    with pytest.raises(BrokerDenied, match="exhausted"):
        ledger.charge(model_calls=1)
    assert ledger.model_calls == 1
    with pytest.raises(BrokerDenied, match="nonnegative"):
        ledger.charge(input_tokens=-1)


def test_broker_budget_cancellation_and_expired_wall_time():
    ledger = BudgetLedger(LiveBudget())
    ledger.cancel()
    with pytest.raises(BrokerDenied, match="cancelled"):
        ledger.charge(model_calls=1)
    expired = BudgetLedger(LiveBudget(wall_seconds=1))
    expired.started -= 2
    with pytest.raises(BrokerDenied, match="wall-time"):
        expired.charge(model_calls=1)
