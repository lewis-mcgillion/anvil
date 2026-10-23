package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHealthHandler(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/health/live", nil)
	response := httptest.NewRecorder()

	healthHandler(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
	}
	if got := response.Body.String(); got != `{"status":"ok"}` {
		t.Fatalf("body = %q, want health response", got)
	}
}

func TestServerRejectsUnknownJSONFieldsWithProblem(t *testing.T) {
	server := newServer("")
	request := httptest.NewRequest(http.MethodPost, "/v1/projects", strings.NewReader(`{"name":"demo","unexpected":true}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "test-key-1")
	response := httptest.NewRecorder()

	server.Handler.ServeHTTP(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
	}
	if got := response.Header().Get("Content-Type"); got != "application/problem+json" {
		t.Fatalf("content type = %q, want problem+json", got)
	}
	if response.Header().Get("X-Request-ID") == "" {
		t.Fatal("response did not include a request ID")
	}
	if !strings.Contains(response.Body.String(), `"code":"invalid_request"`) {
		t.Fatalf("body = %q, want invalid_request problem", response.Body.String())
	}
}

func TestServerRejectsOversizedBody(t *testing.T) {
	server := newServer("")
	request := httptest.NewRequest(http.MethodPost, "/v1/projects", strings.NewReader(`{"name":"`+strings.Repeat("x", maxRequestBody)+`"}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()

	server.Handler.ServeHTTP(response, request)

	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusRequestEntityTooLarge)
	}
	if !strings.Contains(response.Body.String(), `"code":"payload_too_large"`) {
		t.Fatalf("body = %q, want payload_too_large problem", response.Body.String())
	}
}

func TestServerShutdownClosesListener(t *testing.T) {
	server := newServer("127.0.0.1:0")
	listener := mustListen(t, server.Addr)
	go func() { _ = server.Serve(listener) }()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown() error = %v", err)
	}
	if _, err := listener.Accept(); err == nil {
		t.Fatal("listener accepted a connection after shutdown")
	}
}

func mustListen(t *testing.T, addr string) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatalf("Listen() error = %v", err)
	}
	return listener
}

func TestEnvOr(t *testing.T) {
	t.Setenv("ANVIL_TEST_VALUE", "configured")
	if got := envOr("ANVIL_TEST_VALUE", "fallback"); got != "configured" {
		t.Fatalf("envOr() = %q, want configured", got)
	}
	if got := envOr("ANVIL_MISSING_VALUE", "fallback"); got != "fallback" {
		t.Fatalf("envOr() = %q, want fallback", got)
	}
}
