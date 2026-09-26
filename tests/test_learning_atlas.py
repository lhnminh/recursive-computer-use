"""Integration tests for the learning layer against a real Atlas cluster.

Skipped unless ``RCU_ATLAS_TESTS=1`` and ``MONGODB_URI`` are set. Uses its own
database (``rcu_test_claude``) and drops it at the end. Search indexes take a
few minutes to build, so the whole suite takes ~3-5 minutes.

    RCU_ATLAS_TESTS=1 uv run python -m unittest tests.test_learning_atlas -v
"""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import load_dotenv
from pymongo import MongoClient

from recursive_computer_use import agent
from recursive_computer_use.evolution import EvaluationMetrics, EvolutionRuntime
from recursive_computer_use.learning import LearningStore, skill_name
from recursive_computer_use.schema import SEARCH_INDEXES, ensure_schema
from recursive_computer_use.store import ActionStore

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
TEST_DB = "rcu_test_claude"
ENABLED = os.environ.get("RCU_ATLAS_TESTS") == "1" and bool(os.environ.get("MONGODB_URI"))


def wait_for_search_indexes(db, timeout: float = 480) -> None:
    deadline = time.time() + timeout
    pending = {(c, n) for c, idx in SEARCH_INDEXES.items() for n in idx}
    while pending and time.time() < deadline:
        for coll, name in list(pending):
            info = next(iter(db[coll].list_search_indexes(name)), None)
            if info and info.get("status") == "READY":
                pending.discard((coll, name))
        if pending:
            time.sleep(10)
    if pending:
        raise TimeoutError(f"search indexes not READY: {sorted(pending)}")


def policy(task_key: str, version: int, status: str, rules: list[str]):
    return SimpleNamespace(task_key=task_key, version=version, status=status, rules=rules)


def metrics(success: bool) -> EvaluationMetrics:
    return EvaluationMetrics(success_rate=1.0 if success else 0.0, action_count=5)


@unittest.skipUnless(ENABLED, "set RCU_ATLAS_TESTS=1 to run Atlas integration tests")
class LearningAtlasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.environ["MONGODB_URI"])
        cls.client.drop_database(TEST_DB)
        cls.db = cls.client[TEST_DB]
        ensure_schema(cls.db)
        cls.learning = LearningStore(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(TEST_DB)
        cls.client.close()

    def test_1_agent_run_writes_episode_site_and_skills(self):
        store = ActionStore(self.client, TEST_DB, enabled=True)
        final = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        tool_calls=None,
                        content="done",
                        model_dump=lambda **_: {"role": "assistant", "content": "done"},
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=7),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: final))
        )
        with (
            patch.object(agent, "resolve_auth", return_value=SimpleNamespace(api_key="x", base_url=None)),
            patch.object(agent, "OpenAI", return_value=client),
        ):
            agent.run(
                "Open http://127.0.0.1:8765 and complete the check-in",
                model="test-model",
                action_store=store,
                task_key="local-form-v1",
            )

        ep = self.db.episodes.find_one({"task_key": "local-form-v1"})
        self.assertEqual(ep["site"], "127.0.0.1:8765")
        self.assertEqual(ep["outcome"], "unverified")  # no verifier: never "success"
        self.assertEqual((ep["llm_calls"], ep["tokens_in"], ep["tokens_out"]), (1, 120, 7))
        self.assertEqual(len(ep["skills_used"]), 3)  # the three default rules
        self.assertEqual(self.db.sites.find_one({"domain": "127.0.0.1:8765"})["episodes"], 1)
        self.assertEqual(self.db.skills.count_documents({"status": "active"}), 3)

    def test_2_lift_and_retirement(self):
        tk = "lift-test"
        good, bad, base = "Verify the focused field label.", "Click fast without looking.", "Base rule."
        with_good = self.learning.sync_policy_skills(policy(tk, 1, "accepted", [base, good]))
        with_bad = self.learning.sync_policy_skills(policy(tk, 2, "accepted", [base, bad]))
        started = agent.datetime.now(agent.timezone.utc)
        for used, success in [(with_good, True)] * 3 + [(with_bad, False)] * 3:
            self.learning.record_episode(
                run_id="r", prompt="fill form", task_key=tk, model="m",
                outcome="success" if success else "failure", started_at=started,
                skills_used=used, metrics=metrics(success).to_document(),
            )
        get = lambda rule: self.db.skills.find_one({"name": skill_name(tk, rule)})
        self.assertEqual((get(good)["uses"], get(good)["wins"]), (3, 3))
        self.assertAlmostEqual(get(good)["lift"], 1.0)
        self.assertAlmostEqual(get(bad)["lift"], -1.0)
        self.assertIsNone(get(base)["lift"])  # used by every episode: no baseline
        self.assertEqual(get(bad)["status"], "retired")
        self.assertEqual(get(good)["status"], "active")

    def test_3_voyage_retrieval(self):
        # Experiences via the real evolution runtime; lessons embedded by Atlas.
        runtime = EvolutionRuntime(self.db)
        p = runtime.policy_for_run("voyage-test")
        runtime.record_verified_run(p, EvaluationMetrics(success_rate=0.0, wrong_field_entries=1))
        self.assertNotIn("embedding", self.db.experiences.find_one({"task_key": "voyage-test"}))
        self.learning.sync_policy_skills(
            policy("voyage-test", 9, "accepted", ["Press the Submit button to send the form."])
        )

        wait_for_search_indexes(self.db)
        time.sleep(5)  # let the new documents reach the index

        lessons = runtime.memory.find_similar(
            "voyage-test", query_text="typed into the wrong input box", limit=1
        )
        self.assertIn("focused field", lessons[0]["lesson"])
        self.assertIn("score", lessons[0])

        # Different words, same meaning: "confirm control" vs "Submit button".
        skills = self.learning.similar_skills(
            "click the confirm control to finish", task_key="voyage-test", limit=1
        )
        self.assertIn("Submit", skills[0]["description"])
        print(f"\n  voyage: lesson score={lessons[0]['score']:.3f}, skill score={skills[0]['score']:.3f}")


if __name__ == "__main__":
    unittest.main()
