import unittest

from garden.definition import DefinitionError, load_definition_bytes
from tests.helpers import PSTACK_SHA


BASE_TOML = f"""
schema_version = 1

[template]
name = "cli"
version = 1

[common_harness]
name = "pstack"
ref = "{PSTACK_SHA}"

[target_harness]
name = "sample-harness"
version = 1

[spec]
paths = ["docs/specs/app.md"]
approval_branch = "main"

[delegation]
mode = "propose_pr"
allowed_paths = ["src/**", "tests/**"]
forbidden_paths = [".github/workflows/**"]
max_retries = 2
max_duration_sec = 600
max_cost_usd = 5.0
require_checks = ["unit"]

[checks.unit]
kind = "check"
command = ["python3", "-c", "pass"]
covers = ["AC-001"]

[checks.formal_inv]
kind = "formal_check"
tool = "quint"
model_path = "formal/model.qnt"
command = ["python3", "-c", "pass"]
covers = ["INV-001"]
invariants = ["INV-001"]

[agents.default]
adapter = "mock"
command = ["python3", "mock_agent.py"]
max_cost_usd = 2.0

[flows.main_flow]
scope = "all_specs"

[[flows.main_flow.steps]]
id = "run_unit"
type = "check"
check = "unit"
on_fail = "fix_and_retry"
fix_step = "fix_it"
max_retries = 2

[[flows.main_flow.steps]]
id = "fix_it"
type = "agent"
agent = "default"
run_on_demand = true

[[flows.main_flow.steps]]
id = "final_gate"
type = "gate"
require_checks = ["unit", "formal_inv"]
"""


class DefinitionValidationTests(unittest.TestCase):
    def test_valid_definition_forces_forbidden_paths(self):
        definition = load_definition_bytes(BASE_TOML.encode("utf-8"))
        self.assertEqual(definition.schema_version, 1)
        self.assertIn(".garden/**", definition.delegation.forbidden_paths)
        self.assertIn("docs/specs/app.md", definition.delegation.forbidden_paths)
        self.assertIn(".github/workflows/**", definition.delegation.forbidden_paths)
        self.assertEqual(definition.flows["main_flow"].scope, "all_specs")

    def test_rejects_relaxed_delegation_mode_in_flow(self):
        bad = BASE_TOML + """
[flows.relaxed]
[flows.relaxed.delegation]
mode = "auto_merge"
[[flows.relaxed.steps]]
id = "s1"
type = "check"
check = "unit"
"""
        with self.assertRaisesRegex(DefinitionError, "cannot relax delegation mode"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_expanded_allowed_paths_in_flow(self):
        bad = BASE_TOML + """
[flows.expanded]
[flows.expanded.delegation]
allowed_paths = ["src/**", "scripts/**"]
[[flows.expanded.steps]]
id = "s1"
type = "check"
check = "unit"
"""
        with self.assertRaisesRegex(DefinitionError, "outside default allowed_paths"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_removed_require_checks_in_flow(self):
        bad = BASE_TOML + """
[flows.weaker]
[flows.weaker.delegation]
require_checks = []
[[flows.weaker.steps]]
id = "s1"
type = "check"
check = "unit"
"""
        with self.assertRaisesRegex(DefinitionError, "cannot remove default required checks"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_step_retries_exceeding_delegation_cap(self):
        bad = BASE_TOML.replace("max_retries = 2\n\n[[flows.main_flow.steps]]", "max_retries = 3\n\n[[flows.main_flow.steps]]")
        with self.assertRaisesRegex(DefinitionError, "cannot exceed effective delegation max_retries"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_fix_and_retry_in_observe_only_mode(self):
        bad = BASE_TOML + """
[flows.obs]
[flows.obs.delegation]
mode = "observe_only"
allowed_paths = []
[[flows.obs.steps]]
id = "s1"
type = "check"
check = "unit"
on_fail = "fix_and_retry"
fix_step = "f1"
[[flows.obs.steps]]
id = "f1"
type = "agent"
agent = "default"
run_on_demand = true
"""
        with self.assertRaisesRegex(DefinitionError, "observe_only"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_unreferenced_run_on_demand_step(self):
        bad = BASE_TOML + """
[flows.orphan]
[[flows.orphan.steps]]
id = "unused"
type = "agent"
agent = "default"
run_on_demand = true
"""
        with self.assertRaisesRegex(DefinitionError, "never referenced"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_boolean_for_integer_fields(self):
        bad_schema = BASE_TOML.replace("schema_version = 1", "schema_version = true", 1)
        with self.assertRaisesRegex(DefinitionError, "expected integer 1"):
            load_definition_bytes(bad_schema.encode("utf-8"))

        bad_tpl = BASE_TOML.replace(
            '[template]\nname = "cli"\nversion = 1',
            '[template]\nname = "cli"\nversion = true',
            1,
        )
        with self.assertRaisesRegex(DefinitionError, "integer >= 1"):
            load_definition_bytes(bad_tpl.encode("utf-8"))

    def test_rejects_formal_check_missing_invariants(self):
        bad = BASE_TOML.replace('invariants = ["INV-001"]', "")
        with self.assertRaisesRegex(DefinitionError, "invariants"):
            load_definition_bytes(bad.encode("utf-8"))

    def test_rejects_empty_allowed_paths_in_write_modes_default_and_flow_override(self):
        for mode in ("propose_pr", "auto_merge"):
            bad_default = BASE_TOML.replace('mode = "propose_pr"', f'mode = "{mode}"', 1).replace(
                'allowed_paths = ["src/**", "tests/**"]', "allowed_paths = []", 1
            )
            with self.assertRaisesRegex(DefinitionError, "must be non-empty"):
                load_definition_bytes(bad_default.encode("utf-8"))

            bad_flow = BASE_TOML.replace('mode = "propose_pr"', f'mode = "{mode}"', 1) + f"""
[flows.empty_override]
[flows.empty_override.delegation]
mode = "{mode}"
allowed_paths = []
[[flows.empty_override.steps]]
id = "s1"
type = "check"
check = "unit"
"""
            with self.assertRaisesRegex(DefinitionError, "must be non-empty"):
                load_definition_bytes(bad_flow.encode("utf-8"))
