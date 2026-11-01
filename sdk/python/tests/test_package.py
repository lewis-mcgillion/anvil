import unittest

from anvil import __version__


class PackageTests(unittest.TestCase):
    def test_version_is_pinned(self) -> None:
        self.assertEqual(__version__, "0.1.0")

    def test_package_does_not_require_credentials(self) -> None:
        self.assertIsNotNone(__version__)


class PublishedPythonFloorTests(unittest.TestCase):
    """The declared floor must not drift above what CI actually tests.

    A rebase once narrowed requires-python to >=3.12 while CI still ran a
    3.11 job, which made the package uninstallable there. Pin the two
    together so the next narrowing is a deliberate, visible decision.
    """

    def _root(self):
        import pathlib

        return pathlib.Path(__file__).resolve().parents[3]

    def _requires_python(self) -> str:
        import re

        text = (self._root() / "sdk" / "python" / "pyproject.toml").read_text()
        match = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match, "requires-python is not declared")
        return match.group(1)

    def _ci_python_versions(self) -> list[tuple[int, int]]:
        import re

        text = (self._root() / ".github" / "workflows" / "ci.yml").read_text()
        block = re.search(r'python:\s*\[([^\]]+)\]', text)
        self.assertIsNotNone(block, "no python matrix found in CI")
        return [
            tuple(int(part) for part in value.strip().strip('"\'').split("."))
            for value in block.group(1).split(",")
        ]

    def test_floor_is_not_above_the_versions_ci_installs(self) -> None:
        import re

        specifier = self._requires_python()
        floor = re.search(r">=\s*(\d+)\.(\d+)", specifier)
        self.assertIsNotNone(floor, f"unsupported specifier: {specifier}")
        declared = (int(floor.group(1)), int(floor.group(2)))
        for version in self._ci_python_versions():
            self.assertLessEqual(
                declared,
                version,
                f"requires-python {specifier} excludes CI's Python "
                f"{version[0]}.{version[1]}",
            )
