"""Validate candidate attempt metadata before future sandbox orchestration."""

from __future__ import annotations

import re
import asyncio
import time
from collections.abc import Mapping
from collections.abc import Awaitable, Callable


class InvalidAttempt(ValueError):
    pass


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
    sealing = asyncio.create_task(seal(evidence))
    while not sealing.done():
        try:
            await asyncio.shield(sealing)
        except asyncio.CancelledError:
            if sealing.done():
                break
    await sealing
    return evidence
