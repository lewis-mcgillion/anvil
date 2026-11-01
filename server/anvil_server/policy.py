"""Mandatory server-side capture policy (ANVIL-067).

The SDK redacts before export, but the API is a public surface: anything that
can obtain a write-scoped key can POST events directly and bypass the SDK
entirely. A policy that only exists client-side is therefore not a policy, so
this module re-applies a *mandatory* subset on the server, on every path into
storage, with no way for the uploader to opt out.

Two deliberate properties:

* It is **not configurable per request**. A caller cannot disable it, because
  the only callers who would want to are exactly the ones it exists for.
* Redacting makes the payload **replay-ineligible**. The original value is
  gone, and supplying ``[REDACTED]`` to a tool or model during replay would be
  fabricating evidence rather than reproducing it. Marking the event unsafe is
  the honest outcome; silently replaying a placeholder is not.

This is narrower than the SDK policy on purpose. The SDK can afford to be
allowlist-based because the application author knows what their payloads mean.
The server cannot: it sees arbitrary projects' data and must not quietly
destroy legitimate evidence. So it removes only what is credential-shaped on
any reading, and leaves judgement calls to the SDK.
"""

from __future__ import annotations

import re
from typing import Any

MARKER = "[REDACTED]"
MAX_DEPTH = 64

# Keys whose *value* is a credential regardless of what the surrounding
# payload means. Matched on the key, so the value never has to be inspected,
# compared or logged in order to decide.
#
# Matching is word-aware rather than substring-based. `token` must not condemn
# `tokenizer`, and `key` must not condemn `sort_key`: a false positive silently
# destroys legitimate evidence, which is a worse failure here than a narrow
# rule, because nobody finds out until they need the data.
CREDENTIAL_WORDS = frozenset({
    "authorization", "auth", "token", "secret", "password", "passwd",
    "passphrase", "credential", "credentials", "cookie", "apikey", "bearer",
})
CREDENTIAL_PAIRS = frozenset({
    "api key", "private key", "secret key", "session key", "access key",
    "signing key", "encryption key", "set cookie",
})
_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORDS = re.compile(r"[A-Za-z0-9]+")


def is_credential_key(key: str) -> bool:
    """Whether a payload key names a credential."""
    words = [word.lower() for word in _WORDS.findall(_WORD_BREAK.sub(" ", key))]
    if any(word in CREDENTIAL_WORDS for word in words):
        return True
    return any(f"{left} {right}" in CREDENTIAL_PAIRS
               for left, right in zip(words, words[1:]))


# Values that are credential-shaped on their own. These are deliberately
# high-confidence: a false positive destroys real evidence, so patterns that
# merely look suspicious are not included.
CREDENTIAL_VALUES = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;\"']{6,}"),
)


class PolicyError(ValueError):
    """A payload that cannot be made safe.

    The message never contains any part of the payload. An error that quotes
    the value it rejected would leak the exact secret the policy exists to
    remove, into logs and into the client's error response.
    """


def _pointer_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def enforce(event_type: str, payload: dict) -> tuple[dict, list[str]]:
    """Return a scrubbed payload and the JSON pointers that were redacted.

    The returned payload is a new object; the caller's payload is never
    mutated, so a rejected batch cannot leave a half-scrubbed object behind.
    """
    redacted: set[str] = set()

    def scrub(value: Any, path: str, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise PolicyError("payload exceeds maximum nesting depth")
        if isinstance(value, str):
            result = value
            for pattern in CREDENTIAL_VALUES:
                result = pattern.sub(MARKER, result)
            if result != value:
                redacted.add(path)
            return result
        if isinstance(value, list):
            return [scrub(item, f"{path}/{index}", depth + 1)
                    for index, item in enumerate(value)]
        if isinstance(value, dict):
            scrubbed: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise PolicyError("payload object keys must be strings")
                child_path = f"{path}/{_pointer_token(key)}"
                if is_credential_key(key):
                    # Replaced rather than dropped: a reader needs to see that
                    # the field was present, otherwise a redacted credential is
                    # indistinguishable from one that was never sent.
                    scrubbed[key] = MARKER
                    redacted.add(child_path)
                else:
                    scrubbed[key] = scrub(child, child_path, depth + 1)
            return scrubbed
        return value

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in ("redactions", "replay_safe"):
            continue
        if not isinstance(key, str):
            raise PolicyError("payload object keys must be strings")
        child_path = f"/{_pointer_token(key)}"
        if is_credential_key(key):
            result[key] = MARKER
            redacted.add(child_path)
        else:
            result[key] = scrub(value, child_path, 0)

    declared = payload.get("redactions")
    if declared is not None and not isinstance(declared, list):
        raise PolicyError("redactions must be a list of JSON pointers")
    # The client's own declared redactions are preserved. They describe data
    # that was already removed before export, which the server cannot observe
    # and must not silently drop from the record.
    pointers = sorted({*(declared or []), *redacted})
    if pointers:
        result["redactions"] = pointers

    declared_safe = payload.get("replay_safe")
    if declared_safe is not None:
        # Server-side redaction can only ever remove replay eligibility, never
        # grant it, so the two sources combine with `and`.
        result["replay_safe"] = bool(declared_safe) and not redacted
    elif redacted:
        result["replay_safe"] = False

    return result, sorted(redacted)
