"""Typed capture contexts. These hooks record data, never invoke agent tools."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import secrets
import warnings
from contextlib import contextmanager
from typing import Iterator, Literal, Mapping

from anvil_schema import ValidationError, validate

from ._exporter import Exporter, ExporterConfig, ExportStatus, encode
from ._redaction import RedactionPolicy
from ._transport import CaptureError, HTTPTransport, Transport
from ._types import Event, EventType, JSON, Sequence, check_uuid7, timestamp, uuid7

_CURRENT: contextvars.ContextVar[RunCapture | None] = contextvars.ContextVar("anvil_run", default=None)
_SCOPE: contextvars.ContextVar[tuple[str, str, tuple[str, ...]] | None] = contextvars.ContextVar("anvil_scope", default=None)


class CaptureWarning(RuntimeWarning):
    """Ancillary capture lost evidence; inspect status and flush explicitly."""


def current_run() -> RunCapture:
    run = _CURRENT.get()
    if run is None:
        raise CaptureError("no active capture run")
    return run


class _ClientBase:
    def __init__(
        self, base_url: str = "http://127.0.0.1:8080/v1", api_key: str = "",
        *, project_id: str | None = None, transport: Transport | None = None,
        redaction: RedactionPolicy = RedactionPolicy(),
        exporter: ExporterConfig = ExporterConfig(), schema_version: str = "1",
    ) -> None:
        if schema_version != "1":
            raise ValueError("only frozen schema version 1 is supported")
        self.project_id = check_uuid7(project_id) if project_id is not None else None
        self.redaction = redaction
        self._transport = transport if transport is not None else HTTPTransport(base_url, api_key)
        self._exporter = Exporter(self._transport, exporter)

    @property
    def status(self) -> ExportStatus:
        return self._exporter.status

    def run(
        self, agent_name: str, *, run_id: str | None = None, adapter: str = "manual",
        adapter_version: str = "0.1.0", input: JSON = None,
        trace_id: str | None = None,
    ) -> RunCapture:
        return RunCapture(self, agent_name, run_id, adapter, adapter_version, input, trace_id)

    def _create_run(self, payload: dict[str, JSON]) -> str:
        if self.project_id is None:
            raise CaptureError("project_id is required to create a run")
        if self.status.closed:
            raise CaptureError("capture client is closed")
        # Resource creation is deliberately not automatically retried; callers can
        # resume an existing run, and event batches have their own bounded retries.
        response = self._transport.post(
            f"/projects/{self.project_id}/runs", encode(payload), uuid7(),
            self._exporter.config.request_timeout,
        )
        if response.status != 201:
            raise CaptureError(f"run creation HTTP status {response.status}")
        try:
            result = json.loads(response.body)
            if not isinstance(result["id"], str):
                raise ValueError
            return check_uuid7(result["id"])
        except (ValueError, KeyError, TypeError):
            raise CaptureError("invalid run creation acknowledgement") from None


class AnvilClient(_ClientBase):
    def flush(self, timeout: float = 5.0) -> None:
        self._exporter.flush(timeout)

    def close(self, timeout: float = 5.0) -> None:
        self._exporter.close(timeout)

    def __enter__(self) -> AnvilClient:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        try:
            self.close()
        except CaptureError:
            if exc is None:
                raise
            warnings.warn("capture cleanup failed; original exception preserved", CaptureWarning, stacklevel=2)


class AsyncAnvilClient(_ClientBase):
    async def flush(self, timeout: float = 5.0) -> None:
        await asyncio.to_thread(self._exporter.flush, timeout)

    async def close(self, timeout: float = 5.0) -> None:
        await asyncio.to_thread(self._exporter.close, timeout)

    async def __aenter__(self) -> AsyncAnvilClient:
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        try:
            await self.close()
        except CaptureError:
            if exc is None:
                raise
            warnings.warn("capture cleanup failed; original exception preserved", CaptureWarning, stacklevel=2)


class RunCapture:
    def __init__(
        self, client: _ClientBase, agent_name: str, run_id: str | None, adapter: str,
        adapter_version: str, input: JSON, trace_id: str | None,
    ) -> None:
        self.client = client
        self.run_id = check_uuid7(run_id) if run_id is not None else None
        self.stream_id = uuid7()
        self.trace_id = trace_id if trace_id is not None else secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-f]{32}", self.trace_id):
            raise ValueError("trace_id must be 32 lowercase hex characters")
        self._sequence = Sequence()
        self._started = False
        self._ended = False
        self._token: contextvars.Token[RunCapture | None] | None = None
        self._scope_token: contextvars.Token[tuple[str, str, tuple[str, ...]] | None] | None = None
        self._root_scope = (secrets.token_hex(8), uuid7(), ())
        # Redact immediately: the context must not retain raw input before entry.
        self._start = client.redaction.apply("run.started", {
            "agent_name": agent_name, "adapter": adapter, "adapter_version": adapter_version,
        } | ({"input": input} if input is not None else {}))
        self._check_payload("run.started", self._start)

    def _event(self, event_type: EventType, payload: dict[str, JSON], sequence: int) -> Event:
        scope = _SCOPE.get() if _CURRENT.get() is self else None
        span_id, invocation_id, parents = scope or self._root_scope
        return {
            "schema_version": "1", "event_id": uuid7(), "run_id": self.run_id or uuid7(),
            "stream_id": self.stream_id, "sequence": sequence, "type": event_type,
            "occurred_at": timestamp(), "trace_id": self.trace_id, "span_id": span_id,
            "parent_event_ids": list(parents), "invocation_id": invocation_id, "payload": payload,
        }

    def _check_payload(self, event_type: EventType, payload: dict[str, JSON]) -> None:
        try:
            validate("event", encode(self._event(event_type, payload, 0)))
        except ValidationError:
            raise CaptureError("capture payload violates frozen event schema v1") from None

    def _begin(self) -> RunCapture:
        if self._started or self._ended:
            raise CaptureError("run context cannot be reused")
        if self.client.status.closed:
            raise CaptureError("capture client is closed")
        self._started = True
        self._token = _CURRENT.set(self)
        self._scope_token = _SCOPE.set(self._root_scope)
        try:
            self._emit_clean("run.started", self._start)
        except CaptureError:
            _SCOPE.reset(self._scope_token)
            _CURRENT.reset(self._token)
            self._ended = True
            raise
        return self

    def __enter__(self) -> RunCapture:
        if self._started or self._ended:
            raise CaptureError("run context cannot be reused")
        if self.run_id is None:
            self.run_id = self.client._create_run({
                key: value for key, value in self._start.items()
                if key in {"agent_name", "adapter", "adapter_version"}
            } | {"trace_id": self.trace_id})
        return self._begin()

    async def __aenter__(self) -> RunCapture:
        if self._started or self._ended:
            raise CaptureError("run context cannot be reused")
        if self.run_id is None:
            self.run_id = await asyncio.to_thread(self.client._create_run, {
                key: value for key, value in self._start.items()
                if key in {"agent_name", "adapter", "adapter_version"}
            } | {"trace_id": self.trace_id})
        return self._begin()

    def _emit_clean(self, event_type: EventType, payload: dict[str, JSON]) -> str:
        event = self._event(event_type, payload, self._sequence.next())
        if not self.client._exporter.submit(event, self._sequence):
            warnings.warn("capture event dropped; capture is incomplete", CaptureWarning, stacklevel=3)
        return event["event_id"]

    def emit(self, event_type: EventType, payload: Mapping[str, JSON]) -> str:
        if not self._started or self._ended:
            raise CaptureError("capture requires an active run")
        cleaned = self.client.redaction.apply(event_type, payload)
        self._check_payload(event_type, cleaned)
        return self._emit_clean(event_type, cleaned)

    def message(self, role: Literal["user", "assistant", "system", "tool"], content: JSON) -> str:
        return self.emit("message.created", {"role": role, "content": content})

    def model_requested(self, model: str, request: dict[str, JSON], *, provider: str = "") -> str:
        return self.emit("model.requested", {"model": model, "request": request, "provider": provider})

    def model_chunk(self, index: int, chunk: JSON) -> str:
        return self.emit("model.chunk", {"chunk_index": index, "chunk": chunk})

    def model_completed(self, model: str, response: JSON) -> str:
        return self.emit("model.completed", {"model": model, "response": response})

    def model_failed(self, model: str) -> str:
        return self.emit("model.failed", {"model": model, "error": {"type": "agent_error", "message": "[REDACTED]"}})

    def tool_requested(self, name: str, arguments: JSON) -> str:
        return self.emit("tool.requested", {"tool_name": name, "arguments": arguments})

    def tool_completed(self, name: str, arguments: JSON, result: JSON) -> str:
        return self.emit("tool.completed", {"tool_name": name, "arguments": arguments, "result": result})

    def tool_failed(self, name: str, arguments: JSON) -> str:
        return self.emit("tool.failed", {"tool_name": name, "arguments": arguments, "error": {"type": "agent_error", "message": "[REDACTED]"}})

    def checkpoint(self, state: JSON) -> str:
        return self.emit("state.checkpoint", {"state": state})

    @contextmanager
    def span(self, *, parent_event_ids: tuple[str, ...] = ()) -> Iterator[str]:
        if _CURRENT.get() is not self or self._ended:
            raise CaptureError("span requires the current active run")
        for parent in parent_event_ids:
            check_uuid7(parent)
        invocation_id = uuid7()
        token = _SCOPE.set((secrets.token_hex(8), invocation_id, parent_event_ids))
        try:
            yield invocation_id
        finally:
            _SCOPE.reset(token)

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        try:
            if exc is None:
                self.emit("run.completed", {"outcome": "succeeded"})
            else:
                self.emit("run.failed", {"error": {"type": "agent_error", "message": "[REDACTED]"}})
        except CaptureError:
            if exc is None:
                raise
            warnings.warn("terminal capture failed; original exception preserved", CaptureWarning, stacklevel=2)
        finally:
            self._ended = True
            if self._scope_token is not None:
                _SCOPE.reset(self._scope_token)
            if self._token is not None:
                _CURRENT.reset(self._token)

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        self.__exit__(exc_type, exc, tb)
