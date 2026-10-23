"""Compare published REST operations to the running Python service."""

from pathlib import Path

import yaml

from anvil_server.app import app

KNOWN_MISSING = {
    "POST /projects/{project_id}/imports",
    "POST /reexecutions",
    "GET /reexecutions/{reexecution_id}", "POST /comparisons",
    "GET /comparisons/{comparison_id}", "GET /comparisons/{comparison_id}/divergences",
    "POST /projects/{project_id}/suites", "POST /suites/{suite_id}/versions",
    "POST /suite-runs", "GET /suite-runs/{suite_run_id}",
    "GET /suite-runs/{suite_run_id}/report",
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
