"""The comparison HTTP surface: queueing, reading and paginating findings.

These tests stand the FastAPI app up with the persistence functions stubbed,
so route behaviour — authorization, validation, pagination and the published
response shapes — is exercised without PostgreSQL. The real database round
trip is covered by the PostgreSQL integration tests, which run in CI.

A tripwire asserts the central invariant throughout: comparison is a pure read
of two recordings, so nothing on this surface may execute an agent, call a
tool, or reach a model provider.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anvil_server import routes as routes_module
from anvil_server.app import app
from anvil_server.comparisons import ComparisonError, divergence_rows
from anvil_server.divergence import CATEGORIES, compare

from runbuilder import Run, baseline_run

PROJECT = uuid.UUID("0199b167-0000-7000-8000-000000000011")
COMPARISON = uuid.UUID("0199b167-0000-7000-8000-000000000031")
RECORDING = uuid.UUID("0199b167-0000-7000-8000-000000000041")
RUN = uuid.UUID("0199b167-0000-7000-8000-000000000051")
MISSING = uuid.UUID("0199b167-0000-7000-8000-0000000000ff")
AUTH = {"authorization": "Bearer test", "Idempotency-Key": "comparison-key-1"}


class Recorder:
    """Captures calls so a test can assert what the route asked for."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.pages: list[dict] = []
        self.sql: list[str] = []


@pytest.fixture
def state(monkeypatch):
    recorder = Recorder()
    scope = {"permission": None}

    async def allow(engine, request, project_id, required):
        scope["permission"] = required
        return None

    monkeypatch.setattr(routes_module, "authorize", allow)

    async def fake_create(engine, project_id, *, baseline_recording_id,
                          candidate_run_id, judge_profile=None):
        if baseline_recording_id == MISSING or candidate_run_id == MISSING:
            raise ComparisonError("not found")
        recorder.created.append({"project": project_id, "baseline": baseline_recording_id,
                                 "candidate": candidate_run_id, "judge": judge_profile})
        return {"comparison_id": COMPARISON, "job_id": COMPARISON, "status": "queued"}

    async def fake_get(engine, project_id, comparison_id):
        if comparison_id == MISSING:
            return None
        return {"id": str(comparison_id), "status": "succeeded",
                "deterministic": "fail", "judge": "not_run",
                "infrastructure": "ok", "alignment_confidence": "exact"}

    async def fake_report(engine, project_id, comparison_id):
        if comparison_id == MISSING:
            return None
        return {"report_version": "1", "deterministic": "fail", "findings": []}

    async def fake_list(engine, project_id, comparison_id, *, after_ordinal, limit, category):
        if comparison_id == MISSING:
            return None
        recorder.pages.append({"after": after_ordinal, "limit": limit, "category": category})
        start = (after_ordinal or 0)
        items = [{"id": str(uuid.uuid4()), "category": category or "changed_output",
                  "summary": f"finding {index}", "evidence_event_ids": ["e1"]}
                 for index in range(start, start + limit)]
        return items, start + limit

    monkeypatch.setattr(routes_module, "create_comparison", fake_create)
    monkeypatch.setattr(routes_module, "get_comparison", fake_get)
    monkeypatch.setattr(routes_module, "get_report", fake_report)
    monkeypatch.setattr(routes_module, "list_divergences", fake_list)

    class StubResult:
        """Minimal SQLAlchemy result stand-in for the idempotency queries."""

        def __init__(self, row=None):
            self.row = row

        def mappings(self):
            return self

        def first(self):
            return self.row

    class StubConnection:
        def __init__(self, recorder):
            self.recorder = recorder

        async def execute(self, statement, parameters=None):
            sql = " ".join(str(statement).split())
            self.recorder.sql.append(sql)
            # The idempotency lookup must find nothing, so each test queues a
            # fresh comparison rather than replaying a stored response.
            return StubResult(None)

    class StubScope:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return StubConnection(recorder)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(routes_module, "project_connection", lambda *a, **k: StubScope())
    return recorder, scope


@pytest.fixture
def client(state):
    with TestClient(app) as test_client:
        app.state.engine = object()
        app.state.cursor_secret = b"0123456789abcdef0123456789abcdef"
        try:
            yield test_client
        finally:
            app.state.engine = None


def summary_url(comparison=COMPARISON, project=PROJECT):
    return f"/v1/projects/{project}/comparisons/{comparison}"


def divergences_url(comparison=COMPARISON, project=PROJECT):
    return f"/v1/projects/{project}/comparisons/{comparison}/divergences"


class TestReadingAComparison:
    def test_the_three_statuses_are_reported_independently(self, client):
        body = client.get(summary_url(), headers=AUTH).json()
        # The acceptance criterion: a consumer can see what each layer said,
        # rather than one collapsed verdict.
        assert body["deterministic"] == "fail"
        assert body["judge"] == "not_run"
        assert body["infrastructure"] == "ok"
        assert body["status"] == "succeeded"
        assert body["alignment_confidence"] == "exact"

    def test_the_summary_matches_the_published_schema(self, client):
        body = client.get(summary_url(), headers=AUTH).json()
        assert set(body) == {"id", "status", "deterministic", "judge",
                             "infrastructure", "alignment_confidence"}

    def test_reading_requires_only_read_scope(self, client, state):
        _, scope = state
        client.get(summary_url(), headers=AUTH)
        assert scope["permission"] == "runs:read"

    def test_an_unknown_comparison_is_not_found(self, client):
        assert client.get(summary_url(MISSING), headers=AUTH).status_code == 404

    def test_the_report_is_versioned(self, client):
        body = client.get(summary_url() + "/report", headers=AUTH).json()
        assert body["report_version"] == "1"

    def test_a_report_that_does_not_exist_yet_is_not_found(self, client):
        assert client.get(summary_url(MISSING) + "/report", headers=AUTH).status_code == 404

    def test_the_unscoped_alias_requires_a_project(self, client):
        assert client.get(f"/v1/comparisons/{COMPARISON}").status_code == 400
        assert client.get(f"/v1/comparisons/{COMPARISON}?project_id={PROJECT}",
                          headers=AUTH).status_code == 200


class TestQueueingAComparison:
    def test_queueing_returns_a_job_to_poll(self, client):
        response = client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN)})
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["job_url"].endswith(body["job_id"])
        # `JobAccepted` forbids additional properties, so the new comparison
        # is advertised in Location rather than smuggled into the body.
        assert set(body) == {"job_id", "job_url", "status"}
        assert response.headers["location"].endswith(str(COMPARISON))

    def test_an_opt_in_judge_profile_is_passed_through(self, client, state):
        recorder, _ = state
        client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN),
            "judge": "strict"})
        assert recorder.created[-1]["judge"] == "strict"

    def test_judges_are_absent_unless_requested(self, client, state):
        recorder, _ = state
        client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN)})
        assert recorder.created[-1]["judge"] is None

    def test_an_idempotency_key_is_required(self, client):
        response = client.post(f"/v1/projects/{PROJECT}/comparisons",
                               headers={"authorization": "Bearer test"}, json={
                                   "baseline_recording_id": str(RECORDING),
                                   "candidate_run_id": str(RUN)})
        assert response.status_code == 400

    def test_a_malformed_idempotency_key_is_rejected(self, client):
        response = client.post(f"/v1/projects/{PROJECT}/comparisons",
                               headers={"authorization": "Bearer test",
                                        "Idempotency-Key": "no"}, json={
                                   "baseline_recording_id": str(RECORDING),
                                   "candidate_run_id": str(RUN)})
        assert response.status_code == 400

    def test_unknown_inputs_are_rejected_before_the_job_exists(self, client, state):
        recorder, _ = state
        response = client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(MISSING), "candidate_run_id": str(RUN)})
        # Queueing work that cannot be resolved would turn a client error into
        # an asynchronous infrastructure failure.
        assert response.status_code == 404
        assert recorder.created == []

    def test_unknown_fields_are_refused(self, client):
        response = client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN),
            "execute": True})
        # The app maps validation failures onto the published BadRequest, and
        # `ComparisonRequest` forbids additional properties, so a field like
        # `execute` cannot be smuggled through.
        assert response.status_code == 400

    def test_queueing_requires_write_scope(self, client, state):
        _, scope = state
        client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN)})
        assert scope["permission"] == "replay:write"


class TestPaginatingFindings:
    def test_a_page_carries_items_and_a_cursor(self, client):
        body = client.get(divergences_url() + "?limit=3", headers=AUTH).json()
        assert set(body) == {"items", "next_cursor"}
        assert len(body["items"]) == 3
        assert body["next_cursor"]

    def test_findings_match_the_published_divergence_shape(self, client):
        body = client.get(divergences_url() + "?limit=1", headers=AUTH).json()
        assert set(body["items"][0]) == {"id", "category", "summary", "evidence_event_ids"}

    def test_a_cursor_resumes_after_the_previous_page(self, client, state):
        recorder, _ = state
        first = client.get(divergences_url() + "?limit=2", headers=AUTH).json()
        client.get(divergences_url() + f"?limit=2&cursor={first['next_cursor']}", headers=AUTH)
        assert recorder.pages[0]["after"] is None
        assert recorder.pages[1]["after"] == 2

    def test_a_cursor_from_a_different_filter_is_rejected(self, client):
        first = client.get(divergences_url() + "?limit=2&category=changed_output",
                           headers=AUTH).json()
        # Reusing a cursor under a different query would silently skip or
        # repeat findings, so the cursor is bound to its filters.
        response = client.get(divergences_url() + f"?limit=2&cursor={first['next_cursor']}",
                              headers=AUTH)
        assert response.status_code == 400

    def test_a_cursor_from_another_comparison_is_rejected(self, client):
        other = uuid.UUID("0199b167-0000-7000-8000-0000000000aa")
        first = client.get(divergences_url() + "?limit=2", headers=AUTH).json()
        response = client.get(divergences_url(other) + f"?limit=2&cursor={first['next_cursor']}",
                              headers=AUTH)
        assert response.status_code == 400

    def test_a_forged_cursor_is_rejected(self, client):
        assert client.get(divergences_url() + "?cursor=not-a-cursor",
                          headers=AUTH).status_code == 400

    @pytest.mark.parametrize("limit", [0, -1, 501, 100000])
    def test_out_of_range_limits_are_rejected(self, client, limit):
        assert client.get(divergences_url() + f"?limit={limit}",
                          headers=AUTH).status_code == 400

    def test_an_unpublished_category_filter_is_rejected(self, client):
        assert client.get(divergences_url() + "?category=whatever",
                          headers=AUTH).status_code == 400

    @pytest.mark.parametrize("category", sorted(CATEGORIES))
    def test_every_published_category_is_accepted_as_a_filter(self, client, category):
        assert client.get(divergences_url() + f"?category={category}",
                          headers=AUTH).status_code == 200

    def test_an_unknown_comparison_is_not_found(self, client):
        assert client.get(divergences_url(MISSING), headers=AUTH).status_code == 404

    def test_the_unscoped_alias_requires_a_project(self, client):
        assert client.get(f"/v1/comparisons/{COMPARISON}/divergences").status_code == 400


class TestDivergenceRows:
    """The boundary between internal findings and the published shape."""

    def changed(self):
        run = Run()
        start = run.start()
        search = run.tool("search", parents=(start,), arguments={"q": "cap"},
                          result={"hits": 2})
        fetch = run.tool("fetch", parents=(search,), arguments={"id": 1},
                         result={"ok": True})
        run.event("z", "run.completed", parents=(fetch,))
        return run.events

    def test_every_row_uses_a_published_category(self):
        rows = divergence_rows(compare(baseline_run(), self.changed()))
        assert rows
        for row in rows:
            assert row["category"] in CATEGORIES

    def test_a_summary_locates_the_finding_in_the_run(self):
        rows = divergence_rows(compare(baseline_run(), self.changed()))
        # A detail alone can read identically in two places, so the causal
        # path is part of the summary.
        assert any("tool:search#0" in row["summary"] for row in rows)

    def test_summaries_do_not_leak_recorded_values(self):
        rows = divergence_rows(compare(baseline_run(), self.changed()))
        joined = " ".join(row["summary"] for row in rows)
        assert "cap" not in joined and "hat" not in joined

    def test_evidence_event_ids_are_deduplicated(self):
        # Both sides of a pair can carry the same event id when a recording is
        # replayed, and citing it twice would read as two pieces of evidence.
        rows = divergence_rows(compare(baseline_run(), self.changed()))
        for row in rows:
            assert len(row["evidence_event_ids"]) == len(set(row["evidence_event_ids"]))

    def test_a_run_level_finding_cites_no_events_rather_than_wrong_ones(self):
        run = Run()
        start = run.start()
        search = run.tool("search", parents=(start,), arguments={"q": "hat"},
                          result={"hits": 2})
        fetch = run.tool("fetch", parents=(search,), arguments={"id": 1},
                         result={"ok": True})
        run.event("z", "run.failed", parents=(fetch,), payload={"outcome": "failed"})
        rows = divergence_rows(compare(baseline_run(), run.events))
        run_level = [row for row in rows if row["summary"].startswith("run:")]
        for row in run_level:
            assert row["evidence_event_ids"] == []

    def test_identical_runs_produce_no_rows(self):
        assert divergence_rows(compare(baseline_run(), baseline_run())) == []


class TestPublishedShapes:
    """Responses must match the published schemas exactly, not merely contain them."""

    @staticmethod
    def contract():
        import yaml
        from pathlib import Path
        return yaml.safe_load((Path(__file__).parents[2] / "api/openapi.yaml").read_text())

    def properties(self, name):
        schema = self.contract()["components"]["schemas"][name]
        assert schema.get("additionalProperties") is False, name
        return set(schema["properties"])

    def test_the_queue_response_is_exactly_a_job_accepted(self, client):
        response = client.post(f"/v1/projects/{PROJECT}/comparisons", headers=AUTH, json={
            "baseline_recording_id": str(RECORDING), "candidate_run_id": str(RUN)})
        assert set(response.json()) == self.properties("JobAccepted")

    def test_the_comparison_response_is_exactly_a_comparison(self, client):
        response = client.get(summary_url(), headers=AUTH)
        assert set(response.json()) == self.properties("Comparison")

    def test_each_divergence_is_exactly_a_divergence(self, client):
        page = client.get(divergences_url(), headers=AUTH).json()
        assert set(page) == {"items", "next_cursor"}
        for item in page["items"]:
            assert set(item) == self.properties("Divergence")

    def test_every_emitted_category_is_published(self, client):
        published = set(self.contract()["components"]["schemas"]["DivergenceCategory"]["enum"])
        from anvil_server.divergence import CATEGORIES
        assert set(CATEGORIES) <= published

    def test_the_migration_accepts_exactly_the_published_categories(self):
        """A category the database rejects could never be stored or served."""
        from pathlib import Path
        import re

        sql = (Path(__file__).parents[2] / "migrations/0007_comparisons.sql").read_text()
        clause = re.search(r"category text NOT NULL CHECK \(category IN \(([^)]*)\)\)", sql)
        assert clause, "the category column must constrain its vocabulary"
        stored = set(re.findall(r"'([a-z_]+)'", clause.group(1)))
        published = set(self.contract()["components"]["schemas"]["DivergenceCategory"]["enum"])
        assert stored == published
