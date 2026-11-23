# Alpha/beta readiness evidence gate

Status: **not ready**. Never sign or publish a release based only on this
checklist. Evidence must come from executed tests on two independent,
documented self-hosted environments and three synthetic agent workloads.

| Gate | Required proof | Current state |
|---|---|---|
| Synthetic agents | Three different agents capture, seal, replay and compare | Blocked by runner/orchestration #45/#50 |
| Self-hosted installs | Fresh startup in two environments with recorded artifacts | Blocked by Compose #87 |
| CI regression path | Baseline passes and deliberate regression fails without secrets | Blocked by suite/CLI #69/#70/#71 |
| Isolation | Production network/tool side effects denied under adversarial probes | Blocked by #45/#83 |
| Security/operability | TLS/OIDC, backup/restore, budgets and incident tests pass | Blocked by #82/#88/#90/#92 |
| Artifact provenance | Signed artifacts, SBOM and verification | Blocked by #85/#99 |

For each environment, retain commit, image digest, hardware/OS versions,
recording hashes, commands, timings, exit codes, logs with secrets removed,
failure drill outcomes and outstanding issue references. A missing record or
inconclusive test counts as **not ready**, never as a pass. Re-evaluate only
after blockers are resolved; do not silently waive a safety gate.
