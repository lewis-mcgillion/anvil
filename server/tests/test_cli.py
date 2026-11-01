"""Behaviour of the ``anvil playback`` CLI, including its failure paths.

The CLI is the operator-facing half of ANVIL-035. These tests assert the same
invariant the REST tests assert — playback executes nothing — and additionally
pin the exit codes, because CI pipelines branch on them.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from anvil_server import cli
from anvil_server.cli import (
    EXIT_API,
    EXIT_UNPROJECTABLE,
    EXIT_USAGE,
    main,
)

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "conformance" / "v1"
GOLDEN = json.loads((CORPUS / "projections.json").read_text())


def golden_for(fixture_id: str) -> dict:
    return next(
        entry for entry in GOLDEN["projections"] if entry["id"] == fixture_id
    )


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestProjection:
    def test_projects_a_recording_to_its_published_hash(self, capsys):
        code, out, _ = run(
            capsys, "playback", "projection", "--recording", str(CORPUS / "retries.json")
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["projection_hash"] == golden_for("retries")["projection_hash"]
        assert payload["event_count"] == golden_for("retries")["event_count"]

    def test_declares_that_it_executed_nothing(self, capsys):
        _, out, _ = run(
            capsys, "playback", "projection", "--recording", str(CORPUS / "errors.json")
        )
        payload = json.loads(out)
        assert payload["mode"] == "recorded_playback"
        assert payload["executes_agent_code"] is False
        assert payload["executes_tools"] is False
        assert payload["contacts_model_provider"] is False
        assert payload["schedules_jobs"] is False

    def test_output_is_deterministic_across_invocations(self, capsys):
        first = run(
            capsys, "playback", "projection", "--recording", str(CORPUS / "errors.json")
        )[1]
        second = run(
            capsys, "playback", "projection", "--recording", str(CORPUS / "errors.json")
        )[1]
        assert first == second

    def test_cursor_seeks_to_an_earlier_position(self, capsys):
        full = json.loads(
            run(
                capsys,
                "playback",
                "projection",
                "--recording",
                str(CORPUS / "retries.json"),
            )[1]
        )
        cursor_at_two = golden_for("retries")
        code, out, _ = run(
            capsys,
            "playback",
            "projection",
            "--recording",
            str(CORPUS / "retries.json"),
            "--cursor",
            json.loads(
                (CORPUS / "retries.json").read_text()
            )["events"][1]["event_id"],
        )
        assert code == 0
        seeked = json.loads(out)
        assert seeked["position"] < full["position"]
        assert (
            seeked["projection_hash"]
            == cursor_at_two["position_hashes"][seeked["position"]]
        )

    def test_unknown_cursor_is_a_usage_error(self, capsys):
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--recording",
            str(CORPUS / "retries.json"),
            "--cursor",
            "00000000-0000-7000-8000-00000000ffff",
        )
        assert code == EXIT_USAGE
        assert "Cursor does not identify" in err

    def test_unsupported_projection_version_is_refused(self, capsys):
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--recording",
            str(CORPUS / "errors.json"),
            "--projection-version",
            "99",
        )
        assert code == EXIT_USAGE
        assert "not supported" in err


class TestRejections:
    def test_a_capture_gap_cannot_be_projected(self, capsys):
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--recording",
            str(CORPUS / "missing-events.json"),
        )
        assert code == EXIT_UNPROJECTABLE
        assert "cannot be projected" in err

    def test_missing_file_reports_a_usage_error(self, capsys, tmp_path):
        code, _, err = run(
            capsys, "playback", "projection", "--recording", str(tmp_path / "nope.json")
        )
        assert code == EXIT_USAGE
        assert "Cannot read" in err

    def test_invalid_json_reports_a_usage_error(self, capsys, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        code, _, err = run(capsys, "playback", "projection", "--recording", str(bad))
        assert code == EXIT_USAGE
        assert "not valid JSON" in err

    def test_a_json_array_is_rejected(self, capsys, tmp_path):
        bad = tmp_path / "array.json"
        bad.write_text("[]")
        code, _, err = run(capsys, "playback", "projection", "--recording", str(bad))
        assert code == EXIT_USAGE
        assert "must contain a JSON object" in err

    def test_a_document_without_events_is_rejected(self, capsys, tmp_path):
        bad = tmp_path / "empty.json"
        bad.write_text(json.dumps({"run": {"run_id": "r"}}))
        code, _, err = run(capsys, "playback", "projection", "--recording", str(bad))
        assert code == EXIT_USAGE
        assert "no 'events' array" in err

    def test_a_non_object_event_is_rejected(self, capsys, tmp_path):
        bad = tmp_path / "events.json"
        bad.write_text(json.dumps({"events": ["nope"]}))
        code, _, err = run(capsys, "playback", "projection", "--recording", str(bad))
        assert code == EXIT_USAGE
        assert "non-object event" in err

    def test_no_source_is_rejected(self, capsys):
        code, _, err = run(capsys, "playback", "projection")
        assert code == EXIT_USAGE
        assert "--recording" in err

    def test_two_sources_are_rejected(self, capsys):
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--recording",
            str(CORPUS / "errors.json"),
            "--api",
            "https://example.invalid",
        )
        assert code == EXIT_USAGE
        assert "not both" in err

    def test_api_requires_project_and_run(self, capsys):
        code, _, err = run(
            capsys, "playback", "projection", "--api", "https://example.invalid"
        )
        assert code == EXIT_USAGE
        assert "--project" in err

    def test_api_rejects_a_non_http_scheme(self, capsys):
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "file:///etc/passwd",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert code == EXIT_USAGE
        assert "http or https" in err


class TestStreams:
    def test_reconstructs_the_recorded_text(self, capsys):
        code, out, _ = run(
            capsys, "playback", "streams", "--recording", str(CORPUS / "streaming.json")
        )
        assert code == 0
        payload = json.loads(out)
        assert [record["text"] for record in payload["streams"]] == ["hello"]
        assert payload["executes_tools"] is False

    def test_transcript_prints_plain_text(self, capsys):
        code, out, _ = run(
            capsys,
            "playback",
            "streams",
            "--recording",
            str(CORPUS / "streaming.json"),
            "--transcript",
        )
        assert code == 0
        assert out == "hello\n"

    def test_a_non_string_chunk_fails_closed(self, capsys, tmp_path):
        bad = tmp_path / "chunk.json"
        bad.write_text(
            json.dumps(
                {
                    "run": {"run_id": "00000000-0000-7000-8000-000000000001"},
                    "events": [
                        {
                            "event_id": "00000000-0000-7000-8000-000000000010",
                            "stream_id": "00000000-0000-7000-8000-000000000002",
                            "sequence": 1,
                            "type": "model.chunk",
                            "parent_event_ids": [],
                            "invocation_id": "00000000-0000-7000-8000-000000000020",
                            "payload": {"chunk_index": 0, "chunk": 5},
                        }
                    ],
                }
            )
        )
        code, _, err = run(capsys, "playback", "streams", "--recording", str(bad))
        assert code == EXIT_UNPROJECTABLE
        assert "cannot be reconstructed" in err


class TestPlay:
    @pytest.mark.parametrize("speed", ["0", "1", "4"])
    def test_frames_are_identical_regardless_of_speed(self, capsys, speed):
        code, out, _ = run(
            capsys,
            "playback",
            "play",
            "--recording",
            str(CORPUS / "streaming.json"),
            "--speed",
            speed,
        )
        assert code == 0
        assert out == "hello\n"

    def test_play_never_sleeps_in_real_time(self, capsys, monkeypatch):
        """A simulated clock keeps the command safe to run in CI."""
        import time

        def explode(_seconds):  # pragma: no cover - must never run
            raise AssertionError("playback slept against the wall clock")

        monkeypatch.setattr(time, "sleep", explode)
        code, out, _ = run(
            capsys, "playback", "play", "--recording", str(CORPUS / "streaming.json")
        )
        assert code == 0
        assert out == "hello\n"


class TestVerify:
    def test_matching_hash_succeeds(self, capsys):
        code, out, _ = run(
            capsys,
            "playback",
            "verify",
            "--recording",
            str(CORPUS / "retries.json"),
            "--expect",
            golden_for("retries")["projection_hash"],
        )
        assert code == 0
        assert json.loads(out)["matched"] is True

    def test_drifted_hash_fails_and_reports_both_values(self, capsys):
        expected = "0" * 64
        code, out, err = run(
            capsys,
            "playback",
            "verify",
            "--recording",
            str(CORPUS / "retries.json"),
            "--expect",
            expected,
        )
        assert code == EXIT_UNPROJECTABLE
        payload = json.loads(out)
        assert payload["matched"] is False
        assert payload["expected_hash"] == expected
        assert payload["projection_hash"] == golden_for("retries")["projection_hash"]
        assert expected in err

    def test_without_expect_it_reports_the_hash(self, capsys):
        code, out, _ = run(
            capsys, "playback", "verify", "--recording", str(CORPUS / "errors.json")
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["matched"] is True
        assert payload["expected_hash"] is None


class TestApiSource:
    def test_network_failure_is_reported_as_an_api_error(self, capsys, monkeypatch):
        import urllib.error

        def explode(*_args, **_kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(cli.urllib.request, "urlopen", explode)
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert code == EXIT_API
        assert "Cannot reach" in err

    def test_http_error_is_reported_with_its_status(self, capsys, monkeypatch):
        import io
        import urllib.error

        def explode(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://anvil.invalid", 403, "Forbidden", {}, io.BytesIO(b"denied")
            )

        monkeypatch.setattr(cli.urllib.request, "urlopen", explode)
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert code == EXIT_API
        assert "403" in err

    def test_the_api_source_only_issues_a_get(self, capsys, monkeypatch):
        seen = {}

        class Response:
            def read(self):
                return json.dumps(
                    json.loads((CORPUS / "errors.json").read_text())
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def capture(request, timeout=None):
            seen["method"] = request.get_method()
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return Response()

        monkeypatch.setattr(cli.urllib.request, "urlopen", capture)
        code, _, _ = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p1",
            "--run",
            "r1",
            "--token",
            "secret",
        )
        assert code == 0
        assert seen["method"] == "GET"
        assert seen["url"].startswith(
            "https://anvil.invalid/v1/projects/p1/runs/r1/events"
        )
        assert seen["auth"] == "Bearer secret"

    def test_token_falls_back_to_the_environment(self, capsys, monkeypatch):
        seen = {}

        class Response:
            def read(self):
                return (CORPUS / "errors.json").read_text().encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def capture(request, timeout=None):
            seen["auth"] = request.get_header("Authorization")
            return Response()

        monkeypatch.setenv("ANVIL_TOKEN", "from-env")
        monkeypatch.setattr(cli.urllib.request, "urlopen", capture)
        code, _, _ = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert code == 0
        assert seen["auth"] == "Bearer from-env"

    def test_a_token_is_never_written_to_output(self, capsys, monkeypatch):
        class Response:
            def read(self):
                return (CORPUS / "errors.json").read_text().encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(
            cli.urllib.request, "urlopen", lambda *a, **k: Response()
        )
        code, out, err = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
            "--token",
            "super-secret-token",
        )
        assert code == 0
        assert "super-secret-token" not in out
        assert "super-secret-token" not in err


class TestNoExecution:
    """Playback must not reach the job queue or a model provider."""

    def test_cli_imports_no_execution_machinery(self):
        source = pathlib.Path(cli.__file__).read_text()
        for forbidden in (
            "from .jobs import",
            "from .worker import",
            "from .capture import",
            "JobQueue",
            "broker",
        ):
            assert forbidden not in source

    def test_job_queue_is_never_constructed(self, capsys, monkeypatch):
        import anvil_server.jobs as jobs

        for name in dir(jobs):
            attribute = getattr(jobs, name)
            if isinstance(attribute, type) and name.endswith("Queue"):
                monkeypatch.setattr(
                    attribute,
                    "__init__",
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("playback constructed a job queue")
                    ),
                )
        code, _, _ = run(
            capsys, "playback", "projection", "--recording", str(CORPUS / "errors.json")
        )
        assert code == 0

    def test_playback_opens_no_database_connection(self, capsys, monkeypatch):
        import anvil_server.database as database

        monkeypatch.setattr(
            database,
            "engine_for",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("playback opened a database engine")
            ),
        )
        code, _, _ = run(
            capsys, "playback", "streams", "--recording", str(CORPUS / "streaming.json")
        )
        assert code == 0


class TestHelp:
    def test_bare_invocation_shows_help_without_failing_hard(self, capsys):
        code, _, _ = run(capsys)
        assert code in {0, EXIT_USAGE}

    def test_playback_help_states_the_read_only_guarantee(self, capsys):
        code, out, _ = run(capsys, "playback", "--help")
        assert code == 0
        assert "read-only" in out.lower()


class TestClickCompatibility:
    """Typer vendors Click, but a standalone Click is often installed too.

    The two define distinct classes with identical names. Catching only one
    lets the other escape as an unhandled traceback, which is how bare
    ``anvil`` once crashed instead of printing help.
    """

    def test_exception_tuple_covers_every_installed_click(self):
        modules = cli._click_modules()
        assert modules, "no Click implementation was found"
        caught = cli.click_exceptions()
        for module in modules:
            namespace = getattr(module, "exceptions", module)
            usage = getattr(namespace, "UsageError", None) or getattr(
                module, "UsageError", None
            )
            assert usage in caught

    def test_vendored_typer_click_is_preferred_first(self):
        modules = cli._click_modules()
        assert modules[0].__name__.startswith("typer")

    def test_bare_invocation_never_raises(self, capsys):
        # Regression: this used to escape main() as a traceback.
        code = cli.main([])
        capsys.readouterr()
        assert code == EXIT_USAGE


class TestApiPagination:
    """The list-events endpoint pages; projecting one page would truncate."""

    def _serve(self, monkeypatch, pages):
        calls = []

        class Response:
            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake(request, timeout=None):
            calls.append(request.full_url)
            return Response(pages[len(calls) - 1])

        monkeypatch.setattr(cli.urllib.request, "urlopen", fake)
        return calls

    def test_every_page_is_followed(self, capsys, monkeypatch):
        recording = json.loads((CORPUS / "retries.json").read_text())
        events = recording["events"]
        pages = [
            {"items": events[:2], "next_cursor": "c1"},
            {"items": events[2:4], "next_cursor": "c2"},
            {"items": events[4:], "next_cursor": None},
        ]
        calls = self._serve(monkeypatch, pages)
        code, out, _ = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            recording["run"]["run_id"],
        )
        assert code == 0
        assert len(calls) == 3
        payload = json.loads(out)
        # The whole recording must be projected, matching the published golden.
        assert payload["event_count"] == golden_for("retries")["event_count"]
        assert payload["projection_hash"] == golden_for("retries")["projection_hash"]

    def test_a_single_page_still_works(self, capsys, monkeypatch):
        recording = json.loads((CORPUS / "errors.json").read_text())
        calls = self._serve(
            monkeypatch, [{"items": recording["events"], "next_cursor": None}]
        )
        code, out, _ = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            recording["run"]["run_id"],
        )
        assert code == 0
        assert len(calls) == 1
        assert json.loads(out)["projection_hash"] == golden_for("errors")[
            "projection_hash"
        ]

    def test_a_repeated_cursor_fails_instead_of_looping(self, capsys, monkeypatch):
        recording = json.loads((CORPUS / "errors.json").read_text())
        page = {"items": recording["events"], "next_cursor": "same"}
        self._serve(monkeypatch, [page, page, page])
        code, _, err = run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert code == EXIT_API
        assert "repeated or invalid pagination cursor" in err

    def test_the_first_request_sends_no_cursor(self, capsys, monkeypatch):
        recording = json.loads((CORPUS / "errors.json").read_text())
        calls = self._serve(
            monkeypatch, [{"items": recording["events"], "next_cursor": None}]
        )
        run(
            capsys,
            "playback",
            "projection",
            "--api",
            "https://anvil.invalid",
            "--project",
            "p",
            "--run",
            "r",
        )
        assert "cursor=" not in calls[0]
        assert f"limit={cli.PAGE_SIZE}" in calls[0]
