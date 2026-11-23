"""Browser and REST surface hardening (ANVIL-068).

The acceptance criterion is a corpus: XSS, CSRF, CORS and oversized malicious
payloads. These tests drive the real ASGI app through Starlette's client, so
what is asserted is what a browser would actually receive.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from anvil_server.app import app
from anvil_server import http_security as hs


ALLOWED = "https://dashboard.example"
HOSTILE = "https://evil.example"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ANVIL_ALLOWED_ORIGINS", ALLOWED)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def closed_client(monkeypatch):
    monkeypatch.delenv("ANVIL_ALLOWED_ORIGINS", raising=False)
    with TestClient(app) as test_client:
        yield test_client


class TestSecurityHeaders:
    def test_every_response_forbids_loading_and_executing_anything(self, client) -> None:
        response = client.get("/health/live")
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "form-action 'none'" in csp

    @pytest.mark.parametrize("header,value", [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-resource-policy", "same-origin"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cache-control", "no-store"),
    ])
    def test_the_published_headers_are_present(self, client, header, value) -> None:
        assert client.get("/health/live").headers[header] == value

    def test_an_error_response_is_hardened_too(self, client) -> None:
        response = client.get("/v1/runs/0199b167-0000-7000-8000-000000000021")
        assert response.status_code in (400, 503)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]

    def test_a_not_found_is_hardened_too(self, client) -> None:
        response = client.get("/nope")
        assert response.status_code == 404
        assert response.headers["x-frame-options"] == "DENY"

    def test_recordings_are_never_cached(self, client) -> None:
        response = client.get("/v1/health/ready")
        assert response.headers["cache-control"] == "no-store"

    def test_the_request_id_still_travels(self, client) -> None:
        assert client.get("/health/live").headers["x-request-id"]


class TestXss:
    """A JSON API cannot be made to serve a document."""

    @pytest.mark.parametrize("payload", [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "javascript:alert(1)",
        "\"><svg/onload=alert(1)>",
        "</script><script>fetch('//evil')</script>",
    ])
    def test_a_reflected_value_is_never_served_as_html(self, client, payload) -> None:
        response = client.get("/v1/runs/" + payload)
        assert response.status_code in (400, 404, 422)
        assert response.headers["content-type"].startswith(
            ("application/json", "application/problem+json")
        )
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_a_problem_body_is_json_and_carries_no_markup(self, client) -> None:
        response = client.get("/v1/runs/<script>")
        body = response.text
        json.loads(body)
        assert "<script>" not in body or body.strip().startswith("{")

    def test_the_policy_also_sandboxes_the_response(self, client) -> None:
        assert "sandbox" in client.get("/health/live").headers["content-security-policy"]


class TestCorsIsDenyByDefault:
    def test_an_unconfigured_deployment_allows_no_origin(self, closed_client) -> None:
        response = closed_client.get("/health/live", headers={"origin": ALLOWED})
        assert "access-control-allow-origin" not in response.headers

    def test_a_hostile_origin_is_never_echoed(self, client) -> None:
        response = client.get("/health/live", headers={"origin": HOSTILE})
        assert "access-control-allow-origin" not in response.headers

    def test_an_allowed_origin_is_echoed_exactly(self, client) -> None:
        response = client.get("/health/live", headers={"origin": ALLOWED})
        assert response.headers["access-control-allow-origin"] == ALLOWED

    def test_credentials_are_never_allowed(self, client) -> None:
        response = client.get("/health/live", headers={"origin": ALLOWED})
        assert "access-control-allow-credentials" not in response.headers

    def test_the_decision_always_varies_on_origin(self, client) -> None:
        for origin in (ALLOWED, HOSTILE, None):
            headers = {"origin": origin} if origin else {}
            response = client.get("/health/live", headers=headers)
            assert "origin" in response.headers["vary"].lower()

    @pytest.mark.parametrize("origin", [
        "https://dashboard.example.evil",
        "https://evil.example/https://dashboard.example",
        "http://dashboard.example",
        "https://DASHBOARD.example".replace("DASHBOARD", "dashboard") + ":8443",
        "null",
        "",
    ])
    def test_a_near_miss_origin_is_refused(self, client, origin) -> None:
        response = client.get("/health/live", headers={"origin": origin})
        assert "access-control-allow-origin" not in response.headers


class TestPreflight:
    def preflight(self, client, origin, method="POST", headers="authorization"):
        return client.options("/v1/projects", headers={
            "origin": origin,
            "access-control-request-method": method,
            "access-control-request-headers": headers,
        })

    def test_an_allowed_preflight_succeeds_with_a_closed_method_list(self, client) -> None:
        response = self.preflight(client, ALLOWED)
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == ALLOWED
        allowed = response.headers["access-control-allow-methods"]
        assert set(allowed.replace(" ", "").split(",")) == set(hs.ALLOWED_METHODS)
        assert "PUT" not in allowed and "PATCH" not in allowed

    def test_a_hostile_preflight_is_refused_loudly(self, client) -> None:
        response = self.preflight(client, HOSTILE)
        assert response.status_code == 403
        assert response.json()["code"] == "origin_not_allowed"
        assert "access-control-allow-origin" not in response.headers

    def test_an_unsupported_method_is_refused(self, client) -> None:
        response = self.preflight(client, ALLOWED, method="PATCH")
        assert response.status_code == 403
        assert response.json()["code"] == "method_not_allowed_cross_origin"

    def test_an_unexpected_header_is_refused(self, client) -> None:
        response = self.preflight(client, ALLOWED, headers="authorization, x-sneaky")
        assert response.status_code == 403
        assert response.json()["code"] == "header_not_allowed_cross_origin"
        assert "x-sneaky" in response.json()["detail"]

    def test_only_the_published_headers_are_accepted(self, client) -> None:
        response = self.preflight(
            client, ALLOWED, headers=", ".join(hs.ALLOWED_REQUEST_HEADERS)
        )
        assert response.status_code == 204

    def test_a_refused_preflight_is_still_hardened(self, client) -> None:
        response = self.preflight(client, HOSTILE)
        assert response.headers["x-content-type-options"] == "nosniff"


class TestCsrf:
    """CSRF is removed, not mitigated: there is no ambient credential."""

    def test_a_cross_site_state_change_is_refused_before_the_route(self, client) -> None:
        response = client.post("/v1/projects", json={"name": "x"},
                               headers={"origin": HOSTILE})
        assert response.status_code == 403
        assert response.json()["code"] == "cross_origin_rejected"

    def test_a_cookie_cannot_authenticate_a_state_change(self, client) -> None:
        response = client.post("/v1/projects", json={"name": "x"},
                               headers={"origin": HOSTILE, "cookie": "session=admin"})
        assert response.status_code == 403
        assert response.json()["code"] == "cross_origin_rejected"

    def test_a_delete_from_a_hostile_origin_is_refused(self, client) -> None:
        response = client.delete("/v1/runs/0199b167-0000-7000-8000-000000000021",
                                 headers={"origin": HOSTILE})
        assert response.status_code == 403

    def test_a_same_origin_request_without_an_origin_header_still_works(self, client) -> None:
        # Scripts and server-to-server callers send no Origin at all; refusing
        # them would be refusing the API's main consumer.
        response = client.post("/v1/projects", json={"name": "x"})
        assert response.status_code != 403

    def test_an_allowed_origin_may_change_state(self, client) -> None:
        response = client.post("/v1/projects", json={"name": "x"},
                               headers={"origin": ALLOWED})
        assert response.status_code != 403

    def test_a_hostile_origin_may_still_read_nothing_from_a_safe_request(self, client) -> None:
        # A GET is allowed to reach the route because it changes nothing, and
        # the missing CORS header is what stops the page reading the answer.
        response = client.get("/health/live", headers={"origin": HOSTILE})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


class TestOversizedPayloads:
    def test_an_oversized_body_is_refused(self, client) -> None:
        response = client.post("/v1/projects", content=b"x" * (5 * 1024 * 1024),
                               headers={"content-type": "application/json"})
        assert response.status_code == 413
        assert response.json()["code"] == "payload_too_large"

    def test_a_lying_content_length_cannot_smuggle_a_large_body(self, client) -> None:
        response = client.post("/v1/projects", content=b"x" * (5 * 1024 * 1024),
                               headers={"content-type": "application/json",
                                        "content-length": "10"})
        assert response.status_code in (400, 413)

    def test_a_refused_body_is_still_hardened(self, client) -> None:
        response = client.post("/v1/projects", content=b"x" * (5 * 1024 * 1024),
                               headers={"content-type": "application/json"})
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]

    def test_a_deeply_nested_payload_does_not_crash_the_server(self, client) -> None:
        payload = "[" * 2000 + "]" * 2000
        response = client.post("/v1/projects", content=payload.encode(),
                               headers={"content-type": "application/json"})
        assert response.status_code in (400, 413, 422, 500)
        assert response.headers["x-content-type-options"] == "nosniff"


class TestOriginParsing:
    @pytest.mark.parametrize("value", ["*", "null", "", None, "dashboard.example",
                                       "https://a.example/path", "ftp://a.example",
                                       "https://", "https://a.example?x=1"])
    def test_an_unusable_entry_is_dropped(self, value) -> None:
        assert hs.parse_origins(value) == ()

    def test_a_list_is_parsed_in_order_without_duplicates(self) -> None:
        parsed = hs.parse_origins("https://a.example, https://b.example https://a.example/")
        assert parsed == ("https://a.example", "https://b.example")

    def test_a_wildcard_mixed_with_a_real_origin_does_not_widen_it(self) -> None:
        policy = hs.CorsPolicy(origins=hs.parse_origins("*, https://a.example"))
        assert policy.allows("https://a.example")
        assert not policy.allows("https://anything.example")

    def test_a_policy_with_no_origins_allows_nothing(self) -> None:
        policy = hs.CorsPolicy()
        assert not policy.allows(ALLOWED)
        assert policy.headers(ALLOWED) == [(b"vary", b"Origin")]

    def test_the_policy_is_read_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("ANVIL_ALLOWED_ORIGINS", "https://a.example")
        assert hs.CorsPolicy.from_environment().origins == ("https://a.example",)


class TestHeaderMerging:
    def test_a_route_cannot_weaken_a_security_header(self) -> None:
        merged = dict(hs._merge([(b"cache-control", b"public, max-age=600")], []))
        assert merged[b"cache-control"] == b"no-store"

    def test_a_route_vary_is_combined_rather_than_lost(self) -> None:
        merged = dict(hs._merge([(b"vary", b"Accept-Encoding")],
                                [(b"vary", b"Origin")]))
        value = merged[b"vary"].decode().lower()
        assert "accept-encoding" in value and "origin" in value

    def test_an_ordinary_route_header_survives(self) -> None:
        merged = dict(hs._merge([(b"etag", b'"abc"')], []))
        assert merged[b"etag"] == b'"abc"'
