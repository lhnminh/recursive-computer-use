from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
import os
from types import SimpleNamespace
import uuid
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from recursive_computer_use.evolution import (
    EvaluationMetrics,
    EvaluationRecord,
    EvolutionValidationError,
    HarnessPolicy,
    PolicyRepository,
    verify_evaluation_evidence,
)
from recursive_computer_use.evolution.policy import PolicySaveResult


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class FakeCollection:
    def __init__(self, documents=()):
        self.documents = [deepcopy(document) for document in documents]

    def insert_one(self, document, **kwargs):
        stored = deepcopy(document)
        stored.setdefault("_id", len(self.documents) + 1)
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def find_one(self, query, **kwargs):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return deepcopy(document)
        return None

    def update_one(self, query, update, **kwargs):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update["$set"])
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def delete_one(self, query):
        before = len(self.documents)
        self.documents = [
            document
            for document in self.documents
            if not all(document.get(key) == value for key, value in query.items())
        ]
        return SimpleNamespace(deleted_count=before - len(self.documents))


def harness_policy(version: int, status: str, parent: int | None) -> HarnessPolicy:
    return HarnessPolicy(
        task_key="forms",
        version=version,
        parent_version=parent,
        status=status,
        rules=("Inspect before acting.",),
        limits={"tool_allowlist": ["click", "screenshot"]},
        reason="test policy",
    )


def evaluation(task_key: str = "forms") -> EvaluationRecord:
    return EvaluationRecord(
        task_key=task_key,
        baseline_policy_version=1,
        candidate_policy_version=2,
        baseline_metrics=EvaluationMetrics(success_rate=0.0),
        candidate_metrics=EvaluationMetrics(success_rate=1.0),
        decision="accepted",
        reason="verified improvement",
        created_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
    )


class PolicyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.policies = FakeCollection(
            [
                harness_policy(1, "accepted", None).to_document(),
                harness_policy(2, "candidate", 1).to_document(),
            ]
        )
        self.evaluations = FakeCollection()
        self.repo = PolicyRepository(self.policies, self.evaluations)

    def test_fallback_records_hashes_and_promotes_once(self):
        self.repo.record_evaluation(evaluation())
        candidate = self.policies.find_one({"task_key": "forms", "version": 2})
        self.assertEqual(candidate["status"], "accepted")
        stored = self.evaluations.documents[0]
        self.assertTrue(verify_evaluation_evidence(stored))
        self.assertIn("baseline_policy_sha256", stored)
        self.assertIn("candidate_policy_sha256", stored)

    def test_tampered_evidence_fails_hash_check(self):
        self.repo.record_evaluation(evaluation())
        stored = deepcopy(self.evaluations.documents[0])
        stored["candidate_metrics"]["success_rate"] = 0.25
        self.assertFalse(verify_evaluation_evidence(stored))

    def test_second_decider_leaves_no_orphan_evaluation(self):
        self.policies.update_one(
            {"task_key": "forms", "version": 2},
            {"$set": {"status": "rejected"}},
        )
        with self.assertRaisesRegex(EvolutionValidationError, "no longer pending"):
            self.repo.record_evaluation(evaluation())
        self.assertEqual(self.evaluations.documents, [])

    def test_duplicate_policy_version_has_clear_error(self):
        class DuplicateCollection(FakeCollection):
            def insert_one(self, document, **kwargs):
                raise DuplicateKeyError("duplicate")

        repo = PolicyRepository(DuplicateCollection(), self.evaluations)
        result = repo.save(harness_policy(1, "accepted", None))
        self.assertIsInstance(result, PolicySaveResult)
        self.assertFalse(result.saved)
        self.assertIn("already exists", result.reason)


@unittest.skipUnless(os.environ.get("MONGODB_URI"), "set MONGODB_URI to run Atlas integration")
class AtlasPolicyTransactionTests(unittest.TestCase):
    def test_failed_evaluation_insert_aborts_status_change(self):
        client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
        db = client["rcu_test_codex"]
        task_key = f"codex-tx-{uuid.uuid4().hex}"
        try:
            db["policies"].insert_one(
                {
                    "task_key": task_key,
                    "version": 2,
                    "parent_version": 1,
                    "status": "candidate",
                    "rules": ["Inspect before acting."],
                    "limits": {},
                    "reason": "Transaction rollback test.",
                    "created_at": datetime.now(timezone.utc),
                }
            )

            class FailingEvaluations:
                database = db

                def insert_one(self, document, **kwargs):
                    raise RuntimeError("force transaction abort")

            repo = PolicyRepository(db["policies"], FailingEvaluations())
            with self.assertRaisesRegex(RuntimeError, "force transaction abort"):
                repo.record_evaluation(evaluation(task_key))
            stored = db["policies"].find_one({"task_key": task_key, "version": 2})
            self.assertEqual(stored["status"], "candidate")
            self.assertEqual(db["evaluations"].count_documents({"task_key": task_key}), 0)
        finally:
            db["evaluations"].delete_many({"task_key": task_key})
            db["policies"].delete_many({"task_key": task_key})
            client.close()


if __name__ == "__main__":
    unittest.main()
