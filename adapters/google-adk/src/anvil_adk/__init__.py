"""Google ADK integration for Anvil (Apache-2.0)."""

from importlib.metadata import PackageNotFoundError, version

SUPPORTED_ADK = "2.13.0"
ADAPTER_VERSION = "0.1.0"


class Ineligible(ValueError):
    """The input cannot be represented safely by this adapter."""


def check_version() -> None:
    try:
        installed = version("google-adk")
    except PackageNotFoundError as exc:
        raise Ineligible(f"google-adk {SUPPORTED_ADK} is required") from exc
    if installed != SUPPORTED_ADK:
        raise Ineligible(f"unsupported google-adk version: {installed}; expected {SUPPORTED_ADK}")


# Reject unverified versions before importing any version-specific ADK APIs.
check_version()
