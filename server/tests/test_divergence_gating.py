"""Exhaustive truth table for result aggregation and gating (ANVIL-048).

The acceptance criterion is that no combination of statuses can produce a false
pass. These tests therefore enumerate every combination rather than sampling
interesting ones: 3 deterministic x 4 judge x 4 infrastructure x 2 gate
settings, 96 in total, with the invariants asserted over all of them.
"""

from __future__ import annotations

import itertools

import pytest

from anvil_server.divergence import gating as agg
from anvil_server.divergence import compare
from runbuilder import Run, baseline_run

def changed_arguments_run() -> list[dict]:
    """The baseline with exactly one tool argument changed."""
    run = Run()
    start = run.start()
    search = run.tool("search", parents=(start,), arguments={"q": "cap"}, result={"hits": 2})
    fetch = run.tool("fetch", parents=(search,), arguments={"id": 1}, result={"ok": True})
    run.event("z", "run.completed", parents=(fetch,))
    return run.events


ALL_COMBINATIONS = [
    (deterministic, judge, infrastructure, judge_gate)
    for deterministic, judge, infrastructure, judge_gate in itertools.product(
        agg.DETERMINISTIC_STATUSES,
        agg.JUDGE_STATUSES,
        agg.INFRASTRUCTURE_STATUSES,
        (False, True),
    )
]


def run(deterministic: str, judge: str, infrastructure: str, judge_gate: bool) -> agg.Outcome:
    return agg.aggregate(
        deterministic=deterministic,
        judge=judge,
        infrastructure=infrastructure,
        gate=agg.Gate(judge=judge_gate),
    )


class TestCoverage:
    def test_the_table_is_exhaustive(self) -> None:
        assert len(ALL_COMBINATIONS) == 3 * 4 * 4 * 2 == 96

    def test_every_combination_produces_a_published_outcome(self) -> None:
        for combination in ALL_COMBINATIONS:
            outcome = run(*combination)
            assert outcome.outcome in agg.OUTCOMES
            assert outcome.exit_code == agg.EXIT_CODES[outcome.outcome]
            assert outcome.reason

    def test_every_outcome_is_reachable(self) -> None:
        reached = {run(*combination).outcome for combination in ALL_COMBINATIONS}
        assert reached == set(agg.OUTCOMES)

    def test_exit_codes_match_the_published_table(self) -> None:
        # From docs/architecture.md: 0 gates passed, 1 assertion/judge gate
        # failed, 2 invalid input, 3 infrastructure error, 4 cancelled,
        # 5 inconclusive.
        assert agg.EXIT_CODES == {
            "passed": 0,
            "failed": 1,
            "infrastructure_error": 3,
            "cancelled": 4,
            "inconclusive": 5,
        }
        assert agg.InvalidStatusError.exit_code == 2


class TestNoFalsePasses:
    """The acceptance criterion, asserted over the whole table."""

    def test_a_pass_requires_a_deterministic_pass(self) -> None:
        for combination in ALL_COMBINATIONS:
            outcome = run(*combination)
            if outcome.passed:
                assert outcome.deterministic == "pass"

    def test_a_pass_requires_healthy_infrastructure(self) -> None:
        for combination in ALL_COMBINATIONS:
            outcome = run(*combination)
            if outcome.passed:
                assert outcome.infrastructure == "ok"

    def test_partial_evidence_never_passes(self) -> None:
        for judge, infrastructure, judge_gate in itertools.product(
            agg.JUDGE_STATUSES, agg.INFRASTRUCTURE_STATUSES, (False, True)
        ):
            outcome = run("inconclusive", judge, infrastructure, judge_gate)
            assert not outcome.passed

    def test_an_infrastructure_fault_never_passes(self) -> None:
        for deterministic, judge, judge_gate in itertools.product(
            agg.DETERMINISTIC_STATUSES, agg.JUDGE_STATUSES, (False, True)
        ):
            for infrastructure in ("error", "cancelled", "timed_out"):
                assert not run(deterministic, judge, infrastructure, judge_gate).passed

    def test_an_enabled_judge_gate_without_a_judge_never_passes(self) -> None:
        for deterministic, infrastructure in itertools.product(
            agg.DETERMINISTIC_STATUSES, agg.INFRASTRUCTURE_STATUSES
        ):
            assert not run(deterministic, "not_run", infrastructure, True).passed


class TestInfrastructurePrecedence:
    def test_cancellation_is_reported_as_cancellation(self) -> None:
        for deterministic, judge, judge_gate in itertools.product(
            agg.DETERMINISTIC_STATUSES, agg.JUDGE_STATUSES, (False, True)
        ):
            outcome = run(deterministic, judge, "cancelled", judge_gate)
            assert outcome.outcome == "cancelled"
            assert outcome.exit_code == 4

    def test_a_timeout_is_an_infrastructure_error(self) -> None:
        outcome = run("pass", "pass", "timed_out", False)
        assert outcome.outcome == "infrastructure_error"
        assert outcome.exit_code == 3
        assert "timed out" in outcome.reason

    def test_infrastructure_outranks_a_deterministic_failure(self) -> None:
        # The comparison may have been computed over truncated evidence, so its
        # verdict is not what gets reported.
        outcome = run("fail", "fail", "error", True)
        assert outcome.outcome == "infrastructure_error"

    def test_the_underlying_statuses_survive_in_the_report(self) -> None:
        report = run("fail", "pass", "cancelled", True).describe()
        assert report["outcome"] == "cancelled"
        assert report["deterministic"] == "fail"
        assert report["judge"] == "pass"
        assert report["infrastructure"] == "cancelled"


class TestJudgeGating:
    def test_judges_are_off_by_default(self) -> None:
        assert agg.Gate().judge is False
        assert agg.aggregate(deterministic="pass", judge="fail").passed

    def test_a_non_gating_judge_failure_is_recorded_not_enforced(self) -> None:
        outcome = agg.aggregate(deterministic="pass", judge="fail")
        assert outcome.passed
        assert outcome.judge == "fail"
        assert "not gating" in outcome.reason

    def test_a_gating_judge_failure_fails(self) -> None:
        outcome = run("pass", "fail", "ok", True)
        assert outcome.outcome == "failed"
        assert outcome.exit_code == 1

    def test_a_gating_judge_cannot_rescue_a_deterministic_failure(self) -> None:
        for judge in agg.JUDGE_STATUSES:
            for judge_gate in (False, True):
                outcome = run("fail", judge, "ok", judge_gate)
                assert outcome.outcome == "failed"

    def test_a_gating_judge_cannot_rescue_an_infrastructure_failure(self) -> None:
        for judge in agg.JUDGE_STATUSES:
            outcome = run("pass", judge, "error", True)
            assert outcome.outcome == "infrastructure_error"

    def test_a_gating_judge_can_only_withhold_a_pass(self) -> None:
        # Enabling the gate may turn a pass into something else, but can never
        # turn a non-pass into a pass.
        for deterministic, judge, infrastructure in itertools.product(
            agg.DETERMINISTIC_STATUSES, agg.JUDGE_STATUSES, agg.INFRASTRUCTURE_STATUSES
        ):
            ungated = run(deterministic, judge, infrastructure, False)
            gated = run(deterministic, judge, infrastructure, True)
            if gated.passed:
                assert ungated.passed

    def test_an_inconclusive_gating_judge_is_inconclusive(self) -> None:
        outcome = run("pass", "inconclusive", "ok", True)
        assert outcome.outcome == "inconclusive"
        assert outcome.exit_code == 5


class TestInvalidInput:
    @pytest.mark.parametrize(
        ("kwargs", "label"),
        [
            ({"deterministic": "ok"}, "deterministic"),
            ({"deterministic": "pass", "judge": "skipped"}, "judge"),
            ({"deterministic": "pass", "infrastructure": "fail"}, "infrastructure"),
            ({"deterministic": ""}, "deterministic"),
            ({"deterministic": "PASS"}, "deterministic"),
        ],
    )
    def test_an_unpublished_status_is_rejected(self, kwargs: dict, label: str) -> None:
        with pytest.raises(agg.InvalidStatusError) as caught:
            agg.aggregate(**kwargs)
        assert label in str(caught.value)
        assert caught.value.exit_code == 2

    def test_statuses_from_one_vocabulary_are_not_valid_in_another(self) -> None:
        # "not_run" is a judge status only; accepting it elsewhere would be a
        # way to smuggle an unrecognized state past the gate.
        with pytest.raises(agg.InvalidStatusError):
            agg.aggregate(deterministic="not_run")
        with pytest.raises(agg.InvalidStatusError):
            agg.aggregate(deterministic="pass", infrastructure="pass")

    def test_rejection_happens_before_any_decision(self) -> None:
        # A cancelled run with an invalid deterministic status must still be
        # rejected, not short-circuited to "cancelled".
        with pytest.raises(agg.InvalidStatusError):
            agg.aggregate(deterministic="bogus", infrastructure="cancelled")


class TestReportContract:
    def test_outcomes_are_immutable(self) -> None:
        outcome = agg.aggregate(deterministic="pass")
        with pytest.raises(Exception):
            outcome.outcome = "failed"  # type: ignore[misc]

    def test_the_report_is_versioned_and_deterministic(self) -> None:
        first = agg.aggregate(deterministic="pass", judge="pass", gate=agg.Gate(judge=True))
        second = agg.aggregate(deterministic="pass", judge="pass", gate=agg.Gate(judge=True))
        assert first.describe() == second.describe()
        assert first.describe()["aggregation_version"] == agg.AGGREGATION_VERSION

    def test_the_gate_configuration_is_part_of_the_report(self) -> None:
        # The same statuses can produce different outcomes under different
        # gates, so a report that omitted the gate would not be reproducible.
        ungated = agg.aggregate(deterministic="pass", judge="fail")
        gated = agg.aggregate(deterministic="pass", judge="fail", gate=agg.Gate(judge=True))
        assert ungated.describe()["gate"] == {"judge": False}
        assert gated.describe()["gate"] == {"judge": True}
        assert ungated.outcome != gated.outcome

    def test_defaults_describe_an_ungated_run_with_no_judge(self) -> None:
        outcome = agg.aggregate(deterministic="pass")
        assert outcome.judge == "not_run"
        assert outcome.infrastructure == "ok"
        assert outcome.passed


class TestComparisonAdapter:
    """The deterministic status must be read from the comparison itself."""

    def test_a_clean_comparison_passes(self) -> None:
        comparison = compare(baseline_run(), baseline_run())
        assert comparison.deterministic == "pass"
        assert agg.aggregate_comparison(comparison).passed

    def test_a_divergent_comparison_fails(self) -> None:
        comparison = compare(baseline_run(), changed_arguments_run())
        assert comparison.deterministic == "fail"
        outcome = agg.aggregate_comparison(comparison)
        assert outcome.outcome == "failed"
        assert outcome.exit_code == 1

    def test_a_gating_judge_cannot_rescue_a_divergent_comparison(self) -> None:
        comparison = compare(baseline_run(), changed_arguments_run())
        outcome = agg.aggregate_comparison(
            comparison, judge="pass", gate=agg.Gate(judge=True)
        )
        assert outcome.outcome == "failed"

    def test_infrastructure_still_outranks_the_comparison(self) -> None:
        comparison = compare(baseline_run(), baseline_run())
        outcome = agg.aggregate_comparison(comparison, infrastructure="cancelled")
        assert outcome.outcome == "cancelled"
        assert outcome.deterministic == "pass"


class TestModuleNaming:
    def test_the_module_is_not_shadowed_by_an_exported_function(self) -> None:
        # The package exports a function named `aggregate`. If the module were
        # also named `aggregate`, `from ... import aggregate` would resolve to
        # whichever was bound last, which depends on import order.
        import anvil_server.divergence as pkg

        assert callable(pkg.aggregate)
        assert pkg.gating.aggregate is pkg.aggregate
