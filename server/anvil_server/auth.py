"""Scope-limited bearer credentials for Anvil project operations."""

import base64
import hashlib
import secrets

SCOPES = frozenset({"capture:write", "runs:read", "replay:write", "suites:write", "suites:read", "admin", "metrics:read"})


def issue_key() -> str:
    return "anv_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def key_hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def bearer_token(header: str | None) -> str:
    parts = header.split() if header else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].startswith("anv_"):
        raise ValueError("Invalid authorization scheme")
    return parts[1]


def validate_scopes(scopes: list[str]) -> frozenset[str]:
    unique = frozenset(scopes)
    if not unique or len(unique) != len(scopes) or not unique <= SCOPES:
        raise ValueError("Invalid or duplicate scope")
    if "capture:write" in unique and unique & {"runs:read", "replay:write"}:
        raise ValueError("Capture keys cannot read or replay")
    return unique


def allows(scopes: list[str], required: str) -> bool:
    try:
        return required in validate_scopes(scopes)
    except ValueError:
        return False
