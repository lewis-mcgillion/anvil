package storage

import "errors"

var (
	ErrInvalidProjectScope = errors.New("storage: invalid project scope")
	ErrNotFound            = errors.New("storage: not found")
)
