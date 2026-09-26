"""Promote or reject a pending policy from a multi-task replay evidence file.

Usage:
    python scripts/evaluate_replay.py evidence.json

The JSON file contains ``suite_id``, ``task_key``, ``candidate_version`` and a
``cases`` array. Each case contains ``task_id``, ``split`` (evolve, regression,
or holdout), ``baseline_metrics`` and ``candidate_metrics``. Optional token
counts enable the cost rule. Holdout cases are stored but never select policy.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

from recursive_computer_use.evolution import (
    ReplaySuiteRepository,
    replay_case_from_document,
)
from recursive_computer_use.store import DEFAULT_DB, DEFAULT_URI


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--mongodb-uri")
    parser.add_argument("--mongodb-db")
    args = parser.parse_args()

    load_dotenv()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    cases = [replay_case_from_document(item) for item in payload["cases"]]
    client = MongoClient(args.mongodb_uri or os.environ.get("MONGODB_URI", DEFAULT_URI))
    try:
        repository = ReplaySuiteRepository(
            client[args.mongodb_db or os.environ.get("MONGODB_DB", DEFAULT_DB)]
        )
        result = repository.evaluate_and_record(
            suite_id=payload["suite_id"],
            task_key=payload["task_key"],
            candidate_version=int(payload["candidate_version"]),
            cases=cases,
        )
    finally:
        client.close()

    print(json.dumps(as_output(result), indent=2))


def as_output(result: object) -> dict[str, object]:
    return {
        "decision": "accepted" if result.accepted else "rejected",
        "reasons": list(result.reasons),
        "evolve_tasks": result.evolve_tasks,
        "regression_tasks": result.regression_tasks,
        "holdout_tasks_ignored": result.holdout_tasks_ignored,
        "success_gain": result.success_gain,
        "token_delta": result.token_delta,
    }


if __name__ == "__main__":
    main()
