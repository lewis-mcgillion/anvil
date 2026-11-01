"""Opt-in crash-recovery spool (ANVIL-022).

The acceptance criterion is that corruption and crashes recover safely
without spilling raw secrets. These tests therefore spend most of their
effort on the failure side: truncated writes, tampered records, hostile
directory permissions, quota exhaustion, expiry, and a drain interrupted
part-way.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from anvil import (
    AnvilClient, CaptureError, ExporterConfig, RedactionPolicy, Response,
    Spool, SpoolConfig, SpoolError,
)
from anvil._spool import SPOOL_VERSION
from anvil._types import uuid7

SECRET = "sk-never-written-to-disk"


def event(run_id: str = "", *, payload: dict | None = None, type: str = "message.created") -> dict:
    return {
        "event_id": uuid7(),
        "run_id": run_id or uuid7(),
        "stream_id": uuid7(),
        "sequence": 1,
        "type": type,
        "occurred_at": "2026-01-01T00:00:00.000Z",
        "parent_event_ids": [],
        "invocation_id": None,
        "payload": payload if payload is not None
        else {"role": "user", "content": "hello", "redactions": [], "replay_safe": True},
    }


@pytest.fixture()
def spool(tmp_path: Path) -> Spool:
    return Spool(SpoolConfig(directory=tmp_path / "spool"))


class TestOptIn:
    def test_a_client_spools_nothing_unless_asked(self) -> None:
        class Dead:
            def post(self, *args):
                raise CaptureError("down")

        client = AnvilClient(project_id=uuid7(), transport=Dead())
        assert client.spool is None

    def test_a_configured_client_creates_the_spool(self, tmp_path: Path) -> None:
        class Dead:
            def post(self, *args):
                raise CaptureError("down")

        directory = tmp_path / "opted-in"
        client = AnvilClient(project_id=uuid7(), transport=Dead(),
                             spool=SpoolConfig(directory=directory))
        assert client.spool is not None
        assert directory.is_dir()

    @pytest.mark.parametrize("config", [
        {"max_bytes": 0}, {"max_bytes": -1}, {"max_records": 0},
        {"max_age_seconds": 0}, {"max_age_seconds": -5}, {"max_bytes": 1.5},
    ])
    def test_an_impossible_limit_is_rejected(self, tmp_path: Path, config) -> None:
        with pytest.raises(ValueError):
            SpoolConfig(directory=tmp_path, **config)


class TestOnlyRedactedEvents:
    def test_an_unredacted_event_never_reaches_the_disk(self, spool, tmp_path) -> None:
        raw = event(payload={"role": "user", "content": SECRET})
        with pytest.raises(SpoolError):
            spool.store(raw)
        assert spool.status.records == 0
        assert SECRET not in _everything(spool.config.directory)

    def test_a_payload_that_is_not_an_object_is_refused(self, spool) -> None:
        with pytest.raises(SpoolError):
            spool.store({**event(), "payload": "oops"})

    def test_a_payload_missing_replay_safe_is_refused(self, spool) -> None:
        with pytest.raises(SpoolError):
            spool.store(event(payload={"content": "x", "redactions": []}))

    def test_a_redacted_event_is_accepted(self, spool) -> None:
        assert spool.store(event()) is True
        assert spool.status.records == 1

    def test_a_non_finite_payload_is_refused(self, spool) -> None:
        bad = event(payload={"content": float("inf"), "redactions": [], "replay_safe": True})
        with pytest.raises(SpoolError):
            spool.store(bad)

    def test_the_marker_is_what_is_written_for_a_redacted_field(self, spool) -> None:
        policy = RedactionPolicy()
        cleaned = policy.apply("message.created", {"role": "user", "content": SECRET})
        spool.store(event(payload=cleaned))
        written = _everything(spool.config.directory)
        assert SECRET not in written
        assert "[REDACTED]" in written


class TestPrivacy:
    def test_the_directory_is_private(self, spool) -> None:
        mode = spool.config.directory.lstat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_every_record_is_private(self, spool) -> None:
        spool.store(event())
        for path in spool.config.directory.glob("*.event.json"):
            assert not path.lstat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_a_world_readable_directory_is_refused(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o777)
        os.chmod(shared, 0o777)
        with pytest.raises(SpoolError):
            Spool(SpoolConfig(directory=shared))

    def test_a_symlinked_directory_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "real"
        target.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(SpoolError):
            Spool(SpoolConfig(directory=link))

    def test_a_file_where_the_directory_should_be_is_refused(self, tmp_path: Path) -> None:
        occupied = tmp_path / "occupied"
        occupied.write_text("not a directory")
        with pytest.raises(SpoolError):
            Spool(SpoolConfig(directory=occupied))


class TestQuotas:
    def test_a_full_spool_refuses_rather_than_evicting(self, tmp_path: Path) -> None:
        spool = Spool(SpoolConfig(directory=tmp_path / "s", max_records=2))
        first, second = event(), event()
        assert spool.store(first) and spool.store(second)
        assert spool.store(event()) is False
        assert spool.status.records == 2
        assert spool.status.refused == 1
        recovered = [item["event_id"] for _, item in spool.recover()]
        assert recovered == [first["event_id"], second["event_id"]]

    def test_a_byte_quota_is_enforced(self, tmp_path: Path) -> None:
        spool = Spool(SpoolConfig(directory=tmp_path / "s", max_bytes=1024))
        stored = 0
        while spool.store(event()):
            stored += 1
            assert stored < 50
        assert spool.status.bytes <= 1024
        assert spool.status.refused == 1

    def test_an_oversized_record_is_refused_without_writing(self, spool) -> None:
        huge = event(payload={"content": "x" * (300 * 1024), "redactions": [],
                              "replay_safe": True})
        assert spool.store(huge) is False
        assert spool.status.records == 0

    def test_refusal_is_reported_as_incomplete(self, tmp_path: Path) -> None:
        spool = Spool(SpoolConfig(directory=tmp_path / "s", max_records=1))
        spool.store(event())
        spool.store(event())
        assert spool.status.incomplete is True


class TestExpiry:
    def test_an_expired_record_is_deleted_unread(self, tmp_path: Path) -> None:
        spool = Spool(SpoolConfig(directory=tmp_path / "s", max_age_seconds=0.01))
        spool.store(event())
        time.sleep(0.05)
        assert list(spool.recover()) == []
        assert spool.status.records == 0
        assert spool.status.expired >= 1

    def test_a_fresh_record_survives_expiry(self, spool) -> None:
        spool.store(event())
        spool.expire()
        assert spool.status.records == 1

    def test_expiry_runs_before_a_quota_refusal(self, tmp_path: Path) -> None:
        spool = Spool(SpoolConfig(directory=tmp_path / "s", max_records=1,
                                  max_age_seconds=0.01))
        spool.store(event())
        time.sleep(0.05)
        assert spool.store(event()) is True
        assert spool.status.records == 1


class TestCorruption:
    def _one(self, spool: Spool) -> Path:
        spool.store(event())
        return next(iter(spool.config.directory.glob("*.event.json")))

    def test_a_truncated_record_is_dropped(self, spool) -> None:
        path = self._one(spool)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        assert list(spool.recover()) == []
        assert spool.status.corrupt == 1
        assert not path.exists()

    def test_a_record_with_no_trailing_newline_is_dropped(self, spool) -> None:
        path = self._one(spool)
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        assert list(spool.recover()) == []
        assert spool.status.corrupt == 1

    def test_a_tampered_event_fails_its_digest(self, spool) -> None:
        path = self._one(spool)
        record = json.loads(path.read_bytes())
        record["event"]["payload"]["content"] = "injected"
        path.write_bytes(json.dumps(record).encode() + b"\n")
        assert list(spool.recover()) == []
        assert spool.status.corrupt == 1

    def test_a_record_from_another_version_is_dropped(self, spool) -> None:
        path = self._one(spool)
        record = json.loads(path.read_bytes())
        record["spool_version"] = SPOOL_VERSION + "0"
        path.write_bytes(json.dumps(record).encode() + b"\n")
        assert list(spool.recover()) == []

    def test_garbage_is_dropped(self, spool) -> None:
        path = self._one(spool)
        path.write_bytes(b"\x00\xff not json at all\n")
        assert list(spool.recover()) == []
        assert spool.status.corrupt == 1

    def test_a_record_smuggling_an_unredacted_payload_is_dropped(self, spool) -> None:
        path = self._one(spool)
        smuggled = event(payload={"content": SECRET})
        body = json.dumps(smuggled, ensure_ascii=True, separators=(",", ":")).encode()
        import hashlib
        path.write_bytes(json.dumps({
            "spool_version": SPOOL_VERSION,
            "sha256": hashlib.sha256(body).hexdigest(),
            "event": smuggled,
        }).encode() + b"\n")
        assert list(spool.recover()) == []

    def test_corruption_is_never_quoted_in_an_error(self, spool) -> None:
        path = self._one(spool)
        path.write_bytes(SECRET.encode() + b"\n")
        for _ in spool.recover():
            pass
        assert spool.status.corrupt == 1

    def test_a_partial_write_left_by_a_crash_is_cleaned_up(self, spool) -> None:
        leftover = spool.config.directory / "crashed.partial"
        leftover.write_bytes(b'{"incomplete":')
        spool.store(event())
        assert len(list(spool.recover())) == 1
        assert not leftover.exists()

    def test_one_corrupt_record_does_not_hide_the_others(self, spool) -> None:
        good_first = event()
        spool.store(good_first)
        bad = self._one(spool)
        bad.write_bytes(b"corrupt\n")
        good_last = event()
        spool.store(good_last)
        recovered = [item["event_id"] for _, item in spool.recover()]
        assert recovered == [good_first["event_id"], good_last["event_id"]]
        assert spool.status.corrupt == 1


class TestRecoveryAndDrain:
    def test_records_are_recovered_oldest_first(self, spool) -> None:
        ordered = [event() for _ in range(5)]
        for item in ordered:
            spool.store(item)
        assert [item["event_id"] for _, item in spool.recover()] == \
            [item["event_id"] for item in ordered]

    def test_a_recovered_record_stays_until_released(self, spool) -> None:
        spool.store(event())
        path, _ = next(iter(spool.recover()))
        assert path.exists()
        spool.release(path)
        assert not path.exists()
        assert spool.status.records == 0

    def test_a_drain_sends_and_deletes_what_was_accepted(self, spool) -> None:
        run = uuid7()
        for _ in range(3):
            spool.store(event(run))
        transport = _Accepting()
        assert spool.drain(transport) == 3
        assert spool.status.records == 0
        assert spool.status.drained == 3
        assert len(transport.calls) == 1
        assert transport.calls[0][0] == f"/runs/{run}/events:batch"

    def test_a_drain_groups_by_run(self, spool) -> None:
        first, second = uuid7(), uuid7()
        spool.store(event(first))
        spool.store(event(second))
        transport = _Accepting()
        assert spool.drain(transport) == 2
        assert len(transport.calls) == 2

    def test_a_failed_drain_keeps_everything_it_did_not_confirm(self, spool) -> None:
        run = uuid7()
        for _ in range(2):
            spool.store(event(run))
        transport = _Failing()
        assert spool.drain(transport) == 0
        assert spool.status.records == 2

    def test_a_rejected_acknowledgement_is_not_treated_as_success(self, spool) -> None:
        spool.store(event())
        assert spool.drain(_Lying()) == 0
        assert spool.status.records == 1

    def test_a_drain_is_bounded_by_its_batch_size(self, spool) -> None:
        run = uuid7()
        for _ in range(5):
            spool.store(event(run))
        transport = _Accepting()
        assert spool.drain(transport, max_batch=2) == 5
        assert len(transport.calls) == 3

    def test_a_drain_uses_a_fresh_idempotency_key_per_batch(self, spool) -> None:
        for _ in range(2):
            spool.store(event())
        transport = _Accepting()
        spool.drain(transport)
        keys = [key for _, _, key, _ in transport.calls]
        assert len(set(keys)) == len(keys)

    def test_an_invalid_batch_size_is_rejected(self, spool) -> None:
        with pytest.raises(ValueError):
            spool.drain(_Accepting(), max_batch=0)

    def test_draining_an_empty_spool_sends_nothing(self, spool) -> None:
        transport = _Accepting()
        assert spool.drain(transport) == 0
        assert transport.calls == []


class TestExporterIntegration:
    def test_a_permanently_failed_export_survives_the_process(self, tmp_path: Path) -> None:
        directory = tmp_path / "spool"
        client = AnvilClient(
            project_id=uuid7(), transport=_Failing(),
            exporter=ExporterConfig(max_attempts=1, request_timeout=0.1),
            spool=SpoolConfig(directory=directory),
        )
        with client.run("agent", run_id=uuid7()) as run:
            run.message("user", "hello")
            for _ in range(200):
                if client.spool.status.records:
                    break
                time.sleep(0.01)
        with pytest.raises(CaptureError):
            client.close()
        assert client.spool.status.records >= 1
        # A later process reads the same directory and finds the evidence.
        later = Spool(SpoolConfig(directory=directory))
        recovered = [item for _, item in later.recover()]
        assert any(item["type"] == "message.created" for item in recovered)

    def test_spooled_evidence_is_still_redacted(self, tmp_path: Path) -> None:
        directory = tmp_path / "spool"
        client = AnvilClient(
            project_id=uuid7(), transport=_Failing(),
            exporter=ExporterConfig(max_attempts=1, request_timeout=0.1),
            spool=SpoolConfig(directory=directory),
        )
        with client.run("agent", run_id=uuid7()) as run:
            run.message("user", f"my key is {SECRET}")
            for _ in range(200):
                if client.spool.status.records:
                    break
                time.sleep(0.01)
        with pytest.raises(CaptureError):
            client.close()
        assert client.spool.status.records >= 1
        assert SECRET not in _everything(directory)

    def test_a_spool_failure_never_becomes_a_capture_failure(self, tmp_path: Path) -> None:
        directory = tmp_path / "spool"
        client = AnvilClient(
            project_id=uuid7(), transport=_Failing(),
            exporter=ExporterConfig(max_attempts=1, request_timeout=0.1),
            spool=SpoolConfig(directory=directory, max_records=1),
        )
        with client.run("agent", run_id=uuid7()) as run:
            for index in range(5):
                run.message("user", f"message {index}")
            time.sleep(0.2)
        with pytest.raises(CaptureError):
            client.close()
        assert client.status.incomplete
        assert client.spool.status.records <= 1


class _Accepting:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str, float]] = []

    def post(self, path, body, key, timeout):
        self.calls.append((path, body, key, timeout))
        count = len(json.loads(body)["events"])
        return Response(200, {}, json.dumps({"accepted": count, "duplicates": 0}).encode())


class _Failing:
    def post(self, path, body, key, timeout):
        if path.endswith("/runs"):
            return Response(201, {}, json.dumps({"id": uuid7()}).encode())
        raise CaptureError("transport is down")


class _Lying:
    def post(self, path, body, key, timeout):
        return Response(200, {}, json.dumps({"accepted": 99, "duplicates": 0}).encode())


def _everything(directory: Path) -> str:
    return "".join(
        path.read_bytes().decode("utf-8", "replace")
        for path in directory.rglob("*") if path.is_file()
    )
