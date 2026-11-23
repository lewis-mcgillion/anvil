# Recorded playback

Playback answers one question: *what did this run actually do?* It is a pure
read over immutable stored events. Playback never loads an agent, calls a
tool, contacts a model provider or schedules a job — see
[`docs/threat-model.md`](threat-model.md) objective **O1**.

Because that guarantee is invisible in a payload, every playback response —
REST and CLI alike — carries explicit mode labels:

```json
{
  "mode": "recorded_playback",
  "executes_agent_code": false,
  "executes_tools": false,
  "contacts_model_provider": false,
  "schedules_jobs": false
}
```

A client can therefore *prove* which execution mode produced a result rather
than trusting the server. The server-side tests enforce this with tripwires: a
broker that raises on any call and job-queue methods monkeypatched to raise, so
the suite fails if playback ever schedules or calls out.

## Projection

A projection is a deterministic fold over a recording's events, pinned by
`projection_version` (currently `1`). Supported versions are advertised by the
API; an unsupported version is refused rather than approximated.

Ordering is derived from **causality and sequence, never arrival order and
never wall-clock time**. `occurred_at` is presentation metadata: a test
reverses every timestamp in the corpus and asserts the projection hash is
unchanged. Events are ordered by a topological sort over `parent_event_ids`
with a deterministic tie-break of `(stream_id, sequence, event_id)`.

A recording that cannot be projected **fails closed** with an explicit error
rather than producing a partial answer:

| Condition | Error |
| --- | --- |
| A parent event id is not present | `MissingParentError` |
| Causal parents form a cycle | `CausalCycleError` |
| A stream's sequence has a gap or duplicate | `SequenceError` |

Facts playback cannot establish are reported as **replay blockers** rather
than guessed. For example, a `model.chunk` whose invocation was never
requested is recorded as a blocker; its content is deliberately not attributed
to an invocation.

### Chunk payloads

Streamed content uses the `chunk` and `chunk_index` fields published in
[`schemas/v1`](../schemas/v1). These are the fields the capture SDK and the
Google ADK adapter emit, and the fields playback and stream reconstruction
read. A regression test asserts all four agree, because a producer/consumer
field-name split silently drops streamed content from every projection.

## REST interface

Both endpoints require the `runs:read` scope and are project-scoped.

```
GET /v1/projects/{project_id}/runs/{run_id}/projection
    ?cursor=<event_id>&projection_version=1
GET /v1/projects/{project_id}/runs/{run_id}/streams
    ?projection_version=1
```

`projection` returns the fold, its canonical `projection_hash`, the current
`position`, `checkpoint_positions`, and a `next_cursor` for forward paging.
Omitting `cursor` projects the whole recording. `streams` returns the
reconstructed presentation per invocation: the text a user saw, its frames and
whether the stream was `truncated`.

Failure responses use the standard problem shape, where the stable identifier
is in `code` (`type` stays `about:blank`):

| Status | `code` | Cause |
| --- | --- | --- |
| 400 | `unsupported_projection_version` | Unknown `projection_version` |
| 400 | `invalid_cursor` | Cursor is not an event in this recording |
| 401 | `unauthenticated` | Missing or invalid bearer token |
| 403 | `forbidden` | Token lacks `runs:read` for this project |
| 404 | `not_found` | No such run in this project |
| 422 | `unprojectable_recording` | Recording fails closed (table above) |
| 422 | `unreconstructable_stream` | A chunk has no invocation or a non-string payload |
| 503 | `not_ready` | Database unavailable |

## CLI

The CLI is installed as `anvil` (`anvil_server.cli:main`).

```sh
# Project a recording held on disk and print its canonical hash.
anvil playback projection --recording run.json

# Stop at a specific event.
anvil playback projection --recording run.json --cursor <event_id>

# Reconstruct what the user saw.
anvil playback streams --recording run.json --transcript

# Replay the stream frame by frame.
anvil playback play --recording run.json --speed 4

# Fail CI if a recording no longer projects to a known hash.
anvil playback verify --recording run.json --expect <sha256>
```

Read from a live server instead of a file with `--api`, `--project` and
`--run`. The CLI issues `GET` requests only; it has no code path that writes.
The list-events endpoint is paginated, so the CLI follows `next_cursor` to the
end of the recording — projecting a single page would silently truncate the
result. A repeated or non-string cursor is treated as a server fault rather
than looped on, and paging is bounded.

Supply credentials via the `ANVIL_TOKEN` environment variable in preference to
`--token`, and note that a token is never echoed into output.

`--recording` and `--api` are mutually exclusive, and `--api` accepts only
`http`/`https` URLs, so a recording source cannot be redirected at a local
file.

### Exit codes

CI pipelines branch on these, so they are stable:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Usage error — unreadable file, bad option, unknown cursor |
| 2 | The recording could not be projected or reconstructed |
| 3 | The API rejected the request or could not be reached |
| 130 | Interrupted by the operator |

### Timing, cancellation and cleanup

`anvil playback play` reports recorded timing using a **simulated clock**; it
never sleeps against the wall clock, so it cannot hang a pipeline. A test
replaces `time.sleep` with a function that raises to enforce this. Frame
identity is independent of `--speed`.

Each playback session is entered as a context manager and released on every
path, including interrupt. `SIGINT` exits `130`, and a closed downstream pipe
exits `0` rather than raising `BrokenPipeError`.

## Determinism and conformance

ANVIL-036 publishes the guarantee that *the same recording projects to the
same hash everywhere*. The language-neutral corpus lives in
[`fixtures/conformance/v1`](../fixtures/conformance/v1); its projections are
pinned in `projections.json`, which records for each fixture:

* `projection_hash` — the canonical hash of the complete projection,
* `position_hashes` — the hash after every prefix, so cursor seeking is pinned
  too,
* `checkpoint_positions` and `event_count`,
* or, for a recording that must fail, `projectable: false` and the error type.

Hashes are SHA-256 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)
canonical JSON form of the projection. Canonicalization is implemented
dependency-free in `anvil_server.canonical` and fails closed on NaN, infinity,
integers beyond 2^53−1, non-string keys, unpaired surrogates and cycles.

The conformance tests assert the published hashes are unchanged when the input
is shuffled, when every timestamp is reversed, when the host time zone and
locale change, when `PYTHONHASHSEED` varies across subprocesses, and when the
projection is repeated within one process. They also assert the goldens are
actually *sensitive*: mutating a payload or dropping an event must change the
hash or fail closed.

`missing-events` is published as a **rejection**: a recording with a capture
gap is not projectable, and that is part of the contract rather than a bug.

### Changing a golden

Regenerate deliberately:

```sh
UPDATE_GOLDEN=1 python -m pytest server/tests/test_playback_conformance.py
```

A golden changing for an existing fixture is a **compatibility event**:
`projection_version` must be incremented, because downstream consumers may
have stored the previous hash. See [`docs/compatibility.md`](compatibility.md).

## Known limitations

* Playback reads events that are already stored; it does not seal, import or
  repair a recording.
* `anvil playback play` renders text chunks. Non-text streamed content is
  rejected rather than rendered as a guess.
* The CLI's `--api` source reads a run's events in one request and does not
  page; very large recordings are better projected server-side.
