package storage

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"
)

func TestArtifactStoreAndBundleRoundTrip(t *testing.T) {
	store := NewArtifactStore()
	original, err := store.Put("run-1", "trace.json", "application/json", []byte(`{"ok":true}`))
	if err != nil {
		t.Fatal(err)
	}
	manifest := Manifest{
		SchemaVersion: ManifestVersion,
		RecordingID:   "recording-1",
		Artifacts:     []ManifestEntry{{Name: original.Name, MediaType: original.MediaType, Hash: original.Hash, Size: original.Size}},
	}
	var archive bytes.Buffer
	if err := ExportBundle(&archive, manifest, []Artifact{original}); err != nil {
		t.Fatal(err)
	}
	imported, err := ImportBundle(&archive)
	if err != nil {
		t.Fatal(err)
	}
	if got := imported.Artifacts[0].Hash; got != original.Hash {
		t.Fatalf("hash changed after round trip: got %s want %s", got, original.Hash)
	}
}

func TestImportRejectsTraversal(t *testing.T) {
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	entry, err := writer.Create("../artifacts/evil")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = entry.Write([]byte("evil"))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportBundle(&archive); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("expected invalid bundle, got %v", err)
	}
}

func TestImportRejectsTamperedArtifact(t *testing.T) {
	data := []byte("safe")
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	manifest := Manifest{SchemaVersion: ManifestVersion, RecordingID: "recording-1", Artifacts: []ManifestEntry{{Name: "x", Hash: hash, Size: int64(len(data))}}}
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	manifestData := mustJSON(t, manifest)
	manifestEntry, _ := writer.Create("manifest.json")
	_, _ = manifestEntry.Write(manifestData)
	artifactEntry, _ := writer.Create("artifacts/" + hash)
	_, _ = artifactEntry.Write([]byte("tampered"))
	_ = writer.Close()
	if _, err := ImportBundle(&archive); !errors.Is(err, ErrBundleTampered) {
		t.Fatalf("expected tampering rejection, got %v", err)
	}
}

func TestArtifactStoreRejectsOversizedPayload(t *testing.T) {
	_, err := NewArtifactStore().Put("run-1", "large", "application/octet-stream", make([]byte, MaxArtifactSize+1))
	if !errors.Is(err, ErrArtifactTooLarge) {
		t.Fatalf("expected size rejection, got %v", err)
	}
}

func TestImportRejectsUnexpectedEntry(t *testing.T) {
	store := NewArtifactStore()
	artifact, err := store.Put("run-1", "x", "text/plain", []byte("x"))
	if err != nil {
		t.Fatal(err)
	}

	func TestBundleRejectsInvalidManifestMetadata(t *testing.T) {
		artifact := Artifact{Hash: "not-a-digest", Name: "trace.json", MediaType: "application/json", Size: 1, Data: []byte("x")}
		manifest := Manifest{
			SchemaVersion: ManifestVersion,
			RecordingID:   "recording-1",
			Artifacts: []ManifestEntry{{Name: artifact.Name, MediaType: "text/plain", Hash: artifact.Hash, Size: artifact.Size}},
		}
		var archive bytes.Buffer
		if err := ExportBundle(&archive, manifest, []Artifact{artifact}); !errors.Is(err, ErrBundleTampered) {
			t.Fatalf("expected metadata rejection, got %v", err)
		}
	}

	func TestImportRejectsUnknownManifestFields(t *testing.T) {
		manifest := []byte(`{"schema_version":"1","recording_id":"recording-1","artifacts":[],"unexpected":true}`)
		var archive bytes.Buffer
		writer := zip.NewWriter(&archive)
		entry, _ := writer.Create("manifest.json")
		_, _ = entry.Write(manifest)
		_ = writer.Close()
		if _, err := ImportBundle(&archive); !errors.Is(err, ErrInvalidBundle) {
			t.Fatalf("expected unknown manifest field rejection, got %v", err)
		}
	}
	manifest := Manifest{SchemaVersion: ManifestVersion, RecordingID: "recording-1", Artifacts: []ManifestEntry{{Name: artifact.Name, Hash: artifact.Hash, Size: artifact.Size}}}
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	for name, data := range map[string][]byte{"manifest.json": mustJSON(t, manifest), "artifacts/" + artifact.Hash: artifact.Data, "extra": []byte("nope")} {
		entry, _ := writer.Create(name)
		_, _ = entry.Write(data)
	}
	_ = writer.Close()
	if _, err := ImportBundle(&archive); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("expected unexpected-entry rejection, got %v", err)
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
