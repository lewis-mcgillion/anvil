"""Compare published REST operations to the running Python service."""

from pathlib import Path

import yaml

from anvil_server.app import app

KNOWN_MISSING = {
    "GET /projects", "GET /runs/{run_id}",
    "POST /runs/{run_id}/events:batch", "GET /runs/{run_id}/events",
    "POST /runs/{run_id}/artifacts", "POST /runs/{run_id}:seal",
    "GET /runs/{run_id}/watch", "GET /recordings/{recording_id}",
    "GET /recordings/{recording_id}/projection",
    "POST /recordings/{recording_id}/exports", "POST /projects/{project_id}/imports",
    "POST /reexecutions",
    "GET /reexecutions/{reexecution_id}", "GET /jobs/{job_id}",
    "POST /jobs/{job_id}:cancel", "POST /comparisons",
    "GET /comparisons/{comparison_id}", "GET /comparisons/{comparison_id}/divergences",
    "POST /projects/{project_id}/suites", "POST /suites/{suite_id}/versions",
    "POST /suite-runs", "GET /suite-runs/{suite_run_id}",
    "GET /suite-runs/{suite_run_id}/report", "GET /deletions/{deletion_id}",
    "GET /metrics",
}


def test_contract_operations_have_explicit_implementation_status():
    contract = yaml.safe_load((Path(__file__).parents[2] / "api/openapi.yaml").read_text())
    runtime = app.openapi()["paths"]
    missing = []
    for path, operations in contract["paths"].items():
        for method in operations:
            if method.lower() not in {"get", "post", "patch", "delete", "put"}:
                continue
            if method.lower() not in runtime.get("/v1" + path, {}):
                missing.append(f"{method.upper()} {path}")
    assert set(missing) == KNOWN_MISSING, "Contract coverage changed; review missing operations: " + repr(missing)
