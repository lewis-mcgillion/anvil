# Resilience drill plan (not yet executed)

Run these exercises only in disposable test infrastructure with synthetic
fixtures and zero production credentials. For every drill, record software
revision, host/platform, workload size, observed error/outcome, recovery time,
runner/container counts and memory high-water mark. Abort if isolation cannot
be verified or an irreversible operation would target real data.

| Drill | Injection | Required evidence | Stop condition |
|---|---|---|---|
| Database outage | Stop synthetic PostgreSQL while ingesting | Explicit unavailable error, no false seal, healthy retry after restore | Any successful mutation without durable receipt |
| Disk full | Fill capped disposable database volume | Storage error, bounded queue/memory, no corrupt manifest | Unbounded memory or missing failed state |
| Worker crash | Kill worker with leased seal job | Expired lease recoverable, one sealed recording, stale worker fenced | Duplicate immutable recording |
| Broker outage | Refuse fake-provider connections | No paid call before dispatch; charged attempts evidenced after dispatch | Network fallback or unmetered call |
| High load | Send bounded 1k events/sec synthetic batches | Measured 429/Retry-After, finite backlog and memory | Queue growth without limit |
| Cancellation | Cancel isolated synthetic runner during model/tool wait | Terminal cancelled status, no orphan process or UDS | Process survives cancellation timeout |

Current status: these drills cannot yet be run end-to-end because the
isolated supervisor, live broker and complete Compose deployment are not
implemented. A table of expectations is not evidence they pass; do not
promote this checklist to a release gate until actual logs and metrics from
automated drills have been collected.
