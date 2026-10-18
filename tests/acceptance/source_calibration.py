"""Explicit read-only, source-blind calibration; real models require --run-model."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from reportagent.services.report_quality import evaluate_paper
from reportagent.utils.config import get_db_path


def blind_cases(papers):
    cases, key = [], {}
    for index, paper in enumerate(papers, 1):
        case_id = f"B{index:03d}"
        cases.append(
            {
                "case_id": case_id,
                "paper": {
                    name: paper.get(name)
                    for name in ("title", "abstract", "full_text", "has_full_text")
                },
            }
        )
        key[case_id] = {
            name: paper.get(name) for name in ("id", "source", "material_type")
        }
    return cases, key


def validate_labels(labels, cases):
    if not isinstance(labels, dict) or set(labels) != {
        case["case_id"] for case in cases
    }:
        raise ValueError("Human labels must cover every blind case exactly once")
    for label in labels.values():
        if not isinstance(label, dict) or label.get("quality") not in {
            "strong",
            "sound",
            "weak",
            "unknown",
        }:
            raise ValueError("Human labels need a quality level")
        if not isinstance(label.get("basis"), str) or not label["basis"].strip():
            raise ValueError("Human labels need an independent evidence basis")


async def evaluate_cases(cases, labels, llm):
    validate_labels(labels, cases)
    results = {}
    for case in cases:
        case_id = case["case_id"]
        result = await evaluate_paper(case["paper"], llm)
        results[case_id] = {
            "evaluation": result,
            "human_label": labels[case_id],
            "quality_agrees": result["quality"] == labels[case_id]["quality"],
        }
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-ids", required=True, help="Explicit comma-separated library IDs"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--labels",
        type=Path,
        help="Human labels keyed by B001 etc., completed before model output",
    )
    parser.add_argument(
        "--run-model",
        action="store_true",
        help="Spend model quota, without writing the library or sending messages",
    )
    args = parser.parse_args()
    ids = list(dict.fromkeys(int(value) for value in args.report_ids.split(",")))
    if not ids or any(rid < 1 for rid in ids):
        parser.error("Positive report IDs are required")
    if args.run_model and not args.labels:
        parser.error("--run-model requires prior independent --labels")
    with sqlite3.connect(get_db_path().resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        papers = [
            db.execute(
                "SELECT id,title,abstract,full_text,has_full_text,source,material_type FROM reports WHERE id=?",
                (rid,),
            ).fetchone()
            for rid in ids
        ]
    if any(paper is None for paper in papers):
        parser.error("A requested report does not exist")
    cases, key = blind_cases([dict(paper) for paper in papers])
    labels = (
        json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else None
    )
    if labels is not None:
        validate_labels(labels, cases)
    args.output.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        (args.output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save("blind-cases.json", cases)
    save("private-source-key.json", key)
    if args.run_model:
        from reportagent.llm.client import LLMClient

        client = LLMClient(thinking="disabled")
        try:
            results = await evaluate_cases(cases, labels, client)
        finally:
            await client._client.close()
        save("model-results.json", results)
    print(
        f"Prepared {len(cases)} blind cases in {args.output}; model_called={args.run_model}"
    )


if __name__ == "__main__":
    asyncio.run(main())
