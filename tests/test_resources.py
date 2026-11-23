"""Nursery resources must be reachable both from a checkout and from an installed wheel."""

from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from garden.resources import nursery_root


REPO_ROOT = Path(__file__).resolve().parents[1]


class NurseryResourcesTest(unittest.TestCase):
    def test_checkout_resolves_to_repository_root(self) -> None:
        root = nursery_root()
        self.assertTrue((root / "templates" / "cli" / "v1" / "template.toml").is_file())
        self.assertTrue((root / "harnesses" / "pstack.toml").is_file())

    def test_pyproject_ships_templates_and_harnesses(self) -> None:
        config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        setuptools = config["tool"]["setuptools"]
        self.assertEqual(setuptools["package-dir"]["garden._nursery.templates"], "templates")
        self.assertEqual(setuptools["package-dir"]["garden._nursery.harnesses"], "harnesses")
        self.assertIn("garden._nursery.templates", setuptools["packages"])
        self.assertIn("garden._nursery.harnesses", setuptools["packages"])


if __name__ == "__main__":
    unittest.main()
