import json

import pytest
from fastapi.testclient import TestClient

from anvil_server.app import app
from anvil_server.oidc_claims import IdentityDenied
from anvil_server.oidc_config import oidc_settings, read_jwks


def configure(monkeypatch, path):
    monkeypatch.setenv("ANVIL_OIDC_ISSUER", "https://identity.example")
    monkeypatch.setenv("ANVIL_OIDC_AUDIENCE", "anvil-api")
    monkeypatch.setenv("ANVIL_OIDC_JWKS_FILE", str(path))


def test_startup_loads_operator_mounted_jwks(tmp_path, monkeypatch):
    jwks_file = tmp_path / "keys.json"
    jwks_file.write_text(json.dumps({"keys": [{"kid": "current"}]}))
    configure(monkeypatch, jwks_file)
    monkeypatch.delenv("ANVIL_DATABASE_URL", raising=False)
    monkeypatch.delenv("ANVIL_CURSOR_SECRET_FILE", raising=False)
    with TestClient(app):
        assert app.state.oidc == ("https://identity.example", "anvil-api", jwks_file)


def test_partial_oidc_config_fails_closed(monkeypatch):
    monkeypatch.setenv("ANVIL_OIDC_ISSUER", "https://identity.example")
    monkeypatch.delenv("ANVIL_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("ANVIL_OIDC_JWKS_FILE", raising=False)
    with pytest.raises(ValueError, match="requires issuer"):
        oidc_settings()


@pytest.mark.parametrize("content", ["", "{}", '{"keys": []}', "not-json"])
def test_invalid_mounted_keys_rejected(tmp_path, monkeypatch, content):
    jwks_file = tmp_path / "keys.json"
    jwks_file.write_text(content)
    configure(monkeypatch, jwks_file)
    with pytest.raises(IdentityDenied):
        oidc_settings()


def test_removed_key_file_fails_closed(tmp_path):
    with pytest.raises(IdentityDenied):
        read_jwks(tmp_path / "missing")


def test_non_https_issuer_rejected(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path / "keys.json")
    monkeypatch.setenv("ANVIL_OIDC_ISSUER", "http://identity.example")
    with pytest.raises(ValueError, match="HTTPS issuer"):
        oidc_settings()
