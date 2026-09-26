"""Atlas integration tests for the recipe store.

Skipped unless ``RCU_ATLAS_TESTS=1``. Uses ``rcu_test_claude_recipes`` and
drops it at the end. Search indexes take a few minutes to build.

    RCU_ATLAS_TESTS=1 uv run python -m unittest tests.test_recipes_atlas -v
"""

from __future__ import annotations

import copy
import os
import sys
import time
import unittest
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

from recursive_computer_use.network.recipe import Recipe
from recursive_computer_use.recipes import RecipeStore
from recursive_computer_use.schema import ensure_schema

sys.path.insert(0, str(Path(__file__).parent))
from test_learning_atlas import eventually, wait_for_search_indexes  # noqa: E402
from test_recipe import EXAMPLE  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
TEST_DB = "rcu_test_claude_recipes"
ENABLED = os.environ.get("RCU_ATLAS_TESTS") == "1" and bool(os.environ.get("MONGODB_URI"))


def recipe(**changes) -> Recipe:
    d = copy.deepcopy(EXAMPLE)
    d.update(changes)
    return Recipe.from_dict(d)


def order_recipe() -> Recipe:
    d = copy.deepcopy(EXAMPLE)
    d.update(
        name="127.0.0.1:8765:order",
        description="Order a pizza for delivery with size and toppings.",
        params=[{"name": "size"}, {"name": "toppings"}],
    )
    d["steps"][1]["url"] = "http://127.0.0.1:8765/api/order"
    d["steps"][1]["body"] = {"size": "{{size}}", "toppings": "{{toppings}}"}
    return Recipe.from_dict(d)


@unittest.skipUnless(ENABLED, "set RCU_ATLAS_TESTS=1 to run Atlas integration tests")
class RecipeStoreAtlasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.environ["MONGODB_URI"])
        cls.client.drop_database(TEST_DB)
        cls.db = cls.client[TEST_DB]
        ensure_schema(cls.db)
        cls.store = RecipeStore(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(TEST_DB)
        cls.client.close()

    def test_1_save_writes_candidate_and_call_chain(self):
        rid = self.store.save_candidate(recipe())
        doc = self.db.skills.find_one({"_id": rid})
        self.assertEqual((doc["status"], doc["version"], doc["uses"]), ("candidate", 1, 0))
        chain = self.store.call_chain("127.0.0.1:8765", "GET /api/session")
        self.assertEqual(chain[0]["to"], "POST /api/checkin")
        self.assertEqual(chain[0]["via"], {"vars": ["csrf"]})
        # Same name again: next version, linked to the first.
        rid2 = self.store.save_candidate(recipe())
        doc2 = self.db.skills.find_one({"_id": rid2})
        self.assertEqual((doc2["version"], doc2["parent_id"]), (2, rid))
        self.db.skills.delete_one({"_id": rid2})
        self.__class__.rid = rid

    def test_2_success_promotes_then_two_failures_retire(self):
        rid = self.rid
        self.assertEqual(self.store.record_result(rid, ok=True, run_ms=120, steps=[{}, {}]), "active")
        self.assertEqual(self.store.record_result(rid, ok=False, run_ms=90), "active")
        self.assertEqual(self.store.record_result(rid, ok=True, run_ms=80), "active")  # streak resets
        self.assertEqual(self.store.record_result(rid, ok=False, run_ms=90), "active")
        self.assertEqual(self.store.record_result(rid, ok=False, run_ms=90), "retired")
        doc = self.db.skills.find_one({"_id": rid})
        self.assertEqual((doc["uses"], doc["wins"]), (5, 2))
        eps = list(self.db.episodes.find({"skills_used.skill_id": rid}))
        self.assertEqual(len(eps), 5)
        self.assertEqual({e["mode"] for e in eps}, {"api_recipe"})
        self.assertEqual({e["site"] for e in eps}, {"127.0.0.1:8765"})

    def test_3_failed_candidate_retires_immediately(self):
        rid = self.store.save_candidate(recipe(name="bad-one"))
        self.assertEqual(self.store.record_result(rid, ok=False, run_ms=10), "retired")

    def test_4_supersede_and_find_by_meaning(self):
        old = self.store.save_candidate(recipe(name="127.0.0.1:8765:checkin-v"))
        self.store.record_result(old, ok=True, run_ms=50)
        new_id = self.store.supersede(old, recipe())
        new = self.db.skills.find_one({"_id": new_id})
        self.assertEqual((new["version"], new["parent_id"], new["status"]), (2, old, "candidate"))
        self.assertEqual(self.db.skills.find_one({"_id": old})["status"], "retired")
        self.store.record_result(new_id, ok=True, run_ms=40)
        self.store.save_candidate(order_recipe())

        wait_for_search_indexes(self.db)
        time.sleep(5)
        found = eventually(lambda: [
            r for r in self.store.find_for_task(
                "register the arriving visitor Grace Hopper", site="127.0.0.1:8765", limit=2
            ) if r.id == new_id
        ] and self.store.find_for_task(
            "register the arriving visitor Grace Hopper", site="127.0.0.1:8765", limit=2
        ))
        self.assertEqual(found[0].id, new_id, [r.name for r in found])
        pizza = eventually(lambda: [
            r for r in self.store.find_for_task("I want a large pepperoni pie delivered", limit=1)
            if r.name == "127.0.0.1:8765:order"
        ]) or self.store.find_for_task("I want a large pepperoni pie delivered", limit=1)
        self.assertEqual(pizza[0].name, "127.0.0.1:8765:order")
        self.assertEqual(self.store.find_for_task("anything", site="nowhere:1"), [])

    def test_5_watch_delivers_and_resumes_missed_recipes(self):
        got: list[str] = []
        stop = self.store.watch(lambda r: got.append(r.name), agent_id="t-agent")
        a = self.store.save_candidate(recipe(name="w-a"))
        self.store.record_result(a, ok=True, run_ms=1)
        deadline = time.time() + 15
        while "w-a" not in got and time.time() < deadline:
            time.sleep(0.2)
        stop()
        self.assertIn("w-a", got)

        b = self.store.save_candidate(recipe(name="w-b"))
        self.store.record_result(b, ok=True, run_ms=1)  # while the watcher is down
        stop = self.store.watch(lambda r: got.append(r.name), agent_id="t-agent")
        deadline = time.time() + 15
        while "w-b" not in got and time.time() < deadline:
            time.sleep(0.2)
        stop()
        self.assertIn("w-b", got)


if __name__ == "__main__":
    unittest.main()
