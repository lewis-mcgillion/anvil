"""Static fail-closed admission checks for future live-model dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import asyncio
import ipaddress
import json
import ssl
import socket
import time
from urllib.parse import urlsplit


class BrokerDenied(ValueError):
    pass


class BrokerThrottled(BrokerDenied):
    def __init__(self, retry_after_seconds: int | None):
        super().__init__("Model provider is throttling requests")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class OperatorProfile:
    endpoint: str
    credential: str
    approved: bool


async def dispatch_operator_profile(*, profile_id: str,
                                    lookup: Callable[[str], OperatorProfile | None],
                                    payload: dict, ledger: BudgetLedger) -> dict:
    if not profile_id or not profile_id.isascii() or not all(
        character.isalnum() or character in "-_" for character in profile_id
    ):
        raise BrokerDenied("Invalid operator profile identifier")
    profile = lookup(profile_id)
    if not isinstance(profile, OperatorProfile):
        raise BrokerDenied("Approved operator profile is unavailable")
    return await dispatch_live_model(
        endpoint=profile.endpoint, profile_endpoint=profile.endpoint,
        approved=profile.approved, credential=profile.credential,
        payload=payload, ledger=ledger,
    )


@dataclass(frozen=True)
class LiveBudget:
    wall_seconds: int = 120
    model_calls: int = 20
    tool_calls: int = 100
    input_tokens: int = 20_000
    output_tokens: int = 10_000
    estimated_usd: float = 1.0

    def validate(self) -> None:
        values = (self.wall_seconds, self.model_calls, self.tool_calls,
                  self.input_tokens, self.output_tokens)
        if any(type(value) is not int or value <= 0 for value in values):
            raise BrokerDenied("Live model budgets must be positive integers")
        if type(self.estimated_usd) not in {int, float} or not 0 < self.estimated_usd <= 1.0:
            raise BrokerDenied("Invalid live model spending budget")


class BudgetLedger:
    def __init__(self, budget: LiveBudget):
        budget.validate()
        self.budget = budget
        self.started = time.monotonic()
        self.model_calls = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.micro_usd = 0
        self.cancelled = False
        self._dispatch_lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None

    def charge(self, *, model_calls: int = 0, tool_calls: int = 0,
               input_tokens: int = 0, output_tokens: int = 0, micro_usd: int = 0) -> None:
        increments = (model_calls, tool_calls, input_tokens, output_tokens, micro_usd)
        if any(type(value) is not int or value < 0 for value in increments):
            raise BrokerDenied("Broker usage increments must be nonnegative integers")
        if self.cancelled or time.monotonic() - self.started >= self.budget.wall_seconds:
            raise BrokerDenied("Broker run cancelled or wall-time budget exhausted")
        updated = tuple(current + increment for current, increment in zip(
            (self.model_calls, self.tool_calls, self.input_tokens, self.output_tokens, self.micro_usd),
            increments,
        ))
        limits = (self.budget.model_calls, self.budget.tool_calls,
                  self.budget.input_tokens, self.budget.output_tokens,
                  int(self.budget.estimated_usd * 1_000_000))
        if any(value > limit for value, limit in zip(updated, limits)):
            raise BrokerDenied("Broker budget exhausted")
        (self.model_calls, self.tool_calls, self.input_tokens,
         self.output_tokens, self.micro_usd) = updated

    def cancel(self) -> None:
        self.cancelled = True
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()


async def dispatch_live_model(*, endpoint: str, profile_endpoint: str, approved: bool,
                              credential: str, payload: dict, ledger: BudgetLedger,
                              max_response_bytes: int = 1_048_576) -> dict:
    async with ledger._dispatch_lock:
        if ledger.cancelled:
            raise BrokerDenied("Broker run cancelled")
        ledger._active_task = asyncio.current_task()
        try:
            return await _dispatch_live_model_locked(
                endpoint=endpoint, profile_endpoint=profile_endpoint, approved=approved,
                credential=credential, payload=payload, ledger=ledger,
                max_response_bytes=max_response_bytes,
            )
        finally:
            ledger._active_task = None


async def _dispatch_live_model_locked(*, endpoint: str, profile_endpoint: str, approved: bool,
                                      credential: str, payload: dict, ledger: BudgetLedger,
                                      max_response_bytes: int) -> dict:
    admit_live_model(mode="live", approved=approved, profile_endpoint=profile_endpoint,
                     requested_endpoint=endpoint, budget=ledger.budget)
    if not credential or not credential.isascii() or any(ord(character) < 33 or ord(character) > 126 for character in credential):
        raise BrokerDenied("Operator-managed model credential is unavailable")
    if max_response_bytes < 1 or max_response_bytes > 1_048_576:
        raise BrokerDenied("Invalid broker response limit")
    try:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BrokerDenied("Model request is not valid JSON") from error
    if len(body) > 1_048_576:
        raise BrokerDenied("Model request exceeds broker size limit")
    estimated_input_tokens = (len(body) + 3) // 4
    ledger.charge(model_calls=1, input_tokens=estimated_input_tokens)
    hostname = urlsplit(endpoint).hostname
    reader = writer = None
    try:
        async with asyncio.timeout_at(ledger.started + ledger.budget.wall_seconds):
            addresses = await resolve_public_endpoint(endpoint)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], 443, ssl=ssl.create_default_context(),
                                        server_hostname=hostname), timeout=10,
            )
            path = urlsplit(endpoint).path
            headers = (f"POST {path} HTTP/1.1\r\nHost: {hostname}\r\n"
                       f"Authorization: Bearer {credential}\r\nContent-Type: application/json\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
            writer.write(headers.encode("ascii") + body)
            await asyncio.wait_for(writer.drain(), timeout=10)
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) <= 8192 and head.startswith((b"HTTP/1.1 429 ", b"HTTP/1.0 429 ")):
                throttle_headers = head.decode("ascii").lower().split("\r\n")
                retry_values = [line.split(":", 1)[1].strip() for line in throttle_headers[1:]
                                if line.startswith("retry-after:")]
                retry_value = retry_values[0] if len(retry_values) == 1 else ""
                raise BrokerThrottled(min(int(retry_value), 600) if retry_value.isdigit() else None)
            if len(head) > 8192 or not head.startswith((b"HTTP/1.1 200 ", b"HTTP/1.0 200 ")):
                raise BrokerDenied("Model provider rejected request or attempted redirect")
            headers = head.decode("ascii").lower().split("\r\n")
            if any(line.startswith(("transfer-encoding:", "content-encoding:")) for line in headers[1:]):
                raise BrokerDenied("Model provider response uses unsupported encoding")
            if any(line.startswith((" ", "\t")) or (line and ":" not in line) for line in headers[1:]):
                raise BrokerDenied("Model provider response has malformed headers")
            lengths = [line.split(":", 1)[1].strip() for line in headers[1:] if line.startswith("content-length:")]
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise BrokerDenied("Model provider response requires a single content length")
            content_length = int(lengths[0])
            if content_length > max_response_bytes:
                raise BrokerDenied("Model provider response exceeds broker size limit")
            content = await reader.readexactly(content_length)
            response = json.loads(content)
            if not isinstance(response, dict):
                raise BrokerDenied("Model provider response must be an object")
            usage = response.get("usage", {})
            if not isinstance(usage, dict):
                raise BrokerDenied("Model provider usage is invalid")
            actual_input_tokens = usage.get("input_tokens", 0)
            if type(actual_input_tokens) is not int or actual_input_tokens < 0:
                raise BrokerDenied("Model provider usage is invalid")
            ledger.charge(input_tokens=max(0, actual_input_tokens - estimated_input_tokens),
                          output_tokens=usage.get("output_tokens", 0),
                          micro_usd=usage.get("cost_micro_usd", 0))
            return response
    except BrokerDenied:
        raise
    except (OSError, asyncio.TimeoutError, ssl.SSLError, UnicodeError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise BrokerDenied("Model provider transport failed") from None
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def admit_live_model(*, mode: str, approved: bool, profile_endpoint: str,
                     requested_endpoint: str, budget: LiveBudget) -> None:
    if mode != "live" or approved is not True:
        raise BrokerDenied("Live inference requires explicit approved opt-in")
    try:
        parsed = urlsplit(profile_endpoint)
        requested = urlsplit(requested_endpoint)
        port = parsed.port
    except ValueError as error:
        raise BrokerDenied("Model endpoint URL is invalid") from error
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or not parsed.path.startswith("/")
            or requested != parsed or parsed.hostname.endswith((".local", ".localhost", ".internal"))
            or parsed.hostname in {"localhost", "metadata.google.internal"}
            or address is not None or port not in (None, 443)
            or "\\" in profile_endpoint or any(ord(character) < 33 for character in profile_endpoint)
            or parsed.hostname.endswith(".") or "%" in parsed.netloc):
        raise BrokerDenied("Model endpoint is not an approved HTTPS profile endpoint")
    budget.validate()


async def resolve_public_endpoint(endpoint: str) -> tuple[str, ...]:
    hostname = urlsplit(endpoint).hostname
    if not hostname:
        raise BrokerDenied("Model endpoint has no hostname")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise BrokerDenied("Model endpoint DNS resolution failed") from error
    resolved = set()
    for entry in addresses:
        try:
            address = ipaddress.ip_address(entry[4][0])
        except (ValueError, IndexError, TypeError) as error:
            raise BrokerDenied("Model endpoint DNS returned an invalid address") from error
        if not address.is_global:
            raise BrokerDenied("Model endpoint DNS resolved to a non-public address")
        resolved.add(str(address))
    if not resolved:
        raise BrokerDenied("Model endpoint DNS returned no addresses")
    return tuple(sorted(resolved))
