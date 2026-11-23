"""Verify RS256 OIDC JWTs against explicitly trusted JWKS keys."""

import base64
import binascii
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes

from .oidc_claims import IdentityDenied, validate_project_claims


def _decode(part: str) -> bytes:
    if not part or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in part):
        raise ValueError("Invalid base64url")
    return base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)


def verify_project_token(token: str, jwks: Mapping[str, object], *, issuer: str,
                         audience: str, project_id: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            raise ValueError("Malformed token")
        header = json.loads(_decode(parts[0]))
        claims = json.loads(_decode(parts[1]))
        if (type(header) is not dict or type(claims) is not dict
                or header.get("alg") != "RS256" or type(header.get("kid")) is not str
                or not header["kid"] or header.get("crit") or header.get("jku")
                or header.get("jwk") or header.get("x5u") or header.get("x5c")):
            raise ValueError("Unsafe token header")
        keys = jwks.get("keys")
        if type(keys) is not list:
            raise ValueError("Invalid key set")
        matching = [key for key in keys if type(key) is dict and key.get("kid") == header["kid"]]
        if len(matching) != 1:
            raise ValueError("Unknown or ambiguous signing key")
        key = matching[0]
        if (key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig" or not isinstance(key.get("n"), str)
                or not isinstance(key.get("e"), str)):
            raise ValueError("Invalid signing key")
        modulus = int.from_bytes(_decode(key["n"]), "big")
        exponent = int.from_bytes(_decode(key["e"]), "big")
        if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0:
            raise ValueError("Weak signing key")
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        public_key.verify(_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode(),
                          padding.PKCS1v15(), hashes.SHA256())
        return validate_project_claims(claims, issuer=issuer, audience=audience,
                                       project_id=project_id)
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, binascii.Error, InvalidSignature) as error:
        raise IdentityDenied("Untrusted project identity") from error
