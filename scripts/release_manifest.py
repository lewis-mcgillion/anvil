"""Verify required release evidence fields; cryptographic checks remain external."""

import re


class ReleaseNotReady(ValueError):
    pass


def validate_release_manifest(manifest: dict) -> None:
    packages = manifest.get("packages")
    if (manifest.get("version") != "0.1.0" or type(packages) is not list or not packages
            or not manifest.get("compatibility_matrix") or not manifest.get("maintenance_policy")
            or not manifest.get("changelog")):
        raise ReleaseNotReady("Release metadata is incomplete")
    for package in packages:
        if (type(package) is not dict or
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(package.get("digest", "")))
                or not package.get("signature_verified") is True
                or not package.get("sbom_verified") is True):
            raise ReleaseNotReady("Unsigned or unverified package/SBOM")
