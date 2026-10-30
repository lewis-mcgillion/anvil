# Regression suite foundations

The Python `anvil_server.suites` package parses and compiles
`apiVersion: anvil.dev/v1alpha1`, `kind: RegressionSuite` documents and evaluates
deterministic assertions against supplied evidence. It does **not** run an
agent, schedule jobs, resolve remote resources, invoke production tools or
pretend that an unavailable runner succeeded.

## Alpha contract and compatibility

This is the initial, closed alpha suite interface, independent of the frozen
event/recording v1 schemas. `suite_schema()` exposes the structural JSON Schema
from the parser models; semantic checks below are additionally mandatory.
Unknown versions, kinds and fields at every contract object are rejected.
Arbitrary keys are allowed only in JSON data such as initial state, expected
output, arguments and self-contained output schemas. Future incompatible
changes require a new `apiVersion` and compiler version, not reinterpretation
of an existing lock. No existing OpenAPI, event or recording schema is changed.

A suite has `name` and nonempty `cases`. Each uniquely identified case supplies:

| Field | Contract |
|---|---|
| `id` | Unique ASCII identifier |
| `recording` | `{file: relative/path, sha256: <64 lowercase hex>}` |
| `agent` | `image: repository@sha256:<64 hex>`, Python `entrypoint: module:function`, `adapter` name and exact semantic `adapter_version` |
| `seed` | Nonnegative JSON-safe integer |
| `model_mode`, `tool_policy` | Only `recorded` and `strict`; defaulted explicitly in the lock |
| `normalizer_version`, `parameter_version` | Only `"1"`; included in the lock |
| `initial_state` | JSON object, default `{}` |
| `fault_plan` | Optional local file/hash reference; no executable inline plan |
| `budgets` | Closed object with architecture defaults; nonnegative limits, positive wall time |
| `assertions` | Nonempty list of `{kind, expected, target?}` |

Budget names are `max_wall_time_seconds`, `max_model_calls`, `max_tool_calls`,
`max_input_tokens`, `max_output_tokens`, `max_estimated_model_spend_usd`.
See `fixtures/suites/baseline.yaml` for a checksum-only compilation example;
its opaque recording fixture intentionally does not claim sealed-run validity.

## Safe parsing and immutable compilation

`parse_suite(source)` bounds UTF-8 input to 1 MiB, 20,000 YAML events and
32 collection levels before construction. Duplicate/non-string mapping keys,
all anchors/aliases (including merge keys), explicit tags, extra documents,
non-JSON scalars, nonfinite/unsafe numbers, unknown fields, mutable image tags,
template/shell interpolation markers (`${`, `{{`, `$(`, backticks) and unsafe
reference paths fail explicitly with `SuiteError`. There is no expression
evaluation, YAML object construction, shell expansion or URL fetching.

`compile_suite(source, root: Path)` opens only relative regular files beneath
the trusted root directory. Each path component is opened relative to its
directory descriptor with symlinks forbidden, including the final file.
Absolute paths, empty/dot/parent segments, backslashes, URL schemes, missing
files, symlinks, directories and special files fail closed. No extraction
or file writes occur. References are bounded to 64 MiB total and SHA-256
checked against the declared bytes. Shared references are read once;
conflicting hashes reject. Changed content rejects on recompilation.

Compilation captures immutable bytes, not live paths. The returned frozen
`CompiledSuite` contains canonical RFC 8785 bytes, a SHA-256 content address
and a sorted tuple of frozen reference snapshots. The canonical envelope
includes compiler version, complete normalized suite configuration and
reference paths/hashes/sizes. Comments, mapping order and default spelling
do not affect the hash. Image digest, adapter/version, seed, normalizer,
parameters, fault references, budgets and assertions all affect it. No time,
host path or random identifier is included.

An OCI digest is syntactically pinned, not fetched or attested. Adapter
versions are pinned, not claimed installed. Recording/fault-plan snapshots
are opaque, checksum-verified bytes, not yet validated engine inputs.
Runner integration must validate their published formats and supported
adapter availability before execution. Use lock snapshots, never reopen
paths during a case. Persist the canonical bytes and reference snapshots
atomically in the future suite-version repository; this package does not
implement database persistence.

## Evidence and assertions

`evaluate(Assertion(...), Evidence(...))` returns an immutable result with
`kind`, `status` (`pass`, `fail`, `inconclusive`) and `reason`. It is pure and
has no execution fallback. Capture must be explicitly `complete: true` and
alignment `exact`; otherwise every assertion is inconclusive. The caller
must establish these facts from sealed engine evidence, not infer them from
successful parsing or compilation. Evidence is revalidated at evaluation.

| Kind | Expected / target | Evidence and deterministic meaning |
|---|---|---|
| `output.exact` | Any JSON | Canonical equality; booleans differ from numbers |
| `output.contains` | Nonempty string | Literal substring of string output |
| `output.schema` | Self-contained JSON Schema 2020-12 object | Schema validity; no references or regex keywords, formats are annotations |
| `output.pointer` | JSON / RFC 6901 target | Exact equality at pointer, including empty root pointer |
| `tool.called` | Boolean / tool name | At least one call exists (or none if false) |
| `tool.count` | Nonnegative integer / tool name | Exact number of named calls |
| `tool.arguments` | Object / tool name | At least one named call has exact canonical arguments |
| `tool.order` | List of names | Exact causal order supplied explicitly by engine |
| `fixtures.unmatched` | Nonnegative integer | Exact unmatched fixture count |
| `model.request` | Object | At least one exact canonical model request |
| `outcome` | Execution outcome | Known terminal execution outcome equals expected |
| `budget` | Nonnegative upper bound / budget name | Actual measured consumption is at or below bound |
| `fault.applied` | Fault ID string | Named fault explicitly observed as applied |

Absent evidence (`None`, or omitted output) is inconclusive; explicit empty
lists are negative evidence, not missing data. Explicit JSON null is a valid
output. Missing pointer paths fail when the output is present. `unknown`
outcomes remain inconclusive even when expected is `unknown`. A known
unreached fault fails. Ordering must not be manufactured by sorting timestamps
or by assuming cross-stream ordering. Budget evidence must be measured usage,
not the configured limit, and simulated fault latency is not measured wall
time. No judge result can override this evaluator.

## Errors, cancellation and integration boundary

Parsing/compilation raises `SuiteError` for invalid input or reference I/O;
direct assertion/evidence construction raises validation errors for malformed
data. Evaluation does not catch errors and return success. File descriptors
are closed in `finally` and bounded reads close streams even on interruption.
These synchronous, bounded foundations do not spawn workers or allocate
external resources; async callers should run compilation off the event loop
and own cancellation before scheduling any execution.

ANVIL-055 through ANVIL-057 foundations are implemented here. ANVIL-058
case scheduling, quotas, retry/cancellation and real runner isolation,
ANVIL-059 CLI/report exporters/exit codes, and ANVIL-060 reusable CI workflow
remain separate integrations. In particular, compilation is **not** a
test run and must never be reported as a passing suite.
