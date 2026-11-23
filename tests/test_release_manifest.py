import unittest

from scripts.release_manifest import ReleaseNotReady, validate_release_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_missing_signatures_and_sbom_block_release(self):
        value = {"version": "0.1.0", "packages": [{"name": "anvil-api", "digest": "sha256:" + "a" * 64,
                  "signature_verified": True, "sbom_verified": True,
                  "sbom_digest": "sha256:" + "b" * 64, "provenance_reference": "attestations/api.json"}],
                 "compatibility_matrix": "matrix", "maintenance_policy": "policy", "changelog": "notes"}
        validate_release_manifest(value)
        for field in ("signature_verified", "sbom_verified"):
            variant = {**value, "packages": [{**value["packages"][0], field: False}]}
            with self.assertRaises(ReleaseNotReady):
                validate_release_manifest(variant)

        for field in ("name", "sbom_digest", "provenance_reference"):
            variant = {**value, "packages": [{**value["packages"][0], field: ""}]}
            with self.assertRaises(ReleaseNotReady):
                validate_release_manifest(variant)

        duplicate = {**value, "packages": value["packages"] * 2}
        with self.assertRaises(ReleaseNotReady):
            validate_release_manifest(duplicate)

        for reference in ("../secrets.json", "/etc/attestation.json", "folder\\file.json", "notes.txt"):
            variant = {**value, "packages": [{**value["packages"][0],
                                               "provenance_reference": reference}]}
            with self.assertRaises(ReleaseNotReady):
                validate_release_manifest(variant)
