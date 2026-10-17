"""Inspect the same skill library used by all Agent entrypoints."""

import argparse
import json
from reportagent.skills.registry import get_skill, load_all_skills


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "show"])
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    if args.action == "list":
        print(
            json.dumps(
                [
                    {"name": s["name"], "description": s.get("description", "")}
                    for s in load_all_skills()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        skill = get_skill(args.name or "")
        if not skill:
            parser.error("Unknown skill name")
        print(skill["prompt_template"])


if __name__ == "__main__":
    main()
