// Package pagination contains the versioned cursor format shared by list APIs.
package pagination

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

const (
	CurrentVersion = 1
	MaxCursorBytes = 512
)

var (
	ErrInvalidCursor = errors.New("invalid pagination cursor")
	ErrKeyRequired   = errors.New("pagination cursor key is required")
)

// Cursor is a keyset position. Scope and Filters bind a cursor to the query
// that produced it, preventing accidental cross-project or cross-filter reuse.
type Cursor struct {
	Version  int               `json:"v"`
	Scope    string            `json:"s"`
	Resource string            `json:"r"`
	Key      string            `json:"k"`
	Filters  map[string]string `json:"f,omitempty"`
}

type envelope struct {
	Cursor
	Signature string `json:"h"`
}

// Codec signs opaque cursors so callers cannot alter their position or query.
type Codec struct{ key []byte }

func NewCodec(key []byte) (*Codec, error) {
	if len(key) < 16 {
		return nil, ErrKeyRequired
	}
	return &Codec{key: append([]byte(nil), key...)}, nil
}

func (c *Codec) Encode(cursor Cursor) (string, error) {
	if c == nil || len(c.key) < 16 {
		return "", ErrKeyRequired
	}
	if err := validate(cursor); err != nil {
		return "", err
	}
	payload, err := json.Marshal(cursor)
	if err != nil {
		return "", fmt.Errorf("%w: encode: %v", ErrInvalidCursor, err)
	}
	signature := c.sign(payload)
	raw, err := json.Marshal(envelope{Cursor: cursor, Signature: signature})
	if err != nil {
		return "", fmt.Errorf("%w: encode: %v", ErrInvalidCursor, err)
	}
	result := base64.RawURLEncoding.EncodeToString(raw)
	if len(result) > MaxCursorBytes {
		return "", fmt.Errorf("%w: cursor is too large", ErrInvalidCursor)
	}
	return result, nil
}

func (c *Codec) Decode(value string) (Cursor, error) {
	if c == nil || len(c.key) < 16 {
		return Cursor{}, ErrKeyRequired
	}
	if value == "" || len(value) > MaxCursorBytes {
		return Cursor{}, ErrInvalidCursor
	}
	raw, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil {
		return Cursor{}, fmt.Errorf("%w: malformed encoding", ErrInvalidCursor)
	}
	var item envelope
	if err := json.Unmarshal(raw, &item); err != nil {
		return Cursor{}, fmt.Errorf("%w: malformed payload", ErrInvalidCursor)
	}
	if err := validate(item.Cursor); err != nil {
		return Cursor{}, err
	}
	payload, err := json.Marshal(item.Cursor)
	if err != nil || !hmac.Equal([]byte(item.Signature), []byte(c.sign(payload))) {
		return Cursor{}, fmt.Errorf("%w: signature mismatch", ErrInvalidCursor)
	}
	return item.Cursor, nil
}

func (c *Codec) sign(payload []byte) string {
	mac := hmac.New(sha256.New, c.key)
	_, _ = mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

func validate(cursor Cursor) error {
	if cursor.Version != CurrentVersion || strings.TrimSpace(cursor.Scope) == "" ||
		strings.TrimSpace(cursor.Resource) == "" || strings.TrimSpace(cursor.Key) == "" {
		return fmt.Errorf("%w: unsupported or incomplete cursor", ErrInvalidCursor)
	}
	for name, value := range cursor.Filters {
		if strings.TrimSpace(name) == "" || len(name) > 64 || len(value) > 256 {
			return fmt.Errorf("%w: invalid filter", ErrInvalidCursor)
		}
	}
	return nil
}
