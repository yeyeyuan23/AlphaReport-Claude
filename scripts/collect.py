"""Run the scheduled collection workflow once, without sending reports."""

import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from reportagent.services.scheduled_collection import run_scheduled_collection

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run_scheduled_collection()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
