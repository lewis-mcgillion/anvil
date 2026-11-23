from dataclasses import replace

import pytest

from anvil_server.broker_policy import BrokerDenied, BrokerThrottled, BudgetLedger, LiveBudget, OperatorProfile, admit_live_model, resolve_public_endpoint, dispatch_live_model, dispatch_operator_profile


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


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1}, {"output_tokens": True}, {"cost_micro_usd": 1.5},
])
def test_broker_rejects_invalid_provider_usage(usage):
    with pytest.raises(BrokerDenied, match="nonnegative"):
        BudgetLedger(LiveBudget()).charge(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            micro_usd=usage.get("cost_micro_usd", 0),
        )


@pytest.mark.asyncio
async def test_operator_profile_rejects_unknown_or_unapproved_profiles():
    ledger = BudgetLedger(LiveBudget())
    with pytest.raises(BrokerDenied, match="identifier"):
        await dispatch_operator_profile(profile_id="../unsafe", lookup=lambda _: None,
                                        payload={}, ledger=ledger)
    with pytest.raises(BrokerDenied, match="unavailable"):
        await dispatch_operator_profile(profile_id="missing", lookup=lambda _: None,
                                        payload={}, ledger=ledger)
    with pytest.raises(BrokerDenied, match="opt-in"):
        await dispatch_operator_profile(
            profile_id="test", lookup=lambda _: OperatorProfile(
                "https://models.example.com/v1/responses", "secret", False,
            ), payload={}, ledger=ledger,
        )


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
    response = json.dumps({"usage": {"input_tokens": 3, "output_tokens": 2, "cost_micro_usd": 100}, "output": []}).encode()

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
    assert (ledger.model_calls, ledger.input_tokens, ledger.output_tokens) == (1, 4, 2)
    assert ledger.micro_usd == 100
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


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_header", [
    b"Transfer-Encoding: chunked\r\n",
    b"Content-Encoding: gzip\r\n",
    b"Content-Length: 2\r\n",
])
async def test_live_broker_rejects_ambiguous_response_framing(monkeypatch, extra_header):
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
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n" + extra_header + b"\r\n{}")
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    endpoint = "https://models.example.com/v1/responses"
    with pytest.raises(BrokerDenied, match="encoding|content length"):
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={}, ledger=BudgetLedger(LiveBudget()))


@pytest.mark.asyncio
async def test_live_broker_exposes_bounded_retry_after_without_retry(monkeypatch):
    import asyncio

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", lambda endpoint: asyncio.sleep(0, result=("8.8.8.8",)))
    calls = []

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
        calls.append(1)
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 120\r\nContent-Length: 0\r\n\r\n")
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    endpoint = "https://models.example.com/v1/responses"
    with pytest.raises(BrokerThrottled) as error:
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={}, ledger=BudgetLedger(LiveBudget()))
    assert error.value.retry_after_seconds == 120
    assert calls == [1]


@pytest.mark.asyncio
async def test_live_broker_dns_delay_cannot_bypass_wall_budget(monkeypatch):
    import asyncio

    async def delayed_resolution(endpoint):
        await asyncio.sleep(2)
        return ("8.8.8.8",)

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", delayed_resolution)
    endpoint = "https://models.example.com/v1/responses"
    ledger = BudgetLedger(LiveBudget(wall_seconds=1))
    with pytest.raises(BrokerDenied, match="transport failed"):
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={}, ledger=ledger)


@pytest.mark.asyncio
async def test_live_broker_concurrent_requests_cannot_bypass_call_budget(monkeypatch):
    import asyncio
    import json

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", lambda endpoint: asyncio.sleep(0, result=("8.8.8.8",)))

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(0)

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        content = json.dumps({"usage": {"input_tokens": 1}}).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(f"HTTP/1.1 200 OK\r\nContent-Length: {len(content)}\r\n\r\n".encode() + content)
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    endpoint = "https://models.example.com/v1/responses"
    ledger = BudgetLedger(LiveBudget(model_calls=1))
    async def request():
        return await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                         credential="operator-secret", payload={}, ledger=ledger)

    results = await asyncio.gather(request(), request(), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, BrokerDenied) for result in results) == 1
    assert ledger.model_calls == 1


@pytest.mark.asyncio
async def test_live_broker_cancellation_aborts_active_resolution(monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def hanging_resolution(endpoint):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", hanging_resolution)
    endpoint = "https://models.example.com/v1/responses"
    ledger = BudgetLedger(LiveBudget())
    task = asyncio.create_task(dispatch_live_model(
        endpoint=endpoint, profile_endpoint=endpoint, approved=True,
        credential="operator-secret", payload={}, ledger=ledger,
    ))
    await started.wait()
    ledger.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    with pytest.raises(BrokerDenied, match="cancelled"):
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={}, ledger=ledger)


@pytest.mark.asyncio
async def test_live_broker_denies_oversize_prompt_before_dns(monkeypatch):
    calls = []

    async def unexpected_dns(endpoint):
        calls.append(endpoint)
        raise AssertionError("DNS resolution must not be attempted")

    monkeypatch.setattr("anvil_server.broker_policy.resolve_public_endpoint", unexpected_dns)
    endpoint = "https://models.example.com/v1/responses"
    ledger = BudgetLedger(LiveBudget(input_tokens=1))
    with pytest.raises(BrokerDenied, match="exhausted"):
        await dispatch_live_model(endpoint=endpoint, profile_endpoint=endpoint, approved=True,
                                  credential="operator-secret", payload={"prompt": "too long"}, ledger=ledger)
    assert calls == []
