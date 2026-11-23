import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from anvil_server.oidc_claims import IdentityDenied
from anvil_server.oidc_jwks import verify_project_token


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def number(value):
    return encoded(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def signed_token(private_key, *, kid="current", header=None, claims=None):
    header = header or {"alg": "RS256", "kid": kid}
    claims = claims or {"iss": "https://identity.example", "aud": "anvil-api",
                        "sub": "user-a", "project_id": "project-a",
                        "exp": int(datetime.now(timezone.utc).timestamp()) + 120}
    payload = ".".join(encoded(json.dumps(part).encode()) for part in (header, claims))
    signature = private_key.sign(payload.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{payload}.{encoded(signature)}"


def jwk(private_key, kid="current"):
    numbers = private_key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
            "n": number(numbers.n), "e": number(numbers.e)}


def verify(token, keys):
    return verify_project_token(token, {"keys": keys}, issuer="https://identity.example",
                                audience="anvil-api", project_id="project-a")


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_valid_signature_and_rotated_keys(private_key):
    next_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = [jwk(private_key), jwk(next_key, "next")]
    assert verify(signed_token(private_key), keys) == "user-a"
    assert verify(signed_token(next_key, kid="next"), keys) == "user-a"
    with pytest.raises(IdentityDenied):
        verify(signed_token(private_key), [keys[1]])


def test_tampered_signature_and_cross_project_rejected(private_key):
    token = signed_token(private_key)
    parts = token.split(".")
    wrong_payload = signed_token(private_key, claims={"sub": "attacker"}).split(".")[1]
    with pytest.raises(IdentityDenied):
        verify(".".join([parts[0], wrong_payload, parts[2]]), [jwk(private_key)])
    with pytest.raises(IdentityDenied):
        verify(signed_token(private_key, claims={"project_id": "project-b"}), [jwk(private_key)])


@pytest.mark.parametrize("header", [{"alg": "none", "kid": "current"},
                                           {"alg": "HS256", "kid": "current"},
                                           {"alg": "RS256", "kid": "current", "jku": "https://evil"},
                                           {"alg": "RS256", "kid": "current", "crit": ["unknown"]}])
def test_untrusted_header_rejected(private_key, header):
    with pytest.raises(IdentityDenied):
        verify(signed_token(private_key, header=header), [jwk(private_key)])


def test_ambiguous_key_id_rejected(private_key):
    with pytest.raises(IdentityDenied):
        verify(signed_token(private_key), [jwk(private_key), jwk(private_key)])


def test_oversized_token_rejected_before_signature_verification(private_key):
    with pytest.raises(IdentityDenied, match="Untrusted project identity"):
        verify(signed_token(private_key) + "x" * 16_384, [jwk(private_key)])
