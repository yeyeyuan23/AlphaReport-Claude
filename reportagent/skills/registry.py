"""Canonical skill registry for every Agent adapter and the CLI."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from reportagent.utils.config import PROJECT_ROOT, get_config

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return PROJECT_ROOT / get_config("skills", "builtin_dir", default="skills")


def _user_skills_dir() -> Path:
    legacy = get_config("chat", "user_skills_dir", default="data/user_skills")
    path = PROJECT_ROOT / get_config("skills", "user_dir", default=legacy)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
        if path.name == "SKILL.md":
            parts = text.split("---", 2)
            if len(parts) != 3 or parts[0].strip():
                raise ValueError("Expected YAML frontmatter")
            data = yaml.safe_load(parts[1])
            data["prompt_template"] = parts[2].strip()
            if (data.get("metadata") or {}).get("default_params"):
                data["default_params"] = data["metadata"]["default_params"]
        else:
            data = yaml.safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise ValueError("Skill must define a name")
        return data
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        logger.warning("Cannot load skill %s: %s", path, error)
        return None


def load_all_skills() -> list[dict[str, Any]]:
    skills = []
    for directory, source in [(_skills_dir(), "builtin"), (_user_skills_dir(), "user")]:
        paths = sorted(directory.glob("*/SKILL.md"))
        if source == "user":
            paths += sorted(directory.glob("*.yaml"))
        for path in paths:
            data = _read(path)
            if data:
                data.update(_source=source, _file=path.parent.name if path.name == "SKILL.md" else path.stem,
                            _path=str(path.resolve()))
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


def save_user_skill(name: str, description: str, prompt_template: str,
                    default_params: dict[str, Any] | None = None) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    slug = (slug[:48] or "skill") + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
    path = _user_skills_dir() / f"{slug}.yaml"
    data = {"name": name, "description": description, "prompt_template": prompt_template}
    if default_params:
        data["default_params"] = default_params
    # Update old user YAMLs in place instead of leaving two definitions for one name.
    for existing in _user_skills_dir().glob("*.yaml"):
        old = _read(existing)
        if old and old["name"] == name:
            path = existing
            break
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


def delete_user_skill(name: str) -> bool:
    deleted = False
    for path in _user_skills_dir().glob("*.yaml"):
        data = _read(path)
        if data and data["name"] == name:
            path.unlink()
            deleted = True
    return deleted


def format_skills_context(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return ""
    parts = ["\n## Shared skills — use these workflows and their referenced services"]
    for skill in skills:
        if skill.get("default_params"):
            parts.append("Defaults: " + yaml.safe_dump(skill["default_params"], allow_unicode=True).strip())
        parts.append(f"\n### {skill['name']}\n{skill.get('description', '')}\nSource: {skill.get('_path', '')}\n{skill.get('prompt_template', '').strip()}")
    return "\n".join(parts)
