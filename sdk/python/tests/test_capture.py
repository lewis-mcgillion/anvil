from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid

import pytest

from anvil import (
    AnvilClient, AsyncAnvilClient, CaptureError, CaptureIncomplete, CaptureWarning,
    ExporterConfig, RedactionPolicy, Response, current_run,
)
from anvil._types import uuid7
from anvil_schema import validate

CANARY = "canary-never-export"


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str, float]] = []
        self.responses: list[Response | CaptureError] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.created_id = uuid7()

    def post(self, path: str, body: bytes, idempotency_key: str, timeout: float) -> Response:
        self.calls.append((path, body, idempotency_key, timeout))
        self.entered.set()
        if not self.release.wait(timeout):
            raise CaptureError("test transport timeout")
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, CaptureError):
                raise response
            return response
        if path.endswith("/runs"):
            return Response(201, {}, json.dumps({"id": self.created_id, "future_field": True}).encode())
        count = len(json.loads(body)["events"])
        return Response(200, {}, json.dumps({"accepted": count, "duplicates": 0, "future_field": True}).encode())

    @property
    def events(self) -> list[dict]:
        return [e for path, body, _, _ in self.calls if path.endswith("events:batch") for e in json.loads(body)["events"]]


def client(transport: RecordingTransport, **kwargs) -> AnvilClient:
    return AnvilClient(transport=transport, **kwargs)


def assert_contract(events: list[dict]) -> None:
    for event in events:
        validate("event", json.dumps(event))
        assert uuid.UUID(event["event_id"]).version == 7
    streams = {e["stream_id"] for e in events}
    for stream in streams:
        sequences = [e["sequence"] for e in events if e["stream_id"] == stream]
        assert sequences == list(range(len(sequences)))


def test_nested_runs_spans_and_copy_on_capture() -> None:
    transport = RecordingTransport()
    policy = RedactionPolicy(allow=("/content", "/arguments", "/result"))
    with client(transport, redaction=policy) as sdk:
        with sdk.run("outer", run_id=uuid7()) as outer:
            assert current_run() is outer
            root = outer.message("user", "hello")
            value = {"items": [1]}
            with outer.span(parent_event_ids=(root,)) as invocation:
                outer.tool_requested("lookup", value)
                value["items"].append(2)
                outer.tool_completed("lookup", {"items": [1]}, "ok")
                with outer.span():
                    outer.message("assistant", "nested")
            with sdk.run("inner", run_id=uuid7()) as inner:
                assert current_run() is inner
            assert current_run() is outer
            outer.message("assistant", "done")
        with pytest.raises(CaptureError, match="no active"):
            current_run()
    events = transport.events
    requested = next(e for e in events if e["type"] == "tool.requested")
    completed = next(e for e in events if e["type"] == "tool.completed")
    assert requested["payload"]["arguments"] == {"items": [1]}
    assert requested["invocation_id"] == completed["invocation_id"] == invocation
    assert requested["parent_event_ids"] == [root]
    assert_contract(events)


def test_run_creation_is_typed_and_redacted_before_http() -> None:
    transport = RecordingTransport()
    with client(transport, project_id=uuid7(), redaction=RedactionPolicy(patterns=(CANARY,))) as sdk:
        run = sdk.run("agent-" + CANARY, input={"password": CANARY})
        assert CANARY not in repr(run._start)
        with run:
            assert run.run_id == transport.created_id
    assert CANARY.encode() not in b"".join(call[1] for call in transport.calls)
    assert set(json.loads(transport.calls[0][1])) == {"agent_name", "adapter", "adapter_version", "trace_id"}
    assert_contract(transport.events)


def test_original_exception_and_context_restored() -> None:
    transport = RecordingTransport()
    original = RuntimeError(CANARY)
    with client(transport) as sdk:
        with pytest.raises(RuntimeError) as caught:
            with sdk.run("failure", run_id=uuid7()):
                raise original
        assert caught.value is original
        with pytest.raises(CaptureError):
            current_run()
    assert CANARY.encode() not in b"".join(call[1] for call in transport.calls)
    assert transport.events[-1]["type"] == "run.failed"


def test_capture_cleanup_error_does_not_replace_agent_exception() -> None:
    transport = RecordingTransport()
    with client(transport) as sdk:
        with pytest.warns(CaptureWarning, match="terminal capture failed"):
            with pytest.raises(KeyError, match="original"):
                with sdk.run("agent", run_id=uuid7()):
                    sdk.close()
                    raise KeyError("original")
    with pytest.raises(CaptureError):
        current_run()


def test_async_context_cancellation_and_task_isolation() -> None:
    async def scenario() -> None:
        transport = RecordingTransport()
        async with AsyncAnvilClient(transport=transport) as sdk:
            started = asyncio.Event()

            async def cancelled() -> None:
                async with sdk.run("cancelled", run_id=uuid7()) as run:
                    started.set()
                    assert current_run() is run
                    await asyncio.Event().wait()

            task = asyncio.create_task(cancelled())
            await started.wait()
            with pytest.raises(CaptureError):
                current_run()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sdk.run("survivor", run_id=uuid7()) as survivor:
                survivor.message("assistant", "still running")
        assert sum(e["type"] == "run.failed" for e in transport.events) == 1
        assert_contract(transport.events)

    asyncio.run(scenario())


def test_redaction_allows_only_selected_fields_and_scrubs_nested_secrets() -> None:
    transport = RecordingTransport()
    policy = RedactionPolicy(
        allow=("/request/messages", "/content", "/arguments", "/result", "/response"),
        deny=("/request/messages/0/private", "/arguments/a~1b/~0key"),
        patterns=(CANARY,),
    )
    with client(transport, redaction=policy) as sdk:
        with sdk.run("agent", run_id=uuid7()) as run:
            run.model_requested("model", {
                "messages": [{"text": CANARY, "private": "do-not-export", "api_key": "key"}],
                "headers": {"arbitrary": CANARY},
            })
            run.message("user", {"token": CANARY, "text": "Bearer abc123 and sk-abc123", CANARY: "value"})
            run.tool_requested("lookup", {"a/b": {"~key": "hidden-value"}, "safe": [CANARY]})
            run.tool_completed("lookup", {}, {"password": CANARY})
            run.model_completed("model", {"text": CANARY})
            run.emit("model.failed", {"model": "model", "error": {"type": CANARY, "message": CANARY}})
            run.emit("message.created", {"role": "user", "content": "safe", "unknown": CANARY})
    wire = b"".join(c[1] for c in transport.calls)
    for secret in (CANARY.encode(), b"do-not-export", b"hidden-value", b"abc123", b"sk-abc123"):
        assert secret not in wire
    request = next(e["payload"] for e in transport.events if e["type"] == "model.requested")
    assert set(request["request"]) == {"messages"}
    assert not request["replay_safe"]
    assert_contract(transport.events)


def test_stream_fragments_and_errors_never_enter_buffers() -> None:
    transport = RecordingTransport()
    transport.release.clear()
    sdk = client(transport, redaction=RedactionPolicy(allow=("/chunk",)))
    with sdk.run("stream", run_id=uuid7()) as run:
        for i, fragment in enumerate((CANARY[:8], CANARY[8:])):
            run.model_chunk(i, fragment)
        run.emit("tool.failed", {"tool_name": "tool", "arguments": CANARY, "error": {"type": "Error", "message": CANARY}})
        for entry in sdk._exporter._queue:
            assert CANARY[:8].encode() not in entry.wire
            assert CANARY[8:].encode() not in entry.wire
    transport.release.set()
    sdk.close()
    assert all(not e["payload"]["replay_safe"] for e in transport.events if e["type"] == "model.chunk")
    assert_contract(transport.events)


@pytest.mark.parametrize("event_type,payload", [
    ("unknown.type", {}),
    ("message.created", {"role": "invalid", "content": "hello"}),
    ("model.requested", {"model": "model"}),
    ("model.chunk", {"chunk_index": -1, "chunk": "text"}),
    ("message.created", {"role": "user", "content": float("nan")}),
])
def test_rejected_events_never_export(event_type, payload) -> None:
    transport = RecordingTransport()
    with client(transport, redaction=RedactionPolicy(allow=("/content",))) as sdk:
        with sdk.run("agent", run_id=uuid7()) as run:
            with pytest.raises((CaptureError, ValueError)):
                run.emit(event_type, payload)
    assert [e["type"] for e in transport.events] == ["run.started", "run.completed"]


@pytest.mark.parametrize("kwargs", [
    {"schema_version": "2"}, {"project_id": "not-a-uuid"},
])
def test_client_rejects_bad_contract_parameters(kwargs) -> None:
    with pytest.raises(ValueError):
        client(RecordingTransport(), **kwargs)


def test_invalid_context_lifetimes_and_identifiers() -> None:
    transport = RecordingTransport()
    with client(transport) as sdk:
        with pytest.raises(ValueError):
            sdk.run("agent", run_id=uuid7(), trace_id="bad")
        run = sdk.run("agent", run_id=uuid7())
        with pytest.raises(CaptureError):
            run.message("user", "outside")
        with run:
            with pytest.raises(CaptureError):
                run.__enter__()
            with pytest.raises(ValueError):
                with run.span(parent_event_ids=("invalid",)):
                    pass
        with pytest.raises(CaptureError):
            run.message("user", "outside")
        with pytest.raises(CaptureError):
            run.__enter__()
    with pytest.raises(CaptureError):
        sdk.run("agent", run_id=uuid7()).__enter__()


def test_overflow_is_nonblocking_bounded_and_emits_gap() -> None:
    transport = RecordingTransport()
    transport.release.clear()
    sdk = client(transport, exporter=ExporterConfig(max_events=2, request_timeout=2))
    with pytest.warns(CaptureWarning):
        with sdk.run("overflow", run_id=uuid7()) as run:
            assert transport.entered.wait(1)
            start = time.monotonic()
            for _ in range(10):
                run.message("user", CANARY)
            assert time.monotonic() - start < 1
            assert sdk.status.buffered_events == 2  # includes the in-flight request
            assert sdk.status.buffered_bytes <= sdk._exporter.config.max_bytes
    transport.release.set()
    with pytest.raises(CaptureIncomplete):
        sdk.close()
    gaps = [e for e in transport.events if e["type"] == "capture.gap"]
    assert gaps and gaps[0]["payload"]["reason"] == "buffer_overflow"
    assert sum(e["payload"]["expected_count"] for e in gaps) == sdk.status.dropped_events
    assert not sdk._exporter._thread.is_alive()
    assert_contract(transport.events)


def test_size_limit_gap_and_byte_limit() -> None:
    for config, reason in [
        (ExporterConfig(), "size_limit"),
        (ExporterConfig(max_bytes=1024), "buffer_overflow"),
    ]:
        transport = RecordingTransport()
        sdk = client(transport, exporter=config, redaction=RedactionPolicy(allow=("/content",)))
        with sdk.run("agent", run_id=uuid7()) as run:
            with pytest.warns(CaptureWarning):
                run.message("user", "x" * (300 * 1024 if reason == "size_limit" else 10 * 1024))
        with pytest.raises(CaptureIncomplete):
            sdk.close()
        assert any(e["type"] == "capture.gap" and e["payload"]["reason"] == reason for e in transport.events)


@pytest.mark.parametrize("first", [
    Response(429, {"Retry-After": "0"}, b""),
    Response(503, {}, b""),
    CaptureError("network timeout"),
])
def test_retries_preserve_identity_and_request_bytes(first) -> None:
    transport = RecordingTransport()
    transport.responses = [first]
    with client(transport) as sdk:
        with sdk.run("agent", run_id=uuid7()):
            pass
    assert transport.calls[0][1:3] == transport.calls[1][1:3]
    assert transport.calls[0][3] == 1.0
    for _, body, _, _ in transport.calls:
        for event in json.loads(body)["events"]:
            validate("event", json.dumps(event))


@pytest.mark.parametrize("response", [
    Response(409, {}, b""),
    Response(401, {}, b""),
    Response(200, {}, b"not-json"),
    Response(200, {}, b'{"accepted":0,"duplicates":0}'),
    Response(200, {}, b'{"accepted":true,"duplicates":0}'),
    Response(429, {"Retry-After": "600"}, b""),
    Response(429, {"Retry-After": "bad-date"}, b""),
])
def test_terminal_export_failure_is_explicit_and_not_retried(response) -> None:
    transport = RecordingTransport()
    transport.responses = [response]
    sdk = client(transport)
    with sdk.run("agent", run_id=uuid7()):
        pass
    with pytest.raises(CaptureIncomplete):
        sdk.close()
    assert sdk.status.failed_batches == 1
    assert any(e["type"] == "capture.gap" for e in transport.events)
    first_key = transport.calls[0][2]
    assert sum(c[2] == first_key for c in transport.calls) == 1


def test_flush_deadline_close_interrupts_retry_backoff() -> None:
    transport = RecordingTransport()
    transport.responses = [Response(429, {"Retry-After": "5"}, b"")]
    sdk = client(transport)
    with sdk.run("agent", run_id=uuid7()):
        pass
    assert transport.entered.wait(1)
    before = time.monotonic()
    with pytest.raises(CaptureIncomplete):
        sdk.close(timeout=0.02)
    assert time.monotonic() - before < 0.5
    sdk._exporter._thread.join(1)
    assert not sdk._exporter._thread.is_alive()


def test_async_flush_cancellation_does_not_block_loop_or_lose_worker() -> None:
    async def scenario() -> None:
        transport = RecordingTransport()
        transport.release.clear()
        sdk = AsyncAnvilClient(transport=transport)
        async with sdk.run("agent", run_id=uuid7()):
            pass
        task = asyncio.create_task(sdk.flush())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        transport.release.set()
        await sdk.close()
        assert not sdk._exporter._thread.is_alive()

    asyncio.run(scenario())


def test_policy_rejects_invalid_pointers_patterns_and_json() -> None:
    for path in ("", "content", "/a~2b"):
        with pytest.raises(ValueError):
            RedactionPolicy(deny=(path,))
    with pytest.raises(re.error, match="unterminated"):
        RedactionPolicy(patterns=("[",))
    policy = RedactionPolicy(allow=("/content",))
    with pytest.raises(ValueError, match="finite JSON"):
        policy.apply("message.created", {"role": "user", "content": object()})


@pytest.mark.parametrize("kwargs", [
    {"max_events": 0}, {"max_bytes": -1}, {"batch_events": 501},
    {"batch_bytes": 4 * 1024 * 1024 + 1}, {"request_timeout": float("nan")},
    {"max_attempts": 0}, {"max_retry_delay": 0},
])
def test_invalid_exporter_config(kwargs) -> None:
    with pytest.raises(ValueError):
        ExporterConfig(**kwargs)
