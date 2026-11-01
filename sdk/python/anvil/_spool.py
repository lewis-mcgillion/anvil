# Copyright 2026 Anvil contributors
# SPDX-License-Identifier: Apache-2.0
"""Opt-in crash-recovery spool for already-redacted events (ANVIL-022).

The exporter holds events in memory, so a process that dies takes its
unexported evidence with it. This is the optional answer: a small on-disk
queue that survives the crash and can be drained by the next process.

Putting capture evidence on disk is a privacy decision, not a reliability
one, so it is off unless a caller asks for it and is constrained in four
ways:

* **Only redacted events are ever written.** Every payload that has been
  through :class:`~anvil._redaction.RedactionPolicy` carries a ``redactions``
  list and a ``replay_safe`` flag. A record without them is refused, so a raw
  payload cannot reach the disk even by mistake — the check is structural
  rather than a reminder in a docstring.
* **The spool is private.** The directory is created `0o700` and files
  `0o600`, and a directory that is a symlink, is not a directory, or is
  readable by anyone else is refused rather than tightened: something else
  created it, and silently taking it over is how one process writes its
  evidence into another's.
* **It is bounded.** Files and bytes are capped, and a full spool refuses new
  records instead of evicting old ones. A spool that evicts is a spool that
  decides which evidence to lose.
* **It expires.** A record older than the configured age is deleted
  unread. Capture evidence that nobody recovered is not worth keeping on a
  developer's laptop indefinitely.

Corruption is expected, because the interesting case is a process killed
mid-write. Every record is a single line with the SHA-256 of its own event
bytes, so a truncated tail, a partial write or any edit fails the digest and
the record is dropped. A corrupt record is counted and deleted; it is never
yielded, never retried and never quoted in an error message, because the
bytes that failed to parse are exactly the bytes nobody has validated.

Errors, cancellation and cleanup: every file handle is opened in a ``with``
block, writes are ``fsync``-ed and renamed into place, so a crash leaves
either a complete record or a temporary file that recovery deletes. Draining
deletes each record only after the server has acknowledged it, so a crash
mid-drain re-sends rather than loses; the server's idempotency key makes that
safe. :meth:`Spool.drain` stops at the first transport failure and leaves
everything it has not confirmed.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ._transport import CaptureError, Transport, acknowledged
from ._types import uuid7

__all__ = ["SPOOL_VERSION", "SpoolConfig", "SpoolStatus", "SpoolError", "Spool"]

SPOOL_VERSION = "1"

_RECORD_SUFFIX = ".event.json"
_TEMP_SUFFIX = ".partial"
_MAX_RECORD_BYTES = 256 * 1024


class SpoolError(CaptureError):
    """The spool could not be used. Messages never include record contents."""


@dataclass(frozen=True)
class SpoolConfig:
    """Where the spool lives and how much it may keep."""

    directory: Path
    max_bytes: int = 8 * 1024 * 1024
    max_records: int = 1024
    max_age_seconds: float = 24 * 60 * 60

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("spool max_bytes must be a positive integer")
        if type(self.max_records) is not int or self.max_records < 1:
            raise ValueError("spool max_records must be a positive integer")
        if not isinstance(self.max_age_seconds, (int, float)) or self.max_age_seconds <= 0:
            raise ValueError("spool max_age_seconds must be positive")
        object.__setattr__(self, "directory", Path(self.directory))


@dataclass(frozen=True)
class SpoolStatus:
    """What the spool holds, and what it has refused or thrown away."""

    records: int
    bytes: int
    refused: int
    corrupt: int
    expired: int
    drained: int

    @property
    def incomplete(self) -> bool:
        """Whether evidence was lost rather than merely delayed."""
        return bool(self.refused or self.corrupt or self.expired)


class Spool:
    """A bounded, private, self-expiring queue of redacted events."""

    def __init__(self, config: SpoolConfig) -> None:
        self.config = config
        self._refused = self._corrupt = self._expired = self._drained = 0
        self._prepare()

    # -- directory ---------------------------------------------------------

    def _prepare(self) -> None:
        directory = self.config.directory
        if directory.is_symlink():
            raise SpoolError("spool directory must not be a symlink")
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise SpoolError(f"spool directory unusable: {type(error).__name__}") from None
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise SpoolError("spool path is not a directory")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            # Pre-existing and shared: refuse rather than tighten, because
            # something else made it and may still be using it.
            raise SpoolError("spool directory is accessible to other users")

    # -- writing -----------------------------------------------------------

    def store(self, event: dict) -> bool:
        """Persist one already-redacted event. ``False`` means it was refused.

        The caller treats a refusal exactly as it treats a dropped event: as a
        gap. Nothing here raises for a full spool, because a full spool is an
        expected state and not a programming error.
        """
        payload = event.get("payload")
        if not isinstance(payload, dict) or "redactions" not in payload \
                or "replay_safe" not in payload:
            # Structural proof that only post-redaction events are written.
            self._refused += 1
            raise SpoolError("the spool accepts only redacted capture events")

        try:
            body = json.dumps(event, ensure_ascii=True, separators=(",", ":"),
                              allow_nan=False).encode()
        except (TypeError, ValueError):
            self._refused += 1
            raise SpoolError("spooled event is not finite JSON") from None

        if len(body) > _MAX_RECORD_BYTES:
            self._refused += 1
            return False

        record = json.dumps({
            "spool_version": SPOOL_VERSION,
            "sha256": hashlib.sha256(body).hexdigest(),
            "event": json.loads(body),
        }, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"

        self.expire()
        held_records, held_bytes = self._usage()
        if held_records >= self.config.max_records \
                or held_bytes + len(record) > self.config.max_bytes:
            self._refused += 1
            return False

        return self._write(record)

    def _write(self, record: bytes) -> bool:
        directory = self.config.directory
        handle, temporary = tempfile.mkstemp(dir=directory, suffix=_TEMP_SUFFIX)
        path = Path(temporary)
        try:
            with os.fdopen(handle, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(record)
                stream.flush()
                os.fsync(stream.fileno())
            # Named after the write so a reader never sees a partial record
            # under a name it would try to read.
            path.replace(directory / f"{time.time_ns():020d}-{uuid7()}{_RECORD_SUFFIX}")
            self._sync_directory()
            return True
        except OSError:
            path.unlink(missing_ok=True)
            self._refused += 1
            return False

    def _sync_directory(self) -> None:
        try:
            descriptor = os.open(self.config.directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    # -- reading -----------------------------------------------------------

    def _records(self) -> list[Path]:
        try:
            entries = sorted(self.config.directory.iterdir())
        except OSError:
            return []
        return [path for path in entries if path.name.endswith(_RECORD_SUFFIX)]

    def _usage(self) -> tuple[int, int]:
        total = 0
        paths = self._records()
        for path in paths:
            try:
                total += path.lstat().st_size
            except OSError:
                continue
        return len(paths), total

    def expire(self) -> int:
        """Delete records older than the configured age. Returns the count."""
        cutoff = time.time() - self.config.max_age_seconds
        removed = 0
        for path in self._records():
            try:
                if path.lstat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        self._expired += removed
        return removed

    def recover(self) -> Iterator[tuple[Path, dict]]:
        """Yield ``(path, event)`` for every intact record, oldest first.

        Expired records are deleted first and never yielded. A record that
        fails any check — unreadable, not a regular file, oversized, not one
        line of JSON, wrong version, wrong digest, or not a redacted event —
        is counted, deleted and skipped. The caller deletes a yielded record
        once it has been accepted somewhere else.
        """
        self.expire()
        self._cleanup_temporaries()
        for path in self._records():
            event = self._read(path)
            if event is None:
                self._corrupt += 1
                path.unlink(missing_ok=True)
                continue
            yield path, event

    def _read(self, path: Path) -> dict | None:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES * 2:
                return None
            with path.open("rb") as stream:
                raw = stream.read(_MAX_RECORD_BYTES * 2 + 1)
        except OSError:
            return None
        if len(raw) > _MAX_RECORD_BYTES * 2 or not raw.endswith(b"\n"):
            # A missing newline is a write that did not finish.
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("spool_version") != SPOOL_VERSION:
            return None
        event, digest = record.get("event"), record.get("sha256")
        if not isinstance(event, dict) or not isinstance(digest, str):
            return None
        try:
            body = json.dumps(event, ensure_ascii=True, separators=(",", ":"),
                              allow_nan=False).encode()
        except (TypeError, ValueError):
            return None
        if hashlib.sha256(body).hexdigest() != digest:
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict) or "redactions" not in payload:
            return None
        return event

    def _cleanup_temporaries(self) -> None:
        for path in self.config.directory.glob("*" + _TEMP_SUFFIX):
            path.unlink(missing_ok=True)

    def release(self, path: Path) -> None:
        """Delete one recovered record."""
        path.unlink(missing_ok=True)

    # -- draining ----------------------------------------------------------

    def drain(self, transport: Transport, *, timeout: float = 5.0,
              max_batch: int = 100) -> int:
        """Re-send recovered records and return how many were accepted.

        Records are grouped per run, sent with an idempotency key and deleted
        only after the server acknowledges them, so a crash mid-drain
        re-sends rather than loses. Stops at the first failure and leaves
        every unconfirmed record in place; the caller can retry later.
        """
        if max_batch < 1:
            raise ValueError("max_batch must be positive")
        batch: list[tuple[Path, dict]] = []
        run_id: str | None = None
        accepted = 0

        def flush() -> bool:
            nonlocal accepted, batch, run_id
            if not batch:
                return True
            body = b'{"events":[' + b",".join(
                json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode()
                for _, event in batch
            ) + b"]}"
            try:
                response = transport.post(f"/runs/{run_id}/events:batch", body,
                                          uuid7(), timeout)
                acknowledged(response, len(batch))
            except CaptureError:
                batch, run_id = [], None
                return False
            for path, _ in batch:
                self.release(path)
            accepted += len(batch)
            self._drained += len(batch)
            batch, run_id = [], None
            return True

        for path, event in self.recover():
            if run_id is not None and (event.get("run_id") != run_id
                                       or len(batch) >= max_batch):
                if not flush():
                    return accepted
            run_id = event.get("run_id")
            batch.append((path, event))
        flush()
        return accepted

    # -- reporting ---------------------------------------------------------

    @property
    def status(self) -> SpoolStatus:
        records, held = self._usage()
        return SpoolStatus(records=records, bytes=held, refused=self._refused,
                           corrupt=self._corrupt, expired=self._expired,
                           drained=self._drained)
