"""Single-worker exporter with bounded resident bytes and explicit loss evidence."""

from __future__ import annotations

import json
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass

from ._transport import CaptureError, CaptureIncomplete, Transport, acknowledged, retry_after
from ._types import Event, Sequence, timestamp, uuid7


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ExporterConfig:
    max_events: int = 2000
    max_bytes: int = 16 * 1024 * 1024
    batch_events: int = 500
    batch_bytes: int = 4 * 1024 * 1024
    request_timeout: float = 1.0
    max_attempts: int = 3
    max_retry_delay: float = 5.0

    def __post_init__(self) -> None:
        for value in (self.max_events, self.max_bytes, self.batch_events, self.batch_bytes, self.max_attempts):
            if type(value) is not int or value < 1:
                raise ValueError("exporter limits must be positive integers")
        if self.batch_events > 500 or not 1024 <= self.batch_bytes <= 4 * 1024 * 1024:
            raise ValueError("batch limits exceed the v1 contract")
        for value in (self.request_timeout, self.max_retry_delay):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("exporter timeouts must be finite and positive")


@dataclass(frozen=True)
class ExportStatus:
    buffered_events: int
    buffered_bytes: int
    dropped_events: int
    failed_batches: int
    pending_gaps: int
    closed: bool

    @property
    def incomplete(self) -> bool:
        return bool(self.dropped_events or self.failed_batches or (self.closed and (self.buffered_events or self.pending_gaps)))


@dataclass(frozen=True)
class _Entry:
    event: Event
    wire: bytes
    sequence: Sequence


class Exporter:
    def __init__(self, transport: Transport, config: ExporterConfig = ExporterConfig()) -> None:
        self._transport, self.config = transport, config
        self._condition = threading.Condition()
        self._queue: deque[_Entry] = deque()
        self._gaps: dict[tuple[str, str], _Entry] = {}
        self._events = self._bytes = self._dropped = self._failed = 0
        self._active = False
        self._closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._work, name="anvil-exporter", daemon=True)
        self._thread.start()

    @property
    def status(self) -> ExportStatus:
        with self._condition:
            return ExportStatus(self._events, self._bytes, self._dropped, self._failed, len(self._gaps), self._closed)

    def _gap(self, event: Event, reason: str, sequence: Sequence) -> None:
        self._dropped += 1
        key = (event["run_id"], event["stream_id"])
        if key in self._gaps:
            previous = self._gaps[key].event
            count = previous["payload"]["expected_count"]
            if not isinstance(count, int):
                raise AssertionError("invalid internal gap count")
            previous["payload"]["expected_count"] = count + 1
            self._gaps[key] = _Entry(previous, encode(previous), sequence)
        elif len(self._gaps) < min(self.config.max_events, 128):
            gap: Event = {
                **event, "event_id": uuid7(), "occurred_at": timestamp(), "sequence": sequence.next(),
                "type": "capture.gap", "parent_event_ids": [],
                "payload": {
                    "after_sequence": event["sequence"] - 1, "reason": reason,
                    "expected_count": 1, "redactions": [], "replay_safe": False,
                },
            }
            self._gaps[key] = _Entry(gap, encode(gap), sequence)
        self._condition.notify_all()

    def submit(self, event: Event, sequence: Sequence) -> bool:
        # Serialize first so callers cannot mutate queued evidence after admission.
        wire = encode(event)
        snapshot: Event = json.loads(wire)
        with self._condition:
            if self._closed:
                raise CaptureError("capture exporter is closed")
            if len(wire) > 256 * 1024 or len(wire) + 13 > self.config.batch_bytes:
                self._gap(snapshot, "size_limit", sequence)
                return False
            if self._events >= self.config.max_events or self._bytes + len(wire) > self.config.max_bytes:
                self._gap(snapshot, "buffer_overflow", sequence)
                return False
            self._queue.append(_Entry(snapshot, wire, sequence))
            self._events += 1
            self._bytes += len(wire)
            self._condition.notify_all()
            return True

    def flush(self, timeout: float = 5.0) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("flush timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._queue or self._gaps or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    raise CaptureIncomplete("capture flush did not drain before deadline")
                self._condition.wait(remaining)
            if self._dropped or self._failed:
                raise CaptureIncomplete("capture incomplete: events were lost or rejected")

    def close(self, timeout: float = 5.0) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        with self._condition:
            self._closed = True
        try:
            self.flush(timeout)
        finally:
            self._stop.set()
            with self._condition:
                self._condition.notify_all()
            self._thread.join(max(0.0, deadline - time.monotonic()))

    def _send(self, entries: list[_Entry]) -> None:
        body = b'{"events":[' + b",".join(e.wire for e in entries) + b"]}"
        key = uuid7()
        for attempt in range(self.config.max_attempts):
            if self._stop.is_set():
                raise CaptureIncomplete("capture export stopped")
            try:
                response = self._transport.post(
                    f"/runs/{entries[0].event['run_id']}/events:batch",
                    body, key, self.config.request_timeout,
                )
            except CaptureError:
                if attempt + 1 == self.config.max_attempts:
                    raise
                delay = min(self.config.max_retry_delay, 0.1 * 2**attempt)
            else:
                if response.status != 429 and response.status < 500:
                    acknowledged(response, len(entries))
                    return
                if attempt + 1 == self.config.max_attempts:
                    raise CaptureError("capture retry budget exhausted")
                delay = max(retry_after(response.headers), 0.1 * 2**attempt + random.uniform(0, 0.05))
                if delay > self.config.max_retry_delay:
                    raise CaptureError("Retry-After exceeds capture retry budget")
            if self._stop.wait(delay):
                raise CaptureIncomplete("capture export stopped")

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._gaps or self._stop.is_set())
                if self._stop.is_set():
                    return
                control = not self._queue
                if control:
                    key = next(iter(self._gaps))
                    entries = [self._gaps.pop(key)]
                else:
                    entries = [self._queue.popleft()]
                    size = len(entries[0].wire) + 13
                    while self._queue and len(entries) < self.config.batch_events:
                        candidate = self._queue[0]
                        if (
                            candidate.event["run_id"] != entries[0].event["run_id"]
                            or candidate.event["stream_id"] != entries[0].event["stream_id"]
                        ):
                            break
                        if size + len(candidate.wire) + 1 > self.config.batch_bytes:
                            break
                        entries.append(self._queue.popleft())
                        size += len(candidate.wire) + 1
                self._active = True
            try:
                sending: list[_Entry] = []
                for entry in entries:
                    event: Event = {**entry.event, "sequence": entry.sequence.export_next()}
                    if control:
                        event["payload"] = {**event["payload"], "after_sequence": event["sequence"] - 1}
                    sending.append(_Entry(event, encode(event), entry.sequence))
                self._send(sending)
            except CaptureError:
                with self._condition:
                    self._failed += 1
                    if not control:
                        for entry in entries:
                            self._gap(entry.event, "exporter_failure", entry.sequence)
            finally:
                with self._condition:
                    if not control:
                        self._events -= len(entries)
                        self._bytes -= sum(len(e.wire) for e in entries)
                    self._active = False
                    self._condition.notify_all()
