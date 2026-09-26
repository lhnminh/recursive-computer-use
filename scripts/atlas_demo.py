"""Run a disposable end-to-end demonstration against MongoDB Atlas."""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError, WriteError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recursive_computer_use.evolution.analytics import learning_curve, policy_lineage, record_metrics
from recursive_computer_use.evolution.feed import PolicyFeed
from recursive_computer_use.evolution.memory import ExperienceMemory
from recursive_computer_use.evolution.models import EvaluationMetrics, EvaluationRecord, HarnessPolicy
from recursive_computer_use.evolution.policy import PolicyRepository
from recursive_computer_use.schema import COLLECTIONS, ensure_schema
from recursive_computer_use.store import DEFAULT_URI


DB_NAME = "rcu_demo"
TASK_KEY = "atlas-demo"


def _wait_for_indexes(collection: Any, names: set[str], timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        indexes = {item["name"]: item for item in collection.list_search_indexes()}
        if all(indexes.get(name, {}).get("status") == "READY" for name in names):
            return
        failed = [
            name for name in names
            if indexes.get(name, {}).get("status") in {"FAILED", "DOES_NOT_EXIST"}
        ]
        if failed:
            raise RuntimeError(f"search indexes failed: {', '.join(failed)}")
        time.sleep(3)
    raise TimeoutError(f"search indexes not READY after {timeout:.0f}s")


def _accepted_policy(version: int, parent: int | None) -> HarnessPolicy:
    return HarnessPolicy(
        task_key=TASK_KEY,
        version=version,
        parent_version=parent,
        status="accepted",
        rules=("Inspect the screen before acting.",),
        limits={"action_budget": 20, "retry_limit": 1},
        reason=f"Atlas demo policy {version}.",
    )


def _seed(db: Any) -> None:
    db["experiences"].insert_many(
        [
            {
                "task_key": TASK_KEY,
                "outcome": "failure",
                "failure_tags": ["wrong_field"],
                "summary": "The form submission went to the wrong input.",
                "lesson": "Click the Submit button only after checking the form.",
                "policy_version": 1,
                "metrics": {"success_rate": 0.3, "action_count": 12},
                "created_at": datetime.now(timezone.utc),
            },
            {
                "task_key": TASK_KEY,
                "outcome": "failure",
                "failure_tags": ["wrong_click"],
                "summary": "A similar form control was selected during submission.",
                "lesson": "Before submitting, check that the action control is the intended one.",
                "policy_version": 3,
                "metrics": {"success_rate": 0.4, "action_count": 11},
                "created_at": datetime.now(timezone.utc),
            },
            {
                "task_key": TASK_KEY,
                "outcome": "failure",
                "failure_tags": ["wrong_click"],
                "summary": "A nearby control was clicked by mistake.",
                "lesson": "Check the target label before clicking the nearby button.",
                "policy_version": 2,
                "metrics": {"success_rate": 0.7, "action_count": 9},
                "created_at": datetime.now(timezone.utc),
            },
        ]
    )
    db["policies"].insert_many(
        [_accepted_policy(1, None).to_document(), _accepted_policy(2, 1).to_document()]
    )
    metrics = EvaluationMetrics(success_rate=0.3, action_count=12)
    db["evaluations"].insert_one(
        EvaluationRecord(
            task_key=TASK_KEY,
            baseline_policy_version=1,
            candidate_policy_version=2,
            baseline_metrics=metrics,
            candidate_metrics=EvaluationMetrics(success_rate=0.7, action_count=9),
            decision="accepted",
            reason="Higher verified success.",
        ).to_document()
    )
    record_metrics(db, TASK_KEY, 1, {"success_rate": 0.3, "action_count": 12})
    record_metrics(db, TASK_KEY, 2, {"success_rate": 0.7, "action_count": 9})


def _semantic_search(db: Any, query: str) -> list[dict[str, Any]]:
    return list(
        db["experiences"].aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": "experience_auto",
                        "path": "lesson",
                        "query": {"text": query},
                        "model": "voyage-4",
                        "numCandidates": 20,
                        "limit": 5,
                        "filter": {"task_key": TASK_KEY},
                    }
                },
                {"$project": {"_id": 0, "lesson": 1, "policy_version": 1, "score": {"$meta": "vectorSearchScore"}}},
            ]
        )
    )


def _rank_fusion(db: Any, query: str) -> list[dict[str, Any]]:
    return ExperienceMemory(db["experiences"]).ranked_lessons(
        TASK_KEY, query, limit=5
    )


def _show(title: str, results: list[dict[str, Any]]) -> None:
    print(f"\n{title}")
    for result in results:
        print(f"  {result}")
    if not results:
        print("  No results")


def run() -> None:
    load_dotenv(ROOT / ".env")
    client = MongoClient(os.environ.get("MONGODB_URI", DEFAULT_URI), serverSelectionTimeoutMS=5000)
    feed: PolicyFeed | None = None
    try:
        client.admin.command("ping")
        client.drop_database(DB_NAME)
        db = client[DB_NAME]
        ensure_schema(db)
        _seed(db)

        print("\n1. Voyage autoEmbed semantic retrieval")
        _wait_for_indexes(db["experiences"], {"experience_auto", "experience_text"})
        semantic = _semantic_search(db, "press the confirm control")
        _show("Meaning-based lesson matches for 'press the confirm control':", semantic)

        print("\n2. $rankFusion combines semantic, keyword, and accepted-policy usefulness")
        fused = _rank_fusion(db, "verify the field before submitting")
        _show("Hybrid lesson ranking:", fused)

        print("\n3. Change stream delivers an accepted policy to a second agent")
        delivered = threading.Event()
        received: list[dict[str, Any]] = []

        def on_policy(policy: dict[str, Any]) -> None:
            received.append(dict(policy))
            delivered.set()

        feed = PolicyFeed(db, "demo-second-agent")
        feed.watch(TASK_KEY, on_policy)
        time.sleep(0.5)
        candidate = HarnessPolicy(
            task_key=TASK_KEY,
            version=3,
            parent_version=2,
            status="candidate",
            rules=("Inspect the screen before acting.", "Verify the field before typing."),
            limits={"action_budget": 18, "retry_limit": 1},
            reason="Feed demonstration candidate.",
        )
        db["policies"].insert_one(candidate.to_document())
        evaluation_metrics = EvaluationMetrics(success_rate=0.9, action_count=7)
        PolicyRepository(db["policies"], db["evaluations"]).record_evaluation(
            EvaluationRecord(
                task_key=TASK_KEY,
                baseline_policy_version=2,
                candidate_policy_version=3,
                baseline_metrics=EvaluationMetrics(success_rate=0.7, action_count=9),
                candidate_metrics=evaluation_metrics,
                decision="accepted",
                reason="Feed demonstration accepted.",
            )
        )
        if not delivered.wait(10):
            raise TimeoutError("change stream did not deliver the accepted policy")
        _show("Policy received by second agent:", received)
        feed.stop()
        feed = None

        print("\n4. $jsonSchema rejects an invalid policy status")
        db.command(
            "collMod",
            "policies",
            validator=COLLECTIONS["policies"]["validator"],
            validationLevel="strict",
            validationAction="error",
        )
        bad_policy = dict(_accepted_policy(99, 3).to_document(), status="magic")
        try:
            db["policies"].insert_one(bad_policy)
        except WriteError as exc:
            print(f"  Rejected as expected: {exc.__class__.__name__}")
        else:
            raise AssertionError("invalid policy status was accepted")

        print("\n5. Learning curve and policy lineage")
        _show("Learning curve:", learning_curve(db, TASK_KEY))
        _show("Accepted policy lineage:", policy_lineage(db, TASK_KEY))
    finally:
        if feed is not None:
            feed.stop()
        client.drop_database(DB_NAME)
        client.close()
        print(f"\nCleaned up scratch database {DB_NAME}.")


if __name__ == "__main__":
    try:
        run()
    except (PyMongoError, RuntimeError, TimeoutError, AssertionError) as exc:
        print(f"Atlas demo failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
