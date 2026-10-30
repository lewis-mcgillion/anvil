"""Immutable, content-addressed compilation of local suite inputs."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import rfc8785

from .models import local_path
from .parser import SuiteError, parse_suite

MAX_REFERENCE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class LockedReference:
    file: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class CompiledSuite:
    sha256: str
    canonical: bytes
    references: tuple[LockedReference, ...]


def _read_local(root: Path, relative: str, limit: int) -> bytes:
    local_path(relative)
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        parts = relative.split("/")
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SuiteError("References must be regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise SuiteError("Reference exceeds byte limit")
        return content
    except OSError as exc:
        raise SuiteError(f"Cannot read local reference {relative}: {exc.strerror}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def compile_suite(source: str | bytes, root: Path) -> CompiledSuite:
    suite = parse_suite(source)
    locks: dict[str, LockedReference] = {}
    total = 0
    for case in suite.cases:
        for reference in (case.recording, case.fault_plan):
            if reference is None:
                continue
            if reference.file in locks:
                if locks[reference.file].sha256 != reference.sha256:
                    raise SuiteError("Conflicting checksums for the same local reference")
                continue
            content = _read_local(root, reference.file, min(MAX_REFERENCE_BYTES, MAX_TOTAL_BYTES - total))
            digest = hashlib.sha256(content).hexdigest()
            if digest != reference.sha256:
                raise SuiteError(f"SHA-256 mismatch for {reference.file}")
            total += len(content)
            locks[reference.file] = LockedReference(reference.file, digest, content)
    canonical = rfc8785.dumps({
        "compiler_version": "1",
        "suite": suite.model_dump(),
        "references": [
            {"file": lock.file, "sha256": lock.sha256, "size": len(lock.content)}
            for lock in sorted(locks.values(), key=lambda item: item.file)
        ],
    })
    return CompiledSuite(
        hashlib.sha256(canonical).hexdigest(), canonical,
        tuple(sorted(locks.values(), key=lambda item: item.file)),
    )
