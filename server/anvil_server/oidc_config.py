"""Load trusted OIDC settings from operator-managed configuration."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .oidc_claims import IdentityDenied


def oidc_settings() -> tuple[str, str, Path] | None:
    issuer = os.getenv("ANVIL_OIDC_ISSUER")
    audience = os.getenv("ANVIL_OIDC_AUDIENCE")
    jwks_path = os.getenv("ANVIL_OIDC_JWKS_FILE")
    if not any((issuer, audience, jwks_path)):
        return None
    if not all((issuer, audience, jwks_path)) or not Path(jwks_path).is_absolute():
        raise ValueError("OIDC requires issuer, audience and an absolute JWKS file")
    try:
        parsed = urlsplit(issuer)
        port = parsed.port
    except ValueError as error:
        raise ValueError("OIDC requires a valid HTTPS issuer") from error
    if (parsed.scheme != "https" or not parsed.hostname or port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("OIDC requires a valid HTTPS issuer")
    read_jwks(Path(jwks_path))
    return issuer, audience, Path(jwks_path)


def read_jwks(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise IdentityDenied("Trusted JWKS unavailable") from error
    if (type(data) is not dict or type(data.get("keys")) is not list or not data["keys"]
            or any(type(key) is not dict or type(key.get("kid")) is not str
                   or not key["kid"] for key in data["keys"])):
        raise IdentityDenied("Invalid trusted JWKS")
    return data
