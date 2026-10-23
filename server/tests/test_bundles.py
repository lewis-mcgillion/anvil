import io
import zipfile

import pytest

from anvil_server.bundles import MAX_ARTIFACT, export_bundle, import_bundle


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
