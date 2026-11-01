# Anvil documentation

- [Architecture and technical plan](architecture.md) — the baseline contract:
  execution semantics, APIs, storage, security rules, testing and milestones.
- [Architecture decision records](adr/README.md) — binding decisions for
  execution modes, trust boundaries, storage, isolation, replay and the
  model broker.
- [Mandatory server-side capture policy](capture-policy.md) — what the server
  removes from every stored event, why it is not configurable, and what
  redaction costs a recording.
- [Audit trail, deletion and privacy](audit-and-deletion.md) — what is
  audited, what an audit row may contain, and what deleting a run destroys.
- [Threat model](threat-model.md) — assets, adversaries, trust boundaries,
  threat scenarios (untrusted code, malicious recordings, secret leakage,
  tenant isolation) and operational rules for errors, cancellation and
  cleanup.

Documentation is validated in CI by `scripts/check_docs.py` (docs checks
workflow).
