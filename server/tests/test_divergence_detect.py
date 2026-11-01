"""Deterministic divergence detection (ANVIL-045).

The central cases are single-mutation fixtures: two runs that are identical
except for exactly one change. Each must produce exactly one finding, in the
right category, carrying both sides as evidence. Anything less is not
actionable; anything more is noise that hides the real change.
"""

from __future__ import annotations

import random

import pytest

from anvil_server.canonical import NormalizationPolicy
from anvil_server.divergence import detect
from runbuilder import RUN, STREAM_A, STREAM_B, Run

BASE = {
    "input": {"task": "find a hat"},
    "agent_name": "shopper",
    "adapter": "adk",
    "prompt": "find me a hat",
    "model": "gpt-test",
    "model_request": {"messages": [{"role": "user", "content": "find me a hat"}]},
    "model_response": {"text": "searching"},
    "tool_name": "search",
    "tool_args": {"q": "hat"},
    "tool_result": {"hits": 2},
    "outcome": "succeeded",
    "output": {"answer": "a hat"},
    "usage": {"input_tokens": 100, "output_tokens": 20},
}


def make_run(**overrides: object) -> list[dict]:
    """Build a complete run, with exactly the overrides applied."""
    spec = {**BASE, **overrides}
    run = Run()
    start = run.event(
        "e0",
        "run.started",
        payload={
            "agent_name": spec["agent_name"],
            "adapter": spec["adapter"],
            "adapter_version": "1",
            "input": spec["input"],
        },
    )
    message = run.event(
        "m0",
        "message.created",
        parents=(start,),
        payload={"role": "user", "content": spec["prompt"]},
    )
    request = run.event(
        "q0",
        "model.requested",
        parents=(message,),
        invocation="inv-model",
        payload={"model": spec["model"], "request": spec["model_request"]},
    )
    model = run.event(
        "q1",
        "model.completed",
        parents=(request,),
        invocation="inv-model",
        payload={"model": spec["model"], "response": spec["model_response"]},
    )

    last = model
    if not spec.get("drop_tool"):
        last = run.tool(
            str(spec["tool_name"]),
            parents=(model,),
            arguments=dict(spec["tool_args"]),  # type: ignore[arg-type]
            result=dict(spec["tool_result"]),  # type: ignore[arg-type]
            failed=bool(spec.get("tool_failed")),
            unresolved=bool(spec.get("unresolved_tool")),
        )
    if spec.get("extra_tool"):
        last = run.tool("audit", parents=(model,), arguments={}, result={})

    if spec.get("gap"):
        run.event(
            "g0",
            "capture.gap",
            parents=(last,),
            payload={"after_sequence": 1, "expected_count": 2, "reason": "buffer_overflow"},
        )

    if spec.get("never_finished"):
        return run.events

    run.event(
        "z0",
        "run.completed",
        parents=(last,),
        payload={
            "outcome": spec["outcome"],
            "output": spec["output"],
            "usage": spec["usage"],
        },
    )
    return run.events


def compare(**overrides: object) -> detect.Comparison:
    return detect.compare(make_run(), make_run(**overrides))


# Each mutation changes exactly one thing, and names the category it must be
# reported under.
SINGLE_MUTATIONS = [
    ("changed_input", {"input": {"task": "find a scarf"}}),
    ("changed_prompt", {"prompt": "find me a scarf"}),
    ("changed_prompt", {"model_request": {"messages": [{"role": "user", "content": "other"}]}}),
    ("changed_model", {"model": "gpt-other"}),
    ("changed_tool_name", {"tool_name": "lookup"}),
    ("changed_tool_arguments", {"tool_args": {"q": "scarf"}}),
    ("changed_output", {"tool_result": {"hits": 9}}),
    ("changed_output", {"model_response": {"text": "thinking"}}),
    ("changed_output", {"output": {"answer": "a scarf"}}),
    ("changed_control_flow", {"outcome": "failed"}),
    ("changed_control_flow", {"agent_name": "other"}),
    ("changed_budget", {"usage": {"input_tokens": 500, "output_tokens": 20}}),
    ("missing_call", {"drop_tool": True}),
    ("extra_call", {"extra_tool": True}),
    ("capture_gap", {"gap": True}),
]


class TestIdenticalRuns:
    def test_a_run_does_not_diverge_from_itself(self) -> None:
        result = detect.compare(make_run(), make_run())
        assert result.findings == ()
        assert result.deterministic == "pass"
        assert result.alignment.confidence == "exact"

    def test_pass_requires_an_exact_alignment(self) -> None:
        result = detect.compare(make_run(), make_run())
        assert result.alignment.is_complete


class TestSingleMutations:
    @pytest.mark.parametrize(
        ("category", "mutation"),
        SINGLE_MUTATIONS,
        ids=[f"{category}:{sorted(mutation)[0]}" for category, mutation in SINGLE_MUTATIONS],
    )
    def test_one_mutation_yields_one_categorized_finding(
        self, category: str, mutation: dict
    ) -> None:
        result = compare(**mutation)
        assert [finding.category for finding in result.findings] == [category]

    @pytest.mark.parametrize(
        ("category", "mutation"),
        SINGLE_MUTATIONS,
        ids=[f"{category}:{sorted(mutation)[0]}" for category, mutation in SINGLE_MUTATIONS],
    )
    def test_every_finding_carries_actionable_evidence(
        self, category: str, mutation: dict
    ) -> None:
        finding = compare(**mutation).findings[0]
        assert finding.detail
        report = finding.describe()
        assert report["category"] == category
        # A capture gap has no two sides to show; everything else must cite both
        # or, for a missing/extra call, the one side that exists.
        if category == "capture_gap":
            assert "baseline" not in report and "candidate" not in report
        elif category == "missing_call":
            assert report["baseline"] and "candidate" not in report
        elif category == "extra_call":
            assert report["candidate"] and "baseline" not in report
        else:
            assert report["baseline"] != report["candidate"]

    @pytest.mark.parametrize(
        ("category", "mutation"),
        SINGLE_MUTATIONS,
        ids=[f"{category}:{sorted(mutation)[0]}" for category, mutation in SINGLE_MUTATIONS],
    )
    def test_a_change_fails_and_a_gap_is_inconclusive(
        self, category: str, mutation: dict
    ) -> None:
        result = compare(**mutation)
        expected = "inconclusive" if category == "capture_gap" else "fail"
        assert result.deterministic == expected

    def test_every_published_category_is_reachable(self) -> None:
        reached = {category for category, _ in SINGLE_MUTATIONS}
        reached.add("changed_error")  # covered in TestErrors
        reached.add("ambiguous_alignment")  # covered in TestFailsClosed
        assert reached == set(detect.CATEGORIES)

    def test_a_rename_does_not_report_its_whole_subtree(self) -> None:
        # Renaming the model moves the tool call underneath it. The report must
        # show the one rename, not a missing and extra call as well.
        result = compare(model="gpt-other")
        assert [finding.category for finding in result.findings] == ["changed_model"]

    def test_a_rename_does_not_hide_a_change_underneath_it(self) -> None:
        result = compare(model="gpt-other", tool_args={"q": "scarf"})
        assert set(result.categories) == {"changed_model", "changed_tool_arguments"}

    def test_an_ambiguous_rename_stays_missing_and_extra(self) -> None:
        # Two calls removed and two added under one parent have no unique
        # pairing, so nothing is guessed.
        def run_with(names: tuple[str, str]) -> list[dict]:
            run = Run()
            start = run.event("e0", "run.started", payload={"input": {}})
            for name in names:
                run.tool(name, parents=(start,))
            return run.events

        result = detect.compare(run_with(("a", "b")), run_with(("c", "d")))
        assert set(result.categories) == {"missing_call", "extra_call"}
        assert len(result.findings) == 4

    def test_a_mutation_is_located_at_the_right_identity(self) -> None:
        finding = compare(tool_args={"q": "scarf"}).findings[0]
        assert finding.identity == ("model:gpt-test#0", "tool:search#0")

    def test_run_level_findings_have_no_identity(self) -> None:
        assert compare(input={"task": "other"}).findings[0].identity == ()


class TestErrors:
    def test_a_failed_tool_reports_control_flow_and_the_error(self) -> None:
        result = compare(tool_failed=True)
        assert set(result.categories) == {"changed_output", "changed_error", "changed_control_flow"}
        error = next(f for f in result.findings if f.category == "changed_error")
        assert error.candidate == {"message": "boom"}

    def test_a_changed_error_payload_is_its_own_category(self) -> None:
        baseline = make_run(tool_failed=True)
        candidate = make_run(tool_failed=True)
        for event in candidate:
            if event["type"] == "tool.failed":
                event["payload"] = {**event["payload"], "error": {"message": "different"}}
        result = detect.compare(baseline, candidate)
        assert [finding.category for finding in result.findings] == ["changed_error"]
        assert result.findings[0].baseline == {"message": "boom"}


class TestDeterminism:
    def test_findings_do_not_depend_on_input_order(self) -> None:
        baseline = make_run()
        candidate = make_run(tool_args={"q": "scarf"}, output={"answer": "a scarf"})
        reference = detect.compare(baseline, candidate).describe()
        for seed in range(10):
            shuffled_left = list(baseline)
            shuffled_right = list(candidate)
            random.Random(seed).shuffle(shuffled_left)
            random.Random(seed + 100).shuffle(shuffled_right)
            assert detect.compare(shuffled_left, shuffled_right).describe() == reference

    def test_key_order_is_not_a_difference(self) -> None:
        # Canonicalization is what makes this true; without it the comparison
        # would depend on how the recorder happened to serialize a dict.
        baseline = make_run(tool_args={"q": "hat", "limit": 5})
        candidate = make_run(tool_args={"limit": 5, "q": "hat"})
        assert detect.compare(baseline, candidate).deterministic == "pass"

    def test_the_comparison_hash_is_stable_and_sensitive(self) -> None:
        same = detect.compare(make_run(), make_run())
        changed = compare(tool_args={"q": "scarf"})
        assert same.comparison_hash() == detect.compare(make_run(), make_run()).comparison_hash()
        assert same.comparison_hash() != changed.comparison_hash()

    def test_findings_are_ordered_parents_before_children(self) -> None:
        result = compare(input={"task": "other"}, tool_args={"q": "scarf"})
        depths = [len(finding.identity) for finding in result.findings]
        assert depths == sorted(depths)

    def test_a_finding_is_never_reported_twice(self) -> None:
        result = compare(tool_args={"q": "scarf"})
        keys = [(f.category, f.identity, f.detail) for f in result.findings]
        assert len(keys) == len(set(keys))


class TestNormalizationPolicy:
    def test_an_ignored_pointer_suppresses_a_difference(self) -> None:
        policy = NormalizationPolicy(("/hits",))
        result = detect.compare(
            make_run(), make_run(tool_result={"hits": 9}), policy=policy
        )
        assert result.deterministic == "pass"

    def test_the_policy_is_recorded_in_the_report(self) -> None:
        policy = NormalizationPolicy(("/hits",), casefold_strings=True)
        report = detect.compare(make_run(), make_run(), policy=policy).describe()
        assert report["policy"] == policy.descriptor()
        assert report["detection_version"] == detect.DETECTION_VERSION

    def test_the_policy_changes_the_comparison_hash(self) -> None:
        # Two reports that reached the same verdict under different policies
        # are not the same report, and must not hash alike.
        default = detect.compare(make_run(), make_run())
        ignored = detect.compare(make_run(), make_run(), policy=NormalizationPolicy(("/hits",)))
        assert default.deterministic == ignored.deterministic == "pass"
        assert default.comparison_hash() != ignored.comparison_hash()

    def test_casefolding_applies_to_compared_values(self) -> None:
        policy = NormalizationPolicy(casefold_strings=True)
        assert detect.compare(
            make_run(), make_run(tool_args={"q": "HAT"}), policy=policy
        ).deterministic == "pass"
        assert detect.compare(make_run(), make_run(tool_args={"q": "HAT"})).deterministic == "fail"


class TestFailsClosed:
    def test_a_capture_gap_cannot_pass(self) -> None:
        result = compare(gap=True)
        assert result.deterministic == "inconclusive"
        assert result.differences == ()
        assert result.evidence_findings

    def test_a_difference_outranks_a_gap(self) -> None:
        result = compare(gap=True, tool_args={"q": "scarf"})
        assert result.deterministic == "fail"
        assert {finding.category for finding in result.findings} == {
            "capture_gap",
            "changed_tool_arguments",
        }

    def test_an_unresolved_invocation_is_reported_as_a_gap(self) -> None:
        result = detect.compare(make_run(), make_run(unresolved_tool=True))
        assert "capture_gap" in result.categories
        gap = next(f for f in result.findings if f.category == "capture_gap")
        assert "never resolved" in gap.detail

    def test_a_run_that_never_completed_is_a_control_flow_change(self) -> None:
        result = detect.compare(make_run(), make_run(never_finished=True))
        assert result.deterministic == "fail"
        assert "changed_control_flow" in result.categories

    def test_ambiguous_alignment_cannot_pass(self) -> None:
        def fan_out(first: dict, second: dict) -> list[dict]:
            run = Run()
            start = run.event("e0", "run.started", payload={"input": {}})
            run.tool("search", stream=STREAM_A, parents=(start,), result=first)
            run.tool("search", stream=STREAM_B, parents=(start,), result=second)
            return run.events

        result = detect.compare(fan_out({"n": 1}, {"n": 2}), fan_out({"n": 2}, {"n": 1}))
        assert result.deterministic == "inconclusive"
        assert result.categories == ("ambiguous_alignment",)

    def test_an_unorderable_run_is_not_compared(self) -> None:
        from anvil_server.playback import PlaybackError

        broken = make_run()
        broken[0] = {**broken[0], "parent_event_ids": ["nope"]}
        with pytest.raises(PlaybackError):
            detect.compare(broken, make_run())


class TestReportContract:
    def test_an_unknown_category_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown finding category"):
            detect.Finding("made_up", (), "detail")

    def test_findings_are_immutable(self) -> None:
        finding = compare(tool_args={"q": "scarf"}).findings[0]
        with pytest.raises(Exception):
            finding.category = "changed_input"  # type: ignore[misc]

    def test_categories_are_reported_in_published_order(self) -> None:
        result = compare(input={"task": "other"}, usage={"input_tokens": 1, "output_tokens": 1})
        order = [detect.CATEGORIES.index(category) for category in result.categories]
        assert order == sorted(order)

    def test_the_report_states_both_published_statuses(self) -> None:
        report = compare(tool_args={"q": "scarf"}).describe()
        assert report["deterministic"] in {"pass", "fail", "inconclusive"}
        assert report["alignment_confidence"] in {"exact", "partial", "ambiguous"}

    def test_evidence_and_difference_findings_are_disjoint(self) -> None:
        result = compare(gap=True, tool_args={"q": "scarf"})
        # Findings hold captured values, so they are compared by position
        # rather than put in a set.
        assert len(result.differences) + len(result.evidence_findings) == len(result.findings)
        assert all(finding in result.findings for finding in result.differences)
        assert all(finding in result.findings for finding in result.evidence_findings)
        assert not any(finding in result.evidence_findings for finding in result.differences)
