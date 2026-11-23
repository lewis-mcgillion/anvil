from dataclasses import replace
import unittest

from scripts.deployment_preflight import PublicDeployment, UnsafeDeployment


class PublicDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.safe = PublicDeployment(True, "https://identity.example.com", "anvil-api", True, False, True)

    def test_complete_configuration_passes(self):
        self.safe.validate()

    def test_insecure_defaults_fail_closed(self):
        for changes in ({"tls_enabled": False}, {"trusted_oidc_issuer": "http://localhost"},
                        {"trusted_oidc_issuer": "https://localhost"},
                        {"trusted_oidc_issuer": "https://user:pass@identity.example.com"},
                        {"oidc_audience": ""}, {"secrets_mounted": False},
                        {"public_database_port": True}, {"runner_network_disabled": False},
                        {"max_runner_memory_mb": 0}, {"max_runner_memory_mb": 99999},
                        {"max_runner_processes": 0}, {"max_runner_processes": 999},
                        {"max_runner_seconds": 121}, {"max_runner_seconds": True}):
            with self.assertRaises(UnsafeDeployment):
                replace(self.safe, **changes).validate()
