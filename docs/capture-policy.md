# Mandatory server-side capture policy

Anvil's SDK redacts before it buffers anything, but the server cannot assume
the SDK was used: the capture API accepts direct HTTP uploads, and a recording
imported from elsewhere was redacted by someone else's rules. This document
describes the second pass the server applies to every event it stores.

Implementation: `server/anvil_server/policy.py`, applied by
`apply_policy()` in `server/anvil_server/events.py`.

## Where it runs

`accept_batch()` applies the policy as its first action, before validation and
before the content hash is computed. Every write path reaches storage through
`accept_batch()`, so there is no route, worker or helper that can store a
payload the policy has not seen.

Hashing after redaction matters: a recording's identity must attest to what was
actually stored, not to bytes the server deliberately discarded. Hashing first
would produce a digest that nothing in the system can reproduce.

## It is not configurable

There is no project setting, header or scope that turns the policy off. An
operator who could disable it for one project could be persuaded to disable it
for the project that mattered, and the resulting recording would look exactly
like one that had been cleaned. The SDK's redaction *is* configurable, because
there the author knows what their payloads mean; the server sees arbitrary
projects' data and gets no such context.

## What it removes

The server's rules are deliberately narrower than the SDK's allowlist. An
allowlist is right when you know the shape of your data and wrong when you do
not: applied blindly to another project's payloads it would destroy the
evidence the recording exists to preserve. So the server removes only what is
credential-shaped on any reading.

**Credential-named keys**, matched word-aware rather than by substring —
`token` must not condemn `tokenizer`, and `key` must not condemn `sort_key`.
Keys are split on underscores, hyphens, dots and camelCase boundaries, and a
pair list covers `api key`, `private key`, `secret key` and similar. A false
positive is silent: nobody discovers the deleted evidence until they need it.

**Credential-shaped values**, regardless of the key holding them: `Bearer`
tokens, `sk-` provider keys, AWS `AKIA`/`ASIA` identifiers, GitHub `gh*_`
tokens, Slack `xox*-` tokens, JWTs, PEM private key blocks and `key=value`
assignments of credential-named variables.

Nesting is walked to a bounded depth. A payload too deep to inspect is
rejected rather than stored unexamined — the policy never stores what it could
not read.

## What it does to what it removes

A removed value is replaced with `[REDACTED]`, never dropped. A dropped field
is indistinguishable from one that was never sent, which turns a redaction into
a silent falsification of the record.

Each removal is recorded as a JSON pointer in the event's `redactions` array,
so a reader can see exactly what was taken and from where. Pointers the client
declared are preserved; the server's are merged with them.

Any event the server redacted loses replay eligibility: `replay_safe` combines
with `and` and is never granted by the server. Replaying a `[REDACTED]`
placeholder would fabricate evidence of an exchange that never happened.

## Errors never quote the payload

`PolicyError` messages describe the rule that failed and never include the
value that triggered it. An error that names what it rejected copies the exact
secret into application logs and into the client's HTTP response — the two
places the policy exists to keep it out of.

## Limits

This is pattern matching. A secret in an unexpected shape, in a field named
nothing in particular, survives until the policy is updated. The canary tests
in `server/tests/test_postgres_integration.py` measure the policy against real
PostgreSQL storage and a real exported archive, but measuring is not proving.
Treat recordings as sensitive regardless, as the
[threat model](threat-model.md) requires.
