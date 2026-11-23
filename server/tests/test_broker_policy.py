from dataclasses import replace

import pytest

from anvil_server.broker_policy import BrokerDenied, BudgetLedger, LiveBudget, admit_live_model, resolve_public_endpoint, dispatch_live_model


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
    {"profile_endpoint": "https://models.example.com:invalid/v1/responses", "requested_endpoint": "https://models.example.com:invalid/v1/responses"},
    {"profile_endpoint": "https://models.example.com./v1/responses", "requested_endpoint": "https://models.example.com./v1/responses"},
    {"profile_endpoint": "https://models.example.com\\@attacker.example/v1/responses", "requested_endpoint": "https://models.example.com\\@attacker.example/v1/responses"},
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


@pytest.mark.asyncio
async def test_broker_dns_only_accepts_public_addresses(monkeypatch):
    import asyncio
    import socket

    async def addresses(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    assert await resolve_public_endpoint("https://models.example.com/v1/responses") == ("8.8.8.8",)

    async def private_addresses(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private_addresses)
    with pytest.raises(BrokerDenied, match="non-public"):
        await resolve_public_endpoint("https://models.example.com/v1/responses")


@pytest.mark.asyncio
async def test_live_broker_pins_dns_and_accounts_provider_usage(monkeypatch):
    import asyncio
    import json

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", lambda endpoint: asyncio.sleep(0, result=("8.8.8.8",)))
    sent = []
    response = json.dumps({"usage": {"input_tokens": 3, "output_tokens": 2}, "output": []}).encode()

    class Writer:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(host, port, **kwargs):
        assert host == "8.8.8.8" and port == 443
        assert kwargs["server_hostname"] == "models.example.com"
        reader = asyncio.StreamReader()
        reader.feed_data(f"HTTP/1.1 200 OK\r\nContent-Length: {len(response)}\r\n\r\n".encode() + response)
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    endpoint = "https://models.example.com/v1/responses"
    ledger = BudgetLedger(LiveBudget())
    result = await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                       credential="operator-secret", payload={"model": "test"}, ledger=ledger)
    assert result["usage"]["input_tokens"] == 3
    assert (ledger.model_calls, ledger.input_tokens, ledger.output_tokens) == (1, 3, 2)
    assert b"Authorization: Bearer operator-secret" in sent[0]


@pytest.mark.asyncio
async def test_live_broker_rejects_provider_redirect(monkeypatch):
    import asyncio

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", lambda endpoint: asyncio.sleep(0, result=("8.8.8.8",)))

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 302 Found\r\nLocation: https://attacker.example\r\nContent-Length: 0\r\n\r\n")
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    endpoint = "https://models.example.com/v1/responses"
    with pytest.raises(BrokerDenied, match="redirect"):
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={}, ledger=BudgetLedger(LiveBudget()))
