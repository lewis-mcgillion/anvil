"""Include the authoritative frozen schemas in wheels and source distributions."""

from pathlib import Path
from shutil import copy2

from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def _copy_schemas(destination: Path) -> None:
    root = Path(__file__).resolve().parent
    source = root / "anvil_schema" / "schemas"
    if not source.is_dir():
        source = root.parents[1] / "schemas" / "v1"
    destination.mkdir(parents=True, exist_ok=True)
    paths = sorted(source.glob("*.schema.json"))
    if len(paths) != 8:
        raise RuntimeError("expected all eight frozen v1 schema documents")
    for path in paths:
        copy2(path, destination / path.name)


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        _copy_schemas(Path(self.build_lib) / "anvil_schema" / "schemas")


class SDist(sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        _copy_schemas(Path(base_dir) / "anvil_schema" / "schemas")
