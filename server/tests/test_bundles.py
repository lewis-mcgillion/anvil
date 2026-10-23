import io
import json
import zipfile

import pytest

from anvil_server.bundles import MAX_ARTIFACT, MAX_BUNDLE, export_bundle, import_bundle, validate_recording_archive


def test_bundle_round_trip():
    manifest, data = import_bundle(export_bundle("recording", {"trace.json": b"safe"}))
    assert data == {"trace.json": b"safe"}
    assert manifest["recording_id"] == "recording"


def test_unsafe_member_and_oversized_data_rejected():
    with pytest.raises(ValueError):
        export_bundle("recording", {"../secret": b"unsafe"})
    with pytest.raises(ValueError):
        export_bundle("recording", {"big": b"x" * (MAX_ARTIFACT + 1)})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../unexpected", "unsafe")
    with pytest.raises(ValueError):
        import_bundle(output.getvalue())


def test_recording_round_trip_and_tamper_detection():
    recording = {"schema_version": "1", "run_id": "sample", "events": [{"type": "run.completed"}]}
    archive = export_bundle("recording", {}, recording)
    manifest, artifacts = import_bundle(archive)
    assert artifacts == {}
    assert manifest["recording"] == recording
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as original, zipfile.ZipFile(output, "w") as modified:
        for member in original.namelist():
            contents = original.read(member)
            modified.writestr(member, b'{}' if member == "recording.json" else contents)
    with pytest.raises(ValueError, match="integrity"):
        import_bundle(output.getvalue())


def test_legacy_bundle_cannot_be_imported_as_recording():
    with pytest.raises(ValueError, match="Recording content missing"):
        validate_recording_archive(export_bundle("old", {"asset": b"data"}))


def test_unrecognized_manifest_fields_are_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": "1", "recording_id": "test",
                                                      "artifacts": [], "untrusted": "extra"}))
    with pytest.raises(ValueError, match="Invalid manifest"):
        import_bundle(output.getvalue())


@pytest.mark.parametrize("invalid", [b"not a zip", b"PK\\x03\\x04"])
def test_malformed_archive_returns_validation_error(invalid):
    with pytest.raises(ValueError):
        import_bundle(invalid)


@pytest.mark.parametrize("size", [-1, True, "1"])
def test_invalid_artifact_size_is_rejected(size):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": "1", "recording_id": "test",
            "artifacts": [{"name": "asset", "sha256": "a" * 64, "size": size,
                           "media_type": "application/octet-stream"}]}))
        archive.writestr("artifacts/" + "a" * 64, b"x")
    with pytest.raises(ValueError):
        import_bundle(output.getvalue())


def test_duplicate_archive_member_is_rejected():
    output = io.BytesIO()
    manifest = json.dumps({"schema_version": "1", "recording_id": "test", "artifacts": []})
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("manifest.json", manifest)
    with pytest.raises(ValueError, match="Invalid archive members"):
        import_bundle(output.getvalue())


def test_compressed_bomb_is_rejected_before_decompression():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": "1",
                                                      "recording_id": "test", "artifacts": []}))
        archive.writestr("recording.json", b"a" * (MAX_BUNDLE + 1))
    with pytest.raises(ValueError, match="Decompression limit exceeded"):
        import_bundle(output.getvalue())
