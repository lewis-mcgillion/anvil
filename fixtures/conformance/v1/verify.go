package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type manifest struct {
	Fixtures []struct {
		ID   string `json:"id"`
		File string `json:"file"`
		Hash string `json:"sha256"`
	} `json:"fixtures"`
}

func main() {
	root := filepath.Join("fixtures", "conformance", "v1")
	manifestBytes, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		panic(err)
	}
	var index manifest
	if err := json.Unmarshal(manifestBytes, &index); err != nil {
		panic(err)
	}
	for _, fixture := range index.Fixtures {
		raw, err := os.ReadFile(filepath.Join(root, fixture.File))
		if err != nil {
			panic(err)
		}
		var value any
		if err := json.Unmarshal(raw, &value); err != nil {
			panic(err)
		}
		canonical, err := json.Marshal(value)
		if err != nil {
			panic(err)
		}
		digest := sha256.Sum256(canonical)
		if hex.EncodeToString(digest[:]) != fixture.Hash {
			panic(fmt.Sprintf("%s: hash mismatch", fixture.ID))
		}
	}
	fmt.Printf("verified %d fixtures\n", len(index.Fixtures))
}
