"""Shared run fixtures for comparison tests.

Playback requires each stream's sequence numbers to be contiguous, so fixtures
allocate them rather than hard-coding values that drift as cases change.
"""

from __future__ import annotations

RUN = "01900000-0000-7000-8000-00000000r001"
STREAM_A = "01900000-0000-7000-8000-0000000000sa"
STREAM_B = "01900000-0000-7000-8000-0000000000sb"


class Run:
    """Builds a run with contiguous per-stream sequence numbers.

    Playback requires each stream's sequences to be gap-free, so fixtures
    allocate them rather than hard-coding numbers that drift as cases change.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._next: dict[str, int] = {}
        self._calls = 0

    def event(
        self,
        event_id: str,
        event_type: str,
        *,
        stream: str = STREAM_A,
        parents: tuple[str, ...] = (),
        invocation: str | None = None,
        payload: dict | None = None,
    ) -> str:
        sequence = self._next.get(stream, 0)
        self._next[stream] = sequence + 1
        self.events.append(
            {
                "event_id": event_id,
                "run_id": RUN,
                "stream_id": stream,
                "sequence": sequence,
                "type": event_type,
                "occurred_at": "2026-01-01T00:00:00.000Z",
                "parent_event_ids": list(parents),
                "invocation_id": invocation,
                "payload": payload or {},
            }
        )
        return event_id

    def start(self) -> str:
        return self.event("e0", "run.started")

    def tool(
        self,
        name: str,
        *,
        stream: str = STREAM_A,
        parents: tuple[str, ...] = (),
        arguments: dict | None = None,
        result: dict | None = None,
        failed: bool = False,
        unresolved: bool = False,
    ) -> str:
        """Append a tool invocation and return its resolution event id."""
        self._calls += 1
        prefix = f"c{self._calls}"
        arguments = arguments or {}
        request = self.event(
            f"{prefix}r",
            "tool.requested",
            stream=stream,
            parents=parents,
            invocation=f"inv-{prefix}",
            payload={"tool_name": name, "arguments": arguments},
        )
        if unresolved:
            return request
        payload = {"tool_name": name, "arguments": arguments}
        if failed:
            payload["error"] = {"message": "boom"}
        else:
            payload["result"] = result or {}
        return self.event(
            f"{prefix}c",
            "tool.failed" if failed else "tool.completed",
            stream=stream,
            parents=(request,),
            invocation=f"inv-{prefix}",
            payload=payload,
        )


def baseline_run() -> list[dict]:
    run = Run()
    start = run.start()
    search = run.tool("search", parents=(start,), arguments={"q": "hat"}, result={"hits": 2})
    fetch = run.tool("fetch", parents=(search,), arguments={"id": 1}, result={"ok": True})
    run.event("z", "run.completed", parents=(fetch,))
    return run.events
