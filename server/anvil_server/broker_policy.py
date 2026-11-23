"""Static fail-closed admission checks for future live-model dispatch."""

from dataclasses import dataclass
import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit


class BrokerDenied(ValueError):
    pass


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
