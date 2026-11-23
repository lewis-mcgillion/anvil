"""Causal invocation alignment for run comparison (ANVIL-044).

Comparing two runs starts by deciding which invocation in the candidate
corresponds to which invocation in the baseline. Doing that by position fails
the moment anything interesting happens: a parallel branch finishes in a
different order, a retry adds a duplicate call, or a changed prompt moves a
whole subtree.

Alignment here is by *logical identity* rather than position. An invocation's
identity is the chain of invocation identities that caused it, plus its kind,
its name, and its ordinal among the siblings that share that parent, kind and
name. That choice has three consequences the acceptance criteria care about:

* **Parallel reordering is not a difference.** Sibling branches with different
  names are identified by name, not order, so swapping the completion order of
  two independent branches changes nothing.
* **Duplicates align one-to-one.** Repeated identical calls under the same
  parent are distinguished by ordinal, so two retries align with two retries
  and a third retry shows up as extra rather than corrupting the others.
* **A changed branch is localized.** Renaming a call makes that call missing and
  the new one extra, and its descendants are reported as missing or extra with
  it instead of being silently paired with unrelated nodes.

Alignment never decides whether two aligned invocations *differ*; that is
divergence detection's job. It reports structure only, and it fails closed:
where identity cannot be established the pair is ``ambiguous``, and where
evidence is incomplete the result is ``partial``. Neither may be read as a
match.

The module performs no I/O, schedules no jobs and contacts no external system,
so there is nothing to clean up and no cancellation semantics.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from .. import playback
from ..canonical import canonical_hash

__all__ = [
    "ALIGNMENT_VERSION",
    "AlignmentError",
    "Invocation",
    "InvocationGraph",
    "AlignedPair",
    "Alignment",
    "build_graph",
    "align",
]

ALIGNMENT_VERSION = "1"

# Alignment reuses playback's event vocabulary rather than restating it, so a
# new event type cannot be understood by one and ignored by the other.
_REQUEST_TYPES = playback._REQUEST_TYPES
_RESOLUTION_TYPES = playback._RESOLUTION_TYPES

#: Payload fields that name an invocation, in the published schema.
_NAME_FIELDS = ("tool_name", "model")


class AlignmentError(ValueError):
    """Raised when two graphs cannot be aligned at all."""


@dataclass(frozen=True)
class Invocation:
    """One invocation and the causal path that identifies it."""

    identity: tuple[str, ...]
    kind: str
    name: str | None
    ordinal: int
    status: str
    event_id: str
    invocation_id: str | None
    arguments: Any = None
    result: Any = None
    error: Any = None
    parent: tuple[str, ...] | None = None
    concurrent: bool = False
    """Whether this invocation's ordinal was assigned among concurrent siblings.

    Sibling calls with the same parent, kind and name that run on different
    streams have no causal order between them, so their ordinals come from the
    deterministic tie-break on stream identity. That tie-break is stable within
    a run but meaningless across runs, because the two runs have different
    stream ids. Such an ordinal therefore cannot be used to pair one run's call
    with another's, and alignment reports it as ambiguous rather than guessing.
    """

    @property
    def is_identifiable(self) -> bool:
        """Whether this invocation has a name to be matched on.

        An unnamed invocation is a capture gap, not an anonymous match: two of
        them under the same parent cannot be told apart, so they are never
        paired silently.
        """
        return bool(self.name)

    def describe(self) -> dict[str, Any]:
        """Return the serializable identity recorded in reports."""
        return {
            "identity": list(self.identity),
            "kind": self.kind,
            "name": self.name,
            "ordinal": self.ordinal,
            "status": self.status,
            "event_id": self.event_id,
            "concurrent": self.concurrent,
        }


@dataclass(frozen=True)
class InvocationGraph:
    """The invocations of one run, keyed by logical identity."""

    invocations: tuple[Invocation, ...]
    gaps: tuple[str, ...] = ()

    @property
    def by_identity(self) -> dict[tuple[str, ...], Invocation]:
        return {invocation.identity: invocation for invocation in self.invocations}

    def __len__(self) -> int:
        return len(self.invocations)


@dataclass(frozen=True)
class AlignedPair:
    """One alignment decision.

    ``status`` is one of ``matched``, ``missing``, ``extra`` or ``ambiguous``.
    ``matched`` means the two invocations are the same logical step; it does
    *not* mean their contents are equal.
    """

    status: str
    identity: tuple[str, ...]
    baseline: Invocation | None = None
    candidate: Invocation | None = None
    reason: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "identity": list(self.identity),
            "baseline": self.baseline.describe() if self.baseline else None,
            "candidate": self.candidate.describe() if self.candidate else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Alignment:
    """The full alignment of two runs."""

    pairs: tuple[AlignedPair, ...]
    partial: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def matched(self) -> tuple[AlignedPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "matched")

    @property
    def missing(self) -> tuple[AlignedPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "missing")

    @property
    def extra(self) -> tuple[AlignedPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "extra")

    @property
    def ambiguous(self) -> tuple[AlignedPair, ...]:
        return tuple(pair for pair in self.pairs if pair.status == "ambiguous")

    @property
    def is_complete(self) -> bool:
        """Whether every invocation aligned one-to-one with full evidence.

        Ambiguity and partial evidence both make this false. A comparison must
        not report equality on an alignment that is not complete.
        """
        return not self.partial and not self.ambiguous and not self.missing and not self.extra

    @property
    def confidence(self) -> str:
        """The published ``alignment_confidence`` value for this alignment.

        One of ``exact``, ``partial`` or ``ambiguous``, as defined in
        `docs/architecture.md`. Ambiguity outranks incompleteness: if any pair
        could not be identified, the whole alignment is ambiguous regardless of
        how complete the evidence is elsewhere.
        """
        if self.ambiguous:
            return "ambiguous"
        if self.partial:
            return "partial"
        return "exact"

    def describe(self) -> dict[str, Any]:
        return {
            "alignment_version": ALIGNMENT_VERSION,
            "alignment_confidence": self.confidence,
            "partial": self.partial,
            "reasons": list(self.reasons),
            "pairs": [pair.describe() for pair in self.pairs],
        }

    def alignment_hash(self) -> str:
        """Canonical hash of the full report, including per-run evidence.

        Two runs that align identically still produce different values here,
        because the report cites the event ids that justify each decision and
        those are genuinely different events. Use this to pin a stored report;
        use :meth:`structure_hash` to compare two comparisons.
        """
        return canonical_hash(self.describe())

    def structure(self) -> dict[str, Any]:
        """Return only the alignment decisions, with no per-run evidence."""
        return {
            "alignment_version": ALIGNMENT_VERSION,
            "alignment_confidence": self.confidence,
            "pairs": [
                {"status": pair.status, "identity": list(pair.identity)} for pair in self.pairs
            ],
        }

    def structure_hash(self) -> str:
        """Canonical hash of the decisions alone.

        This is the value a gate should compare: it is stable across event
        ids, stream assignment and the completion order of parallel branches,
        and changes only when the aligned shape of the run changes.
        """
        return canonical_hash(self.structure())


def _name_of(payload: Mapping[str, Any]) -> str | None:
    for key in _NAME_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _arguments_of(payload: Mapping[str, Any]) -> Any:
    for key in ("arguments", "request"):
        if key in payload:
            return payload[key]
    return None


def build_graph(events: Iterable[Mapping[str, Any]]) -> InvocationGraph:
    """Derive the causal invocation graph of one run.

    Events are ordered with :func:`playback.order_events`, so the ordering is
    the same deterministic causal order the projection uses rather than wall
    clock. Any ordering failure is a :class:`playback.PlaybackError` and is
    allowed to propagate: an unorderable run cannot be aligned.
    """
    ordered = playback.order_events(events)

    # Map each event to the invocation that caused it, so an invocation's
    # parent is the nearest enclosing invocation in the causal chain.
    owner: dict[str, tuple[str, ...]] = {}
    invocations: list[Invocation] = []
    by_invocation_id: dict[str, Invocation] = {}
    pending: dict[tuple[str, ...], str] = {}
    counters: dict[tuple[tuple[str, ...], str, str | None], int] = defaultdict(int)
    group_streams: dict[tuple[tuple[str, ...], str, str | None], set[str]] = defaultdict(set)
    group_members: dict[tuple[tuple[str, ...], str, str | None], list[int]] = defaultdict(list)
    gaps: list[str] = []

    for event in ordered:
        event_id = str(event["event_id"])
        event_type = str(event["type"])
        payload = event.get("payload") or {}
        parents = [str(parent) for parent in event.get("parent_event_ids") or []]

        # The enclosing invocation is the first parent that resolves to one.
        enclosing: tuple[str, ...] = ()
        for parent in parents:
            if parent in owner:
                enclosing = owner[parent]
                break

        if event_type in _REQUEST_TYPES:
            kind = _REQUEST_TYPES[event_type]
            name = _name_of(payload)
            key = (enclosing, kind, name)
            ordinal = counters[key]
            counters[key] += 1
            label = f"{kind}:{name if name else '?'}#{ordinal}"
            identity = enclosing + (label,)
            invocation_id = event.get("invocation_id")
            invocation = Invocation(
                identity=identity,
                kind=kind,
                name=name,
                ordinal=ordinal,
                status="pending",
                event_id=event_id,
                invocation_id=str(invocation_id) if invocation_id is not None else None,
                arguments=_arguments_of(payload),
                parent=enclosing or None,
            )
            group_streams[key].add(str(event["stream_id"]))
            group_members[key].append(len(invocations))
            invocations.append(invocation)
            owner[event_id] = identity
            if invocation.invocation_id is not None:
                by_invocation_id[invocation.invocation_id] = invocation
            pending[identity] = event_id
            if name is None:
                gaps.append(f"{event_id}: {kind} invocation has no name")
            continue

        if event_type in _RESOLUTION_TYPES:
            kind, status = _RESOLUTION_TYPES[event_type]
            target = _resolve_target(invocations, by_invocation_id, event, enclosing, kind)
            if target is None:
                gaps.append(f"{event_id}: {event_type} without a matching request")
                owner[event_id] = enclosing
                continue
            index = invocations.index(target)
            invocations[index] = _resolved(target, status, payload)
            if target.invocation_id is not None:
                by_invocation_id[target.invocation_id] = invocations[index]
            pending.pop(target.identity, None)
            owner[event_id] = target.identity
            continue

        # Any other event belongs to the enclosing invocation, so that nested
        # requests underneath it inherit the right parent.
        owner[event_id] = enclosing

    # Siblings that share a name but span streams have no causal order, so
    # their ordinals are a deterministic tie-break rather than an identity.
    for key, streams in group_streams.items():
        if len(streams) < 2:
            continue
        for index in group_members[key]:
            invocations[index] = replace(invocations[index], concurrent=True)

    for identity in pending:
        gaps.append(f"{'/'.join(identity)}: invocation never resolved")

    return InvocationGraph(tuple(invocations), tuple(gaps))


def _resolved(invocation: Invocation, status: str, payload: Mapping[str, Any]) -> Invocation:
    result = payload.get("result")
    if result is None:
        result = payload.get("response")  # model.completed carries "response"
    return replace(
        invocation,
        status=status,
        result=result,
        error=payload.get("error"),
    )


def _resolve_target(
    invocations: Sequence[Invocation],
    by_invocation_id: Mapping[str, Invocation],
    event: Mapping[str, Any],
    enclosing: tuple[str, ...],
    kind: str,
) -> Invocation | None:
    """Find the pending invocation a resolution event closes."""
    invocation_id = event.get("invocation_id")
    if invocation_id is not None:
        candidate = by_invocation_id.get(str(invocation_id))
        if candidate is not None and candidate.status == "pending":
            return candidate
        if candidate is not None:
            return None
    # Fall back to the innermost pending invocation of the right kind on this
    # causal path, which is what the enclosing identity points at.
    for invocation in reversed(invocations):
        if invocation.status != "pending" or invocation.kind != kind:
            continue
        if invocation.identity == enclosing or invocation.parent == enclosing:
            return invocation
    return None


def align(baseline: InvocationGraph, candidate: InvocationGraph) -> Alignment:
    """Align two invocation graphs by logical identity.

    Returns every decision, including the ones that prevent a clean comparison.
    The result is ``partial`` when either side carries capture gaps, because an
    alignment computed from incomplete evidence cannot be trusted to be
    one-to-one even where it looks complete.
    """
    if not isinstance(baseline, InvocationGraph) or not isinstance(candidate, InvocationGraph):
        raise AlignmentError("align expects two InvocationGraph values")

    baseline_by_identity = baseline.by_identity
    candidate_by_identity = candidate.by_identity

    pairs: list[AlignedPair] = []
    identities = sorted(
        set(baseline_by_identity) | set(candidate_by_identity),
        key=lambda identity: (len(identity), identity),
    )

    for identity in identities:
        left = baseline_by_identity.get(identity)
        right = candidate_by_identity.get(identity)
        if left is not None and right is not None:
            if not left.is_identifiable or not right.is_identifiable:
                pairs.append(
                    AlignedPair(
                        "ambiguous",
                        identity,
                        left,
                        right,
                        "invocation has no name, so identity cannot be established",
                    )
                )
            elif left.concurrent or right.concurrent:
                pairs.append(
                    AlignedPair(
                        "ambiguous",
                        identity,
                        left,
                        right,
                        "concurrent siblings share a name, so their order is not an identity",
                    )
                )
            elif left.kind != right.kind:
                pairs.append(
                    AlignedPair(
                        "ambiguous", identity, left, right, "kind differs at the same identity"
                    )
                )
            else:
                pairs.append(AlignedPair("matched", identity, left, right))
        elif left is not None:
            pairs.append(AlignedPair("missing", identity, left, None, "absent from the candidate run"))
        else:
            pairs.append(AlignedPair("extra", identity, None, right, "not present in the baseline run"))

    reasons = tuple(
        [f"baseline: {gap}" for gap in baseline.gaps]
        + [f"candidate: {gap}" for gap in candidate.gaps]
    )
    return Alignment(tuple(pairs), partial=bool(reasons), reasons=reasons)
