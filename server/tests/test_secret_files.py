import pytest
from fastapi.testclient import TestClient

from anvil_server.app import app
from anvil_server.secret_files import deployment_secret


def test_secret_file_supports_operator_mount(tmp_path, monkeypatch):
    secret_path = tmp_path / "cursor-secret"
    secret_path.write_text("mounted-secret\n")
    monkeypatch.delenv("ANVIL_CURSOR_SECRET", raising=False)
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", str(secret_path))
    assert deployment_secret("ANVIL_CURSOR_SECRET") == "mounted-secret"


def test_secret_file_rejects_conflicting_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("ANVIL_CURSOR_SECRET", "inline")
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", str(tmp_path / "secret"))
    with pytest.raises(ValueError, match="only one"):
        deployment_secret("ANVIL_CURSOR_SECRET")


@pytest.mark.parametrize("contents", ["", "\n"])
def test_empty_mounted_secret_rejected(tmp_path, monkeypatch, contents):
    secret_path = tmp_path / "secret"
    secret_path.write_text(contents)
    monkeypatch.delenv("ANVIL_CURSOR_SECRET", raising=False)
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", str(secret_path))
    with pytest.raises(ValueError, match="must not be empty"):
        deployment_secret("ANVIL_CURSOR_SECRET")


def test_relative_secret_path_rejected(monkeypatch):
    monkeypatch.delenv("ANVIL_CURSOR_SECRET", raising=False)
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", "relative")
    with pytest.raises(ValueError, match="absolute"):
        deployment_secret("ANVIL_CURSOR_SECRET")


def test_api_lifespan_loads_mounted_cursor_secret(tmp_path, monkeypatch):
    secret_path = tmp_path / "cursor-secret"
    secret_path.write_text("sufficiently-long-secret-value\n")
    monkeypatch.delenv("ANVIL_DATABASE_URL", raising=False)
    monkeypatch.delenv("ANVIL_CURSOR_SECRET", raising=False)
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", str(secret_path))
    with TestClient(app):
        assert app.state.cursor_secret == b"sufficiently-long-secret-value"


def test_api_lifespan_rejects_missing_secret_mount(tmp_path, monkeypatch):
    monkeypatch.delenv("ANVIL_DATABASE_URL", raising=False)
    monkeypatch.delenv("ANVIL_CURSOR_SECRET", raising=False)
    monkeypatch.setenv("ANVIL_CURSOR_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        with TestClient(app):
            pass
