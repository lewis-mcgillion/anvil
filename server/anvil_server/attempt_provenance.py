"""Validate candidate attempt metadata before future sandbox orchestration."""

from __future__ import annotations

import re
import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .database import project_connection
from .jobs import JobQueue
from .identifiers import new_uuid7


class InvalidAttempt(ValueError):
    pass


def verify_evidence(evidence: Mapping[str, object]) -> bool:
    digest = evidence.get("sha256")
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False
    try:
        canonical = json.dumps({key: value for key, value in evidence.items() if key != "sha256"},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return hashlib.sha256(canonical).hexdigest() == digest


async def persist_evidence(engine: AsyncEngine, project_id: UUID, job_id: UUID,
                           evidence_id: UUID, evidence: Mapping[str, object]) -> None:
    if not verify_evidence(evidence):
        raise InvalidAttempt("Candidate evidence checksum is invalid")
    if not all(isinstance(identifier, UUID) for identifier in (project_id, job_id, evidence_id)):
        raise InvalidAttempt("Candidate evidence requires UUID identities")
    serialized = json.dumps(evidence, sort_keys=True, allow_nan=False)
    async with project_connection(engine, project_id) as connection:
        await connection.execute(text("""
            INSERT INTO attempt_evidence (id, project_id, job_id, evidence, digest)
            VALUES (:id, :project, :job, CAST(:evidence AS jsonb), :digest)
            ON CONFLICT (job_id, digest) DO NOTHING
        """), {"id": evidence_id, "project": project_id, "job": job_id,
               "evidence": serialized, "digest": evidence["sha256"]})


async def load_evidence(engine: AsyncEngine, project_id: UUID, job_id: UUID,
                        evidence_id: UUID) -> dict[str, object] | None:
    if not all(isinstance(identifier, UUID) for identifier in (project_id, job_id, evidence_id)):
        raise InvalidAttempt("Candidate evidence requires UUID identities")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT evidence, digest FROM attempt_evidence
            WHERE project_id = :project AND job_id = :job AND id = :id
        """), {"project": project_id, "job": job_id, "id": evidence_id})).mappings().first()
    if row is None:
        return None
    evidence = row["evidence"]
    if not isinstance(evidence, dict) or evidence.get("sha256") != row["digest"] or not verify_evidence(evidence):
        raise InvalidAttempt("Stored candidate evidence checksum mismatch")
    return evidence


async def validate_registered_agent(engine: AsyncEngine, project_id: UUID,
                                    agent_version_id: UUID, details: Mapping[str, object]) -> None:
    validate_attempt(details)
    if not isinstance(agent_version_id, UUID):
        raise InvalidAttempt("Candidate agent identity must be a UUID")
    async with project_connection(engine, project_id) as connection:
        row = (await connection.execute(text("""
            SELECT image, adapter, adapter_version FROM agent_versions
            WHERE project_id = :project AND id = :agent
        """), {"project": project_id, "agent": agent_version_id})).mappings().first()
    if row is None or any((row["image"] != details["image_digest"],
                           row["adapter"] != details["adapter"],
                           row["adapter_version"] != details["adapter_version"])):
        raise InvalidAttempt("Candidate does not match a registered project agent version")


_DIGEST = re.compile(r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}")
_REQUIRED = frozenset({"reexecution_id", "recording_id", "agent_version_id",
                       "image_digest", "adapter", "adapter_version", "model_mode",
                       "fixture_policy", "seed", "budgets"})


def validate_attempt(details: Mapping[str, object]) -> None:
    if not isinstance(details, Mapping) or not _REQUIRED <= details.keys():
        raise InvalidAttempt("Missing candidate attempt provenance")
    image = details["image_digest"]
    if type(image) is not str or _DIGEST.fullmatch(image) is None:
        raise InvalidAttempt("Candidate requires a pinned OCI image")
    if details["model_mode"] not in {"recorded", "live"} or details["fixture_policy"] != "strict":
        raise InvalidAttempt("Unsupported model or fixture policy")
    if type(details["seed"]) is not int or details["seed"] < 0:
        raise InvalidAttempt("Candidate seed must be a nonnegative integer")
    if any(type(details[field]) is not str or not details[field]
           for field in ("adapter", "adapter_version")):
        raise InvalidAttempt("Missing candidate adapter identity")
    if type(details["budgets"]) is not dict or not details["budgets"]:
        raise InvalidAttempt("Candidate must declare execution budgets")
    for budget_name, budget_value in details["budgets"].items():
        if budget_name not in {"wall_seconds", "model_calls", "tool_calls",
                               "input_tokens", "output_tokens", "max_spend_micro_usd"}:
            raise InvalidAttempt("Candidate contains an unsupported budget")
        if type(budget_value) is not int or budget_value <= 0:
            raise InvalidAttempt("Candidate budgets must be positive integers")
    for identifier in ("reexecution_id", "recording_id", "agent_version_id"):
        if type(details[identifier]) is not str or not details[identifier].strip():
            raise InvalidAttempt("Candidate identity fields must be nonempty")


async def execute_attempt(
    details: Mapping[str, object], execute: Callable[[], Awaitable[object]],
    seal: Callable[[dict[str, object]], Awaitable[None]],
) -> dict[str, object]:
    validate_attempt(details)
    budget = details["budgets"]
    wall_seconds = budget.get("wall_seconds")
    if type(wall_seconds) is not int or wall_seconds <= 0:
        raise InvalidAttempt("Candidate wall-time budget must be positive")
    started = time.monotonic()
    outcome = "failed"
    failure = None
    try:
        async with asyncio.timeout(wall_seconds):
            result = await execute()
        if result is False or (type(result) is int and result != 1):
            failure = "runner_outcome"
        else:
            outcome = "succeeded"
    except asyncio.CancelledError:
        outcome = "cancelled"
    except TimeoutError:
        outcome = "timed_out"
    except Exception:
        failure = "runner_error"
    evidence = {"provenance": dict(details), "outcome": outcome,
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "failure_code": failure}
    try:
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InvalidAttempt("Candidate evidence is not serializable") from error
    evidence["sha256"] = hashlib.sha256(canonical).hexdigest()
    sealing = asyncio.create_task(seal(evidence))
    while not sealing.done():
        try:
            await asyncio.shield(sealing)
        except asyncio.CancelledError:
            if sealing.done():
                break
    await sealing
    return evidence


async def run_leased_attempt(
    queue: JobQueue, project_id: UUID, job_id: UUID, lease_token: UUID,
    details: Mapping[str, object], execute: Callable[[], Awaitable[object]],
    seal: Callable[[dict[str, object]], Awaitable[None]],
) -> dict[str, object] | None:
    validate_attempt(details)
    if type(details["budgets"].get("wall_seconds")) is not int or details["budgets"]["wall_seconds"] <= 0:
        raise InvalidAttempt("Candidate wall-time budget must be positive")
    if await queue.acquire_job(project_id, job_id, lease_token) is None:
        return None
    lease_duration = getattr(queue, "lease_duration", None)
    heartbeat_interval = min(10.0, lease_duration.total_seconds() / 3) if lease_duration else 10.0
    async def renew_lease() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval)
            await queue.heartbeat(project_id, job_id, lease_token)

    heartbeat_task = asyncio.create_task(renew_lease()) if callable(getattr(queue, "heartbeat", None)) else None
    attempt_task = None
    completion_task = None
    try:
        attempt_task = asyncio.create_task(execute_attempt(details, execute, seal))
        if heartbeat_task is not None:
            done, _ = await asyncio.wait({attempt_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat_task in done:
                attempt_task.cancel()
                await asyncio.gather(attempt_task, return_exceptions=True)
                await heartbeat_task
                raise InvalidAttempt("Candidate lease renewal stopped unexpectedly")
        evidence = await attempt_task
        terminal_status = "succeeded" if evidence["outcome"] == "succeeded" else (
            "cancelled" if evidence["outcome"] == "cancelled" else "failed"
        )
        completion_task = asyncio.create_task(queue.complete(project_id, job_id, lease_token, terminal_status))
        if heartbeat_task is not None:
            done, _ = await asyncio.wait({completion_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat_task in done:
                completion_task.cancel()
                await asyncio.gather(completion_task, return_exceptions=True)
                await heartbeat_task
                raise InvalidAttempt("Candidate lease renewal stopped during completion")
        await completion_task
    finally:
        for task in (attempt_task, completion_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
    return evidence


async def run_persisted_attempt(
    engine: AsyncEngine, queue: JobQueue, project_id: UUID, job_id: UUID,
    lease_token: UUID, details: Mapping[str, object],
    execute: Callable[[], Awaitable[object]],
) -> dict[str, object] | None:
    try:
        agent_version_id = UUID(details["agent_version_id"])
    except (KeyError, ValueError, TypeError) as error:
        raise InvalidAttempt("Candidate agent version ID is invalid") from error
    await validate_registered_agent(engine, project_id, agent_version_id, details)

    async def seal(evidence: dict[str, object]) -> None:
        await persist_evidence(engine, project_id, job_id, new_uuid7(), evidence)

    return await run_leased_attempt(queue, project_id, job_id, lease_token, details, execute, seal)
