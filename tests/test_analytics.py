from __future__ import annotations

import unittest
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

from recursive_computer_use.evolution.analytics import (
    learning_curve,
    policy_lineage,
    record_metrics,
)
from recursive_computer_use.evolution.models import EvaluationMetrics, EvaluationRecord, HarnessPolicy


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class FakeCollection:
    def __init__(self):
        self.inserted = None
        self.pipeline = None

    def insert_one(self, document):
        self.inserted = document
        return "inserted"

    def aggregate(self, pipeline):
        self.pipeline = pipeline
        return [{"policy_version": 1}]


class FakeDB:
    def __init__(self):
        self.collections = {name: FakeCollection() for name in ("run_metrics", "policies")}

    def __getitem__(self, name):
        return self.collections[name]


class AnalyticsTests(unittest.TestCase):
    def test_records_timeseries_contract(self):
        db = FakeDB()
        result = record_metrics(
            db,
            "task",
            3,
            {"success_rate": 0.75, "action_count": 8, "policy_violations": 1},
        )
        self.assertEqual(result, "inserted")
        document = db["run_metrics"].inserted
        self.assertEqual(document["meta"], {"task_key": "task", "policy_version": 3})
        self.assertEqual(document["success_rate"], 0.75)
        self.assertIn("ts", document)

    def test_queries_aggregate_and_graph_lookup(self):
        db = FakeDB()
        self.assertEqual(learning_curve(db, "task"), [{"policy_version": 1}])
        self.assertIn("$lookup", db["run_metrics"].pipeline[2])
        self.assertEqual(policy_lineage(db, "task"), [{"policy_version": 1}])
        self.assertIn("$graphLookup", db["policies"].pipeline[3])

    def test_rejects_unknown_measurement(self):
        with self.assertRaisesRegex(ValueError, "unsupported run metric"):
            record_metrics(FakeDB(), "task", 1, {"secret": 1})


@unittest.skipUnless(os.environ.get("MONGODB_URI"), "set MONGODB_URI to run Atlas integration")
class AtlasAnalyticsTests(unittest.TestCase):
    def test_seeded_learning_curve_and_lineage(self):
        client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
        db = client["rcu_test_codex"]
        task_key = f"codex-analytics-{uuid.uuid4().hex}"
        try:
            if "run_metrics" not in db.list_collection_names():
                db.create_collection(
                    "run_metrics",
                    timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"},
                )
            db["policies"].insert_many(
                [
                    HarnessPolicy(
                        task_key=task_key,
                        version=1,
                        parent_version=None,
                        status="accepted",
                        rules=("Inspect before acting.",),
                        limits={},
                        reason="Analytics seed.",
                    ).to_document(),
                    HarnessPolicy(
                        task_key=task_key,
                        version=2,
                        parent_version=1,
                        status="accepted",
                        rules=("Inspect before acting.", "Verify the field."),
                        limits={},
                        reason="Analytics seed.",
                    ).to_document(),
                ]
            )
            record_metrics(db, task_key, 1, {"success_rate": 0.25, "action_count": 12})
            record_metrics(db, task_key, 2, {"success_rate": 0.75, "action_count": 8})
            db["evaluations"].insert_one(
                EvaluationRecord(
                    task_key=task_key,
                    baseline_policy_version=1,
                    candidate_policy_version=2,
                    baseline_metrics=EvaluationMetrics(success_rate=0.25, action_count=12),
                    candidate_metrics=EvaluationMetrics(success_rate=0.75, action_count=8),
                    decision="accepted",
                    reason="Analytics seed.",
                ).to_document()
            )

            curve = learning_curve(db, task_key)
            self.assertEqual([row["policy_version"] for row in curve], [1, 2])
            self.assertEqual(curve[1]["decision"], "accepted")
            self.assertEqual(curve[1]["mean_action_count"], 8)
            self.assertEqual([row["version"] for row in policy_lineage(db, task_key)], [1, 2])
        finally:
            db["run_metrics"].delete_many({"meta.task_key": task_key})
            db["evaluations"].delete_many({"task_key": task_key})
            db["policies"].delete_many({"task_key": task_key})
            client.close()


if __name__ == "__main__":
    unittest.main()
