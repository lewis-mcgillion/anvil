"""Fail-closed API skeleton; database-backed operations are not enabled yet."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


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


app = FastAPI(title="Anvil API", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    return problem(request, 400, "invalid_request", "Request failed schema validation")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 4 * 1024 * 1024:
        response = problem(request, 413, "payload_too_large", "Request body exceeds 4 MiB")
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def problem(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    content = Problem(title=code.replace("_", " ").capitalize(), status=status,
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
    return problem(request, 503, "not_ready", "Persistence and authorization are not configured")


@app.post("/v1/projects")
async def create_project(request: Request, payload: ProjectCreate):
    return problem(request, 503, "not_ready", "Project creation requires configured persistence and authentication")
