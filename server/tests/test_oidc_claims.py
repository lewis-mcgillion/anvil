from datetime import datetime, timezone

import pytest

from anvil_server.oidc_claims import IdentityDenied, validate_project_claims


NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def claims():
    return {"iss": "https://identity.example.com", "aud": ["anvil-api"],
            "exp": int(NOW.timestamp()) + 60, "project_id": "project-a", "sub": "user-1"}


def check(value):
    return validate_project_claims(value, issuer="https://identity.example.com",
                                   audience="anvil-api", project_id="project-a", now=NOW)


def test_project_claim_accepts_matching_issuer_audience_and_expiry():
    assert check(claims()) == "user-1"


@pytest.mark.parametrize("change", [{"iss": "https://evil.example.com"}, {"aud": ["other"]},
                                     {"project_id": "project-b"}, {"exp": int(NOW.timestamp())},
                                     {"sub": ""}, {"nbf": int(NOW.timestamp()) + 1},
                                     {"iat": int(NOW.timestamp()) + 1}, {"nbf": "future"},
                                     {"iat": True}, {"sub": "   "}, {"aud": []}])
def test_cross_project_expired_or_untrusted_claim_denied(change):
    with pytest.raises(IdentityDenied):
        check(claims() | change)


@pytest.mark.parametrize("issuer", ["http://identity.example.com", "https://",
                                    "https://user@identity.example.com",
                                    "https://identity.example.com?token=secret",
                                    "https://identity.example.com#fragment"])
def test_invalid_issuer_configuration_denied(issuer):
    with pytest.raises(IdentityDenied):
        validate_project_claims(claims() | {"iss": issuer}, issuer=issuer,
                                audience="anvil-api", project_id="project-a", now=NOW)


def test_multiple_audiences_require_matching_authorized_party():
    multiple = claims() | {"aud": ["anvil-api", "other-service"]}
    with pytest.raises(IdentityDenied):
        check(multiple)
    with pytest.raises(IdentityDenied):
        check(multiple | {"azp": "other-service"})
    assert check(multiple | {"azp": "anvil-api"}) == "user-1"


def test_single_audience_rejects_conflicting_authorized_party():
    with pytest.raises(IdentityDenied):
        check(claims() | {"azp": "other-service"})
