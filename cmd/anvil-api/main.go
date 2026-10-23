package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/lewis-mcgillion/anvil/internal/capture"
)

func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func newServer(addr string) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/health/live", healthHandler)
	service := capture.NewService()
	mux.HandleFunc("/v1/projects/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/runs") {
			capture.Problem(w, http.StatusNotFound, "not_found", "resource not found")
			return
		}
		project := strings.TrimSuffix(strings.TrimPrefix(strings.TrimPrefix(r.URL.Path, "/v1/projects/"), "/"), "/runs")
		var request capture.CreateRunRequest
		if !decodeJSON(w, r, &request) {
			return
		}
		run, err := service.CreateRun(project, r.Header.Get("Idempotency-Key"), request, capture.ParseScopes(r.Header.Get("Authorization")))
		if err != nil {
			writeCaptureError(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, run)
	})
	mux.HandleFunc("/v1/runs/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/v1/runs/")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(path, "/events:batch"):
			runID := strings.TrimSuffix(path, "/events:batch")
			var request capture.EventBatch
			if !decodeJSON(w, r, &request) {
				return
			}
			result, err := service.Ingest(runID, r.Header.Get("Idempotency-Key"), request, capture.ParseScopes(r.Header.Get("Authorization")))
			if err != nil {
				writeCaptureError(w, err)
				return
			}
			writeJSON(w, http.StatusOK, result)
		case r.Method == http.MethodPost && strings.HasSuffix(path, "/artifacts"):
			runID := strings.TrimSuffix(path, "/artifacts")
			var request capture.ArtifactRequest
			if !decodeJSON(w, r, &request) {
				return
			}
			result, err := service.UploadArtifact(runID, r.Header.Get("Idempotency-Key"), request, capture.ParseScopes(r.Header.Get("Authorization")))
			if err != nil {
				writeCaptureError(w, err)
				return
			}
			writeJSON(w, http.StatusCreated, result)
		case r.Method == http.MethodPost && strings.HasSuffix(path, ":seal"):
			runID := strings.TrimSuffix(path, ":seal")
			job, err := service.Seal(runID, r.Header.Get("Idempotency-Key"), false, capture.ParseScopes(r.Header.Get("Authorization")))
			if err != nil {
				writeCaptureError(w, err)
				return
			}
			writeJSON(w, http.StatusAccepted, job)
		default:
			capture.Problem(w, http.StatusNotFound, "not_found", "resource not found")
		}
	})
	mux.HandleFunc("/v1/jobs/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			capture.Problem(w, http.StatusNotFound, "not_found", "resource not found")
			return
		}
		job, err := service.Job(strings.TrimPrefix(r.URL.Path, "/v1/jobs/"), capture.ParseScopes(r.Header.Get("Authorization")))
		if err != nil {
			writeCaptureError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, job)
	})
	return &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
}

func decodeJSON(w http.ResponseWriter, r *http.Request, value any) bool {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		capture.Problem(w, http.StatusBadRequest, "invalid_request", "request body is invalid")
		return false
	}
	var extra any
	if err := decoder.Decode(&extra); err == nil {
		capture.Problem(w, http.StatusBadRequest, "invalid_request", "request body must contain one JSON value")
		return false
	} else if !errors.Is(err, io.EOF) {
		capture.Problem(w, http.StatusBadRequest, "invalid_request", "request body is invalid")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeCaptureError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, capture.ErrInvalidRequest):
		capture.Problem(w, http.StatusBadRequest, "invalid_request", "request is invalid")
	case errors.Is(err, capture.ErrUnauthorized):
		w.Header().Set("WWW-Authenticate", "Bearer")
		capture.Problem(w, http.StatusUnauthorized, "unauthenticated", "a bearer token is required")
	case errors.Is(err, capture.ErrForbidden):
		capture.Problem(w, http.StatusForbidden, "forbidden", "the required scope is missing")
	case errors.Is(err, capture.ErrNotFound):
		capture.Problem(w, http.StatusNotFound, "not_found", "resource not found")
	case errors.Is(err, capture.ErrTooLarge):
		capture.Problem(w, http.StatusRequestEntityTooLarge, "payload_too_large", "the documented size limit was exceeded")
	case errors.Is(err, capture.ErrThrottled):
		w.Header().Set("Retry-After", "1")
		capture.Problem(w, http.StatusTooManyRequests, "throttled", "quota exceeded; retry later")
	default:
		capture.Problem(w, http.StatusConflict, "conflict", "request conflicts with the current resource state")
	}
}

func main() {
	server := newServer(envOr("ANVIL_ADDR", "127.0.0.1:8080"))
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stop)

	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("server failed: %v", err)
			stop <- syscall.SIGTERM
		}
	}()

	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.Printf("server shutdown failed: %v", err)
	}
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
