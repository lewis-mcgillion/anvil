"""Result aggregation and gating (ANVIL-048).

A comparison produces three independent statuses — deterministic, judge and
infrastructure — and something has to turn them into one answer and one exit
code without losing what each of them said. That is this module.

The whole design is one rule: **a pass must be earned, never defaulted to.**
Every combination is enumerated explicitly, so a status nobody thought about
cannot fall through to success. The published vocabularies are closed, and a
value outside them is invalid input rather than a silently ignored case.

Three consequences follow, each required by `docs/architecture.md`:

* **Infrastructure outranks everything.** If the run was cancelled, errored or
  timed out, the deterministic comparison was computed from evidence that may
  be truncated, so its verdict is not reported as the outcome.
* **Judges cannot rescue a failure.** Judge gating is off by default and must
  be enabled explicitly. Even then a judge can only *withhold* a pass; it can
  never override a deterministic or infrastructure failure.
* **Absent evidence is inconclusive, never a pass.** That includes enabling
  judge gating and then having no judge result: the gate was asked for an
  opinion it does not have.

Aggregation is pure: no I/O, no jobs, no external calls, so there is nothing
to clean up and no cancellation semantics of its own. It *reports* a cancelled
run; it does not cancel anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .detect import Comparison

__all__ = [
    "AGGREGATION_VERSION",
    "DETERMINISTIC_STATUSES",
    "JUDGE_STATUSES",
    "INFRASTRUCTURE_STATUSES",
    "OUTCOMES",
    "EXIT_CODES",
    "InvalidStatusError",
    "Gate",
    "Outcome",
    "aggregate",
    "aggregate_comparison",
]

AGGREGATION_VERSION = "1"

#: Published status vocabularies. These are closed sets; see architecture.md.
DETERMINISTIC_STATUSES = ("pass", "fail", "inconclusive")
JUDGE_STATUSES = ("pass", "fail", "inconclusive", "not_run")
INFRASTRUCTURE_STATUSES = ("ok", "error", "cancelled", "timed_out")

#: Overall outcomes, in decreasing severity.
OUTCOMES = ("cancelled", "infrastructure_error", "failed", "inconclusive", "passed")

#: Published CLI exit codes, from architecture.md.
EXIT_CODES = {
    "passed": 0,
    "failed": 1,
    # 2 is invalid input, which is raised rather than returned.
    "infrastructure_error": 3,
    "cancelled": 4,
    "inconclusive": 5,
}

EXIT_INVALID_INPUT = 2


class InvalidStatusError(ValueError):
    """A status outside the published vocabulary.

    Raised rather than coerced. A gate that quietly maps an unrecognized status
    onto a known one is a gate that can be made to pass by sending it
    something it does not understand.
    """

    exit_code = EXIT_INVALID_INPUT


@dataclass(frozen=True)
class Gate:
    """Gating configuration.

    ``judge`` gating is off by default: an optional LLM judge must be opted
    into explicitly before it can withhold a pass.
    """

    judge: bool = False

    def describe(self) -> dict[str, Any]:
        return {"judge": self.judge}


@dataclass(frozen=True)
class Outcome:
    """The aggregated result, with the reasoning that produced it."""

    outcome: str
    exit_code: int
    reason: str
    deterministic: str
    judge: str
    infrastructure: str
    gate: Gate

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"

    def describe(self) -> dict[str, Any]:
        """Return the report, preserving all three independent statuses."""
        return {
            "aggregation_version": AGGREGATION_VERSION,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "reason": self.reason,
            # Reported independently and never collapsed into the outcome, so
            # a consumer can always see what each layer actually said.
            "deterministic": self.deterministic,
            "judge": self.judge,
            "infrastructure": self.infrastructure,
            "gate": self.gate.describe(),
        }


def _validate(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise InvalidStatusError(
            f"{label} status {value!r} is not one of {', '.join(allowed)}"
        )
    return value


def aggregate(
    *,
    deterministic: str,
    judge: str = "not_run",
    infrastructure: str = "ok",
    gate: Gate | None = None,
) -> Outcome:
    """Combine the three independent statuses into one gated outcome.

    Raises :class:`InvalidStatusError` (exit code 2) for any status outside the
    published vocabulary.
    """
    gate = gate or Gate()
    _validate(deterministic, DETERMINISTIC_STATUSES, "deterministic")
    _validate(judge, JUDGE_STATUSES, "judge")
    _validate(infrastructure, INFRASTRUCTURE_STATUSES, "infrastructure")

    def decide() -> tuple[str, str]:
        # Infrastructure first: if the run did not complete normally, the
        # comparison was computed over evidence that may be truncated, so its
        # verdict is not the outcome being reported.
        if infrastructure == "cancelled":
            return "cancelled", "the run was cancelled"
        if infrastructure == "timed_out":
            return "infrastructure_error", "the run timed out"
        if infrastructure == "error":
            return "infrastructure_error", "the run hit an infrastructure error"

        if deterministic == "fail":
            return "failed", "deterministic comparison failed"
        if deterministic == "inconclusive":
            return "inconclusive", "deterministic comparison had incomplete evidence"

        if gate.judge:
            if judge == "fail":
                return "failed", "the judge gate failed"
            if judge == "inconclusive":
                return "inconclusive", "the judge was inconclusive"
            if judge == "not_run":
                # Asking a gate for an opinion it does not have is not a pass.
                return "inconclusive", "judge gating is enabled but no judge ran"

        if judge == "fail":
            return "passed", "deterministic comparison passed; judge failed but is not gating"

        return "passed", "deterministic comparison passed"

    outcome, reason = decide()
    return Outcome(
        outcome=outcome,
        exit_code=EXIT_CODES[outcome],
        reason=reason,
        deterministic=deterministic,
        judge=judge,
        infrastructure=infrastructure,
        gate=gate,
    )


def aggregate_comparison(
    comparison: Comparison,
    *,
    judge: str = "not_run",
    infrastructure: str = "ok",
    gate: Gate | None = None,
) -> Outcome:
    """Gate a :class:`~anvil_server.divergence.detect.Comparison`.

    A thin adapter so the deterministic status is read from the comparison
    rather than restated by a caller, which is the only place it could drift.
    """
    return aggregate(
        deterministic=comparison.deterministic,
        judge=judge,
        infrastructure=infrastructure,
        gate=gate,
    )
