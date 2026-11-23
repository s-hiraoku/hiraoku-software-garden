"""Regression tests for GitHub Actions workflow YAML structure and safety invariants."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\s*$")


def _check_yaml_block_scalars_and_tabs(path: Path) -> list[str]:
    """Verify no tab indentation and no unindented lines prematurely closing YAML block scalars."""
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_block = False
    block_parent_indent = 0
    block_content_indent: int | None = None

    for lineno, line in enumerate(lines, 1):
        if "\t" in line[: len(line) - len(line.lstrip(" \t"))]:
            errors.append(f"{path}:{lineno}: tab character in indentation")
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)

        if in_block:
            if not stripped:
                continue
            if indent <= block_parent_indent:
                # Leaving block scalar: must be a valid YAML mapping key, list item, or comment
                if not (
                    stripped.startswith("- ")
                    or stripped.startswith("#")
                    or ":" in stripped
                ):
                    errors.append(
                        f"{path}:{lineno}: unindented content inside block scalar: {line!r}"
                    )
                in_block = False
                block_content_indent = None
            else:
                if block_content_indent is None:
                    block_content_indent = indent
                elif indent < block_content_indent:
                    errors.append(
                        f"{path}:{lineno}: block scalar line less indented than first content line"
                    )
                continue

        if not stripped or stripped.startswith("#"):
            continue
        if BLOCK_SCALAR_RE.search(stripped):
            in_block = True
            block_parent_indent = indent
            block_content_indent = None

    return errors


class WorkflowYamlTests(unittest.TestCase):
    def test_all_workflows_and_template_workflows_have_valid_block_scalars_and_no_tabs(self):
        workflow_files = sorted(
            list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
            + list((REPO_ROOT / "templates").glob("*/*/workflow.yml.tmpl"))
        )
        self.assertGreaterEqual(len(workflow_files), 7)
        all_errors: list[str] = []
        for wf in workflow_files:
            all_errors.extend(_check_yaml_block_scalars_and_tabs(wf))
        self.assertEqual(all_errors, [], "\n".join(all_errors))

    def test_garden_reusable_workflow_has_job_level_concurrency_and_reflect_inputs(self):
        text = (REPO_ROOT / ".github" / "workflows" / "garden-reusable.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_call:", text)
        self.assertIn("reflect:", text)
        self.assertIn("base_branch:", text)
        # Concurrency must be scoped under jobs.run-garden (not top-level)
        self.assertNotIn("\nconcurrency:\n", text)
        self.assertIn("\n    concurrency:\n", text)
        # Ensure embedded Python heredoc is properly indented inside run: |
        self.assertIn("          import json, pathlib, sys\n", text)
        self.assertIn("          PY\n", text)

    def test_garden_dogfood_workflow_uses_trusted_origin_main_cli(self):
        text = (REPO_ROOT / ".github" / "workflows" / "garden-dogfood.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("git archive origin/main", text)
        self.assertIn("TRUSTED_CLI", text)
