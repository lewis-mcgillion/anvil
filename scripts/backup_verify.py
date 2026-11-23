"""Verify recorded archive hashes after operator-managed backup/restore."""

import hashlib
from pathlib import Path


def recording_hashes(files: list[Path]) -> dict[str, str]:
    hashes = {}
    for path in files:
        if not path.is_file() or path.name in hashes:
            raise ValueError("Missing or duplicated recording archive")
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


def verify_restore(expected: dict[str, str], restored: list[Path]) -> None:
    if not expected or recording_hashes(restored) != expected:
        raise ValueError("Restored recordings differ from backup manifest")
