"""Reject insecure public deployment settings before provisioning resources."""

from dataclasses import dataclass


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

    def validate(self) -> None:
        if (self.tls_enabled is not True or not self.trusted_oidc_issuer.startswith("https://")
                or not self.trusted_oidc_issuer.removeprefix("https://")
                or not self.oidc_audience or self.secrets_mounted is not True
                or self.public_database_port is not False
                or self.runner_network_disabled is not True):
            raise UnsafeDeployment("Public deployment requires TLS, OIDC, mounted secrets and runner isolation")
