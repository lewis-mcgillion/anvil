from fastapi.testclient import TestClient

from anvil_server.app import app


client = TestClient(app)


def test_live_health():
    response = client.get("/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_ready_fails_closed():
    response = client.get("/v1/health/ready")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"


def test_mutation_does_not_fake_success():
    response = client.post("/v1/projects", json={"name": "example"})
    assert response.status_code == 503


def test_invalid_project_rejected():
    response = client.post("/v1/projects", json={"name": "example", "unknown": 1})
    assert response.status_code == 400
