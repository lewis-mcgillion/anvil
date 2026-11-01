# Comparing two runs: causal alignment

Before Anvil can say whether a candidate run *differs* from its baseline, it
has to decide which step in one run corresponds to which step in the other.
That decision is alignment, and it is implemented in
`server/anvil_server/divergence/alignment.py`.

Alignment is deliberately separate from divergence detection. It answers only
"which step is this?" — never "did it change?". Keeping the two apart is what
lets a changed argument be reported against the right call instead of
cascading into a wall of spurious differences.

## Why position does not work

The obvious approach is to compare the two runs step by step. It breaks
immediately on ordinary agent behaviour:

- A parallel branch finishes in a different order, so every later step shifts.
- A retry adds one call, so every subsequent call pairs with the wrong one.
- An edit moves a subtree, so a whole branch reports as changed.

In each case the run is substantially the same, but a positional comparison
reports near-total divergence. The noise is worse than no answer at all,
because it hides the one difference that mattered.

## Logical identity

An invocation is identified by the chain of invocations that caused it, plus
its own kind, name and ordinal among the siblings sharing that parent, kind and
name. A nested lookup under a search is:

```
("tool:search#0", "tool:fetch#0")
```

Causal parentage comes from `parent_event_ids`, ordered by
`playback.order_events`, so alignment sees the same deterministic causal order
the projection does and never consults a captured timestamp.

This gives the three properties that matter:

- **Reordering parallel branches changes nothing.** Siblings are identified by
  name, not position, so two independent branches may complete in either order.
- **Duplicates align one-to-one.** Retries are distinguished by ordinal, so two
  retries align with two retries and a third shows up as `extra` without
  disturbing the first two.
- **A changed branch stays local.** A renamed call is one `missing` and one
  `extra`; its descendants move with it rather than pairing with unrelated
  steps.

## Statuses and confidence

Each pair carries a status:

| Status | Meaning |
| --- | --- |
| `matched` | Same logical step in both runs. Says nothing about contents. |
| `missing` | In the baseline, absent from the candidate. |
| `extra` | In the candidate, absent from the baseline. |
| `ambiguous` | Identity could not be established. Never read as a match. |

The alignment as a whole reports the published `alignment_confidence` value
from [architecture.md](architecture.md):

| Confidence | When |
| --- | --- |
| `exact` | Every invocation identified, no capture gaps. |
| `partial` | Identification succeeded, but evidence is incomplete. |
| `ambiguous` | At least one pair could not be identified. |

Ambiguity outranks incompleteness. `Alignment.is_complete` is true only for an
`exact` alignment with nothing missing or extra, which is the precondition a
gate must check before it is allowed to report a pass.

## Where alignment refuses to guess

Two cases are reported rather than resolved.

**Unnamed invocations.** A request whose payload carries no `tool_name` or
`model` is a capture gap. Two of them under the same parent cannot be told
apart, so they are `ambiguous` rather than paired by position.

**Concurrent identical calls.** Siblings sharing a parent, kind and name that
run on *different streams* have no causal order between them. Their ordinals
come from the deterministic tie-break on stream identity: stable within a run,
but meaningless across runs, because the two runs have different stream ids.
Pairing on that ordinal would be a coin flip, so these are reported as
`ambiguous`. This is the ambiguity `docs/architecture.md` requires for parallel
identical calls.

Note the boundary: two identical calls made *sequentially* on one stream are
causally ordered and align normally. Only genuinely concurrent ones are
ambiguous.

## Two hashes, for two different questions

`alignment_hash()` covers the full report, including the event ids that justify
each decision. Two runs that align identically still produce different values,
because they really are different events. Use it to pin a stored report.

`structure_hash()` covers the decisions alone. It is stable across event ids,
stream assignment and the completion order of parallel branches, and changes
only when the aligned shape of the run changes. This is the value a gate should
compare.

## What alignment does not do

- It does not compare arguments, results or errors. Aligned pairs routinely
  differ; that is the point.
- Reports are structural: `describe()` carries identities, statuses and event
  ids, never captured payloads. Content is compared elsewhere, under the
  redaction policy.
- It performs no I/O, schedules no job and contacts no external system, so
  there is nothing to clean up and no cancellation semantics. An unorderable
  run raises `playback.PlaybackError` and is not aligned at all.

## Limitations

- Alignment trusts the recorded causal graph. A recording that omits
  `parent_event_ids` flattens every invocation to the run root, where siblings
  are distinguished only by name and ordinal.
- Concurrency is inferred from stream identity. An emitter that multiplexes
  genuinely concurrent branches onto one stream will have them aligned as
  sequential duplicates, and one that splits sequential work across streams
  will see avoidable ambiguity.
- Alignment itself reports a renamed call as a `missing`/`extra` pair, because
  the name is part of the identity. Detection recovers the rename where it can
  do so exactly; see below.

# Detecting divergence

Once the steps are aligned, `server/anvil_server/divergence/detect.py` decides
what changed and reports it in terms an engineer can act on: not "these runs
differ" but "`search` was called with different arguments, here are both".

## Findings

Every finding names one of the published diff dimensions, cites the identity it
was found at, and carries both sides as evidence. A category without evidence
is not actionable; evidence without a category cannot be aggregated or gated.

| Category | Reported when |
| --- | --- |
| `changed_input` | The run input differs. |
| `changed_prompt` | Messages or a model request differ. |
| `changed_model` | A model invocation names a different model. |
| `changed_tool_name` | A tool call was renamed. |
| `changed_tool_arguments` | An aligned tool call received different arguments. |
| `changed_output` | A run output, tool result or model response differs. |
| `changed_error` | A failure payload differs. |
| `changed_control_flow` | Outcome, agent, adapter or a call's success changed. |
| `changed_budget` | Reported usage differs. |
| `missing_call` | In the baseline, absent from the candidate. |
| `extra_call` | In the candidate, absent from the baseline. |
| `capture_gap` | Evidence is declared or known to be incomplete. |
| `ambiguous_alignment` | A step could not be identified. |

Findings are deduplicated and ordered by identity depth, then identity, then
published category order, so run-level findings come first and nested calls
follow their parents. The order depends only on content, never on the order
events arrived in.

## Status

The published `deterministic` status follows two rules:

- An observed difference is a `fail`, even alongside missing evidence. A real
  change is not made uncertain by an unrelated gap.
- Missing evidence alone — a capture gap, an unresolved invocation, or anything
  less than an `exact` alignment — is `inconclusive`. It is never a `pass`.

A `pass` therefore means: complete evidence, exact alignment, no differences.

## Normalization

All values are compared through an explicit, versioned
`NormalizationPolicy`, so key order and number formatting are not differences
but genuine changes are. The policy descriptor is recorded in the report and
contributes to `comparison_hash()`: two reports that reached the same verdict
under different policies are not the same report and do not hash alike.

## Recovering renames

A renamed call cannot align, because the name is part of the identity, so it
arrives as one `missing` and one `extra`. Detection recovers it, but only under
an exact structural rule: the two must share a parent, a kind and an ordinal,
and must be the only missing and the only extra call in that group. Where more
than one candidate exists there is no unique answer, and both stay `missing`
and `extra` rather than being paired on a guess.

Because a rename moves the whole subtree, descendants are re-based under the
recovered pair by exact prefix substitution. A descendant that *also* changed
has no exact counterpart and stays `missing` or `extra`, so a rename can never
hide a second change underneath it.

## Limitations

- Detection compares what the recording contains. Fields removed by SDK-side
  redaction before capture are not visible to it, and a policy that ignores a
  pointer suppresses differences there by design — which is why the policy is
  part of the report.
- Budgets are compared as recorded usage. Detection does not model cost or
  apply thresholds; gating on a budget belongs to the aggregation layer.
- Streaming model chunks are not compared chunk by chunk. The completed
  response is, so a different stream shape with an identical result is not a
  difference.
