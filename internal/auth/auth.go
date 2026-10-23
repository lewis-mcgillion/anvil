// Package auth contains the protocol-independent authentication and
// authorization rules used at the API boundary.
package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"unicode"
)

type Scope string

const (
	CaptureWrite Scope = "capture:write"
	RunsRead     Scope = "runs:read"
	ReplayWrite  Scope = "replay:write"
	SuitesWrite  Scope = "suites:write"
	SuitesRead   Scope = "suites:read"
	Admin        Scope = "admin"
	MetricsRead  Scope = "metrics:read"
)

var validScopes = map[Scope]struct{}{
	CaptureWrite: {}, RunsRead: {}, ReplayWrite: {}, SuitesWrite: {},
	SuitesRead: {}, Admin: {}, MetricsRead: {},
}

var (
	ErrInvalidBearer = errors.New("auth: invalid bearer token")
	ErrInvalidScope  = errors.New("auth: invalid scope")
	ErrScopeConflict = errors.New("auth: capture scope cannot include read or replay")
)

// ParseBearerToken parses an RFC 6750 Authorization header without accepting
// credentials in any other authentication scheme.
func ParseBearerToken(header string) (string, error) {
	fields := strings.Fields(header)
	if len(fields) != 2 || !strings.EqualFold(fields[0], "Bearer") ||
		fields[1] == "" || strings.IndexFunc(fields[1], unicode.IsSpace) >= 0 {
		return "", ErrInvalidBearer
	}
	return fields[1], nil
}

// HashKey returns the value persisted for a bearer secret. Callers must never
// persist or log the unhashed value.
func HashKey(secret string) [32]byte {
	return sha256.Sum256([]byte(secret))
}

// GenerateKey returns a cryptographically random, URL-safe bearer secret.
func GenerateKey() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("generate api key: %w", err)
	}
	return "anv_" + base64.RawURLEncoding.EncodeToString(raw), nil
}

func ValidateScopes(scopes []Scope) error {
	if len(scopes) == 0 {
		return ErrInvalidScope
	}
	seen := make(map[Scope]struct{}, len(scopes))
	for _, scope := range scopes {
		if _, ok := validScopes[scope]; !ok {
			return fmt.Errorf("%w: %q", ErrInvalidScope, scope)
		}
		if _, ok := seen[scope]; ok {
			return fmt.Errorf("%w: duplicate %q", ErrInvalidScope, scope)
		}
		seen[scope] = struct{}{}
	}
	if _, capture := seen[CaptureWrite]; capture {
		if _, read := seen[RunsRead]; read {
			return ErrScopeConflict
		}
		if _, replay := seen[ReplayWrite]; replay {
			return ErrScopeConflict
		}
	}
	return nil
}

// Allows reports whether a key with scopes may perform an operation requiring
// required. Scope matching is exact; a capture key never inherits read or
// replay access.
func Allows(scopes []Scope, required Scope) bool {
	if ValidateScopes(scopes) != nil {
		return false
	}
	_, valid := validScopes[required]
	if !valid {
		return false
	}
	for _, scope := range scopes {
		if scope == required {
			return true
		}
	}
	return false
}
