"""Deterministic divergence detection between two runs (ANVIL-045).

Alignment decides which step in the candidate corresponds to which step in the
baseline. Detection decides what actually changed, and says so in terms an
engineer can act on: not "these runs differ" but "the ``search`` tool was
called with different arguments, here are both".

Three rules shape the implementation.

**Every finding is categorized and carries evidence.** A finding names one of
the published diff dimensions from `docs/architecture.md` and cites the
identity it was found at plus both sides' values. A category without evidence
is not actionable, and evidence without a category cannot be aggregated or
gated.

**Comparison is deterministic.** Values are compared through an explicit,
versioned :class:`~anvil_server.canonical.NormalizationPolicy`, so the answer
depends only on the two recordings and the policy — never on key order, float
formatting, or the order events arrived in. The same two runs always produce
the same findings in the same order.

**It cannot pass on evidence it does not have.** Capture gaps and ambiguous
alignment both yield ``inconclusive``; only a complete comparison with no
differences is a ``pass``. A real difference is a ``fail`` regardless of how
complete the rest of the evidence is.

Detection performs no I/O, schedules no job and contacts no external system.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..canonical import NormalizationPolicy, canonical_hash
from .alignment import Alignment, InvocationGraph, align, build_graph

__all__ = [
    "DETECTION_VERSION",
    "CATEGORIES",
    "Finding",
    "Comparison",
    "compare",
    "compare_graphs",
]

DETECTION_VERSION = "1"

#: The published diff dimensions, as named in `docs/architecture.md`, plus the
#: refinements needed to make each one actionable on its own.
CATEGORIES = (
    "changed_input",
    "changed_prompt",
    "changed_model",
    "changed_tool_name",
    "changed_tool_args",
    "changed_output",
    "error_difference",
    "control_flow",
    "budget_exceeded",
    "missing_tool_call",
    "extra_tool_call",
    "capture_gap",
    "ambiguous_alignment",
)

#: Categories that report incomplete evidence rather than an observed change.
#: These can only ever downgrade a result to ``inconclusive``.
_EVIDENCE_CATEGORIES = frozenset({"capture_gap", "ambiguous_alignment"})

class _Absent:
    """Marker for "this side had no value at all"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


ABSENT = _Absent()


@dataclass(frozen=True)
class Finding:
    """One categorized, evidenced difference."""

    category: str
    identity: tuple[str, ...]
    detail: str
    baseline: Any = ABSENT
    candidate: Any = ABSENT

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"Unknown finding category {self.category!r}")

    @property
    def is_evidence_only(self) -> bool:
        """Whether this reports missing evidence rather than a change."""
        return self.category in _EVIDENCE_CATEGORIES

    def describe(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "category": self.category,
            "identity": list(self.identity),
            "detail": self.detail,
        }
        if self.baseline is not ABSENT:
            report["baseline"] = self.baseline
        if self.candidate is not ABSENT:
            report["candidate"] = self.candidate
        return report


@dataclass(frozen=True)
class Comparison:
    """The deterministic result of comparing two runs."""

    findings: tuple[Finding, ...]
    alignment: Alignment
    policy: NormalizationPolicy

    @property
    def differences(self) -> tuple[Finding, ...]:
        """Findings that report an observed change."""
        return tuple(finding for finding in self.findings if not finding.is_evidence_only)

    @property
    def evidence_findings(self) -> tuple[Finding, ...]:
        """Findings that report missing or unusable evidence."""
        return tuple(finding for finding in self.findings if finding.is_evidence_only)

    @property
    def deterministic(self) -> str:
        """The published ``deterministic`` status: pass, fail or inconclusive.

        A difference is a failure even when other evidence is missing, because
        an observed change is not made uncertain by an unrelated gap. Missing
        evidence alone is inconclusive, never a pass.
        """
        if self.differences:
            return "fail"
        if self.evidence_findings or self.alignment.confidence != "exact":
            return "inconclusive"
        return "pass"

    @property
    def categories(self) -> tuple[str, ...]:
        """Distinct categories present, in published order."""
        present = {finding.category for finding in self.findings}
        return tuple(category for category in CATEGORIES if category in present)

    def describe(self) -> dict[str, Any]:
        return {
            "detection_version": DETECTION_VERSION,
            "deterministic": self.deterministic,
            "alignment_confidence": self.alignment.confidence,
            "policy": self.policy.descriptor(),
            "categories": list(self.categories),
            "findings": [finding.describe() for finding in self.findings],
        }

    def comparison_hash(self) -> str:
        """Canonical hash of the comparison, for pinning a stored report."""
        return canonical_hash(self.describe())


def _normalized(policy: NormalizationPolicy, value: Any) -> tuple[Any, str]:
    normalized, _ = policy.apply(value)
    return normalized, canonical_hash(normalized)


def _differs(policy: NormalizationPolicy, left: Any, right: Any) -> tuple[bool, Any, Any]:
    """Compare two values under the policy, returning the normalized pair."""
    left_value, left_hash = _normalized(policy, left)
    right_value, right_hash = _normalized(policy, right)
    return left_hash != right_hash, left_value, right_value


def _payload_of(events: Iterable[Mapping[str, Any]], event_type: str) -> Mapping[str, Any] | None:
    for event in events:
        if str(event["type"]) == event_type:
            return event.get("payload") or {}
    return None


def _messages(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": (event.get("payload") or {}).get("role"),
            "content": (event.get("payload") or {}).get("content"),
        }
        for event in events
        if str(event["type"]) == "message.created"
    ]


def _capture_gaps(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "reason": (event.get("payload") or {}).get("reason"),
            "after_sequence": (event.get("payload") or {}).get("after_sequence"),
        }
        for event in events
        if str(event["type"]) == "capture.gap"
    ]


def compare(
    baseline_events: Iterable[Mapping[str, Any]],
    candidate_events: Iterable[Mapping[str, Any]],
    *,
    policy: NormalizationPolicy | None = None,
) -> Comparison:
    """Compare two recorded runs and return categorized, evidenced findings.

    Ordering failures in either recording propagate as
    :class:`~anvil_server.playback.PlaybackError`: a run that cannot be ordered
    cannot be compared, and guessing an order would make the result
    non-deterministic.
    """
    policy = policy or NormalizationPolicy()
    baseline = list(baseline_events)
    candidate = list(candidate_events)

    # Graphs are built once: building them twice would double the ordering
    # work and let the two halves of the report disagree if it were not pure.
    baseline_graph = build_graph(baseline)
    candidate_graph = build_graph(candidate)
    invocation_result = compare_graphs(baseline_graph, candidate_graph, policy=policy)

    findings = [
        *_run_level_findings(policy, baseline, candidate),
        *invocation_result.findings,
        *_capture_gap_findings(baseline, candidate),
    ]
    return Comparison(_ordered(findings), invocation_result.alignment, policy)


def compare_graphs(
    baseline: InvocationGraph,
    candidate: InvocationGraph,
    *,
    policy: NormalizationPolicy | None = None,
) -> Comparison:
    """Compare two already-built invocation graphs."""
    policy = policy or NormalizationPolicy()
    alignment = align(baseline, candidate)
    findings: list[Finding] = []

    renames = _renames(alignment)
    for identity, (left, right) in sorted(renames.items()):
        category = "changed_model" if left.kind == "model" else "changed_tool_name"
        findings.append(
            Finding(
                category,
                identity,
                f"{left.kind} name changed from {left.name!r} to {right.name!r}",
                baseline=left.describe(),
                candidate=right.describe(),
            )
        )
    renamed = set(renames) | {right.identity for _, right in renames.values()}

    # A rename moves the whole subtree, so every descendant arrives as missing
    # and extra too. Re-basing them under the recovered pair is an exact prefix
    # substitution, so the report shows the one change rather than the churn.
    rebased, rebased_identities = _rebase_descendants(renames, baseline, candidate)
    renamed |= rebased_identities
    for identity, (left, right) in sorted(rebased.items()):
        findings.extend(_pair_findings(policy, identity, left, right))

    for pair in alignment.pairs:
        if pair.identity in renamed:
            continue
        if pair.status == "missing":
            findings.append(
                Finding(
                    "missing_tool_call",
                    pair.identity,
                    f"{pair.baseline.kind} call {pair.baseline.name!r} is absent from the candidate",
                    baseline=pair.baseline.describe(),
                )
            )
            continue
        if pair.status == "extra":
            findings.append(
                Finding(
                    "extra_tool_call",
                    pair.identity,
                    f"{pair.candidate.kind} call {pair.candidate.name!r} is not in the baseline",
                    candidate=pair.candidate.describe(),
                )
            )
            continue
        if pair.status == "ambiguous":
            findings.append(
                Finding(
                    "ambiguous_alignment",
                    pair.identity,
                    pair.reason or "alignment could not identify this invocation",
                    baseline=pair.baseline.describe() if pair.baseline else ABSENT,
                    candidate=pair.candidate.describe() if pair.candidate else ABSENT,
                )
            )
            continue

        findings.extend(_pair_findings(policy, pair.identity, pair.baseline, pair.candidate))

    for graph, side in ((baseline, "baseline"), (candidate, "candidate")):
        for gap in graph.gaps:
            findings.append(Finding("capture_gap", (), f"{side}: {gap}"))

    return Comparison(_ordered(findings), alignment, policy)


def _renames(alignment: Alignment) -> dict[tuple[str, ...], tuple[Any, Any]]:
    """Pair a missing call with an extra call that replaced it.

    Alignment cannot match a renamed call, because the name is part of the
    identity, so a rename arrives as one missing and one extra. Recovering it
    here is an exact structural rule, not a similarity guess: the two must
    share a parent, a kind and an ordinal, and must be the only missing and
    extra call in that group. Where more than one candidate exists there is no
    unique answer, so both are left as missing and extra rather than paired on
    a guess.
    """
    groups: dict[tuple[Any, ...], dict[str, list[Any]]] = defaultdict(
        lambda: {"missing": [], "extra": []}
    )
    for pair in alignment.pairs:
        if pair.status == "missing":
            invocation = pair.baseline
        elif pair.status == "extra":
            invocation = pair.candidate
        else:
            continue
        key = (invocation.parent or (), invocation.kind, invocation.ordinal)
        groups[key][pair.status].append(invocation)

    renames: dict[tuple[str, ...], tuple[Any, Any]] = {}
    for group in groups.values():
        if len(group["missing"]) != 1 or len(group["extra"]) != 1:
            continue
        left, right = group["missing"][0], group["extra"][0]
        if left.name == right.name:
            continue
        renames[left.identity] = (left, right)
    return renames


def _rebase_descendants(
    renames: Mapping[tuple[str, ...], tuple[Any, Any]],
    baseline: InvocationGraph,
    candidate: InvocationGraph,
) -> tuple[dict[tuple[str, ...], tuple[Any, Any]], set[tuple[str, ...]]]:
    """Pair the descendants of a renamed invocation with their counterparts.

    Only exact matches are paired: the candidate must have an invocation whose
    identity is the baseline identity with the old prefix replaced by the new
    one. A descendant that also changed stays missing or extra, so a rename
    cannot hide a second change underneath it.
    """
    by_identity = candidate.by_identity
    paired: dict[tuple[str, ...], tuple[Any, Any]] = {}
    consumed: set[tuple[str, ...]] = set()

    for old_prefix, (_, new_invocation) in renames.items():
        new_prefix = new_invocation.identity
        for invocation in baseline.invocations:
            identity = invocation.identity
            if len(identity) <= len(old_prefix) or identity[: len(old_prefix)] != old_prefix:
                continue
            rebased_identity = new_prefix + identity[len(old_prefix) :]
            counterpart = by_identity.get(rebased_identity)
            if counterpart is None:
                continue
            paired[identity] = (invocation, counterpart)
            consumed.add(identity)
            consumed.add(rebased_identity)
    return paired, consumed


def _pair_findings(
    policy: NormalizationPolicy,
    identity: tuple[str, ...],
    left: Any,
    right: Any,
) -> list[Finding]:
    findings: list[Finding] = []

    changed, left_args, right_args = _differs(policy, left.arguments, right.arguments)
    if changed:
        category = "changed_prompt" if left.kind == "model" else "changed_tool_args"
        noun = "request" if left.kind == "model" else "arguments"
        findings.append(
            Finding(
                category,
                identity,
                f"{left.kind} {noun} differ",
                baseline=left_args,
                candidate=right_args,
            )
        )

    if left.status != right.status:
        findings.append(
            Finding(
                "control_flow",
                identity,
                f"call outcome changed from {left.status!r} to {right.status!r}",
                baseline=left.status,
                candidate=right.status,
            )
        )

    changed, left_result, right_result = _differs(policy, left.result, right.result)
    if changed:
        findings.append(
            Finding(
                "changed_output",
                identity,
                f"{left.kind} result differs",
                baseline=left_result,
                candidate=right_result,
            )
        )

    changed, left_error, right_error = _differs(policy, left.error, right.error)
    if changed:
        findings.append(
            Finding(
                "error_difference",
                identity,
                f"{left.kind} error differs",
                baseline=left_error,
                candidate=right_error,
            )
        )

    return findings


def _run_level_findings(
    policy: NormalizationPolicy,
    baseline: list[Mapping[str, Any]],
    candidate: list[Mapping[str, Any]],
) -> list[Finding]:
    findings: list[Finding] = []

    left_start = _payload_of(baseline, "run.started") or {}
    right_start = _payload_of(candidate, "run.started") or {}

    changed, left_input, right_input = _differs(
        policy, left_start.get("input"), right_start.get("input")
    )
    if changed:
        findings.append(
            Finding("changed_input", (), "run input differs", left_input, right_input)
        )

    for field, detail in (("agent_name", "agent"), ("adapter", "adapter")):
        if left_start.get(field) != right_start.get(field):
            findings.append(
                Finding(
                    "control_flow",
                    (),
                    f"run {detail} changed from {left_start.get(field)!r} to {right_start.get(field)!r}",
                    baseline=left_start.get(field),
                    candidate=right_start.get(field),
                )
            )

    changed, left_messages, right_messages = _differs(
        policy, _messages(baseline), _messages(candidate)
    )
    if changed:
        findings.append(
            Finding("changed_prompt", (), "run messages differ", left_messages, right_messages)
        )

    left_end = _payload_of(baseline, "run.completed")
    right_end = _payload_of(candidate, "run.completed")
    left_failure = _payload_of(baseline, "run.failed")
    right_failure = _payload_of(candidate, "run.failed")

    if (left_end is None) != (right_end is None):
        findings.append(
            Finding(
                "control_flow",
                (),
                "one run completed and the other did not",
                baseline="completed" if left_end is not None else "not completed",
                candidate="completed" if right_end is not None else "not completed",
            )
        )

    left_end = left_end or {}
    right_end = right_end or {}

    if left_end.get("outcome") != right_end.get("outcome"):
        findings.append(
            Finding(
                "control_flow",
                (),
                f"run outcome changed from {left_end.get('outcome')!r} to {right_end.get('outcome')!r}",
                baseline=left_end.get("outcome"),
                candidate=right_end.get("outcome"),
            )
        )

    changed, left_output, right_output = _differs(
        policy, left_end.get("output"), right_end.get("output")
    )
    if changed:
        findings.append(
            Finding("changed_output", (), "run output differs", left_output, right_output)
        )

    changed, left_usage, right_usage = _differs(
        policy, left_end.get("usage"), right_end.get("usage")
    )
    if changed:
        findings.append(
            Finding("budget_exceeded", (), "run usage differs", left_usage, right_usage)
        )

    changed, left_error, right_error = _differs(
        policy,
        (left_failure or {}).get("error"),
        (right_failure or {}).get("error"),
    )
    if changed:
        findings.append(
            Finding("error_difference", (), "run error differs", left_error, right_error)
        )

    return findings


def _capture_gap_findings(
    baseline: list[Mapping[str, Any]], candidate: list[Mapping[str, Any]]
) -> list[Finding]:
    findings: list[Finding] = []
    for events, side in ((baseline, "baseline"), (candidate, "candidate")):
        for gap in _capture_gaps(events):
            findings.append(
                Finding(
                    "capture_gap",
                    (),
                    f"{side}: declared capture gap after sequence {gap['after_sequence']}"
                    f" ({gap['reason']})",
                )
            )
    return findings


def _ordered(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Return findings in a deterministic, report-stable order.

    Sorted by identity depth then identity then category, so run-level findings
    come first and nested calls follow their parents.
    """
    deduplicated: list[Finding] = []
    seen: set[tuple[Any, ...]] = set()
    for finding in findings:
        key = (
            finding.category,
            finding.identity,
            finding.detail,
            canonical_hash(
                [
                    None if finding.baseline is ABSENT else finding.baseline,
                    None if finding.candidate is ABSENT else finding.candidate,
                ]
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(finding)

    return tuple(
        sorted(
            deduplicated,
            key=lambda finding: (
                len(finding.identity),
                finding.identity,
                CATEGORIES.index(finding.category),
                finding.detail,
            ),
        )
    )
