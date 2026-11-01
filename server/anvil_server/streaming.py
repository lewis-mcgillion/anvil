"""Streaming presentation reconstruction and a controllable playback clock.

Streaming output is captured as discrete ``model.chunk`` events. This module
reassembles those chunks into the text a user originally saw and drives their
presentation over a caller-supplied clock.

The central invariant is that the clock is presentation-only. Playback speed,
pausing and stepping change *when* a frame is surfaced, never *what* it
contains: the frame sequence, its text and the resulting transcript are
identical at any speed. Tests pin this with a fake clock so the guarantee is
checked rather than asserted in prose.

Captured ``occurred_at`` values are used solely to derive relative presentation
offsets within a single stream, clamped to be non-decreasing. They are never
used for ordering, which comes from :func:`anvil_server.playback.order_events`.

Nothing here executes agent code, tools or models. The clock protocol is
synchronous and owns no resources; :meth:`PlaybackSession.close` marks a session
finished so a cancelled presentation stops yielding frames, and callers own any
surrounding timeout.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from .playback import order_events

__all__ = [
    "StreamingError",
    "Frame",
    "Clock",
    "ManualClock",
    "reconstruct_streams",
    "PlaybackSession",
]

MAX_SPEED = 1000.0
MIN_SPEED = 0.01


class StreamingError(ValueError):
    """Raised when streaming events cannot be reconstructed unambiguously."""


class Clock(Protocol):
    """Minimal clock surface used by :class:`PlaybackSession`."""

    def now(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, seconds: float) -> None:
        """Advance by ``seconds``; may return immediately."""


class ManualClock:
    """Deterministic fake clock for tests and headless stepping."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Cannot sleep for a negative duration")
        self.sleeps.append(seconds)
        self._now += seconds


class Frame:
    """One presentation step of a reconstructed stream.

    ``offset`` is the capture-relative presentation time in seconds at speed
    1.0. ``text`` is the cumulative text after this chunk, and ``delta`` is the
    chunk that produced it.
    """

    __slots__ = ("invocation_id", "event_id", "index", "delta", "text", "offset", "final")

    def __init__(
        self,
        invocation_id: str,
        event_id: str,
        index: int,
        delta: str,
        text: str,
        offset: float,
        final: bool,
    ) -> None:
        self.invocation_id = invocation_id
        self.event_id = event_id
        self.index = index
        self.delta = delta
        self.text = text
        self.offset = offset
        self.final = final

    def as_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "event_id": self.event_id,
            "index": self.index,
            "delta": self.delta,
            "text": self.text,
            "offset": self.offset,
            "final": self.final,
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Frame) and self.as_dict() == other.as_dict()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Frame({self.as_dict()!r})"


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def _new_record(invocation: str, model: Any = None) -> dict[str, Any]:
    return {
        "invocation_id": invocation,
        "model": model,
        "text": "",
        "frames": [],
        "truncated": False,
        "status": "pending",
        "_chunks": [],
    }


def reconstruct_streams(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble ``model.chunk`` events into per-invocation presentations.

    Returns one record per streamed invocation in deterministic causal order,
    each containing the reconstructed ``text``, its ``frames`` and whether the
    stream was ``truncated`` (chunks present with no terminal resolution).

    Raises :class:`StreamingError` if a chunk carries no invocation identity or
    a non-string ``chunk`` payload, because the reconstruction would otherwise
    be guesswork.
    """
    ordered = order_events(events)
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for event in ordered:
        event_type = str(event["type"])
        invocation = event.get("invocation_id")
        payload = event.get("payload") or {}

        if event_type == "model.requested":
            if invocation is None:
                continue
            invocation = str(invocation)
            if invocation not in records:
                order.append(invocation)
                records[invocation] = _new_record(invocation, payload.get("model"))
            continue

        if event_type == "model.chunk":
            if invocation is None:
                raise StreamingError(
                    f"Event {event['event_id']} is a model.chunk without an invocation_id"
                )
            invocation = str(invocation)
            # ``chunk`` is the field published in schemas/v1 and emitted by both
            # the capture SDK and the ADK adapter.
            delta = payload.get("chunk")
            if not isinstance(delta, str):
                raise StreamingError(
                    f"Event {event['event_id']} has a non-string chunk payload"
                )
            record = records.get(invocation)
            if record is None:
                order.append(invocation)
                record = records[invocation] = _new_record(invocation)
            record["_chunks"].append(
                (str(event["event_id"]), delta, _parse_time(event.get("occurred_at")))
            )
            continue

        if event_type in {"model.completed", "model.failed"} and invocation is not None:
            record = records.get(str(invocation))
            if record is not None:
                record["status"] = "completed" if event_type == "model.completed" else "failed"

    results: list[dict[str, Any]] = []
    for invocation in order:
        record = records[invocation]
        chunks = record.pop("_chunks")
        base = next((moment for _, _, moment in chunks if moment is not None), None)
        text = ""
        previous_offset = 0.0
        frames: list[Frame] = []
        for index, (event_id, delta, moment) in enumerate(chunks):
            text += delta
            if base is None or moment is None:
                offset = previous_offset
            else:
                offset = max(moment - base, previous_offset)
            previous_offset = offset
            frames.append(
                Frame(invocation, event_id, index, delta, text, offset, index == len(chunks) - 1)
            )
        record["text"] = text
        record["frames"] = frames
        record["truncated"] = bool(chunks) and record["status"] == "pending"
        results.append(record)
    return results


class PlaybackSession:
    """Drive reconstructed frames over a controllable, presentation-only clock.

    ``speed`` scales the delay between frames; pausing suspends emission and
    :meth:`step` emits exactly one frame regardless of pause state. None of
    these controls can alter frame content or order, which is fixed at
    construction time.
    """

    def __init__(
        self,
        frames: Sequence[Frame],
        *,
        clock: Clock | None = None,
        speed: float = 1.0,
        paused: bool = False,
    ) -> None:
        self._frames = list(frames)
        self._clock = clock if clock is not None else ManualClock()
        self._position = 0
        self._paused = bool(paused)
        self._closed = False
        self._elapsed = 0.0
        self.speed = speed

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        value = float(value)
        if not MIN_SPEED <= value <= MAX_SPEED:
            raise ValueError(f"Speed must be between {MIN_SPEED} and {MAX_SPEED}")
        self._speed = value

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> int:
        return self._position

    @property
    def finished(self) -> bool:
        return self._closed or self._position >= len(self._frames)

    @property
    def simulated_elapsed(self) -> float:
        """Presentation time consumed so far; simulated, not measured."""
        return self._elapsed

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def seek(self, position: int) -> None:
        """Jump to ``position`` without emitting intermediate frames."""
        if position < 0 or position > len(self._frames):
            raise IndexError(f"Position {position} is outside the frame sequence")
        self._position = position

    def close(self) -> None:
        """Mark the session finished so no further frames are produced."""
        self._closed = True

    def __enter__(self) -> "PlaybackSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def step(self) -> Frame | None:
        """Emit exactly one frame, honouring the clock but ignoring pause."""
        if self._closed or self._position >= len(self._frames):
            return None
        frame = self._frames[self._position]
        previous = self._frames[self._position - 1].offset if self._position else 0.0
        delay = max(frame.offset - previous, 0.0) / self._speed
        if delay:
            self._clock.sleep(delay)
            self._elapsed += delay
        self._position += 1
        return frame

    def play(self) -> Iterator[Frame]:
        """Yield frames until the session is paused, closed or exhausted."""
        while not self._paused:
            frame = self.step()
            if frame is None:
                return
            yield frame

    def drain(self) -> list[Frame]:
        """Resume and return every remaining frame."""
        self.resume()
        return list(self.play())

    def transcript(self) -> str:
        """Return the text presented up to the current position."""
        if self._position == 0:
            return ""
        return self._frames[self._position - 1].text
