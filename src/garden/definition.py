"""Loader and strict validator for target-repository garden definitions (.garden/garden.toml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any


class DefinitionError(ValueError):
    """Raised when .garden/garden.toml violates the schema or policy constraints."""


SCHEMA_VERSION = 1
GARDEN_DEF_REL_PATH = ".garden/garden.toml"
FORBIDDEN_ALWAYS = (".garden/**",)

MODE_RANK = {
    "observe_only": 0,
    "propose_pr": 1,
    "auto_merge": 2,
}
CHECK_KINDS = ("check", "formal_check")
CHECK_STATUSES = ("active", "proposed")
FORMAL_TOOLS = ("tlc", "quint", "lean4")
AGENT_ADAPTERS = ("codex", "claude", "antigravity", "custom", "mock")
FLOW_SCOPES = ("flow_checks", "all_specs")
STEP_TYPES = ("check", "agent", "gate", "report")
ON_FAIL_ACTIONS = ("abort", "escalate", "retry", "fix_and_retry")
ON_INCONCLUSIVE_ACTIONS = ("escalate", "retry", "abort")
REPORT_KINDS = ("auto", "harvest", "spec_question", "escalation")

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
INV_ID_RE = re.compile(r"^INV-\d{3,}$")


@dataclass(frozen=True)
class TemplateConfig:
    name: str
    version: int


@dataclass(frozen=True)
class CommonHarnessConfig:
    name: str
    ref: str
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetHarnessConfig:
    name: str
    version: int
    setup: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecConfig:
    paths: tuple[str, ...]
    approval_branch: str = "main"


@dataclass(frozen=True)
class DelegationConfig:
    mode: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    max_retries: int
    max_duration_sec: int
    max_cost_usd: float
    require_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckConfig:
    name: str
    kind: str
    command: tuple[str, ...]
    timeout_sec: int = 300
    covers: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    inconclusive_exit_codes: tuple[int, ...] = (75,)
    report_path: str | None = None
    status: str = "active"
    tool: str | None = None
    model_path: str | None = None
    invariants: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentConfig:
    name: str
    adapter: str
    command: tuple[str, ...]
    timeout_sec: int = 600
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class StepConfig:
    id: str
    type: str
    run_on_demand: bool = False
    check: str | None = None
    on_fail: str = "escalate"
    fix_step: str | None = None
    on_inconclusive: str = "escalate"
    max_retries: int | None = None
    agent: str | None = None
    purpose: str = "fix"
    skill: str | None = None
    require_checks: tuple[str, ...] = ()
    kind: str = "auto"


@dataclass(frozen=True)
class FlowConfig:
    name: str
    description: str
    scope: str
    delegation: DelegationConfig
    steps: tuple[StepConfig, ...]


@dataclass(frozen=True)
class TriggerConfig:
    id: str
    event: str
    flow: str
    conditions: dict[str, Any]


@dataclass(frozen=True)
class GardenDefinition:
    schema_version: int
    template: TemplateConfig
    common_harness: CommonHarnessConfig
    target_harness: TargetHarnessConfig
    spec: SpecConfig
    delegation: DelegationConfig
    checks: dict[str, CheckConfig]
    agents: dict[str, AgentConfig]
    flows: dict[str, FlowConfig]
    triggers: tuple[TriggerConfig, ...]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_table(parent: dict[str, Any], key: str, ctx: str = "") -> dict[str, Any]:
    prefix = f"{ctx}." if ctx else ""
    value = parent.get(key)
    if not isinstance(value, dict):
        raise DefinitionError(f"{prefix}{key}: expected a table")
    return value


def _require_nonempty_str(table: dict[str, Any], key: str, ctx: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{ctx}.{key}: must be a non-empty string")
    return value


def _optional_str(table: dict[str, Any], key: str, ctx: str, default: str | None = None) -> str | None:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{ctx}.{key}: must be a non-empty string")
    return value


def _require_pos_int(table: dict[str, Any], key: str, ctx: str) -> int:
    value = table.get(key)
    if not _is_int(value) or value < 1:
        raise DefinitionError(f"{ctx}.{key}: must be an integer >= 1")
    return value


def _require_nonneg_int(table: dict[str, Any], key: str, ctx: str) -> int:
    value = table.get(key)
    if not _is_int(value) or value < 0:
        raise DefinitionError(f"{ctx}.{key}: must be an integer >= 0")
    return value


def _require_nonneg_number(table: dict[str, Any], key: str, ctx: str) -> float:
    value = table.get(key)
    if not _is_number(value) or value < 0:
        raise DefinitionError(f"{ctx}.{key}: must be a number >= 0")
    return float(value)


def _validate_str_list(
    value: Any, ctx: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise DefinitionError(f"{ctx}: must be an array of non-empty strings")
    if not allow_empty and not value:
        raise DefinitionError(f"{ctx}: must not be empty")
    return tuple(value)


def _validate_rel_path(path_str: str, ctx: str) -> str:
    if not isinstance(path_str, str) or not path_str.strip():
        raise DefinitionError(f"{ctx}: path must be a non-empty string")
    if path_str.startswith("/") or path_str.startswith("\\"):
        raise DefinitionError(f"{ctx}: path must be relative: {path_str}")
    if ":" in PurePosixPath(path_str).parts[0]:
        raise DefinitionError(f"{ctx}: path must not include a drive letter: {path_str}")
    parts = PurePosixPath(path_str.replace("\\", "/")).parts
    if ".." in parts:
        raise DefinitionError(f"{ctx}: path must not contain '..': {path_str}")
    return path_str


def _dedupe_preserve(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _parse_delegation(
    table: dict[str, Any],
    ctx: str,
    forced_forbidden: tuple[str, ...],
    valid_checks: set[str],
) -> DelegationConfig:
    mode = _require_nonempty_str(table, "mode", ctx)
    if mode not in MODE_RANK:
        raise DefinitionError(f"{ctx}.mode: must be one of {', '.join(MODE_RANK)}")

    allowed_raw = table.get("allowed_paths", [])
    allowed_paths = _dedupe_preserve(_validate_str_list(allowed_raw, f"{ctx}.allowed_paths"))
    if mode == "observe_only" and allowed_paths:
        raise DefinitionError(f"{ctx}.allowed_paths: must be empty when mode is 'observe_only'")

    forbidden_raw = table.get("forbidden_paths", [])
    forbidden_list = _validate_str_list(forbidden_raw, f"{ctx}.forbidden_paths")
    forbidden_paths = _dedupe_preserve((*forbidden_list, *forced_forbidden))

    max_retries = _require_nonneg_int(table, "max_retries", ctx)
    max_duration_sec = _require_pos_int(table, "max_duration_sec", ctx)
    max_cost_usd = _require_nonneg_number(table, "max_cost_usd", ctx)

    require_checks_raw = table.get("require_checks", [])
    require_checks = _dedupe_preserve(
        _validate_str_list(require_checks_raw, f"{ctx}.require_checks")
    )
    for check_name in require_checks:
        if check_name not in valid_checks:
            raise DefinitionError(
                f"{ctx}.require_checks: unknown check '{check_name}'"
            )

    return DelegationConfig(
        mode=mode,
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        max_retries=max_retries,
        max_duration_sec=max_duration_sec,
        max_cost_usd=max_cost_usd,
        require_checks=require_checks,
    )


def _merge_strict_delegation(
    base: DelegationConfig,
    override: dict[str, Any],
    ctx: str,
    valid_checks: set[str],
) -> DelegationConfig:
    mode = _optional_str(override, "mode", ctx, base.mode) or base.mode
    if mode not in MODE_RANK:
        raise DefinitionError(f"{ctx}.mode: must be one of {', '.join(MODE_RANK)}")
    if MODE_RANK[mode] > MODE_RANK[base.mode]:
        raise DefinitionError(
            f"{ctx}.mode: cannot relax delegation mode from '{base.mode}' to '{mode}'"
        )

    if "allowed_paths" in override:
        allowed_paths = _dedupe_preserve(
            _validate_str_list(override["allowed_paths"], f"{ctx}.allowed_paths")
        )
        if mode == "observe_only" and allowed_paths:
            raise DefinitionError(
                f"{ctx}.allowed_paths: must be empty when mode is 'observe_only'"
            )
        base_allowed = set(base.allowed_paths)
        extra = [p for p in allowed_paths if p not in base_allowed]
        if extra:
            raise DefinitionError(
                f"{ctx}.allowed_paths: cannot add paths outside default allowed_paths: {', '.join(extra)}"
            )
    else:
        allowed_paths = () if mode == "observe_only" else base.allowed_paths

    if "forbidden_paths" in override:
        extra_forbidden = _validate_str_list(
            override["forbidden_paths"], f"{ctx}.forbidden_paths"
        )
        forbidden_paths = _dedupe_preserve((*base.forbidden_paths, *extra_forbidden))
    else:
        forbidden_paths = base.forbidden_paths

    if "max_retries" in override:
        max_retries = _require_nonneg_int(override, "max_retries", ctx)
        if max_retries > base.max_retries:
            raise DefinitionError(
                f"{ctx}.max_retries: cannot exceed default max_retries ({base.max_retries})"
            )
    else:
        max_retries = base.max_retries

    if "max_duration_sec" in override:
        max_duration_sec = _require_pos_int(override, "max_duration_sec", ctx)
        if max_duration_sec > base.max_duration_sec:
            raise DefinitionError(
                f"{ctx}.max_duration_sec: cannot exceed default max_duration_sec ({base.max_duration_sec})"
            )
    else:
        max_duration_sec = base.max_duration_sec

    if "max_cost_usd" in override:
        max_cost_usd = _require_nonneg_number(override, "max_cost_usd", ctx)
        if max_cost_usd > base.max_cost_usd:
            raise DefinitionError(
                f"{ctx}.max_cost_usd: cannot exceed default max_cost_usd ({base.max_cost_usd})"
            )
    else:
        max_cost_usd = base.max_cost_usd

    if "require_checks" in override:
        override_checks = _dedupe_preserve(
            _validate_str_list(override["require_checks"], f"{ctx}.require_checks")
        )
        for check_name in override_checks:
            if check_name not in valid_checks:
                raise DefinitionError(
                    f"{ctx}.require_checks: unknown check '{check_name}'"
                )
        missing = [c for c in base.require_checks if c not in override_checks]
        if missing:
            raise DefinitionError(
                f"{ctx}.require_checks: cannot remove default required checks: {', '.join(missing)}"
            )
        require_checks = override_checks
    else:
        require_checks = base.require_checks

    return DelegationConfig(
        mode=mode,
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        max_retries=max_retries,
        max_duration_sec=max_duration_sec,
        max_cost_usd=max_cost_usd,
        require_checks=require_checks,
    )


def load_definition_bytes(raw: bytes, source: str = GARDEN_DEF_REL_PATH) -> GardenDefinition:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise DefinitionError(f"{source}: invalid TOML: {error}") from error

    version = data.get("schema_version")
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise DefinitionError(f"schema_version: expected integer {SCHEMA_VERSION}")

    tpl_tbl = _require_table(data, "template")
    template = TemplateConfig(
        name=_require_nonempty_str(tpl_tbl, "name", "template"),
        version=_require_pos_int(tpl_tbl, "version", "template"),
    )

    ch_tbl = _require_table(data, "common_harness")
    ch_name = _optional_str(ch_tbl, "name", "common_harness", "pstack") or "pstack"
    ch_ref = _require_nonempty_str(ch_tbl, "ref", "common_harness")
    if not SHA40_RE.match(ch_ref):
        raise DefinitionError(
            "common_harness.ref: must be a 40-character lowercase hexadecimal Git SHA"
        )
    ch_skills = _dedupe_preserve(
        _validate_str_list(ch_tbl.get("skills", []), "common_harness.skills")
    )
    common_harness = CommonHarnessConfig(name=ch_name, ref=ch_ref, skills=ch_skills)

    th_tbl = _require_table(data, "target_harness")
    th_setup = (
        _validate_str_list(th_tbl["setup"], "target_harness.setup", allow_empty=False)
        if "setup" in th_tbl
        else ()
    )
    target_harness = TargetHarnessConfig(
        name=_require_nonempty_str(th_tbl, "name", "target_harness"),
        version=_require_pos_int(th_tbl, "version", "target_harness"),
        setup=th_setup,
    )

    spec_tbl = _require_table(data, "spec")
    raw_spec_paths = _validate_str_list(
        spec_tbl.get("paths"), "spec.paths", allow_empty=False
    )
    spec_paths = _dedupe_preserve(
        tuple(_validate_rel_path(p, "spec.paths") for p in raw_spec_paths)
    )
    approval_branch = (
        _optional_str(spec_tbl, "approval_branch", "spec", "main") or "main"
    )
    spec = SpecConfig(paths=spec_paths, approval_branch=approval_branch)

    checks_tbl = _require_table(data, "checks")
    if not checks_tbl:
        raise DefinitionError("checks: at least one check must be defined")
    checks: dict[str, CheckConfig] = {}
    for check_name, entry in checks_tbl.items():
        ctx = f"checks.{check_name}"
        if not isinstance(entry, dict):
            raise DefinitionError(f"{ctx}: expected a table")
        kind = _optional_str(entry, "kind", ctx, "check") or "check"
        if kind not in CHECK_KINDS:
            raise DefinitionError(f"{ctx}.kind: must be one of {', '.join(CHECK_KINDS)}")
        command = _validate_str_list(
            entry.get("command"), f"{ctx}.command", allow_empty=False
        )
        timeout_sec = (
            _require_pos_int(entry, "timeout_sec", ctx)
            if "timeout_sec" in entry
            else 300
        )
        covers = _dedupe_preserve(
            _validate_str_list(entry.get("covers", []), f"{ctx}.covers")
        )
        req_ev_raw = _validate_str_list(
            entry.get("required_evidence", []), f"{ctx}.required_evidence"
        )
        required_evidence = _dedupe_preserve(
            tuple(_validate_rel_path(p, f"{ctx}.required_evidence") for p in req_ev_raw)
        )
        inc_codes_raw = entry.get("inconclusive_exit_codes", [75])
        if not isinstance(inc_codes_raw, list) or not all(
            _is_int(c) and c > 0 for c in inc_codes_raw
        ):
            raise DefinitionError(
                f"{ctx}.inconclusive_exit_codes: must be an array of positive integers"
            )
        report_path_raw = _optional_str(entry, "report_path", ctx, None)
        report_path = (
            _validate_rel_path(report_path_raw, f"{ctx}.report_path")
            if report_path_raw is not None
            else None
        )
        status = _optional_str(entry, "status", ctx, "active") or "active"
        if status not in CHECK_STATUSES:
            raise DefinitionError(
                f"{ctx}.status: must be one of {', '.join(CHECK_STATUSES)}"
            )

        tool: str | None = None
        model_path: str | None = None
        invariants: tuple[str, ...] = ()
        if kind == "formal_check":
            tool = _require_nonempty_str(entry, "tool", ctx)
            if tool not in FORMAL_TOOLS:
                raise DefinitionError(
                    f"{ctx}.tool: must be one of {', '.join(FORMAL_TOOLS)}"
                )
            model_path = _validate_rel_path(
                _require_nonempty_str(entry, "model_path", ctx),
                f"{ctx}.model_path",
            )
            invariants = _dedupe_preserve(
                _validate_str_list(
                    entry.get("invariants"), f"{ctx}.invariants", allow_empty=False
                )
            )
            for inv_id in invariants:
                if not INV_ID_RE.match(inv_id):
                    raise DefinitionError(
                        f"{ctx}.invariants: invalid invariant ID '{inv_id}' (expected INV-NNN)"
                    )

        checks[check_name] = CheckConfig(
            name=check_name,
            kind=kind,
            command=command,
            timeout_sec=timeout_sec,
            covers=covers,
            required_evidence=required_evidence,
            inconclusive_exit_codes=tuple(inc_codes_raw),
            report_path=report_path,
            status=status,
            tool=tool,
            model_path=model_path,
            invariants=invariants,
        )

    valid_check_names = set(checks.keys())

    agents: dict[str, AgentConfig] = {}
    if "agents" in data:
        agents_tbl = _require_table(data, "agents")
        for agent_name, entry in agents_tbl.items():
            ctx = f"agents.{agent_name}"
            if not isinstance(entry, dict):
                raise DefinitionError(f"{ctx}: expected a table")
            adapter = _require_nonempty_str(entry, "adapter", ctx)
            if adapter not in AGENT_ADAPTERS:
                raise DefinitionError(
                    f"{ctx}.adapter: must be one of {', '.join(AGENT_ADAPTERS)}"
                )
            command = _validate_str_list(
                entry.get("command"), f"{ctx}.command", allow_empty=False
            )
            timeout_sec = (
                _require_pos_int(entry, "timeout_sec", ctx)
                if "timeout_sec" in entry
                else 600
            )
            max_cost_usd = (
                _require_nonneg_number(entry, "max_cost_usd", ctx)
                if "max_cost_usd" in entry
                else None
            )
            agents[agent_name] = AgentConfig(
                name=agent_name,
                adapter=adapter,
                command=command,
                timeout_sec=timeout_sec,
                max_cost_usd=max_cost_usd,
            )

    forced_forbidden = (*FORBIDDEN_ALWAYS, *spec.paths)
    default_delegation = _parse_delegation(
        _require_table(data, "delegation"),
        "delegation",
        forced_forbidden,
        valid_check_names,
    )

    flows_tbl = _require_table(data, "flows")
    if not flows_tbl:
        raise DefinitionError("flows: at least one flow must be defined")
    flows: dict[str, FlowConfig] = {}
    for flow_name, entry in flows_tbl.items():
        ctx = f"flows.{flow_name}"
        if not isinstance(entry, dict):
            raise DefinitionError(f"{ctx}: expected a table")
        description = _optional_str(entry, "description", ctx, "") or ""
        scope = _optional_str(entry, "scope", ctx, "flow_checks") or "flow_checks"
        if scope not in FLOW_SCOPES:
            raise DefinitionError(
                f"{ctx}.scope: must be one of {', '.join(FLOW_SCOPES)}"
            )
        if "delegation" in entry:
            flow_del_tbl = _require_table(entry, "delegation", ctx)
            eff_delegation = _merge_strict_delegation(
                default_delegation, flow_del_tbl, f"{ctx}.delegation", valid_check_names
            )
        else:
            eff_delegation = default_delegation

        raw_steps = entry.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise DefinitionError(f"{ctx}.steps: must be a non-empty array of tables")

        steps: list[StepConfig] = []
        seen_ids: set[str] = set()
        for idx, step_tbl in enumerate(raw_steps):
            sctx = f"{ctx}.steps[{idx}]"
            if not isinstance(step_tbl, dict):
                raise DefinitionError(f"{sctx}: expected a table")
            step_id = _require_nonempty_str(step_tbl, "id", sctx)
            if step_id in seen_ids:
                raise DefinitionError(f"{sctx}.id: duplicate step id '{step_id}'")
            seen_ids.add(step_id)

            step_type = _require_nonempty_str(step_tbl, "type", sctx)
            if step_type not in STEP_TYPES:
                raise DefinitionError(
                    f"{sctx}.type: must be one of {', '.join(STEP_TYPES)}"
                )

            run_on_demand = step_tbl.get("run_on_demand", False)
            if not isinstance(run_on_demand, bool):
                raise DefinitionError(f"{sctx}.run_on_demand: must be a boolean")

            check_ref: str | None = None
            on_fail = "escalate"
            fix_step: str | None = None
            on_inconclusive = "escalate"
            step_max_retries: int | None = None
            agent_ref: str | None = None
            purpose = "fix"
            skill: str | None = None
            gate_checks: tuple[str, ...] = ()
            report_kind = "auto"

            if step_type == "check":
                check_ref = _require_nonempty_str(step_tbl, "check", sctx)
                if check_ref not in valid_check_names:
                    raise DefinitionError(
                        f"{sctx}.check: unknown check '{check_ref}'"
                    )
                on_fail = _optional_str(step_tbl, "on_fail", sctx, "escalate") or "escalate"
                if on_fail not in ON_FAIL_ACTIONS:
                    raise DefinitionError(
                        f"{sctx}.on_fail: must be one of {', '.join(ON_FAIL_ACTIONS)}"
                    )
                on_inconclusive = (
                    _optional_str(step_tbl, "on_inconclusive", sctx, "escalate")
                    or "escalate"
                )
                if on_inconclusive not in ON_INCONCLUSIVE_ACTIONS:
                    raise DefinitionError(
                        f"{sctx}.on_inconclusive: must be one of {', '.join(ON_INCONCLUSIVE_ACTIONS)}"
                    )
                if "max_retries" in step_tbl:
                    step_max_retries = _require_nonneg_int(step_tbl, "max_retries", sctx)
                    if step_max_retries > eff_delegation.max_retries:
                        raise DefinitionError(
                            f"{sctx}.max_retries: cannot exceed effective delegation max_retries ({eff_delegation.max_retries})"
                        )
                if on_fail == "fix_and_retry":
                    fix_step = _require_nonempty_str(step_tbl, "fix_step", sctx)
                    if eff_delegation.mode == "observe_only":
                        raise DefinitionError(
                            f"{sctx}: on_fail='fix_and_retry' is forbidden when delegation mode is 'observe_only'"
                        )
                elif "fix_step" in step_tbl:
                    fix_step = _require_nonempty_str(step_tbl, "fix_step", sctx)

            elif step_type == "agent":
                agent_ref = _require_nonempty_str(step_tbl, "agent", sctx)
                if agent_ref not in agents:
                    raise DefinitionError(
                        f"{sctx}.agent: unknown agent '{agent_ref}'"
                    )
                purpose = _optional_str(step_tbl, "purpose", sctx, "fix") or "fix"
                skill = _optional_str(step_tbl, "skill", sctx, None)

            elif step_type == "gate":
                gate_checks = _dedupe_preserve(
                    _validate_str_list(
                        step_tbl.get("require_checks"),
                        f"{sctx}.require_checks",
                        allow_empty=False,
                    )
                )
                for c_name in gate_checks:
                    if c_name not in valid_check_names:
                        raise DefinitionError(
                            f"{sctx}.require_checks: unknown check '{c_name}'"
                        )

            elif step_type == "report":
                report_kind = _optional_str(step_tbl, "kind", sctx, "auto") or "auto"
                if report_kind not in REPORT_KINDS:
                    raise DefinitionError(
                        f"{sctx}.kind: must be one of {', '.join(REPORT_KINDS)}"
                    )

            steps.append(
                StepConfig(
                    id=step_id,
                    type=step_type,
                    run_on_demand=run_on_demand,
                    check=check_ref,
                    on_fail=on_fail,
                    fix_step=fix_step,
                    on_inconclusive=on_inconclusive,
                    max_retries=step_max_retries,
                    agent=agent_ref,
                    purpose=purpose,
                    skill=skill,
                    require_checks=gate_checks,
                    kind=report_kind,
                )
            )

        steps_by_id = {s.id: s for s in steps}
        referenced_on_demand: set[str] = set()
        for step in steps:
            if step.fix_step is not None:
                target_step = steps_by_id.get(step.fix_step)
                if target_step is None:
                    raise DefinitionError(
                        f"{ctx}.steps[{step.id}].fix_step: unknown step '{step.fix_step}'"
                    )
                if target_step.type != "agent":
                    raise DefinitionError(
                        f"{ctx}.steps[{step.id}].fix_step: target step '{step.fix_step}' must have type='agent'"
                    )
                referenced_on_demand.add(step.fix_step)

        for step in steps:
            if step.run_on_demand and step.id not in referenced_on_demand:
                raise DefinitionError(
                    f"{ctx}.steps[{step.id}]: run_on_demand=true step is never referenced"
                )

        flows[flow_name] = FlowConfig(
            name=flow_name,
            description=description,
            scope=scope,
            delegation=eff_delegation,
            steps=tuple(steps),
        )

    triggers: list[TriggerConfig] = []
    if "triggers" in data:
        raw_triggers = data["triggers"]
        if not isinstance(raw_triggers, list):
            raise DefinitionError("triggers: must be an array of tables")
        seen_trigger_ids: set[str] = set()
        for idx, trig_tbl in enumerate(raw_triggers):
            tctx = f"triggers[{idx}]"
            if not isinstance(trig_tbl, dict):
                raise DefinitionError(f"{tctx}: expected a table")
            trig_id = _require_nonempty_str(trig_tbl, "id", tctx)
            if trig_id in seen_trigger_ids:
                raise DefinitionError(f"{tctx}.id: duplicate trigger id '{trig_id}'")
            seen_trigger_ids.add(trig_id)
            event = _require_nonempty_str(trig_tbl, "event", tctx)
            flow_ref = _require_nonempty_str(trig_tbl, "flow", tctx)
            if flow_ref not in flows:
                raise DefinitionError(f"{tctx}.flow: unknown flow '{flow_ref}'")
            conditions = trig_tbl.get("conditions", {})
            if not isinstance(conditions, dict):
                raise DefinitionError(f"{tctx}.conditions: expected a table")
            triggers.append(
                TriggerConfig(
                    id=trig_id, event=event, flow=flow_ref, conditions=conditions
                )
            )

    return GardenDefinition(
        schema_version=SCHEMA_VERSION,
        template=template,
        common_harness=common_harness,
        target_harness=target_harness,
        spec=spec,
        delegation=default_delegation,
        checks=checks,
        agents=agents,
        flows=flows,
        triggers=tuple(triggers),
    )


def load_definition(path: Path) -> GardenDefinition:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise DefinitionError(f"{path}: {error}") from error
    return load_definition_bytes(raw, str(path))
