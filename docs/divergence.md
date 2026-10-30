# Offline deterministic comparison (E08)

The internal Python package `anvil_server.divergence` compares recorded evidence
in memory. It never schedules a job, calls a model, invokes a tool, reads a
database, or opens a network connection. Its synchronous functions have no owned
resources to close; callers should schedule CPU work off an async request loop.
There is no partial-success return on an exception. Cancellation of orchestration
must be supplied as an independent infrastructure status, not converted to pass.

## Dependencies and scope

This package depends on the shared `anvil_server.canonical` implementation from
commit `e188862` and on the existing local `anvil-schema` package in `sdk/python`.
No third-party canonicalization library is needed. The server package declares that dependency, and repository CI installs the
local SDK distribution before the server so its frozen schemas are available.

The library addresses ANVIL-043 (#52), ANVIL-044 (#53), ANVIL-045 (#54), and
ANVIL-048 (#57). REST comparison jobs, persistence, pagination, CLI wiring (#55),
and model-broker/schema-constrained judge execution (#56) are not implemented
here. The library only accepts an already-established judge/infrastructure status.
Those adapters must preserve the independent statuses and authenticate evidence
before constructing a `Recording`.

## Version-1 input and normalization

Construct a `Recording(events=tuple_of_v1_event_dicts,
capture_completeness="sealed_complete", logical_branches={})`, then call
`compare(baseline, candidate)`. Every event is validated against the frozen v1
schema before alignment. Unknown fields/types and malformed values are errors,
never silently removed. Duplicate IDs/sequences, mixed run IDs, and causal cycles
are rejected. Missing parents, sequence gaps, open/incomplete capture, redactions,
unmatched invocation lifecycle events, and disconnected evidence make comparison
inconclusive even if both recordings are identically damaged.

Normalization uses RFC 8785, including ECMAScript binary64 number serialization
and UTF-16 property ordering. It preserves Unicode spelling, array order, every
payload field, and measured timing by default. NaN, infinity, integers outside the
safe integer domain, lone surrogates, cycles, and non-string object keys are
rejected by the shared canonicalizer, including in fields configured for removal.
Numbers `1` and `1.0` are canonically identical; booleans are not numbers.

`NormalizationPolicy(version="1", ignore_pointers=("/duration_ms",),
text_pointers=("/output",), normalize_whitespace=True, casefold_strings=True)`
is an explicit opt-in example, not a default. Pointers are RFC 6901 paths relative
to **each event payload**. There are no wildcard paths or implicit time/text
ignores. Root removal, invalid escapes, duplicate/overlapping pointers, non-string
text targets, and non-boolean transformation switches are errors. Array indices
refer to the original document, independent of removal order. A pointer may be
absent from an event type, but must match at least one payload in the comparison;
otherwise configuration fails rather than silently accepting a typo.

The report pins the full policy descriptor and records every applied pointer,
event ID, original payload hash, and normalized hash. Inputs are never mutated.
Redaction completeness is checked **before** normalization and cannot be ignored
away.

## Causal alignment and confidence

`causal-payload-v1` aligns structural causal paths, event family/phase, and model
chunk index. A terminal success/failure uses the same alignment phase so a changed
error produces actionable evidence instead of an unrelated missing invocation.
Requests and their terminal/chunk events must retain causal linkage. Arbitrary
event/run/invocation/stream IDs, trace/span IDs, arrival order, per-stream sequence
numbers, and source timestamps are capture metadata, not behavioral payload.
They are validated and retained in original evidence hashes and report references,
but their literal values do not determine cross-run identity or payload equality.
Causal edges, by contrast, are part of the structural identity.

Frozen v1 events do not publish stable branch path fields. To distinguish parallel
calls, supply `logical_branches={invocation_id: ("agent", "branch", "call-site")}`
on **each** recording, using stable logical labels from trusted adapter metadata.
These are sidecars, not hidden additions to the frozen event schema. Unknown
invocation references and invalid paths fail validation. A changed branch path
is missing/extra evidence, not opportunistically matched by equal arguments.
Absent distinguishing paths, parallel siblings are ambiguous, even if their
arguments/results happen to differ. No sequence/timestamp tie-breaker, payload
similarity guess, or greedy consumption is used to turn ambiguity into a pass.
Reports list the ambiguous event IDs instead.

`exact` means every event has one structurally unique counterpart and evidence is
complete. `partial` means missing/extra paths or incomplete evidence. `ambiguous`
means at least one structural key has multiple candidates. Changed causal
branches may therefore produce several missing/extra events, not a fabricated
single-call match. Unique matched subgraphs still yield findings, but incomplete
or ambiguous evidence makes the deterministic result inconclusive.

## Findings and result semantics

Every matched payload is recursively compared without a field allowlist. Finding
categories include input, model request, tool identity/schema, arguments,
results, errors, outputs, usage/timing budgets, control flow, and otherwise
unclassified payload changes. Missing/extra events are separate categories.
Findings carry event IDs, RFC 6901 paths, presence flags (missing differs from
JSON null), and canonical fragment SHA-256 digests. Raw prompt/result values are
not copied into reports or logs; callers may resolve pointers against the original
immutable evidence under their existing access controls. Reports include original
recording content hashes, independent of input list order, to bind the evidence.
Usage/timing changes are detected; the library does not enforce an external
suite's absolute budget thresholds.

The internal report has `report_version="1"`, `projection_version`, normalization
descriptor/evidence, recording hashes, findings, alignment evidence, and separate
`deterministic`, `judge`, `infrastructure`, and `alignment_confidence` fields.
This is not a new REST/schema contract: API adapters must explicitly version and
validate their public report rather than expose this dict as an undocumented
OpenAPI change.

`aggregate()` validates all statuses and combines them in this exhaustive
precedence order. Every status combination is covered by the tests.

| Condition (first match wins) | Gate | Exit |
|---|---|---|
| Infrastructure cancelled | cancelled | 4 |
| Infrastructure error or timed out | infrastructure_error | 3 |
| Deterministic fail, or required judge fail | fail | 1 |
| Deterministic inconclusive, or alignment not exact | inconclusive | 5 |
| Required judge inconclusive or not_run | inconclusive | 5 |
| Complete deterministic pass and all enabled gates pass | pass | 0 |

Judges are optional/off by default. Their pass never overrides deterministic
failure, ambiguous/incomplete evidence, or infrastructure failure. A disabled
judge gate has no effect, even if the separately reported judge failed. Invalid
input raises an explicit exception; the eventual CLI adapter should map it to
exit 2. Invalid/refused judge responses must be mapped by the judge adapter to
`inconclusive`, never supplied as `pass`.

## Checks

From `server/`, with the shared canonical dependency available:

```sh
PYTHONPATH=.:../sdk/python /tmp/anvilvenv/bin/python -m pytest -q tests/test_divergence.py
```

The tests cover single-mutation categories, IDs/timestamps/order invariance,
duplicate/changed branches, missing evidence, malformed graphs, explicit
normalization, pointer escapes and array removal, non-mutation, unknown-field
rejection, all 288 status/gate combinations, and shared Python/Node RFC 8785
fixtures. Node is required for the cross-language gate, not silently skipped.
All test model/tool data is synthetic; no provider credentials are needed.
The architecture's 10k-event/5-second target is not claimed as achieved here.
