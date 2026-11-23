"""Reject insecure public deployment settings before provisioning resources."""

from dataclasses import dataclass
from urllib.parse import urlsplit


class UnsafeDeployment(ValueError):
    pass


@dataclass(frozen=True)
class PublicDeployment:
    tls_enabled: bool
    trusted_oidc_issuer: str
    oidc_audience: str
    secrets_mounted: bool
    public_database_port: bool
    runner_network_disabled: bool
    max_runner_memory_mb: int = 512
    max_runner_processes: int = 64
    max_runner_seconds: int = 120

    def validate(self) -> None:
        try:
            issuer = urlsplit(self.trusted_oidc_issuer)
            issuer_port = issuer.port
        except (ValueError, TypeError) as error:
            raise UnsafeDeployment("Invalid public OIDC issuer") from error
        if (self.tls_enabled is not True or issuer.scheme != "https"
                or issuer.hostname in {None, "localhost", "127.0.0.1", "::1"}
                or issuer.username is not None or issuer.password is not None
                or issuer.query or issuer.fragment or issuer_port == 0
                or not self.oidc_audience or self.secrets_mounted is not True
                or self.public_database_port is not False
                or self.runner_network_disabled is not True
                or type(self.max_runner_memory_mb) is not int
                or not 64 <= self.max_runner_memory_mb <= 4096
                or type(self.max_runner_processes) is not int
                or not 1 <= self.max_runner_processes <= 256
                or type(self.max_runner_seconds) is not int
                or not 1 <= self.max_runner_seconds <= 120):
            raise UnsafeDeployment("Public deployment requires TLS, OIDC, mounted secrets and runner isolation")
