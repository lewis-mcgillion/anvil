"""Causal alignment of two runs by logical identity (ANVIL-044).

The cases here are the ones that defeat positional comparison: parallel
branches that finish in a different order, retries that duplicate a call, and
edits that change or move a whole branch.
"""

from __future__ import annotations

import random

import pytest

from anvil_server import playback
from anvil_server.divergence import alignment as al

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


def graph_of(events: list[dict]) -> al.InvocationGraph:
    return al.build_graph(events)


class TestVocabulary:
    def test_alignment_and_playback_share_event_types(self) -> None:
        # Divergence must not understand a vocabulary playback does not, or a
        # new event type would be aligned but never projected.
        assert al._REQUEST_TYPES is playback._REQUEST_TYPES
        assert al._RESOLUTION_TYPES is playback._RESOLUTION_TYPES


class TestGraph:
    def test_identity_follows_the_causal_chain(self) -> None:
        graph = graph_of(baseline_run())
        identities = [invocation.identity for invocation in graph.invocations]
        assert identities == [
            ("tool:search#0",),
            ("tool:search#0", "tool:fetch#0"),
        ]

    def test_resolution_is_recorded(self) -> None:
        graph = graph_of(baseline_run())
        statuses = {invocation.name: invocation.status for invocation in graph.invocations}
        assert statuses == {"search": "completed", "fetch": "completed"}

    def test_failure_is_a_status_not_a_gap(self) -> None:
        run = Run()
        run.tool("search", parents=(run.start(),), failed=True)
        graph = graph_of(run.events)
        assert graph.invocations[0].status == "failed"
        assert graph.invocations[0].error == {"message": "boom"}
        assert graph.gaps == ()

    def test_unresolved_invocation_is_a_gap(self) -> None:
        run = Run()
        run.tool("search", parents=(run.start(),), unresolved=True)
        graph = graph_of(run.events)
        assert graph.invocations[0].status == "pending"
        assert any("never resolved" in gap for gap in graph.gaps)

    def test_resolution_without_a_request_is_a_gap(self) -> None:
        run = Run()
        start = run.start()
        run.event(
            "orphan",
            "tool.completed",
            parents=(start,),
            invocation="inv-ghost",
            payload={"tool_name": "search", "arguments": {}, "result": {}},
        )
        graph = graph_of(run.events)
        assert graph.invocations == ()
        assert any("without a matching request" in gap for gap in graph.gaps)

    def test_graph_is_independent_of_input_order(self) -> None:
        events = baseline_run()
        reference = graph_of(events)
        shuffled = list(events)
        for seed in range(12):
            random.Random(seed).shuffle(shuffled)
            assert graph_of(shuffled) == reference

    def test_unorderable_run_cannot_be_aligned(self) -> None:
        run = Run()
        run.event("e0", "run.started", parents=("nope",))
        with pytest.raises(playback.PlaybackError):
            graph_of(run.events)


class TestParallelReordering:
    """Independent branches that finish in a different order are not changes."""

    @staticmethod
    def parallel(first: str, second: str) -> list[dict]:
        run = Run()
        start = run.start()
        run.tool(first, stream=STREAM_A, parents=(start,))
        run.tool(second, stream=STREAM_B, parents=(start,))
        return run.events

    def test_reordered_parallel_branches_align(self) -> None:
        left = graph_of(self.parallel("search", "fetch"))
        right = graph_of(self.parallel("fetch", "search"))
        result = al.align(left, right)
        assert [pair.status for pair in result.pairs] == ["matched", "matched"]
        assert result.is_complete

    def test_reordering_does_not_change_the_structure_hash(self) -> None:
        left = al.align(
            graph_of(self.parallel("search", "fetch")),
            graph_of(self.parallel("search", "fetch")),
        )
        right = al.align(
            graph_of(self.parallel("search", "fetch")),
            graph_of(self.parallel("fetch", "search")),
        )
        assert left.structure_hash() == right.structure_hash()

    def test_evidence_still_cites_the_events_that_actually_ran(self) -> None:
        # The structure hash is stable, but the report must not pretend the
        # two runs emitted the same events.
        left = al.align(
            graph_of(self.parallel("search", "fetch")),
            graph_of(self.parallel("search", "fetch")),
        )
        right = al.align(
            graph_of(self.parallel("search", "fetch")),
            graph_of(self.parallel("fetch", "search")),
        )
        assert left.alignment_hash() != right.alignment_hash()


class TestDuplicates:
    """Repeated identical calls align one-to-one, by ordinal."""

    @staticmethod
    def retries(count: int) -> list[dict]:
        run = Run()
        start = run.start()
        for _ in range(count):
            run.tool("search", parents=(start,), arguments={"q": "hat"})
        return run.events

    def test_equal_duplicate_counts_align_pairwise(self) -> None:
        result = al.align(graph_of(self.retries(3)), graph_of(self.retries(3)))
        assert [pair.status for pair in result.pairs] == ["matched"] * 3
        assert [pair.identity for pair in result.pairs] == [
            ("tool:search#0",),
            ("tool:search#1",),
            ("tool:search#2",),
        ]

    def test_an_extra_retry_does_not_corrupt_the_others(self) -> None:
        result = al.align(graph_of(self.retries(2)), graph_of(self.retries(3)))
        statuses = [pair.status for pair in result.pairs]
        assert statuses == ["matched", "matched", "extra"]
        assert result.extra[0].identity == ("tool:search#2",)
        assert not result.is_complete

    def test_a_dropped_retry_is_missing(self) -> None:
        result = al.align(graph_of(self.retries(3)), graph_of(self.retries(2)))
        assert [pair.status for pair in result.pairs] == ["matched", "matched", "missing"]
        assert result.missing[0].identity == ("tool:search#2",)


class TestChangedBranches:
    def test_a_renamed_call_is_missing_and_extra_not_matched(self) -> None:
        run = Run()
        search = run.tool("search", parents=(run.start(),), arguments={"q": "hat"}, result={"hits": 2})
        run.tool("lookup", parents=(search,), arguments={"id": 1}, result={"ok": True})
        result = al.align(graph_of(baseline_run()), graph_of(run.events))
        by_status = {pair.status for pair in result.pairs}
        assert by_status == {"matched", "missing", "extra"}
        assert result.missing[0].identity == ("tool:search#0", "tool:fetch#0")
        assert result.extra[0].identity == ("tool:search#0", "tool:lookup#0")

    def test_a_moved_branch_is_localized_to_its_new_parent(self) -> None:
        # "fetch" moves from under "search" to the run root.
        run = Run()
        start = run.start()
        run.tool("search", parents=(start,), arguments={"q": "hat"}, result={"hits": 2})
        run.tool("fetch", parents=(start,), arguments={"id": 1}, result={"ok": True})
        result = al.align(graph_of(baseline_run()), graph_of(run.events))
        assert result.missing[0].identity == ("tool:search#0", "tool:fetch#0")
        assert result.extra[0].identity == ("tool:fetch#0",)
        assert len(result.matched) == 1

    def test_identical_runs_align_completely(self) -> None:
        result = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        assert result.is_complete
        assert len(result.matched) == 2
        assert result.reasons == ()

    def test_changed_arguments_still_align(self) -> None:
        # Alignment answers "which step is this", not "did it differ".
        run = Run()
        search = run.tool("search", parents=(run.start(),), arguments={"q": "SCARF"}, result={"hits": 0})
        run.tool("fetch", parents=(search,), arguments={"id": 9}, result={"ok": False})
        result = al.align(graph_of(baseline_run()), graph_of(run.events))
        assert result.is_complete
        assert result.matched[0].baseline.arguments != result.matched[0].candidate.arguments


class TestFailsClosed:
    def test_a_capture_gap_makes_the_alignment_partial(self) -> None:
        run = Run()
        start = run.start()
        search = run.tool("search", parents=(start,), arguments={"q": "hat"}, result={"hits": 2})
        run.tool("fetch", parents=(search,), arguments={"id": 1}, result={"ok": True})
        run.tool("audit", parents=(start,), unresolved=True)
        result = al.align(graph_of(baseline_run()), graph_of(run.events))
        assert result.partial
        assert not result.is_complete
        assert any("never resolved" in reason for reason in result.reasons)
        assert all(reason.startswith(("baseline: ", "candidate: ")) for reason in result.reasons)

    def test_an_unnamed_invocation_is_ambiguous_not_matched(self) -> None:
        def anonymous() -> list[dict]:
            run = Run()
            start = run.start()
            request = run.event(
                "ar", "tool.requested", parents=(start,), invocation="inv-a", payload={"arguments": {}}
            )
            run.event(
                "ac",
                "tool.completed",
                parents=(request,),
                invocation="inv-a",
                payload={"arguments": {}, "result": {}},
            )
            return run.events

        result = al.align(graph_of(anonymous()), graph_of(anonymous()))
        assert [pair.status for pair in result.pairs] == ["ambiguous"]
        assert "identity cannot be established" in result.ambiguous[0].reason
        assert not result.is_complete

    def test_differing_kinds_at_one_identity_are_ambiguous(self) -> None:
        left = al.InvocationGraph(
            (
                al.Invocation(
                    identity=("x",),
                    kind="tool",
                    name="search",
                    ordinal=0,
                    status="completed",
                    event_id="a",
                    invocation_id=None,
                ),
            )
        )
        right = al.InvocationGraph(
            (
                al.Invocation(
                    identity=("x",),
                    kind="model",
                    name="search",
                    ordinal=0,
                    status="completed",
                    event_id="b",
                    invocation_id=None,
                ),
            )
        )
        result = al.align(left, right)
        assert result.ambiguous[0].reason == "kind differs at the same identity"

    def test_align_rejects_values_that_are_not_graphs(self) -> None:
        graph = graph_of(baseline_run())
        with pytest.raises(al.AlignmentError):
            al.align(graph, [])  # type: ignore[arg-type]
        with pytest.raises(al.AlignmentError):
            al.align(None, graph)  # type: ignore[arg-type]

    def test_empty_runs_align_trivially_but_report_nothing_matched(self) -> None:
        empty = graph_of(Run().events + [
            {
                "event_id": "e0",
                "run_id": RUN,
                "stream_id": STREAM_A,
                "sequence": 0,
                "type": "run.started",
                "occurred_at": "2026-01-01T00:00:00.000Z",
                "parent_event_ids": [],
                "invocation_id": None,
                "payload": {},
            }
        ])
        result = al.align(empty, empty)
        assert result.pairs == ()
        assert result.is_complete
        assert len(empty) == 0


class TestReportShape:
    def test_alignment_is_deterministic_and_hashable(self) -> None:
        first = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        second = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        assert first.describe() == second.describe()
        assert first.alignment_hash() == second.alignment_hash()
        assert first.structure_hash() == second.structure_hash()

    def test_a_changed_shape_changes_the_structure_hash(self) -> None:
        same = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        run = Run()
        run.tool("search", parents=(run.start(),), arguments={"q": "hat"}, result={"hits": 2})
        shorter = al.align(graph_of(baseline_run()), graph_of(run.events))
        assert same.structure_hash() != shorter.structure_hash()

    def test_report_records_the_version_and_both_sides(self) -> None:
        result = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        report = result.describe()
        assert report["alignment_version"] == al.ALIGNMENT_VERSION
        pair = report["pairs"][0]
        assert pair["baseline"]["name"] == "search"
        assert pair["candidate"]["name"] == "search"
        assert pair["identity"] == ["tool:search#0"]

    def test_parents_are_reported_before_their_children(self) -> None:
        result = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        depths = [len(pair.identity) for pair in result.pairs]
        assert depths == sorted(depths)

    def test_pairs_and_invocations_are_immutable(self) -> None:
        result = al.align(graph_of(baseline_run()), graph_of(baseline_run()))
        with pytest.raises(Exception):
            result.pairs[0].status = "missing"  # type: ignore[misc]
        with pytest.raises(Exception):
            result.matched[0].baseline.name = "other"  # type: ignore[misc]

    def test_describe_does_not_leak_arguments_or_results(self) -> None:
        # Reports are structural. Payloads are compared elsewhere, under the
        # redaction policy, so identity reports carry no captured content.
        report = al.align(graph_of(baseline_run()), graph_of(baseline_run())).describe()
        assert "hat" not in repr(report)
        assert "hits" not in repr(report)


class TestConcurrentIdenticalCalls:
    """Identical calls on parallel branches cannot be told apart.

    `docs/architecture.md` requires parallel identical calls to have distinct
    causal branch identities or to produce an ambiguity. They have no causal
    order, so their ordinals come from a tie-break on stream identity — stable
    within a run, meaningless across runs, because the stream ids differ.
    """

    @staticmethod
    def fan_out(result_a: dict, result_b: dict) -> list[dict]:
        run = Run()
        start = run.start()
        run.tool("search", stream=STREAM_A, parents=(start,), arguments={"q": "hat"}, result=result_a)
        run.tool("search", stream=STREAM_B, parents=(start,), arguments={"q": "hat"}, result=result_b)
        return run.events

    def test_concurrent_siblings_are_flagged(self) -> None:
        graph = graph_of(self.fan_out({"hits": 1}, {"hits": 2}))
        assert [invocation.concurrent for invocation in graph.invocations] == [True, True]

    def test_sequential_duplicates_are_not_flagged(self) -> None:
        run = Run()
        start = run.start()
        run.tool("search", parents=(start,), arguments={"q": "hat"})
        run.tool("search", parents=(start,), arguments={"q": "hat"})
        graph = graph_of(run.events)
        assert [invocation.concurrent for invocation in graph.invocations] == [False, False]

    def test_alignment_refuses_to_pair_them(self) -> None:
        result = al.align(
            graph_of(self.fan_out({"hits": 1}, {"hits": 2})),
            graph_of(self.fan_out({"hits": 2}, {"hits": 1})),
        )
        assert [pair.status for pair in result.pairs] == ["ambiguous", "ambiguous"]
        assert "not an identity" in result.ambiguous[0].reason
        assert not result.is_complete

    def test_differently_named_parallel_branches_stay_exact(self) -> None:
        run = Run()
        start = run.start()
        run.tool("search", stream=STREAM_A, parents=(start,))
        run.tool("fetch", stream=STREAM_B, parents=(start,))
        graph = graph_of(run.events)
        assert [invocation.concurrent for invocation in graph.invocations] == [False, False]
        assert al.align(graph, graph).confidence == "exact"


class TestConfidence:
    """The published `alignment_confidence` vocabulary."""

    def test_identical_runs_are_exact(self) -> None:
        assert al.align(graph_of(baseline_run()), graph_of(baseline_run())).confidence == "exact"

    def test_a_capture_gap_is_partial(self) -> None:
        run = Run()
        start = run.start()
        search = run.tool("search", parents=(start,), arguments={"q": "hat"}, result={"hits": 2})
        run.tool("fetch", parents=(search,), arguments={"id": 1}, result={"ok": True})
        run.tool("audit", parents=(start,), unresolved=True)
        result = al.align(graph_of(baseline_run()), graph_of(run.events))
        assert result.confidence == "partial"

    def test_ambiguity_outranks_incompleteness(self) -> None:
        run = Run()
        start = run.start()
        run.tool("search", stream=STREAM_A, parents=(start,))
        run.tool("search", stream=STREAM_B, parents=(start,))
        run.tool("audit", parents=(start,), unresolved=True)
        graph = graph_of(run.events)
        result = al.align(graph, graph)
        assert result.partial
        assert result.confidence == "ambiguous"

    def test_confidence_is_one_of_the_published_values(self) -> None:
        published = {"exact", "partial", "ambiguous"}
        run = Run()
        run.tool("search", parents=(run.start(),))
        for left, right in (
            (baseline_run(), baseline_run()),
            (baseline_run(), run.events),
            (run.events, baseline_run()),
        ):
            result = al.align(graph_of(left), graph_of(right))
            assert result.confidence in published
            assert result.describe()["alignment_confidence"] == result.confidence

    def test_only_an_exact_alignment_is_complete(self) -> None:
        # Nothing may pass on incomplete or ambiguous evidence.
        run = Run()
        run.tool("search", parents=(run.start(),))
        for left, right in (
            (baseline_run(), run.events),
            (run.events, baseline_run()),
        ):
            result = al.align(graph_of(left), graph_of(right))
            assert result.is_complete == (result.confidence == "exact" and not result.missing and not result.extra)
            assert not result.is_complete
