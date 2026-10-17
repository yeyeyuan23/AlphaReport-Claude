"""Prepare an evidence-backed reproduction checklist for one stored report."""

import argparse
import asyncio
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="May call the model; does not persist"
    )
    args = parser.parse_args()
    from reportagent.db.engine import init_db, get_session_factory
    from reportagent.services.reproduction import prepare_report

    init_db()
    with get_session_factory()() as session:
        result = asyncio.run(
            prepare_report(
                args.report_id, session, force=args.force, persist=not args.dry_run
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
