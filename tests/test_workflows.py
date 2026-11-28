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
        self.assertIn("reflect_token:", text)
        self.assertIn("nursery_token:", text)
        self.assertIn("fetch --depth 1", text)
        self.assertIn("if: success() && inputs.reflect", text)
        # Concurrency must be scoped under jobs.run-garden and serialize per repository + issue/PR
        self.assertNotIn("\nconcurrency:\n", text)
        self.assertIn("\n    concurrency:\n", text)
        self.assertIn(
            "group: garden-${{ github.repository }}-${{ inputs.issue_number || github.event.pull_request.number || github.ref }}",
            text,
        )
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("queue: max", text)
        self.assertNotIn("group: garden-${{ inputs.flow }}", text)
        # Ensure embedded Python heredoc is properly indented inside run: |
        self.assertIn("          import json, pathlib, sys\n", text)
        self.assertIn("          PY\n", text)

    def test_garden_reusable_workflow_can_set_up_node_for_target_checks(self):
        text = (REPO_ROOT / ".github" / "workflows" / "garden-reusable.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("node_version:", text)
        self.assertIn("node_version_file:", text)
        self.assertIn("sync_pstack:", text)
        self.assertIn("harness sync-pstack", text)
        self.assertIn("uses: actions/setup-node@v4", text)
        self.assertIn("node-version: ${{ inputs.node_version }}", text)
        self.assertIn("node-version-file: ${{ inputs.node_version_file }}", text)
        self.assertIn("npm@*|pnpm@*", text)
        self.assertIn("--mark-running", text)
        self.assertIn("--clear-running", text)
        # Node setup, optional pstack sync, and running-label mark must run before the Garden flow
        self.assertLess(text.index("actions/setup-node@v4"), text.index("- name: Run Garden flow"))
        self.assertLess(text.index("harness sync-pstack"), text.index("- name: Run Garden flow"))
        self.assertLess(text.index("--mark-running"), text.index("- name: Run Garden flow"))
        self.assertLess(text.index("- name: Post report to GitHub Issue / PR"), text.index("--clear-running"))

    def test_garden_dogfood_workflow_uses_trusted_origin_main_cli_and_reusable_smoke(self):
        text = (REPO_ROOT / ".github" / "workflows" / "garden-dogfood.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("git archive origin/main", text)
        self.assertIn("TRUSTED_CLI", text)
        self.assertIn("uses: ./.github/workflows/garden-reusable.yml", text)

    def test_garden_feedback_reusable_workflow_enforces_authorization_and_body_file(self):
        text = (
            REPO_ROOT / ".github" / "workflows" / "garden-feedback-reusable.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_call:", text)
        self.assertIn("OWNER|MEMBER|COLLABORATOR", text)
        self.assertIn("--body-file", text)
        self.assertIn("open_pr:", text)
        self.assertIn("feedback_token:", text)
        self.assertIn("nursery_token:", text)
        self.assertIn("fetch --depth 1", text)
        self.assertIn("ref: ${{ inputs.base_branch }}", text)
        self.assertNotIn("\nconcurrency:\n", text)
        self.assertIn("\n    concurrency:\n", text)

    def test_template_workflows_support_nursery_ref_pinning_and_feedback_job(self):
        for tmpl in sorted((REPO_ROOT / "templates").glob("*/*/workflow.yml.tmpl")):
            text = tmpl.read_text(encoding="utf-8")
            self.assertIn("garden-reusable.yml@__NURSERY_REF__", text, str(tmpl))
            self.assertIn("garden-feedback-reusable.yml@__NURSERY_REF__", text, str(tmpl))
            self.assertIn("startsWith(github.event.comment.body, '/garden feedback')", text, str(tmpl))
