# Comparison normalization

Divergence detection compares two runs by hashing them. Normalization is what
makes that comparison meaningful: it decides which differences are real and
which are noise, and it must reach the same answer in every language that
implements it.

This document describes the versioned policy (ANVIL-043) implemented in
`server/anvil_server/canonical.py` and, independently, in
`web/schemas/src/normalization.ts`.

## Canonical form

Values are serialized with RFC 8785 (JSON Canonicalization Scheme). Object keys
are sorted by UTF-16 code unit, numbers use ECMAScript double formatting, and
strings use the JCS escape set. Arrays are sequences, so their order is data and
is never sorted.

Canonicalization is fail-closed. It refuses any value whose canonical form would
be ambiguous rather than silently coercing it:

| Rejected input | Why |
| --- | --- |
| `NaN`, `Infinity` | No JSON representation |
| Python `int` beyond ±(2⁵³−1) | JCS numbers are doubles; the round-trip would be lossy |
| Non-string object keys | Coercion could make two different keys collide |
| Unpaired UTF-16 surrogates | Cannot be encoded as UTF-8 |
| Cycles | Would recurse until the interpreter fails |

The module performs no I/O and holds no resources, so there is nothing to clean
up and no cancellation semantics; callers own any surrounding timeouts.

## Policy

A `NormalizationPolicy` is versioned (`VERSION = "1"`) and explicitly
configured. It has three knobs, all off by default:

- `ignore_pointers` — JSON pointers to remove before hashing, for genuinely
  incomparable evidence such as timing or trace identifiers.
- `normalize_whitespace` — collapse runs of whitespace and trim.
- `casefold_strings` — Unicode full case folding.

The policy descriptor is hashed together with the data, so a lenient policy can
never produce the same hash as a strict one. Comparing two runs under different
policies therefore cannot silently look equal.

### Hidden field removal is impossible

This is the property the policy exists to guarantee:

- Only pointers the policy explicitly names are removed. There are no wildcards
  and no recursive matching, so `/trace_id` does not touch `/a/trace_id`.
- The document root cannot be ignored.
- Every removal is reported in `NormalizationEvidence.removed`.
- A pointer that matches nothing is reported in `NormalizationEvidence.unmatched`
  rather than passing silently. An ignore that quietly matches nothing is how a
  policy drifts away from the data it was written for, so a report can never
  imply a field was absent when the ignore was simply misdirected.

Evidence is for reporting only; it does not affect the hash.

### Array-index ignores are positional

A pointer such as `/items/1` addresses a position, not an element. Removing it
shifts the remaining elements, so applying the same policy twice removes a
different element the second time. Policies are applied once per document, so
this is a sharp edge rather than a defect, but prefer ignoring a named field
over an array index where you have the choice. The behaviour is pinned by a
test so it stays visible.

## Cross-language agreement

`fixtures/normalization/v1/cases.json` is the contract. Each case publishes the
policy descriptor, the input, the expected normalized value, the expected
canonical string, the expected evidence and the expected hash.

Two independent implementations are checked against it:

- Python — `server/tests/test_normalization_fixtures.py`
- TypeScript — `web/schemas/test/normalization.test.ts`

The implementations deliberately share no code. Agreement between them is what
makes the canonical form a contract rather than an artefact of one runtime.
The corpus also includes sensitivity checks, so a golden that could not fail
would be caught.

Regenerating the corpus changes published hashes and is therefore a
compatibility event, not a routine refresh.

### Case folding

JavaScript has no case-folding operation. `String.prototype.toLowerCase` is a
lowercase mapping, so it leaves `ß` alone where Unicode full folding maps it to
`ss`, and it leaves the `ﬁ` ligature and final sigma unfolded.

`web/schemas/src/casefold-exceptions.ts` is a generated table of every code
point where folding and lowercasing differ. The TypeScript implementation
consults it before falling back to `toLowerCase`. A Python test regenerates the
table and fails if it has drifted from the running Unicode data, so the two
languages cannot silently diverge after a Unicode upgrade.

Regenerate with:

```
python server/tests/tools/generate_casefold_exceptions.py
```

## Known limitations

- The large-integer rejection is reachable only from Python, which has a
  distinct `int` type. JSON input cannot express that case, so the TypeScript
  implementation accepts large doubles such as `1e21`, exactly as Python does
  for floats.
- Whitespace folding uses each language's native whitespace definition. These
  agree for the whitespace in the published corpus, but the definitions are not
  character-for-character identical across the full Unicode range.
- Normalization operates on decoded JSON values. It says nothing about how those
  values were captured; capture gaps are a separate concern and must not be
  normalized away.
