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
- A renamed call is reported as a `missing`/`extra` pair, not as a rename.
  Recognising that two differently named calls are "the same step" would need
  a similarity heuristic, which is deliberately out of scope: it would let a
  gate pass on a guess.
