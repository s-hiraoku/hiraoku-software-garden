"""Template detection, garden init, and non-destructive structured garden upgrade."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from garden.resources import nursery_root as _resolve_nursery_root
from garden.definition import GARDEN_DEF_REL_PATH, load_definition
from garden.spec import SpecError, parse_spec_markdown


VERSION_DIR_RE = re.compile(r"^v(\d+)$")
NURSERY_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
TEMPLATE_VERSION_LINE_RE = re.compile(
    r"(?ms)(^\[template\]\s*\n(?:[^\[]*?))^(version\s*=\s*)(\d+)(\s*)$"
)


@dataclass(frozen=True)
class DetectionMatch:
    template_name: str
    version: int
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProposedCheckPlan:
    name: str
    classification: str  # within_existing_spec | requires_spec_change
    missing_criteria: tuple[str, ...]
    config: dict[str, Any]


@dataclass(frozen=True)
class UpgradePlan:
    template_name: str
    from_version: int
    to_version: int
    preserved_custom_checks: tuple[str, ...]
    modified_template_checks: tuple[str, ...]
    proposed_new_checks: tuple[ProposedCheckPlan, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template_name,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "preserved_custom_checks": list(self.preserved_custom_checks),
            "modified_template_checks": list(self.modified_template_checks),
            "proposed_new_checks": [
                {
                    "name": p.name,
                    "classification": p.classification,
                    "missing_criteria": list(p.missing_criteria),
                    "status": "proposed",
                }
                for p in self.proposed_new_checks
            ],
        }


def default_nursery_root() -> Path:
    return _resolve_nursery_root()


def available_template_versions(
    template_name: str, nursery_root: Path | None = None
) -> list[int]:
    n_root = nursery_root or default_nursery_root()
    base = n_root / "templates" / template_name
    if not base.is_dir():
        return []
    versions: list[int] = []
    for child in base.iterdir():
        if child.is_dir():
            m = VERSION_DIR_RE.match(child.name)
            if m and (child / "template.toml").is_file():
                versions.append(int(m.group(1)))
    return sorted(versions)


def load_template_meta(
    template_name: str, version: int, nursery_root: Path | None = None
) -> tuple[Path, dict[str, Any]]:
    n_root = nursery_root or default_nursery_root()
    tdir = n_root / "templates" / template_name / f"v{version}"
    meta_path = tdir / "template.toml"
    if not meta_path.is_file():
        raise ValueError(f"template '{template_name}' v{version} not found at {meta_path}")
    with meta_path.open("rb") as f:
        meta = tomllib.load(f)
    return tdir, meta


def detect_templates(
    target_root: Path, nursery_root: Path | None = None
) -> list[DetectionMatch]:
    n_root = nursery_root or default_nursery_root()
    templates_dir = n_root / "templates"
    if not templates_dir.is_dir():
        return []

    matches: list[DetectionMatch] = []
    for type_dir in sorted(templates_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        versions = available_template_versions(type_dir.name, n_root)
        if not versions:
            continue
        latest_ver = versions[-1]
        _, meta = load_template_meta(type_dir.name, latest_ver, n_root)
        detect_cfg = meta.get("detect") or {}
        files_any = detect_cfg.get("files_any") or []
        keywords_any = detect_cfg.get("keywords_any") or []
        exclude_keywords = detect_cfg.get("exclude_keywords") or []

        score = 0
        reasons: list[str] = []
        matched_files: list[Path] = []
        for rel in files_any:
            candidate = target_root / rel
            if candidate.is_file():
                score += 1
                reasons.append(f"file:{rel}")
                matched_files.append(candidate)

        scan_files = list(matched_files)
        readme = target_root / "README.md"
        if readme.is_file() and readme not in scan_files:
            scan_files.append(readme)

        for sf in scan_files:
            try:
                content = sf.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for kw in keywords_any:
                if kw in content:
                    score += 3
                    reasons.append(f"keyword:{kw}@{sf.name}")
            for ex in exclude_keywords:
                if ex in content:
                    score -= 3

        if score > 0:
            matches.append(
                DetectionMatch(
                    template_name=type_dir.name,
                    version=latest_ver,
                    score=score,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )

    return sorted(matches, key=lambda m: (-m.score, m.template_name))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "initial"


def init_garden(
    target_root: Path,
    *,
    template_type: str | None = None,
    version: int | None = None,
    force: bool = False,
    nursery_ref: str = "main",
    nursery_root: Path | None = None,
) -> dict[str, Any]:
    if not NURSERY_REF_RE.match(nursery_ref) or ".." in nursery_ref:
        raise ValueError(f"invalid nursery_ref {nursery_ref!r}")
    n_root = nursery_root or default_nursery_root()
    target = target_root.resolve()
    if not target.is_dir():
        raise ValueError(f"target directory does not exist: {target}")

    detected_reasons: tuple[str, ...] = ()
    if template_type is None:
        matches = detect_templates(target, n_root)
        if not matches:
            raise ValueError(
                "could not detect repository type automatically; pass --type (e.g., --type cli)"
            )
        chosen = matches[0]
        template_type = chosen.template_name
        version = version or chosen.version
        detected_reasons = chosen.reasons
    else:
        versions = available_template_versions(template_type, n_root)
        if not versions:
            raise ValueError(f"unknown template type '{template_type}'")
        version = version or versions[-1]

    tdir, meta = load_template_meta(template_type, version, n_root)

    pstack_ref = (meta.get("common_harness") or {}).get(
        "ref", "7a583913eb1b8d1a0ee76753d1a2cf072e7b54f9"
    )
    repo_name = target.name
    spec_id = _slugify(repo_name)

    replacements = {
        "repo_name": repo_name,
        "spec_id": spec_id,
        "pstack_ref": pstack_ref,
    }

    planned_files: list[tuple[Path, str]] = [
        (
            target / GARDEN_DEF_REL_PATH,
            (tdir / "garden.toml.tmpl").read_text(encoding="utf-8").format(**replacements),
        ),
        (
            target / ".garden" / ".gitignore",
            "runs/\nworktrees/\n",
        ),
        (
            target / "docs" / "specs" / f"{spec_id}.md",
            (tdir / "spec.md.tmpl").read_text(encoding="utf-8").format(**replacements),
        ),
        (
            target / ".github" / "workflows" / "garden.yml",
            (tdir / "workflow.yml.tmpl")
            .read_text(encoding="utf-8")
            .replace("__NURSERY_REF__", nursery_ref),
        ),
    ]

    if not force:
        existing = [str(p.relative_to(target)) for p, _ in planned_files if p.exists()]
        if existing:
            raise ValueError(
                f"refusing to overwrite existing file(s) without --force: {', '.join(existing)}"
            )

    created: list[str] = []
    for path, content in planned_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(str(path.relative_to(target)))

    return {
        "target": str(target),
        "template": template_type,
        "version": version,
        "nursery_ref": nursery_ref,
        "detection_reasons": list(detected_reasons),
        "created_files": created,
    }


def _collect_existing_criteria_ids(target_root: Path, spec_paths: tuple[str, ...]) -> set[str]:
    ids: set[str] = set()
    for rel in spec_paths:
        sp = target_root / rel
        if not sp.is_file():
            continue
        try:
            doc = parse_spec_markdown(sp.read_text(encoding="utf-8"), rel)
            ids.update(doc.all_criteria_ids)
        except (OSError, SpecError):
            continue
    return ids


def plan_upgrade(
    target_root: Path,
    *,
    to_version: int | None = None,
    nursery_root: Path | None = None,
) -> UpgradePlan:
    n_root = nursery_root or default_nursery_root()
    target = target_root.resolve()
    def_path = target / GARDEN_DEF_REL_PATH
    definition = load_definition(def_path)

    tname = definition.template.name
    from_ver = definition.template.version
    versions = available_template_versions(tname, n_root)
    if not versions:
        raise ValueError(f"no templates found for '{tname}'")
    target_ver = to_version if to_version is not None else versions[-1]
    if target_ver not in versions:
        raise ValueError(f"template '{tname}' v{target_ver} does not exist")
    if target_ver < from_ver:
        raise ValueError(f"cannot downgrade template from v{from_ver} to v{target_ver}")

    _, old_meta = load_template_meta(tname, from_ver, n_root)
    _, new_meta = load_template_meta(tname, target_ver, n_root)

    old_defaults = (old_meta.get("defaults") or {}).get("checks") or {}
    new_defaults = (new_meta.get("defaults") or {}).get("checks") or {}

    preserved_custom: list[str] = []
    modified_template: list[str] = []
    for cname, cfg in definition.checks.items():
        if cname not in old_defaults:
            preserved_custom.append(cname)
        else:
            old_cmd = tuple(old_defaults[cname].get("command") or ())
            if cfg.command != old_cmd:
                modified_template.append(cname)

    existing_criteria = _collect_existing_criteria_ids(target, definition.spec.paths)

    proposed_new: list[ProposedCheckPlan] = []
    for cname, new_cfg in new_defaults.items():
        if cname in definition.checks:
            continue
        covers = list(new_cfg.get("covers") or [])
        missing = tuple(cid for cid in covers if cid not in existing_criteria)
        classification = (
            "requires_spec_change" if missing else "within_existing_spec"
        )
        proposed_new.append(
            ProposedCheckPlan(
                name=cname,
                classification=classification,
                missing_criteria=missing,
                config=dict(new_cfg),
            )
        )

    return UpgradePlan(
        template_name=tname,
        from_version=from_ver,
        to_version=target_ver,
        preserved_custom_checks=tuple(sorted(preserved_custom)),
        modified_template_checks=tuple(sorted(modified_template)),
        proposed_new_checks=tuple(proposed_new),
    )


def _render_proposed_check_toml(plan_item: ProposedCheckPlan) -> str:
    cfg = plan_item.config
    existing_covers = [
        cid for cid in (cfg.get("covers") or []) if cid not in plan_item.missing_criteria
    ]
    lines = [
        "",
        f"# Proposed by template upgrade ({plan_item.classification})",
    ]
    if plan_item.missing_criteria:
        lines.append(
            f"# Spec change proposal required for: {', '.join(plan_item.missing_criteria)}"
        )
    lines.append(f"[checks.{plan_item.name}]")
    lines.append(f'kind = {json.dumps(cfg.get("kind", "check"))}')
    lines.append(f'command = {json.dumps(cfg.get("command", []), ensure_ascii=False)}')
    if "timeout_sec" in cfg:
        lines.append(f'timeout_sec = {int(cfg["timeout_sec"])}')
    lines.append(f"covers = {json.dumps(existing_covers, ensure_ascii=False)}")
    if "inconclusive_exit_codes" in cfg:
        lines.append(
            f'inconclusive_exit_codes = {json.dumps(cfg["inconclusive_exit_codes"])}'
        )
    if cfg.get("kind") == "formal_check":
        lines.append(f'tool = {json.dumps(cfg["tool"])}')
        lines.append(f'model_path = {json.dumps(cfg["model_path"])}')
        lines.append(f'invariants = {json.dumps(cfg["invariants"])}')
    lines.append('status = "proposed"')
    return "\n".join(lines) + "\n"


def apply_upgrade(
    target_root: Path,
    *,
    to_version: int | None = None,
    nursery_root: Path | None = None,
) -> UpgradePlan:
    plan = plan_upgrade(target_root, to_version=to_version, nursery_root=nursery_root)
    if plan.from_version == plan.to_version and not plan.proposed_new_checks:
        return plan

    def_path = target_root.resolve() / GARDEN_DEF_REL_PATH
    text = def_path.read_text(encoding="utf-8")

    updated, count = TEMPLATE_VERSION_LINE_RE.subn(
        rf"\g<1>\g<2>{plan.to_version}\g<4>", text, count=1
    )
    if count != 1:
        raise ValueError(f"{def_path}: could not locate [template].version to update")

    for item in plan.proposed_new_checks:
        updated += _render_proposed_check_toml(item)

    # Validate before persisting to guarantee we never write an invalid definition
    from garden.definition import load_definition_bytes

    load_definition_bytes(updated.encode("utf-8"), str(def_path))
    def_path.write_text(updated, encoding="utf-8")
    return plan
