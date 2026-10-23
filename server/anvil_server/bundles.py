"""Bounded portable recording bundles, without filesystem extraction."""

import hashlib
import io
import json
import re
import zipfile
from uuid import UUID

from pydantic import ValidationError

from .events import Event
from .sealing import seal

MAX_ARTIFACT = 8 * 1024 * 1024
MAX_BUNDLE = 64 * 1024 * 1024
MAX_FILES = 1024


def _validate_name(name: str):
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Unsafe artifact name")


def export_bundle(recording_id: str, artifacts: dict[str, bytes],
                  recording: dict | None = None) -> bytes:
    if not recording_id or len(artifacts) + (2 if recording is not None else 1) > MAX_FILES:
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
    recording_bytes = None
    if recording is not None:
        recording_bytes = json.dumps(recording, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()
        if len(recording_bytes) > MAX_BUNDLE - MAX_ARTIFACT:
            raise ValueError("Recording too large")
        manifest["recording_sha256"] = hashlib.sha256(recording_bytes).hexdigest()
        manifest["recording_size"] = len(recording_bytes)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        if recording_bytes is not None:
            archive.writestr("recording.json", recording_bytes)
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
        if any(info.is_dir() or (info.file_size > MAX_ARTIFACT and info.filename != "recording.json")
               or info.external_attr >> 16 & 0o170000 == 0o120000
               or info.filename not in {"manifest.json", "recording.json"}
               and not re.fullmatch(r"artifacts/[0-9a-f]{64}", info.filename) for info in files):
            raise ValueError("Unsafe archive member")
        if sum(info.file_size for info in files) > MAX_BUNDLE:
            raise ValueError("Decompression limit exceeded")
        manifest_info = archive.getinfo("manifest.json")
        if manifest_info.file_size > MAX_ARTIFACT:
            raise ValueError("Manifest too large")
        manifest = json.loads(archive.read("manifest.json"))
        if (not isinstance(manifest, dict) or
                set(manifest) - {"schema_version", "recording_id", "artifacts", "recording_sha256", "recording_size"} or
                manifest.get("schema_version") != "1" or not manifest.get("recording_id") or
                not isinstance(manifest.get("artifacts"), list)):
            raise ValueError("Invalid manifest")
        artifacts = {}
        expected = {"manifest.json"}
        if "recording_sha256" in manifest:
            if "recording.json" not in names or not isinstance(manifest.get("recording_size"), int):
                raise ValueError("Missing recording data")
            with archive.open("recording.json") as member:
                contents = member.read(MAX_BUNDLE + 1)
            if (len(contents) != manifest["recording_size"] or
                    hashlib.sha256(contents).hexdigest() != manifest["recording_sha256"]):
                raise ValueError("Recording integrity failure")
            try:
                recording = json.loads(contents)
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("Invalid recording data") from error
            if not isinstance(recording, dict) or recording.get("schema_version") != "1":
                raise ValueError("Invalid recording schema")
            manifest["recording"] = recording
            expected.add("recording.json")
        elif "recording.json" in names:
            raise ValueError("Unmanifested recording")
        for entry in manifest["artifacts"]:
            if not isinstance(entry, dict) or set(entry) - {"name", "sha256", "size", "media_type"}:
                raise ValueError("Invalid artifact entry")
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


def validate_recording_archive(data: bytes) -> tuple[dict, list[Event], dict[str, bytes]]:
    manifest, artifacts = import_bundle(data)
    recording = manifest.get("recording")
    if not isinstance(recording, dict) or not isinstance(recording.get("manifest"), dict):
        raise ValueError("Recording content missing")
    try:
        recording_id = UUID(manifest["recording_id"])
        run_id = UUID(recording["run_id"])
        project_id = UUID(recording["manifest"]["project_id"])
        if not isinstance(recording["events"], list):
            raise ValueError("Events must be a list")
        events = [Event.model_validate(item) for item in recording["events"]]
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ValueError("Invalid recording contents") from error
    if (recording_id.version != 7 or run_id.version != 7 or project_id.version != 7 or
            any(event.run_id != run_id for event in events)):
        raise ValueError("Invalid recording identities")
    if recording["manifest"].get("run_id") != str(run_id):
        raise ValueError("Recording run mismatch")
    if recording["manifest"].get("project_id") != str(project_id):
        raise ValueError("Recording project mismatch")
    digest = hashlib.sha256(json.dumps(recording["manifest"], sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    if digest != recording.get("manifest_sha256"):
        raise ValueError("Recording manifest integrity failure")
    if recording["manifest"].get("event_count") != len(events):
        raise ValueError("Event count mismatch")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("Duplicate event identities")
    positions = [(event.stream_id, event.sequence) for event in events]
    if len(set(positions)) != len(positions):
        raise ValueError("Duplicate event positions")
    encoded_events = json.dumps(recording["events"], sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).encode()
    if hashlib.sha256(encoded_events).hexdigest() != recording.get("events_sha256"):
        raise ValueError("Recording event integrity failure")
    streams = sorted({str(event.stream_id) for event in events})
    stream_details = [{"stream_id": stream_id,
                       "terminal": any(str(event.stream_id) == stream_id and
                                       event.type in {"run.completed", "run.failed"} for event in events)}
                      for stream_id in streams]
    summary = [{"event_id": str(event.event_id), "stream_id": str(event.stream_id),
                "sequence": event.sequence, "type": event.type,
                "parent_event_ids": [str(parent) for parent in event.parent_event_ids],
                "invocation_id": str(event.invocation_id) if event.invocation_id else None,
                "replay_safe": event.replay_safe} for event in events]
    checked = seal(str(run_id), str(project_id), summary, stream_details, [], True)
    if any(recording["manifest"].get(field) != checked[field] for field in (
            "capture_completeness", "execution_outcome", "replay_eligibility", "payload_sha256",
            "stream_count", "event_count")):
        raise ValueError("Recording evidence does not match manifest")
    if (recording["manifest"].get("capture_completeness") not in
            {"sealed_complete", "sealed_incomplete"}):
        raise ValueError("Invalid recording state")
    listed_artifacts = recording["manifest"].get("artifacts", [])
    if (not isinstance(listed_artifacts, list) or len(listed_artifacts) != len(artifacts) or
            {entry.get("name"): entry.get("sha256") for entry in listed_artifacts if isinstance(entry, dict)} !=
            {name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()}):
        raise ValueError("Recording artifact evidence mismatch")
    return recording, events, artifacts
