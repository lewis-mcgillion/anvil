"""Validate already-verified identity claims; signature verification belongs to the IdP integration."""

from collections.abc import Mapping
from datetime import datetime, timezone


class IdentityDenied(ValueError):
    pass


def validate_project_claims(claims: Mapping[str, object], *, issuer: str,
                            audience: str, project_id: str, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    permitted_audiences = claims.get("aud")
    if type(permitted_audiences) is str:
        permitted_audiences = [permitted_audiences]
    if (claims.get("iss") != issuer or type(permitted_audiences) is not list
            or audience not in permitted_audiences or any(type(item) is not str for item in permitted_audiences)
            or type(claims.get("exp")) is not int or claims["exp"] <= current.timestamp()
            or claims.get("project_id") != project_id
            or type(claims.get("sub")) is not str or not claims["sub"]):
        raise IdentityDenied("Untrusted or expired project identity")
    return claims["sub"]
