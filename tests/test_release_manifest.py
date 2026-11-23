import unittest

from scripts.release_manifest import ReleaseNotReady, validate_release_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_missing_signatures_and_sbom_block_release(self):
        value = {"version": "0.1.0", "packages": [{"digest": "sha256:" + "a" * 64,
                  "signature_verified": True, "sbom_verified": True}],
                 "compatibility_matrix": "matrix", "maintenance_policy": "policy", "changelog": "notes"}
        validate_release_manifest(value)
        for field in ("signature_verified", "sbom_verified"):
            variant = {**value, "packages": [{**value["packages"][0], field: False}]}
            with self.assertRaises(ReleaseNotReady):
                validate_release_manifest(variant)
