# Compatibility and CI policy

The foundation supports Go 1.23 and 1.24, Python 3.11 through 3.13, and Node.js
20 and 22. CI runs the complete matrix from a clean checkout without provider
credentials.

The Go API uses `/health/live` as its only foundation endpoint. It returns
`200 application/json` with `{"status":"ok"}`. The process fails explicitly on
startup errors and treats cancellation as a graceful shutdown; cleanup is bounded
to ten seconds before the process exits.

The Python SDK and TypeScript web package currently expose only version/health
helpers. Their lockfiles are authoritative. No foundation build or container may
invoke a model provider, production tool, or replay execution path.
