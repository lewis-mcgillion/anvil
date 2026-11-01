# Anvil documentation

- [Architecture and technical plan](architecture.md) — the baseline contract:
  execution semantics, APIs, storage, security rules, testing and milestones.
- [What Anvil guarantees](guarantees.md) — every claim with the test that
  proves it, and an explicit list of what is not guaranteed.
- [Architecture decision records](adr/README.md) — binding decisions for
  execution modes, trust boundaries, storage, isolation, replay and the
  model broker.
- [Mandatory server-side capture policy](capture-policy.md) — what the server
  removes from every stored event, why it is not configurable, and what
  redaction costs a recording.
- [Audit trail, deletion and privacy](audit-and-deletion.md) — what is
  audited, what an audit row may contain, and what deleting a run destroys.
- [Hardening the HTTP surface](http-security.md) — the headers on every
  response, deny-by-default cross-origin access, and why CSRF is removed
  rather than mitigated.
- [Threat model](threat-model.md) — assets, adversaries, trust boundaries,
  threat scenarios (untrusted code, malicious recordings, secret leakage,
  tenant isolation) and operational rules for errors, cancellation and
  cleanup.

Documentation is validated in CI by `scripts/check_docs.py` (docs checks
workflow) and by `tests/test_guarantees.py`, which fails if a documented
guarantee cites a test that does not exist.
