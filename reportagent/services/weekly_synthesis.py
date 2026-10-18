"""Condense saved assessments into short conclusions with individual references."""

import asyncio
import json
from collections import defaultdict

from reportagent.llm.client import LLMClient
from reportagent.services.paper_scoring import load_enabled_directions
from reportagent.services.weekly_briefing import research_papers
from reportagent.skills.registry import skill_resource


def _validate_summaries(result, expected):
    groups = result.get("directions") if isinstance(result, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Missing direction summaries")
    summaries = {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid direction summary")
        direction = group.get("direction")
        if (
            not isinstance(direction, str)
            or direction not in expected
            or direction in summaries
        ):
            raise ValueError("Unknown or repeated direction")
        updates = group.get("updates")
        if not isinstance(updates, list) or len(updates) != len(expected[direction]):
            raise ValueError("Each paper requires exactly one short conclusion")
        covered = set()
        normalized = []
        for update in updates:
            if not isinstance(update, dict):
                raise ValueError("Invalid research update")
            text, ids = update.get("text"), update.get("paper_ids")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Empty summary")
            text = " ".join(text.split())
            if (
                not isinstance(ids, list)
                or len(ids) != 1
                or any(type(rid) is not int for rid in ids)
            ):
                raise ValueError("Each conclusion must reference one paper")
            if not set(ids) <= expected[direction] or covered.intersection(ids):
                raise ValueError("Unknown or repeated paper references")
            covered.update(ids)
            normalized.append({"text": text, "paper_ids": list(ids)})
        if covered != expected[direction]:
            raise ValueError("Summary omitted papers")
        summaries[direction] = normalized
    if set(summaries) != set(expected):
        raise ValueError("Summary omitted directions")
    return summaries


async def synthesize_weekly(papers):
    directions = load_enabled_directions()
    grouped = defaultdict(list)
    for paper in research_papers(papers):
        evaluation = paper["evaluation"]
        grouped[paper.get("selection_direction") or "other"].append(
            {
                "id": paper["id"],
                "title": paper["title"],
                "research_question": evaluation.get("research_question"),
                "method": evaluation.get("method"),
                "findings": evaluation.get("findings") or [],
                "limitations": evaluation.get("limitations") or [],
                "critical_concerns": evaluation.get("critical_concerns") or [],
            }
        )
    if not grouped:
        return {}
    payload = {
        "directions": [
            {
                "direction": direction,
                "name": directions.get(direction, {}).get("name") or direction,
                "papers": rows,
            }
            for direction, rows in sorted(grouped.items())
        ]
    }
    client = LLMClient()
    result = await asyncio.wait_for(
        client.chat_json(
            [
                {
                    "role": "system",
                    "content": skill_resource(
                        "weekly-report", "synthesis.md"
                    ).read_text(encoding="utf-8"),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=max(6000, sum(len(rows) for rows in grouped.values()) * 200),
        ),
        timeout=120,
    )
    return _validate_summaries(
        result, {key: {p["id"] for p in rows} for key, rows in grouped.items()}
    )
