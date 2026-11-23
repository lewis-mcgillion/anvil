import unittest

from scripts.license_gate import check_inventory


class LicenseGateTests(unittest.TestCase):
    def test_approved_licenses(self):
        check_inventory([{"name": "library", "license": "MIT"}])

    def test_unknown_or_incompatible_licenses_fail_closed(self):
        for value in ("GPL-3.0-only", "UNKNOWN", "", "MIT OR GPL-3.0-only"):
            with self.assertRaises(ValueError):
                check_inventory([{"name": "library", "license": value}])
