"""Bounded capture-only HTTP transport; no redirects, replay, or tool execution."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping, Protocol
from urllib.parse import urlsplit


class CaptureError(Exception):
    """Capture failed. Messages never include request bodies or server error text."""


class CaptureIncomplete(CaptureError):
    """Capture lost evidence or could not drain within the caller's deadline."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def post(self, path: str, body: bytes, idempotency_key: str, timeout: float) -> Response:
        """Perform one bounded request. Implementations must honor timeout."""
        ...


class HTTPTransport:
    def __init__(self, base_url: str, api_key: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("capture endpoint must be HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1":
            raise ValueError("capture endpoint must end in /v1 without credentials or query")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise ValueError("non-loopback capture requires HTTPS")
        if not api_key or re.search(r"[\r\n]", api_key):
            raise ValueError("a valid capture API key is required")
        self._host = parsed.hostname
        self._port = parsed.port
        self._https = parsed.scheme == "https"
        self._api_key = api_key

    def post(self, path: str, body: bytes, idempotency_key: str, timeout: float) -> Response:
        if not re.fullmatch(r"/(?:projects/[0-9a-f-]+/runs|runs/[0-9a-f-]+/events:batch)", path):
            raise ValueError("transport permits only capture creation and ingestion")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise ValueError("invalid idempotency key")
        connection_type = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        connection = connection_type(self._host, self._port, timeout=timeout)
        try:
            connection.request("POST", "/v1" + path, body=body, headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            })
            response = connection.getresponse()
            payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise CaptureError("capture response exceeds 64 KiB")
            return Response(response.status, {k.lower(): v for k, v in response.getheaders()}, payload)
        except (OSError, socket.timeout, http.client.HTTPException):
            raise CaptureError("capture transport failed") from None
        finally:
            connection.close()


def retry_after(headers: Mapping[str, str]) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "0")
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError
        return max(0.0, seconds)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            raise CaptureError("invalid Retry-After header") from None


def acknowledged(response: Response, count: int) -> None:
    if response.status != 200:
        raise CaptureError(f"capture HTTP status {response.status}")
    try:
        result = json.loads(response.body)
        accepted, duplicates = result["accepted"], result["duplicates"]
        if type(accepted) is not int or type(duplicates) is not int:
            raise ValueError
        if accepted < 0 or duplicates < 0 or accepted + duplicates != count:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise CaptureError("invalid event batch acknowledgement") from None
