"""Fail-closed Python API with project-scoped PostgreSQL capture routes."""

from __future__ import annotations

import uuid
import os
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .database import engine_for, project_connection
from .repositories import RunRepository
from .security import AccessDenied, Unauthenticated, authorize
from .routes import router


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    request_id: str


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    retention_days: int | None = Field(default=None, ge=1, le=365)


@asynccontextmanager
async def lifespan(application: FastAPI):
    database_url = os.getenv("ANVIL_DATABASE_URL")
    application.state.engine = engine_for(database_url) if database_url else None
    cursor_secret = os.getenv("ANVIL_CURSOR_SECRET")
    application.state.cursor_secret = cursor_secret.encode() if cursor_secret and len(cursor_secret) >= 16 else None
    try:
        yield
    finally:
        if application.state.engine is not None:
            await application.state.engine.dispose()


app = FastAPI(title="Anvil API", version="1.0.0", lifespan=lifespan)
app.include_router(router)


class BodyLimitMiddleware:
    def __init__(self, application: ASGIApp):
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        path = scope.get("path", "")
        max_body = 12 * 1024 * 1024 if path.endswith("/artifacts") else 4 * 1024 * 1024
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length", b"")
        if content_length.isdigit() and int(content_length) > max_body:
            await self._reject(send, scope)
            return
        total = 0
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > max_body:
                    await self._reject(send, scope)
                    return
            chunks.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break

        async def replay_receive():
            if chunks:
                return chunks.pop(0)
            return await receive()

        await self.application(scope, replay_receive, send)

    @staticmethod
    async def _reject(send: Send, scope: Scope):
        request_id = scope.setdefault("state", {}).get("request_id") or str(uuid.uuid4())
        body = json.dumps({"type": "https://anvil.dev/problems/payload-too-large",
                           "title": "Payload too large", "status": 413,
                           "detail": "Request body exceeds configured limit",
                           "code": "payload_too_large", "request_id": request_id}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/problem+json"),
                                (b"x-request-id", request_id.encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    return problem(request, 400, "invalid_request", "Request failed schema validation")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def problem(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    content = Problem(type=f"https://anvil.dev/problems/{code.replace('_', '-')}",
                      title=code.replace("_", " ").capitalize(), status=status,
                      detail=detail, code=code, request_id=request.state.request_id)
    return JSONResponse(content=content.model_dump(), status_code=status,
                        media_type="application/problem+json")


@app.get("/health/live")
@app.get("/v1/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
@app.get("/v1/health/ready")
async def ready(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return problem(request, 503, "not_ready", "Persistence and authorization are not configured")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return problem(request, 503, "not_ready", "Database unavailable")
    return {"status": "ok"}


@app.post("/v1/projects")
async def create_project(request: Request, payload: ProjectCreate):
    return problem(request, 503, "not_ready", "Project creation requires configured persistence and authentication")


@app.get("/v1/projects/{project_id}/runs/{run_id}")
async def get_project_run(request: Request, project_id: uuid.UUID, run_id: uuid.UUID):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return problem(request, 503, "not_ready", "Database unavailable")
    try:
        await authorize(engine, request, project_id, "runs:read")
    except Unauthenticated:
        response = problem(request, 401, "unauthenticated", "Invalid credentials")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    except AccessDenied:
        return problem(request, 403, "forbidden", "Access denied")
    run = await RunRepository(engine).get(project_id, run_id)
    if run is None:
        return problem(request, 404, "not_found", "Run not found")
    return {"id": str(run["id"]), "project_id": str(run["project_id"]),
            "execution_state": run["execution_outcome"], "capture_state": run["capture_status"],
            "agent_name": run["agent_name"], "adapter": run["adapter"],
            "adapter_version": run["adapter_version"], "trace_id": run["trace_id"],
            "started_at": (run["started_at"] or run["created_at"]).isoformat(),
            "ended_at": run["finished_at"].isoformat() if run["finished_at"] else None}
