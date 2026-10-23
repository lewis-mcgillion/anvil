package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

const maxRequestBody = 4 << 20

type requestIDKey struct{}

type problem struct {
	Type      string `json:"type"`
	Title     string `json:"title"`
	Status    int    `json:"status"`
	Detail    string `json:"detail,omitempty"`
	Code      string `json:"code"`
	RequestID string `json:"request_id"`
}

type projectCreate struct {
	Name          string `json:"name"`
	RetentionDays *int   `json:"retention_days,omitempty"`
}

func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func newServer(addr string) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/health/live", healthHandler)
	mux.HandleFunc("/health/ready", healthHandler)
	mux.HandleFunc("/v1/health/live", healthHandler)
	mux.HandleFunc("/v1/health/ready", healthHandler)
	mux.Handle("/v1/", http.HandlerFunc(apiHandler))
	mux.Handle("/", http.HandlerFunc(notFoundHandler))
	return &http.Server{
		Addr:              addr,
		Handler:           requestIDMiddleware(bodyLimitMiddleware(mux)),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}

func apiHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost && r.URL.Path == "/v1/projects" {
		var request projectCreate
		if err := decodeJSON(r, &request); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				writeProblem(w, r, http.StatusRequestEntityTooLarge, "payload_too_large", "Payload too large", "Request body exceeds the 4 MiB limit.")
				return
			}
			writeProblem(w, r, http.StatusBadRequest, "invalid_request", "Invalid request", err.Error())
			return
		}
		if request.Name == "" || len(request.Name) > 128 {
			writeProblem(w, r, http.StatusBadRequest, "invalid_request", "Invalid request", "name must contain between 1 and 128 characters.")
			return
		}
		if request.RetentionDays != nil && (*request.RetentionDays < 1 || *request.RetentionDays > 365) {
			writeProblem(w, r, http.StatusBadRequest, "invalid_request", "Invalid request", "retention_days must be between 1 and 365.")
			return
		}
		if !validIdempotencyKey(r.Header.Get("Idempotency-Key")) {
			writeProblem(w, r, http.StatusBadRequest, "invalid_request", "Invalid request", "Idempotency-Key must contain 8-128 alphanumeric, underscore, or hyphen characters.")
			return
		}
	}
	writeProblem(w, r, http.StatusNotImplemented, "internal", "Not implemented", "This API operation is not implemented.")
}

func notFoundHandler(w http.ResponseWriter, r *http.Request) {
	writeProblem(w, r, http.StatusNotFound, "not_found", "Not found", "The requested resource was not found.")
}

func bodyLimitMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ContentLength > maxRequestBody {
			writeProblem(w, r, http.StatusRequestEntityTooLarge, "payload_too_large", "Payload too large", "Request body exceeds the 4 MiB limit.")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)
		next.ServeHTTP(w, r)
	})
}

func requestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := r.Header.Get("X-Request-ID")
		if requestID == "" {
			requestID = newRequestID()
		}
		w.Header().Set("X-Request-ID", requestID)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey{}, requestID)))
	})
}

func newRequestID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "unknown"
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(value[0:4]), hex.EncodeToString(value[4:6]),
		hex.EncodeToString(value[6:8]), hex.EncodeToString(value[8:10]),
		hex.EncodeToString(value[10:16]))
}

func validIdempotencyKey(key string) bool {
	if len(key) < 8 || len(key) > 128 {
		return false
	}
	for _, character := range key {
		if (character < 'A' || character > 'Z') &&
			(character < 'a' || character > 'z') &&
			(character < '0' || character > '9') &&
			character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func writeProblem(w http.ResponseWriter, r *http.Request, status int, code, title, detail string) {
	requestID, _ := r.Context().Value(requestIDKey{}).(string)
	if requestID == "" {
		requestID = newRequestID()
		w.Header().Set("X-Request-ID", requestID)
	}
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(problem{
		Type:      "https://anvil.dev/problems/" + strings.ReplaceAll(code, "_", "-"),
		Title:     title,
		Status:    status,
		Detail:    detail,
		Code:      code,
		RequestID: requestID,
	})
}

func decodeJSON(r *http.Request, destination any) error {
	if !strings.HasPrefix(strings.ToLower(r.Header.Get("Content-Type")), "application/json") {
		return errors.New("Content-Type must be application/json")
	}
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return errors.New("request body must contain exactly one JSON value")
	}
	return nil
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
