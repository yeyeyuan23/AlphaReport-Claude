"""Canonical skill registry for every Agent adapter and the CLI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from reportagent.utils.config import PROJECT_ROOT, get_config

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return PROJECT_ROOT / get_config("skills", "builtin_dir", default="skills")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("Expected YAML frontmatter")
        data = yaml.safe_load(parts[1])
        if not isinstance(data, dict):
            raise ValueError("Expected task metadata")
        data["prompt_template"] = parts[2].strip()
        if (data.get("metadata") or {}).get("default_params"):
            data["default_params"] = data["metadata"]["default_params"]
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise ValueError("Skill must define a name")
        return data
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        logger.warning("Cannot load skill %s: %s", path, error)
        return None


def load_all_skills() -> list[dict[str, Any]]:
    skills = []
    for path in sorted(_skills_dir().glob("*/SKILL.md")):
        data = _read(path)
        if data:
            data.update(
                _source="builtin",
                _file=path.parent.name,
                _path=str(path.resolve()),
            )
            skills.append(data)
    return skills


def get_skill(name: str) -> dict[str, Any] | None:
    for skill in load_all_skills():
        aliases = (skill.get("metadata") or {}).get("aliases", [])
        if name == skill["name"] or name in aliases:
            return skill
    return None


def skill_resource(name: str, filename: str) -> Path:
    skill = get_skill(name)
    if not skill:
        raise ValueError(f"Unknown skill: {name}")
    directory = Path(skill["_path"]).parent.resolve()
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Skill resource must stay inside its skill directory")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def format_skills_context(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return ""
    parts = ["\n## Shared skills — use these workflows and their referenced services"]
    for skill in skills:
        if skill.get("default_params"):
            parts.append(
                "Defaults: "
                + yaml.safe_dump(skill["default_params"], allow_unicode=True).strip()
            )
        parts.append(
            f"\n### {skill['name']}\n{skill.get('description', '')}\nSource: {skill.get('_path', '')}\n{skill.get('prompt_template', '').strip()}"
        )
    return "\n".join(parts)
