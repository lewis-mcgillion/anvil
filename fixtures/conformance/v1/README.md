# Conformance fixtures

This directory is an immutable, language-neutral corpus for Anvil event and
recording implementations. Every fixture is a complete JSON recording. The
`manifest.json` file is the index and the SHA-256 digest of each fixture is
the digest of its RFC 8785 canonical JSON representation (not its
pretty-printed file bytes).

The corpus deliberately uses deterministic IDs and timestamps so that Go,
Python, and TypeScript implementations can compare both parsed values and
canonical hashes. Fixture consumers must reject unknown event types, duplicate
event IDs, invalid sequence values, and a recording whose expected outcome
cannot be established from its events.

Scenarios covered:

* `streaming.json` — ordered model chunks and their completed message.
* `parallel-calls.json` — two causal branches sharing a stream.
* `errors.json` — a failed tool and failed run.
* `redaction.json` — a redacted credential and replay ineligibility.
* `missing-events.json` — an explicit capture gap and incomplete capture.
* `retries.json` — a failed first attempt followed by a successful retry.

Verify the corpus without installing dependencies:

```sh
python3 fixtures/conformance/v1/verify.py
```
