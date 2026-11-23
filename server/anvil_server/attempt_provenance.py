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
        """), {"id": evidence_id, "project": project_id, "job": job_id,
               "evidence": serialized, "digest": evidence["sha256"]})


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
            await execute()
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
