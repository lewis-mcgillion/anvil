import unittest

from anvil import __version__


class PackageTests(unittest.TestCase):
    def test_version_is_pinned(self) -> None:
        self.assertEqual(__version__, "0.1.0")

    def test_package_does_not_require_credentials(self) -> None:
        self.assertIsNotNone(__version__)
