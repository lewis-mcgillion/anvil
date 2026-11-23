"""Validate already-verified identity claims; signature verification belongs to the IdP integration."""

from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit


class IdentityDenied(ValueError):
    pass


def validate_project_claims(claims: Mapping[str, object], *, issuer: str,
                            audience: str, project_id: str, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    parsed_issuer = urlsplit(issuer)
    permitted_audiences = claims.get("aud")
    if type(permitted_audiences) is str:
        permitted_audiences = [permitted_audiences]
    if (parsed_issuer.scheme != "https" or not parsed_issuer.hostname
            or parsed_issuer.username is not None or parsed_issuer.password is not None
            or parsed_issuer.fragment or parsed_issuer.query or not audience or not project_id
            or claims.get("iss") != issuer or type(permitted_audiences) is not list
            or audience not in permitted_audiences or any(type(item) is not str for item in permitted_audiences)
            or not permitted_audiences
            or (len(permitted_audiences) > 1 and claims.get("azp") != audience)
            or ("azp" in claims and claims["azp"] != audience)
            or type(claims.get("exp")) is not int or claims["exp"] <= current.timestamp()
            or ("nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > current.timestamp()))
            or ("iat" in claims and (type(claims["iat"]) is not int or claims["iat"] > current.timestamp()))
            or claims.get("project_id") != project_id
            or type(claims.get("sub")) is not str or not claims["sub"].strip()):
        raise IdentityDenied("Untrusted or expired project identity")
    return claims["sub"]
