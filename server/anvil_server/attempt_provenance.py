"""Validate candidate attempt metadata before future sandbox orchestration."""

from __future__ import annotations

import re
from collections.abc import Mapping


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
