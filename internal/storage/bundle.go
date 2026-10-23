package storage

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"strings"
)

const (
	ManifestVersion       = "1"
	MaxBundleBytes  int64 = 64 << 20
	MaxBundleFiles        = 1024
)

var (
	ErrInvalidBundle  = errors.New("invalid recording bundle")
	ErrBundleTooLarge = errors.New("recording bundle exceeds its limit")
	ErrBundleTampered = errors.New("recording bundle integrity check failed")
)

type Manifest struct {
	SchemaVersion string          `json:"schema_version"`
	RecordingID   string          `json:"recording_id"`
	Artifacts     []ManifestEntry `json:"artifacts"`
}

type ManifestEntry struct {
	Name      string `json:"name"`
	MediaType string `json:"media_type,omitempty"`
	Hash      string `json:"sha256"`
	Size      int64  `json:"size"`
}

type ImportedBundle struct {
	Manifest  Manifest
	Artifacts []Artifact
}

func ExportBundle(w io.Writer, manifest Manifest, artifacts []Artifact) error {
	if manifest.SchemaVersion == "" {
		manifest.SchemaVersion = ManifestVersion
	}
	if manifest.SchemaVersion != ManifestVersion || len(artifacts) != len(manifest.Artifacts) {
		return fmt.Errorf("%w: unsupported manifest or artifact count", ErrInvalidBundle)
	}
	entries := make(map[string]Artifact, len(artifacts))
	for _, artifact := range artifacts {
		if err := validateArtifact(artifact); err != nil {
			return err
		}
		if _, exists := entries[artifact.Hash]; exists {
			return fmt.Errorf("%w: duplicate artifact hash", ErrInvalidBundle)
		}
		entries[artifact.Hash] = artifact
	}
	for _, entry := range manifest.Artifacts {
		artifact, ok := entries[entry.Hash]
		if !ok || entry.Size != int64(len(artifact.Data)) || entry.Name != artifact.Name {
			return fmt.Errorf("%w: manifest does not match artifact", ErrBundleTampered)
		}
	}
	seenManifest := make(map[string]bool, len(manifest.Artifacts))
	for _, entry := range manifest.Artifacts {
		if seenManifest[entry.Hash] {
			return fmt.Errorf("%w: duplicate manifest artifact", ErrInvalidBundle)
		}
		seenManifest[entry.Hash] = true
	}
	var buffer bytes.Buffer
	zw := zip.NewWriter(&buffer)
	manifestBytes, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	if err := writeZipEntry(zw, "manifest.json", manifestBytes); err != nil {
		return err
	}
	for _, entry := range manifest.Artifacts {
		if err := writeZipEntry(zw, "artifacts/"+entry.Hash, entries[entry.Hash].Data); err != nil {
			return err
		}
	}
	if err := zw.Close(); err != nil {
		return err
	}
	if int64(buffer.Len()) > MaxBundleBytes {
		return ErrBundleTooLarge
	}
	_, err = w.Write(buffer.Bytes())
	return err
}

func ImportBundle(r io.Reader) (ImportedBundle, error) {
	raw, err := io.ReadAll(io.LimitReader(r, MaxBundleBytes+1))
	if err != nil {
		return ImportedBundle{}, err
	}
	if int64(len(raw)) > MaxBundleBytes {
		return ImportedBundle{}, ErrBundleTooLarge
	}
	reader, err := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if err != nil {
		return ImportedBundle{}, fmt.Errorf("%w: %v", ErrInvalidBundle, err)
	}
	if len(reader.File) == 0 || len(reader.File) > MaxBundleFiles {
		return ImportedBundle{}, fmt.Errorf("%w: invalid file count", ErrInvalidBundle)
	}
	files := make(map[string]*zip.File, len(reader.File))
	var extracted int64
	for _, file := range reader.File {
		if !safeEntryName(file.Name) || file.FileInfo().Mode()&0o170000 != 0 {
			return ImportedBundle{}, fmt.Errorf("%w: unsafe archive path", ErrInvalidBundle)
		}
		if _, exists := files[file.Name]; exists {
			return ImportedBundle{}, fmt.Errorf("%w: duplicate archive path", ErrInvalidBundle)
		}
		if file.UncompressedSize64 > uint64(MaxBundleBytes) || file.UncompressedSize64 > uint64(MaxArtifactSize) && strings.HasPrefix(file.Name, "artifacts/") {
			return ImportedBundle{}, ErrBundleTooLarge
		}
		extracted += int64(file.UncompressedSize64)
		if extracted > MaxBundleBytes {
			return ImportedBundle{}, ErrBundleTooLarge
		}
		files[file.Name] = file
	}
	manifestFile, ok := files["manifest.json"]
	if !ok {
		return ImportedBundle{}, fmt.Errorf("%w: missing manifest", ErrInvalidBundle)
	}
	manifestBytes, err := readBounded(manifestFile, MaxArtifactSize)
	if err != nil {
		return ImportedBundle{}, err
	}
	var manifest Manifest
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil || manifest.SchemaVersion != ManifestVersion || manifest.RecordingID == "" {
		return ImportedBundle{}, fmt.Errorf("%w: unsupported manifest", ErrInvalidBundle)
	}
	if len(manifest.Artifacts) > MaxBundleFiles-1 {
		return ImportedBundle{}, fmt.Errorf("%w: too many artifacts", ErrInvalidBundle)
	}
	imported := ImportedBundle{Manifest: manifest, Artifacts: make([]Artifact, 0, len(manifest.Artifacts))}
	seen := make(map[string]bool, len(manifest.Artifacts))
	actualExtracted := int64(len(manifestBytes))
	for _, entry := range manifest.Artifacts {
		if seen[entry.Hash] || !validHash(entry.Hash) || entry.Size < 0 || entry.Size > MaxArtifactSize {
			return ImportedBundle{}, fmt.Errorf("%w: invalid artifact manifest", ErrInvalidBundle)
		}
		seen[entry.Hash] = true
		file, ok := files["artifacts/"+entry.Hash]
		if !ok {
			return ImportedBundle{}, fmt.Errorf("%w: missing artifact", ErrInvalidBundle)
		}
		data, err := readBounded(file, MaxArtifactSize)
		if errors.Is(err, ErrBundleTooLarge) {
			return ImportedBundle{}, err
		}
		if err != nil || int64(len(data)) != entry.Size {
			return ImportedBundle{}, fmt.Errorf("%w: artifact size mismatch", ErrBundleTampered)
		}
		actualExtracted += int64(len(data))
		if actualExtracted > MaxBundleBytes {
			return ImportedBundle{}, ErrBundleTooLarge
		}
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) != entry.Hash {
			return ImportedBundle{}, ErrBundleTampered
		}
		imported.Artifacts = append(imported.Artifacts, Artifact{Hash: entry.Hash, Name: entry.Name, MediaType: entry.MediaType, Size: int64(len(data)), Data: data})
	}
	if len(files) != len(manifest.Artifacts)+1 {
		return ImportedBundle{}, fmt.Errorf("%w: unexpected archive entry", ErrInvalidBundle)
	}
	return imported, nil
}

func validateArtifact(a Artifact) error {
	if int64(len(a.Data)) > MaxArtifactSize {
		return ErrArtifactTooLarge
	}
	sum := sha256.Sum256(a.Data)
	if a.Hash != hex.EncodeToString(sum[:]) || a.Size != int64(len(a.Data)) {
		return ErrBundleTampered
	}
	return nil
}

func validHash(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func safeEntryName(name string) bool {
	return name != "" && !path.IsAbs(name) && !strings.HasPrefix(name, "/") && path.Clean(name) == name && !strings.Contains(name, "\\") && name != "." && !strings.HasPrefix(name, "../") && !strings.Contains(name, "/../")
}

func readBounded(file *zip.File, limit int64) ([]byte, error) {
	if file.UncompressedSize64 > uint64(limit) {
		return nil, ErrBundleTooLarge
	}
	reader, err := file.Open()
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidBundle, err)
	}
	defer reader.Close()
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, ErrBundleTooLarge
	}
	return data, nil
}

func writeZipEntry(zw *zip.Writer, name string, data []byte) error {
	writer, err := zw.Create(name)
	if err != nil {
		return err
	}
	_, err = writer.Write(data)
	return err
}
