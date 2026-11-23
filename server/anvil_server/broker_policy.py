"""Static fail-closed admission checks for future live-model dispatch."""

from dataclasses import dataclass
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


def admit_live_model(*, mode: str, approved: bool, profile_endpoint: str,
                     requested_endpoint: str, budget: LiveBudget) -> None:
    if mode != "live" or approved is not True:
        raise BrokerDenied("Live inference requires explicit approved opt-in")
    parsed = urlsplit(profile_endpoint)
    requested = urlsplit(requested_endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or not parsed.path.startswith("/")
            or requested != parsed or parsed.hostname.endswith(".local")):
        raise BrokerDenied("Model endpoint is not an approved HTTPS profile endpoint")
    budget.validate()
