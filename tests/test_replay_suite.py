from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace

from recursive_computer_use.evolution import (
    EvaluationMetrics,
    HarnessPolicy,
    ReplayCase,
    ReplaySuiteRepository,
)


class Collection:
    def __init__(self, documents=()):
        self.documents = [deepcopy(item) for item in documents]

    def find_one(self, query, **kwargs):
        for item in self.documents:
            if all(item.get(key) == value for key, value in query.items()):
                return deepcopy(item)
        return None

    def insert_one(self, document, **kwargs):
        item = deepcopy(document)
        item["_id"] = len(self.documents) + 1
        self.documents.append(item)
        return SimpleNamespace(inserted_id=item["_id"])

    def update_one(self, query, update, **kwargs):
        for item in self.documents:
            if all(item.get(key) == value for key, value in query.items()):
                item.update(update["$set"])
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def delete_one(self, query):
        self.documents = [
            item
            for item in self.documents
            if not all(item.get(key) == value for key, value in query.items())
        ]


class Database:
    def __init__(self):
        base = HarnessPolicy(
            task_key="forms",
            version=1,
            parent_version=None,
            status="accepted",
            rules=("Inspect first.",),
            limits={"tool_allowlist": ["click", "screenshot"]},
            reason="base",
        )
        candidate = HarnessPolicy(
            task_key="forms",
            version=2,
            parent_version=1,
            status="candidate",
            rules=("Inspect first.", "Verify focus."),
            limits={"tool_allowlist": ["click", "screenshot"]},
            reason="candidate",
        )
        self.collections = {
            "policies": Collection([base.to_document(), candidate.to_document()]),
            "evaluations": Collection(),
            "replay_evaluations": Collection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class ReplaySuiteTests(unittest.TestCase):
    def test_suite_promotes_candidate_and_ignores_holdout_failure(self):
        db = Database()
        result = ReplaySuiteRepository(db).evaluate_and_record(
            suite_id="suite-1",
            task_key="forms",
            candidate_version=2,
            cases=[
                ReplayCase(
                    "new",
                    "evolve",
                    EvaluationMetrics(success_rate=0.0),
                    EvaluationMetrics(success_rate=1.0),
                ),
                ReplayCase(
                    "old",
                    "regression",
                    EvaluationMetrics(success_rate=1.0),
                    EvaluationMetrics(success_rate=1.0),
                ),
                ReplayCase(
                    "secret",
                    "holdout",
                    EvaluationMetrics(success_rate=1.0),
                    EvaluationMetrics(success_rate=0.0, wrong_clicks=5),
                ),
            ],
        )
        self.assertTrue(result.accepted)
        candidate = db["policies"].find_one({"task_key": "forms", "version": 2})
        self.assertEqual(candidate["status"], "accepted")
        self.assertEqual(result.holdout_tasks_ignored, 1)
        self.assertEqual(len(db["replay_evaluations"].documents), 1)


if __name__ == "__main__":
    unittest.main()
