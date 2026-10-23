"""Bounded portable recording bundles, without filesystem extraction."""

import hashlib
import io
import json
import re
import zipfile

MAX_ARTIFACT = 8 * 1024 * 1024
MAX_BUNDLE = 64 * 1024 * 1024
MAX_FILES = 1024


def _validate_name(name: str):
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Unsafe artifact name")


def export_bundle(recording_id: str, artifacts: dict[str, bytes]) -> bytes:
    if not recording_id or len(artifacts) + 1 > MAX_FILES:
        raise ValueError("Invalid bundle metadata")
    entries = []
    hashes = set()
    for name, contents in artifacts.items():
        _validate_name(name)
        if len(contents) > MAX_ARTIFACT:
            raise ValueError("Artifact too large")
        digest = hashlib.sha256(contents).hexdigest()
        if digest in hashes:
            raise ValueError("Duplicate artifact content")
        hashes.add(digest)
        entries.append({"name": name, "sha256": digest, "size": len(contents), "media_type": "application/octet-stream"})
    manifest = {"schema_version": "1", "recording_id": recording_id, "artifacts": entries}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        for entry in entries:
            archive.writestr("artifacts/" + entry["sha256"], artifacts[entry["name"]])
    if output.tell() > MAX_BUNDLE:
        raise ValueError("Bundle too large")
    return output.getvalue()


def import_bundle(data: bytes) -> tuple[dict, dict[str, bytes]]:
    if len(data) > MAX_BUNDLE:
        raise ValueError("Bundle too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = archive.infolist()
        names = [info.filename for info in files]
        if len(files) > MAX_FILES or len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("Invalid archive members")
        if any(info.is_dir() or info.file_size > MAX_ARTIFACT or info.external_attr >> 16 & 0o170000 == 0o120000
               or info.filename != "manifest.json" and not re.fullmatch(r"artifacts/[0-9a-f]{64}", info.filename) for info in files):
            raise ValueError("Unsafe archive member")
        if sum(info.file_size for info in files) > MAX_BUNDLE:
            raise ValueError("Decompression limit exceeded")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != "1" or not manifest.get("recording_id") or not isinstance(manifest.get("artifacts"), list):
            raise ValueError("Invalid manifest")
        artifacts = {}
        expected = {"manifest.json"}
        for entry in manifest["artifacts"]:
            name, digest, size = entry["name"], entry["sha256"], entry["size"]
            _validate_name(name)
            path = "artifacts/" + digest
            if path in expected or name in artifacts or path not in names or size > MAX_ARTIFACT:
                raise ValueError("Invalid artifact mapping")
            expected.add(path)
            with archive.open(path) as artifact:
                contents = artifact.read(MAX_ARTIFACT + 1)
            if len(contents) != size or hashlib.sha256(contents).hexdigest() != digest:
                raise ValueError("Artifact integrity failure")
            artifacts[name] = contents
        if set(names) != expected:
            raise ValueError("Unexpected archive members")
        return manifest, artifacts
