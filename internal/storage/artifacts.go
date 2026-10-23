// Package storage contains bounded, content-addressed recording storage.
package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
)

const MaxArtifactSize int64 = 8 << 20

var (
	ErrArtifactTooLarge = errors.New("artifact exceeds the 8 MiB limit")
	ErrArtifactNotFound = errors.New("artifact not found")
)

type Artifact struct {
	Hash      string
	Name      string
	MediaType string
	Size      int64
	Data      []byte
}

type ArtifactStore struct {
	mu         sync.RWMutex
	artifacts  map[string]Artifact
	maxRunSize int64
	runBytes   map[string]int64
}

func NewArtifactStore() *ArtifactStore {
	return &ArtifactStore{artifacts: make(map[string]Artifact), maxRunSize: 64 << 20, runBytes: make(map[string]int64)}
}

func (s *ArtifactStore) Put(runID, name, mediaType string, data []byte) (Artifact, error) {
	if int64(len(data)) > MaxArtifactSize {
		return Artifact{}, ErrArtifactTooLarge
	}
	hash := sha256.Sum256(data)
	key := hex.EncodeToString(hash[:])
	s.mu.Lock()
	defer s.mu.Unlock()
	if current := s.runBytes[runID]; current+int64(len(data)) > s.maxRunSize {
		return Artifact{}, fmt.Errorf("run exceeds the 64 MiB limit")
	}
	if existing, ok := s.artifacts[key]; ok {
		return cloneArtifact(existing), nil
	}
	artifact := Artifact{Hash: key, Name: name, MediaType: mediaType, Size: int64(len(data)), Data: append([]byte(nil), data...)}
	s.artifacts[key] = artifact
	s.runBytes[runID] += artifact.Size
	return cloneArtifact(artifact), nil
}

func (s *ArtifactStore) Get(hash string) (Artifact, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	artifact, ok := s.artifacts[hash]
	if !ok {
		return Artifact{}, ErrArtifactNotFound
	}
	return cloneArtifact(artifact), nil
}

func cloneArtifact(a Artifact) Artifact {
	a.Data = append([]byte(nil), a.Data...)
	return a
}
