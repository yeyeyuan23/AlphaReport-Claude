"""Generate scoped candidate research hypotheses from the report library."""

import argparse
import asyncio


def main():
    from reportagent.services.research_ideation import ANGLES, generate_ideas

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--angle", choices=sorted(ANGLES), default="general")
    parser.add_argument(
        "--verify-external",
        action="store_true",
        help="Search external leads with configured Tavily; does not establish novelty",
    )
    args = parser.parse_args()
    print(
        asyncio.run(
            generate_ideas(args.topic, args.angle, verify_external=args.verify_external)
        )
    )


if __name__ == "__main__":
    main()
