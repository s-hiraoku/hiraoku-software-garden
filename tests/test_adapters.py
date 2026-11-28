import json
from pathlib import Path
import sys
import tempfile
import unittest

from garden.adapters import AgentRequest, run_command_adapter
from garden.definition import AgentConfig


class ProviderAdapterTests(unittest.TestCase):
    def _sample_request(self, worktree: Path) -> AgentRequest:
        return AgentRequest(
            run_id="20261123T100000Z-12345678",
            step_id="fix_unit",
            purpose="fix",
            skill="poteto-mode",
            worktree_path=str(worktree),
            specs=[{"spec_id": "app", "path": "docs/specs/app.md", "version": 1, "blob_sha": "a" * 40}],
            delegation={
                "mode": "propose_pr",
                "allowed_paths": ["src/**"],
                "forbidden_paths": [".garden/**", "docs/specs/app.md"],
                "max_cost_usd": 1.0,
            },
            context={"failed_step_id": "s_unit", "failed_check": "unit"},
        )

    def test_codex_adapter_receives_prompt_on_stdin_and_extracts_fenced_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt = tmp_path / "wt"
            wt.mkdir()
            stub = tmp_path / "fake_codex.py"
            stub.write_text(
                "import sys\n"
                "prompt = sys.stdin.read()\n"
                "assert 'Garden Agent Task (fix)' in prompt\n"
                "print('Thinking about the bug...')\n"
                "print('```json')\n"
                "print('{\"schema_version\": 1, \"outcome\": \"completed\", \"summary\": \"patched src/a.py\", \"changed_files\": [\"src/a.py\"], \"usage\": {\"input_tokens\": 50, \"output_tokens\": 20, \"cost_usd\": 0.03}}')\n"
                "print('```')\n",
                encoding="utf-8",
            )
            cfg = AgentConfig(
                name="codex_cli",
                adapter="codex",
                command=(sys.executable, str(stub)),
                timeout_sec=30,
                max_cost_usd=1.0,
            )
            step_dir = tmp_path / "step"
            resp, code = run_command_adapter(
                cfg, self._sample_request(wt), worktree=wt, step_dir=step_dir
            )
            self.assertEqual(code, 0)
            self.assertEqual(resp.outcome, "completed")
            self.assertEqual(resp.summary, "patched src/a.py")
            self.assertAlmostEqual(resp.usage.cost_usd, 0.03)
            self.assertTrue((step_dir / "prompt.md").is_file())
            self.assertTrue((step_dir / "response.json").is_file())

    def test_claude_adapter_extracts_json_envelope_usage_and_spec_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt = tmp_path / "wt"
            wt.mkdir()
            stub = tmp_path / "fake_claude.py"
            stub.write_text(
                "import json, sys\n"
                "_ = sys.stdin.read()\n"
                "inner = '```json\\n' + json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'needs_spec_decision',\n"
                "  'summary': 'ambiguous retry order',\n"
                "  'question': {'prompt': 'FIFO or LIFO?', 'options': ['FIFO', 'LIFO']},\n"
                "}) + '\\n```'\n"
                "print(json.dumps({\n"
                "  'type': 'result',\n"
                "  'result': inner,\n"
                "  'model': 'claude-sonnet',\n"
                "  'total_cost_usd': 0.14,\n"
                "  'usage': {'input_tokens': 300, 'output_tokens': 120},\n"
                "}))\n",
                encoding="utf-8",
            )
            cfg = AgentConfig(
                name="claude_cli",
                adapter="claude",
                command=(sys.executable, str(stub)),
                timeout_sec=30,
                max_cost_usd=1.0,
            )
            step_dir = tmp_path / "step"
            resp, code = run_command_adapter(
                cfg, self._sample_request(wt), worktree=wt, step_dir=step_dir
            )
            self.assertEqual(code, 0)
            self.assertEqual(resp.outcome, "needs_spec_decision")
            self.assertEqual(resp.question["prompt"], "FIFO or LIFO?")
            self.assertAlmostEqual(resp.usage.cost_usd, 0.14)
            self.assertEqual(resp.usage.input_tokens, 300)

    def test_antigravity_adapter_supports_prompt_placeholder_and_handles_invalid_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt = tmp_path / "wt"
            wt.mkdir()
            stub = tmp_path / "fake_agy.py"
            stub.write_text(
                "import sys\n"
                "assert 'Garden Agent Task (fix)' in sys.argv[1]\n"
                "print('not valid json output')\n",
                encoding="utf-8",
            )
            cfg = AgentConfig(
                name="agy_cli",
                adapter="antigravity",
                command=(sys.executable, str(stub), "{prompt}"),
                timeout_sec=30,
            )
            step_dir = tmp_path / "step"
            resp, code = run_command_adapter(
                cfg, self._sample_request(wt), worktree=wt, step_dir=step_dir
            )
            self.assertEqual(code, 0)
            self.assertEqual(resp.outcome, "failed")
            self.assertIn("did not emit a valid Garden JSON response", resp.summary)
            # Even fallback response is normalized to response.json
            saved = json.loads((step_dir / "response.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outcome"], "failed")

    def test_gemini_adapter_extracts_json_envelope_usage_and_issue_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt = tmp_path / "wt"
            wt.mkdir()
            stub = tmp_path / "fake_gemini.py"
            stub.write_text(
                "import json, sys\n"
                "prompt = sys.stdin.read()\n"
                "assert '## Task Issue (#42: Add greeting flag)' in prompt\n"
                "assert 'Support --greet' in prompt\n"
                "assert '## Failure Context' not in prompt\n"
                "inner = '```json\\n' + json.dumps({\n"
                "  'schema_version': 1,\n"
                "  'outcome': 'completed',\n"
                "  'summary': 'implemented --greet',\n"
                "  'changed_files': ['src/cli.py'],\n"
                "}) + '\\n```'\n"
                "print(json.dumps({\n"
                "  'session_id': 'sess-1',\n"
                "  'response': inner,\n"
                "  'stats': {'models': {'gemini-2.5-pro': {'tokens': {'prompt': 420, 'candidates': 85}}}},\n"
                "}))\n",
                encoding="utf-8",
            )
            req = AgentRequest(
                run_id="20261128T130000Z-12345678",
                step_id="impl_issue",
                purpose="implement",
                skill="poteto-mode",
                worktree_path=str(wt),
                specs=[{"spec_id": "app", "path": "docs/specs/app.md", "version": 1, "blob_sha": "a" * 40}],
                delegation={
                    "mode": "propose_pr",
                    "allowed_paths": ["src/**"],
                    "forbidden_paths": [".garden/**", "docs/specs/app.md"],
                    "max_cost_usd": 1.0,
                },
                context={
                    "issue": {
                        "number": 42,
                        "title": "Add greeting flag",
                        "body": "Support --greet in CLI.\n```sh\ngarden --greet\n```",
                        "url": "https://github.com/example/repo/issues/42",
                        "labels": ["garden:ready"],
                        "comments": [{"author": "maintainer", "body": "Keep output UTF-8."}],
                    }
                },
            )
            cfg = AgentConfig(
                name="gemini_cli",
                adapter="gemini",
                command=(sys.executable, str(stub)),
                timeout_sec=30,
                max_cost_usd=1.0,
            )
            step_dir = tmp_path / "step"
            resp, code = run_command_adapter(
                cfg, req, worktree=wt, step_dir=step_dir
            )
            self.assertEqual(code, 0)
            self.assertEqual(resp.outcome, "completed")
            self.assertEqual(resp.changed_files, ("src/cli.py",))
            self.assertEqual(resp.usage.model, "gemini-2.5-pro")
            self.assertEqual(resp.usage.input_tokens, 420)
            self.assertEqual(resp.usage.output_tokens, 85)
            prompt_text = (step_dir / "prompt.md").read_text(encoding="utf-8")
            self.assertIn("````markdown", prompt_text)
