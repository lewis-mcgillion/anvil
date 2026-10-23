package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"
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

func TestEnvOr(t *testing.T) {
	t.Setenv("ANVIL_TEST_VALUE", "configured")
	if got := envOr("ANVIL_TEST_VALUE", "fallback"); got != "configured" {
		t.Fatalf("envOr() = %q, want configured", got)
	}
	if got := envOr("ANVIL_MISSING_VALUE", "fallback"); got != "fallback" {
		t.Fatalf("envOr() = %q, want fallback", got)
	}
}

func TestCaptureEndpointsRequireAuthorizationAndReturnJobs(t *testing.T) {
	server := newServer("127.0.0.1:0")
	body := bytes.NewBufferString(`{"agent_name":"agent","adapter":"adk","adapter_version":"1"}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/projects/project-1/runs", body)
	response := httptest.NewRecorder()
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized status = %d, want %d", response.Code, http.StatusUnauthorized)
	}

	body = bytes.NewBufferString(`{"agent_name":"agent","adapter":"adk","adapter_version":"1"}`)
	request = httptest.NewRequest(http.MethodPost, "/v1/projects/project-1/runs", body)
	request.Header.Set("Authorization", "Bearer "+"capture"+":write")
	request.Header.Set("Idempotency-Key", "create-1")
	response = httptest.NewRecorder()
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusCreated {
		t.Fatalf("create status = %d, body = %s", response.Code, response.Body.String())
	}
}

func TestCaptureEndpointRejectsUnknownFields(t *testing.T) {
	server := newServer("127.0.0.1:0")
	request := httptest.NewRequest(http.MethodPost, "/v1/projects/project-1/runs",
		bytes.NewBufferString(`{"agent_name":"agent","adapter":"adk","adapter_version":"1","extra":true}`))
	request.Header.Set("Authorization", "Bearer "+"capture"+":write")
	request.Header.Set("Idempotency-Key", "create-1")
	response := httptest.NewRecorder()
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid request status = %d, want %d", response.Code, http.StatusBadRequest)
	}
}
