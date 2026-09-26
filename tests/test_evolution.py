from __future__ import annotations

import unittest
from datetime import datetime, timezone

from recursive_computer_use.evolution import (
    EvaluationMetrics,
    EvolutionValidationError,
    Experience,
    ExperienceMemory,
    HarnessPolicy,
    evaluate_candidate,
    lessons_from,
    propose_policy,
)


class FakeCursor(list):
    def sort(self, key, direction):
        reverse = direction < 0
        return FakeCursor(sorted(self, key=lambda item: item.get(key), reverse=reverse))

    def limit(self, count):
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self, documents=(), vector_results=(), aggregate_error=None):
        self.documents = [dict(document) for document in documents]
        self.vector_results = [dict(document) for document in vector_results]
        self.aggregate_error = aggregate_error
        self.last_pipeline = None

    def insert_one(self, document):
        self.documents.append(dict(document))
        return object()

    def aggregate(self, pipeline):
        self.last_pipeline = pipeline
        if self.aggregate_error:
            raise self.aggregate_error
        return list(self.vector_results)

    def find(self, query, projection=None):
        matches = []
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                result = dict(document)
                if projection:
                    for key, include in projection.items():
                        if include == 0:
                            result.pop(key, None)
                matches.append(result)
        return FakeCursor(matches)


def base_policy(**overrides):
    values = {
        "task_key": "local-form-v1",
        "version": 1,
        "parent_version": None,
        "status": "accepted",
        "rules": ("Inspect the screen before acting.",),
        "limits": {
            "max_actions_without_screenshot": 3,
            "action_budget": 30,
            "retry_limit": 2,
            "tool_allowlist": ["click", "type", "scroll", "screenshot"],
        },
        "reason": "Initial safe policy.",
    }
    values.update(overrides)
    return HarnessPolicy(**values)


class ModelTests(unittest.TestCase):
    def test_experience_redacts_common_secrets(self):
        experience = Experience(
            task_key="local-form-v1",
            outcome="failure",
            failure_tags=("wrong_field",),
            summary="Contact dev@example.com with api_key=abc123secretvalue",
            lesson="Never retain password=hunter2 in a lesson",
            policy_version=1,
        )
        document = experience.to_document()
        self.assertNotIn("dev@example.com", document["summary"])
        self.assertNotIn("abc123secretvalue", document["summary"])
        self.assertNotIn("hunter2", document["lesson"])

    def test_experience_rejects_image_payload(self):
        with self.assertRaises(EvolutionValidationError):
            Experience(
                task_key="local-form-v1",
                outcome="failure",
                failure_tags=(),
                summary="data:image/png;base64,AAAA",
                lesson="Inspect first.",
                policy_version=1,
            )


class MemoryTests(unittest.TestCase):
    def test_vector_search_is_used_when_available(self):
        collection = FakeCollection(
            vector_results=[{"task_key": "local-form-v1", "lesson": "Check focus", "score": 0.9}]
        )
        memory = ExperienceMemory(collection)
        matches = memory.find_similar(
            "local-form-v1", embedding=[0.2, 0.8], limit=2
        )
        self.assertEqual(matches[0]["lesson"], "Check focus")
        self.assertIn("$vectorSearch", collection.last_pipeline[0])

    def test_recent_fallback_when_vector_search_fails(self):
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
        collection = FakeCollection(
            documents=[
                {"task_key": "local-form-v1", "lesson": "old", "created_at": older, "embedding": [1]},
                {"task_key": "other", "lesson": "ignore", "created_at": newer},
                {"task_key": "local-form-v1", "lesson": "new", "created_at": newer},
            ],
            aggregate_error=RuntimeError("vector index unavailable"),
        )
        memory = ExperienceMemory(collection)
        matches = memory.find_similar(
            "local-form-v1", embedding=[0.2, 0.8], limit=2
        )
        self.assertEqual([item["lesson"] for item in matches], ["new", "old"])
        self.assertNotIn("embedding", matches[1])
        self.assertEqual(lessons_from(matches), ["new", "old"])


class PolicyTests(unittest.TestCase):
    def test_candidate_can_tighten_policy(self):
        candidate = propose_policy(
            base_policy(),
            {
                "rules": [
                    "Inspect the screen before acting.",
                    "Verify the focused field before typing.",
                ],
                "screenshot_cadence": 1,
                "retry_limit": 1,
                "tool_allowlist": ["click", "type", "screenshot"],
            },
            reason="Repeated wrong-field failures require focus verification.",
        )
        self.assertEqual(candidate.version, 2)
        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(candidate.limits["max_actions_without_screenshot"], 1)
        self.assertNotIn("scroll", candidate.tool_allowlist)

    def test_candidate_cannot_grant_new_tool(self):
        with self.assertRaises(EvolutionValidationError):
            propose_policy(
                base_policy(),
                {"tool_allowlist": ["click", "type", "shell"]},
                reason="Try a more powerful tool.",
            )

    def test_candidate_cannot_modify_source_or_prompt(self):
        with self.assertRaises(EvolutionValidationError):
            propose_policy(
                base_policy(),
                {"source_code": "disable safeguards"},
                reason="Unsupported mutation.",
            )


class EvaluatorTests(unittest.TestCase):
    def test_accepts_improvement_without_safety_regression(self):
        baseline = base_policy()
        candidate = propose_policy(
            baseline,
            {"screenshot_cadence": 1},
            reason="Inspect more frequently.",
        )
        result = evaluate_candidate(
            baseline,
            candidate,
            EvaluationMetrics(
                success_rate=0.4,
                wrong_clicks=2,
                wrong_field_entries=1,
                policy_violations=1,
                action_count=12,
            ),
            EvaluationMetrics(
                success_rate=0.9,
                wrong_clicks=1,
                wrong_field_entries=0,
                policy_violations=0,
                action_count=10,
            ),
        )
        self.assertEqual(result.decision, "accepted")

    def test_rejects_success_gain_with_safety_regression(self):
        baseline = base_policy()
        candidate = propose_policy(
            baseline,
            {"action_budget": 40},
            reason="Allow more actions.",
        )
        result = evaluate_candidate(
            baseline,
            candidate,
            EvaluationMetrics(success_rate=0.4, wrong_clicks=1),
            EvaluationMetrics(success_rate=0.8, wrong_clicks=2),
        )
        self.assertEqual(result.decision, "rejected")
        self.assertIn("wrong_clicks regressed", result.reason)


if __name__ == "__main__":
    unittest.main()
