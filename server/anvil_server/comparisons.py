"""Asynchronous comparison jobs and their versioned reports (ANVIL-046).

Comparing two runs is not a request-scoped operation: it reads two complete
recordings, aligns them and classifies every difference, so it is queued as a
job and the result is read back separately. That shape drives three decisions
worth stating.

**The three statuses are stored as columns, not derived on read.**
``deterministic``, ``judge`` and ``infrastructure`` describe different things
and a consumer has to be able to see each of them. Collapsing them into one
field, or recomputing them when the report is fetched, would mean a stored
comparison could change its mind later.

**A comparison that has not finished does not read as a pass.** The row is
created with ``deterministic = 'inconclusive'``, so every state between
"queued" and "the detector said so" is honestly inconclusive rather than
accidentally clean. The same applies to cancellation and worker failure, which
set ``infrastructure`` rather than inventing a deterministic verdict.

**Findings are rows, not a blob.** They are the paginated resource, and they
have no natural time ordering, so each one is stored with the detector's
deterministic ordinal and pages are cut on that ordinal. A page is therefore
stable and resumable without the detector being re-run.

Comparison is a pure read of two recordings. It queues no execution, touches
no provider and cannot mutate a recording — the only writes are to the
comparison's own rows.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection
from .divergence import compare
from .divergence.gating import Gate, aggregate
from .identifiers import new_uuid7
from .jobs import JobQueue, LeaseLost
from .repositories import json_safe

REPORT_VERSION = "1"

#: How a job's terminal state maps onto the infrastructure status. A job that
#: did not finish normally means the comparison's evidence is untrustworthy,
#: which the gating layer treats as outranking any deterministic verdict.
JOB_INFRASTRUCTURE = {
    "cancelled": "cancelled",
    "cancelling": "cancelled",
    "failed": "error",
}


class ComparisonError(ValueError):
    """A comparison could not be created or resolved."""


async def create_comparison(engine: AsyncEngine, project_id: UUID, *,
                            baseline_recording_id: UUID, candidate_run_id: UUID,
                            judge_profile: str | None = None) -> dict:
    """Queue a comparison and return its job envelope.

    Both sides are validated before the job exists. Queueing work that is
    already known to be unresolvable would turn a client error into an
    asynchronous infrastructure failure, which is much harder to act on.
    """
    comparison_id, job_id = new_uuid7(), new_uuid7()
    queue = JobQueue(engine, timedelta(seconds=60))
    async with project_connection(engine, project_id) as connection:
        baseline = (await connection.execute(text("""
            SELECT recordings.run_id, runs.capture_status FROM recordings
            JOIN runs ON runs.id = recordings.run_id
            WHERE recordings.project_id = :project AND recordings.id = :recording
        """), {"project": project_id, "recording": baseline_recording_id})).mappings().first()
        if baseline is None:
            raise ComparisonError("baseline recording not found")
        candidate = (await connection.execute(text("""
            SELECT id, capture_status FROM runs
            WHERE project_id = :project AND id = :run
        """), {"project": project_id, "run": candidate_run_id})).mappings().first()
        if candidate is None:
            raise ComparisonError("candidate run not found")
    await queue.create(project_id, job_id, "comparison")
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            INSERT INTO comparisons (id, project_id, baseline_recording_id,
              candidate_run_id, job_id, judge_profile)
            VALUES (:id, :project, :baseline, :candidate, :job, :judge)
        """), {"id": comparison_id, "project": project_id,
               "baseline": baseline_recording_id, "candidate": candidate_run_id,
               "job": job_id, "judge": judge_profile})
    return {"comparison_id": comparison_id, "job_id": job_id, "status": "queued"}


async def get_comparison(engine: AsyncEngine, project_id: UUID, comparison_id: UUID) -> dict | None:
    """Return the published comparison summary, or ``None`` if absent.

    The job's status is joined in rather than mirrored into the comparison row,
    so the two can never disagree about whether the work is still running.
    """
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT comparisons.id, comparisons.deterministic, comparisons.judge,
                   comparisons.infrastructure, comparisons.alignment_confidence,
                   comparisons.report_version, jobs.status
            FROM comparisons JOIN jobs ON jobs.id = comparisons.job_id
              AND jobs.project_id = comparisons.project_id
            WHERE comparisons.project_id = :project AND comparisons.id = :comparison
        """), {"project": project_id, "comparison": comparison_id})).mappings().first()
    if row is None:
        return None
    return {"id": str(row["id"]), "status": row["status"],
            "deterministic": row["deterministic"], "judge": row["judge"],
            "infrastructure": row["infrastructure"],
            "alignment_confidence": row["alignment_confidence"]}


async def get_report(engine: AsyncEngine, project_id: UUID, comparison_id: UUID) -> dict | None:
    """Return the stored versioned report, or ``None`` while one does not exist."""
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT report, report_version FROM comparisons
            WHERE project_id = :project AND id = :comparison
        """), {"project": project_id, "comparison": comparison_id})).mappings().first()
    if row is None or row["report"] is None:
        return None
    return {"report_version": row["report_version"], **row["report"]}


async def list_divergences(engine: AsyncEngine, project_id: UUID, comparison_id: UUID, *,
                           after_ordinal: int | None = None, limit: int = 100,
                           category: str | None = None) -> tuple[list[dict], int | None] | None:
    """Return one page of findings and the ordinal to resume after.

    Returns ``None`` when the comparison does not exist, which the route turns
    into a 404. An existing comparison with no findings is an empty page, not
    a missing resource — "nothing diverged" is a real answer.
    """
    async with project_connection(engine, project_id) as connection:
        exists = (await connection.execute(text("""
            SELECT 1 FROM comparisons WHERE project_id = :project AND id = :comparison
        """), {"project": project_id, "comparison": comparison_id})).first()
        if exists is None:
            return None
        rows = (await connection.execute(text("""
            SELECT id, ordinal, category, summary, evidence_event_ids
            FROM comparison_divergences
            WHERE project_id = :project AND comparison_id = :comparison
              AND (CAST(:after AS integer) IS NULL OR ordinal > CAST(:after AS integer))
              AND (CAST(:category AS text) IS NULL OR category = :category)
            ORDER BY ordinal LIMIT :limit
        """), {"project": project_id, "comparison": comparison_id,
               "after": after_ordinal, "category": category,
               "limit": limit + 1})).mappings().all()
    page = [{"id": str(row["id"]), "category": row["category"],
             "summary": row["summary"],
             "evidence_event_ids": list(row["evidence_event_ids"])}
            for row in rows[:limit]]
    next_ordinal = rows[limit - 1]["ordinal"] if len(rows) > limit else None
    return page, next_ordinal



def divergence_rows(comparison) -> list[dict]:
    """Map internal findings onto the published ``Divergence`` shape.

    This is a deliberate boundary. Detection works in terms of a logical
    *identity* — the causal path that locates a step in both runs — because
    that is what makes a finding comparable across runs. The published API
    instead promises ``summary`` and ``evidence_event_ids``, which are what a
    reader needs to go and look at the recording. The two are different
    vocabularies for good reasons, so the translation is explicit here rather
    than either side being bent to fit the other.

    Event ids are recovered from the alignment, which is the only place that
    knows which concrete events a logical identity resolved to on each side. A
    run-level finding has no identity and therefore no events to cite; it gets
    an empty list rather than a misleading one.
    """
    events: dict[tuple[str, ...], list[str]] = {}
    for pair in comparison.alignment.pairs:
        cited: list[str] = []
        for side in (pair.baseline, pair.candidate):
            # Two runs normally have distinct event ids, but a replayed or
            # re-imported recording can reuse them, and citing the same id
            # twice would read as two pieces of evidence.
            if side is not None and side.event_id and side.event_id not in cited:
                cited.append(side.event_id)
        if cited:
            events[pair.identity] = cited
    rows = []
    for finding in comparison.findings:
        rows.append({
            "category": finding.category,
            "summary": _summary(finding),
            "evidence_event_ids": events.get(tuple(finding.identity), []),
        })
    return rows


def _summary(finding) -> str:
    """A one-line human summary, with the identity included for location.

    The detail alone can read identically for two findings in different parts
    of a run, so the causal path is prefixed. Values are not included: the
    report carries those, and a summary is not the place to leak payloads into
    a listing endpoint.
    """
    where = " > ".join(finding.identity) if finding.identity else "run"
    return f"{where}: {finding.detail}"


async def _events(connection, project_id: UUID, run_id: UUID) -> list[dict]:
    rows = (await connection.execute(text("""
        SELECT event_id, run_id, stream_id, sequence, event_type AS type,
               occurred_at, parent_event_ids, invocation_id, payload
        FROM events WHERE project_id = :project AND run_id = :run
        ORDER BY stream_id, sequence
    """), {"project": project_id, "run": run_id})).mappings().all()
    return [{key: json_safe(value) for key, value in row.items()} for row in rows]


async def _record_infrastructure(engine: AsyncEngine, project_id: UUID, job_id: UUID,
                                 status: str, detail: str) -> None:
    """Record a non-deterministic failure without inventing a verdict.

    ``deterministic`` is deliberately left at its ``inconclusive`` default: the
    comparison did not reach a conclusion, and recording one here would be the
    exact false pass (or false fail) the gating layer exists to prevent.
    """
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            UPDATE comparisons SET infrastructure = :status, report_version = :version,
              report = CAST(:report AS jsonb), finished_at = clock_timestamp()
            WHERE project_id = :project AND job_id = :job
        """), {"project": project_id, "job": job_id, "status": status,
               "version": REPORT_VERSION,
               "report": json.dumps({"error": detail, "deterministic": "inconclusive",
                                     "infrastructure": status})})


async def process_comparison(engine: AsyncEngine, project_id: UUID, job_id: UUID) -> bool:
    """Resolve one queued comparison under a lease.

    Returns ``False`` when the job could not be acquired or could not be
    resolved. Every exit path leaves the comparison in a state that reflects
    what actually happened: a detector result, or an infrastructure status.
    """
    queue = JobQueue(engine, timedelta(seconds=60))
    token = new_uuid7()
    if await queue.acquire_job(project_id, job_id, token) is None:
        return False
    async with project_connection(engine, project_id) as connection:
        entry = (await connection.execute(text("""
            SELECT comparisons.id, comparisons.baseline_recording_id,
                   comparisons.candidate_run_id, recordings.run_id AS baseline_run_id
            FROM comparisons
            LEFT JOIN recordings ON recordings.id = comparisons.baseline_recording_id
              AND recordings.project_id = comparisons.project_id
            WHERE comparisons.project_id = :project AND comparisons.job_id = :job
        """), {"project": project_id, "job": job_id})).mappings().first()
    if entry is None or entry["baseline_run_id"] is None:
        await _record_infrastructure(engine, project_id, job_id, "error",
                                     "comparison inputs are no longer available")
        await _finish(queue, project_id, job_id, token, "failed")
        return False

    try:
        async with project_connection(engine, project_id) as connection:
            baseline = await _events(connection, project_id, entry["baseline_run_id"])
            candidate = await _events(connection, project_id, entry["candidate_run_id"])
        comparison = compare(baseline, candidate)
    except Exception as error:  # noqa: BLE001 - recorded, not swallowed
        await _record_infrastructure(engine, project_id, job_id, "error",
                                     f"comparison failed: {type(error).__name__}")
        await _finish(queue, project_id, job_id, token, "failed")
        return False

    outcome = aggregate(deterministic=comparison.deterministic, gate=Gate())
    report = {**comparison.describe(), "outcome": outcome.describe()}
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            UPDATE comparisons SET deterministic = :deterministic,
              alignment_confidence = :confidence, report_version = :version,
              report = CAST(:report AS jsonb), finished_at = clock_timestamp()
            WHERE project_id = :project AND id = :comparison
        """), {"project": project_id, "comparison": entry["id"],
               "deterministic": comparison.deterministic,
               "confidence": comparison.alignment.confidence,
               "version": REPORT_VERSION, "report": json.dumps(report)})
        for ordinal, row in enumerate(divergence_rows(comparison)):
            await connection.execute(text("""
                INSERT INTO comparison_divergences (id, comparison_id, project_id,
                  ordinal, category, summary, evidence_event_ids)
                VALUES (:id, :comparison, :project, :ordinal, :category, :summary,
                  CAST(:evidence AS jsonb))
                ON CONFLICT (comparison_id, ordinal) DO NOTHING
            """), {"id": new_uuid7(), "comparison": entry["id"], "project": project_id,
                   "ordinal": ordinal, "category": row["category"],
                   "summary": row["summary"][:2048],
                   "evidence": json.dumps(row["evidence_event_ids"])})
    await _finish(queue, project_id, job_id, token, "succeeded")
    return True


async def _finish(queue: JobQueue, project_id: UUID, job_id: UUID, token: UUID,
                  status: str) -> None:
    """Complete the job, tolerating a cancellation that raced the work.

    A lost lease here means the job was cancelled or reclaimed while the
    comparison ran. The comparison rows are already written and honest; losing
    the race does not justify raising out of the worker loop.
    """
    try:
        await queue.complete(project_id, job_id, token, status)
    except (LeaseLost, ValueError):
        return
