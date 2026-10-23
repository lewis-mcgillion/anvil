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


def test_python_capture_routes_are_published():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/projects/{project_id}/runs" in paths
    assert "/v1/projects/{project_id}/runs/{run_id}/events:batch" in paths
    assert "/v1/projects/{project_id}/recordings/{recording_id}/archive" in paths


def test_capture_without_credentials_is_rejected():
    response = client.post("/v1/projects/0199b167-0000-7000-8000-000000000011/runs",
                           json={"agent_name": "test", "adapter": "adk", "adapter_version": "1"})
    assert response.status_code == 503
