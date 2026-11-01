from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anvil import (
    AnvilClient, AsyncAnvilClient, CaptureError, CaptureIncomplete, HTTPTransport,
    ExporterConfig, Response,
)
from anvil._transport import retry_after
from anvil._types import uuid7
from test_capture import RecordingTransport


@pytest.fixture
def capture_server():
    calls = []
    responses = []
    run_id = uuid7()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, dict(self.headers), json.loads(body)))
            if responses:
                status, payload, extra = responses.pop(0)
            elif self.path.endswith("/runs"):
                status, payload, extra = 201, json.dumps({"id": run_id}).encode(), {}
            else:
                count = len(json.loads(body)["events"])
                status, payload, extra = 200, json.dumps({"accepted": count, "duplicates": 0}).encode(), {}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The client deliberately closes oversized responses.

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls, responses, run_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_http_creation_ingest_auth_and_idempotency(capture_server) -> None:
    endpoint, calls, _, run_id = capture_server
    project_id = uuid7()
    with AnvilClient(endpoint, "test-capture-key", project_id=project_id) as sdk:
        with sdk.run("agent") as run:
            assert run.run_id == run_id
            run.message("user", {"password": "do-not-export"})
    assert calls[0][0] == f"/v1/projects/{project_id}/runs"
    assert all(c[1]["Authorization"] == "Bearer test-capture-key" for c in calls)
    assert all(8 <= len(c[1]["Idempotency-Key"]) <= 128 for c in calls)
    assert "do-not-export" not in repr(calls)
    assert all(c[0] == f"/v1/runs/{run_id}/events:batch" for c in calls[1:])


def test_real_http_retry_after_preserves_idempotency(capture_server) -> None:
    endpoint, calls, responses, _ = capture_server
    responses.append((429, b"{}", {"Retry-After": "0"}))
    with AnvilClient(endpoint, "test-key") as sdk:
        with sdk.run("agent", run_id=uuid7()):
            pass
    assert calls[0][1]["Idempotency-Key"] == calls[1][1]["Idempotency-Key"]
    assert calls[0][2] == calls[1][2]


def test_redirect_is_never_followed(capture_server) -> None:
    endpoint, calls, responses, _ = capture_server
    responses.append((307, b"", {"Location": "http://127.0.0.1:1/steal"}))
    sdk = AnvilClient(endpoint, "test-key")
    with sdk.run("agent", run_id=uuid7()):
        pass
    with pytest.raises(CaptureIncomplete):
        sdk.close()
    assert sdk.status.failed_batches == 1
    assert all("/steal" not in c[0] for c in calls)


def test_oversized_response_fails_explicitly(capture_server) -> None:
    endpoint, calls, responses, _ = capture_server
    responses.append((200, b"x" * (64 * 1024 + 1), {}))
    transport = HTTPTransport(endpoint, "key")
    with pytest.raises(CaptureError, match="64 KiB"):
        transport.post(f"/runs/{uuid7()}/events:batch", b'{"events":[]}', uuid7(), 1)
    assert len(calls) == 1


@pytest.mark.parametrize("endpoint", [
    "http://example.com/v1", "ftp://localhost/v1", "https://example.com/v2",
    "https://user:password@example.com/v1", "https://example.com/v1?secret=yes",
    "https://example.com/v1#fragment",
])
def test_unsafe_endpoints_rejected(endpoint) -> None:
    with pytest.raises(ValueError):
        HTTPTransport(endpoint, "key")


def test_transport_rejects_replay_and_header_injection() -> None:
    with pytest.raises(ValueError):
        HTTPTransport("http://127.0.0.1/v1", "key\r\nHeader: injected")
    transport = HTTPTransport("http://127.0.0.1/v1", "key")
    with pytest.raises(ValueError, match="only capture"):
        transport.post("/reexecutions", b"{}", uuid7(), 1)
    with pytest.raises(ValueError, match="idempotency"):
        transport.post(f"/runs/{uuid7()}/events:batch", b"{}", "bad", 1)


def test_http_date_retry_after() -> None:
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert 28 <= retry_after({"Retry-After": date}) <= 30
    for value in ("nan", "inf", "not-a-date"):
        with pytest.raises(CaptureError):
            retry_after({"retry-after": value})


def test_async_run_creation_and_rejection() -> None:
    async def scenario() -> None:
        transport = RecordingTransport()
        async with AsyncAnvilClient(transport=transport, project_id=uuid7()) as sdk:
            async with sdk.run("agent") as run:
                assert run.run_id == transport.created_id
        async with AsyncAnvilClient(transport=transport) as sdk:
            with pytest.raises(CaptureError, match="project_id"):
                async with sdk.run("missing-project"):
                    pass
    asyncio.run(scenario())


def test_cancelled_run_creation_does_not_activate_context() -> None:
    async def scenario() -> None:
        transport = RecordingTransport()
        transport.release.clear()
        sdk = AsyncAnvilClient(transport=transport, project_id=uuid7())
        run = sdk.run("agent")
        task = asyncio.create_task(run.__aenter__())
        await asyncio.to_thread(transport.entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        transport.release.set()
        assert not run._started
        await sdk.close()
    asyncio.run(scenario())


def test_duplicate_acknowledgement_and_retry_exhaustion() -> None:
    transport = RecordingTransport()
    transport.responses = [Response(200, {}, b'{"accepted":0,"duplicates":1}')]
    with AnvilClient(transport=transport) as sdk:
        with sdk.run("agent", run_id=uuid7()):
            sdk.flush()
    transport = RecordingTransport()
    transport.responses = [CaptureError("timeout")] * 2
    sdk = AnvilClient(transport=transport, exporter=ExporterConfig(max_attempts=2))
    with sdk.run("agent", run_id=uuid7()):
        pass
    with pytest.raises(CaptureIncomplete):
        sdk.close()
    assert transport.calls[0][1:3] == transport.calls[1][1:3]
    assert sdk.status.incomplete
