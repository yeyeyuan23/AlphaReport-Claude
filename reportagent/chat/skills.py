"""Compatibility imports; the skill implementation lives in reportagent.skills."""

from reportagent.skills.registry import (
    delete_user_skill,
    format_skills_context,
    get_skill,
    load_all_skills,
    save_user_skill,
)

__all__ = [
    "delete_user_skill",
    "format_skills_context",
    "get_skill",
    "load_all_skills",
    "save_user_skill",
]
