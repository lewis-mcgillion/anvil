from fastapi.testclient import TestClient

from anvil_server.app import app


def test_chunked_request_body_limit():
    from httpx import ASGITransport, AsyncClient
    import asyncio

    async def check():
        async def chunks():
            for _ in range(5):
                yield b"x" * (1024 * 1024)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/projects", content=chunks())
            assert response.status_code == 413
            assert response.json()["code"] == "payload_too_large"
            assert response.json()["request_id"] == response.headers["X-Request-ID"]

    asyncio.run(check())


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


def test_unscoped_run_list_is_rejected():
    response = client.get("/v1/runs")
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
