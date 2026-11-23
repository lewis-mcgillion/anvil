from dataclasses import replace

import pytest

from anvil_server.broker_policy import BrokerDenied, LiveBudget, admit_live_model


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
