#!/usr/bin/env python3
"""Prepare one arXiv report; optionally push its saved brief to an explicit chat."""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from reportagent.services.one_report import prepare_report, push_artifact, save_artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--arxiv-id")
    source.add_argument("--artifact", type=Path, help="Reuse an already prepared JSON, without another model call")
    parser.add_argument("--chat-id", help="Explicit destination; omit to prepare without sending")
    parser.add_argument("--force", action="store_true", help="Re-analyze even when cached; preserve previous delivery receipts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.artifact:
        path = args.artifact
        artifact = json.loads(path.read_text(encoding="utf-8"))
    else:
        path, artifact = asyncio.run(prepare_report(args.arxiv_id, force=args.force))
    if args.chat_id:
        receipt = push_artifact(artifact, args.chat_id)
        save_artifact(path, artifact)
        print("Feishu delivered:", receipt["message_id"])
    print("Artifact:", path)


if __name__ == "__main__":
    main()
