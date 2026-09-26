from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from recursive_computer_use.network.loop import do_task
from recursive_computer_use.network.recipe import Recipe


def _recipe():
    return Recipe.from_dict({
        "name": "local:checkin",
        "description": "Check in a guest by name.",
        "scope": {"site": "127.0.0.1:8765", "task_key": "checkin"},
        "params": [{"name": "name", "description": "Guest name"}],
        "steps": [{"id": "submit", "method": "POST", "url": "http://127.0.0.1:8765/api/checkin", "extract": []}],
        "verify": {"url": "http://127.0.0.1:8765/api/result"},
    })


class _Store:
    def __init__(self, recipes=()):
        self.recipes = list(recipes)
        self.recorded = []
        self.saved = []
        self.superseded = []

    def find_for_task(self, task, *, site, limit):
        self.lookup = (task, site, limit)
        return self.recipes

    def record_result(self, recipe_id, **kwargs):
        self.recorded.append((recipe_id, kwargs))
        return "active" if kwargs["ok"] else "retired"

    def save_candidate(self, recipe, *, recording_id=None):
        self.saved.append((recipe, recording_id))
        return "new-recipe"

    def supersede(self, old_id, new_recipe):
        self.superseded.append((old_id, new_recipe))
        return "new-recipe"


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.captures = []
        self.learns = []
        self.runs = []

    def capture(self, url, **kwargs):
        self.captures.append((url, kwargs))
        return SimpleNamespace(har_path=Path("local.har"), ok=True, duration_ms=321, recording_id="rec-1")

    def learn(self, har_path, task, **kwargs):
        self.learns.append((har_path, task, kwargs))
        return _recipe()

    def fill(self, recipe, task):
        return {"name": "Ada"}

    def runner(self, recipe, params):
        self.runs.append((recipe, params))
        ok = len(self.runs) == 1 and getattr(recipe, "id", None) == "old-recipe"
        return {"ok": ok, "steps": [{"id": "submit", "status": 200 if ok else 410, "ms": 2}], "duration_ms": 12, "error": None if ok else "changed"}

    def invoke(self, store, **overrides):
        args = {
            "task": "check in Ada",
            "site": "127.0.0.1:8765",
            "task_key": "checkin",
            "store": store,
            "capture_fn": self.capture,
            "learn_fn": self.learn,
            "fill_fn": self.fill,
            "runner_fn": self.runner,
        }
        args.update(overrides)
        return do_task(**args)

    def test_recipe_hit_returns_without_capture_or_learning(self):
        recipe = _recipe()
        recipe.id = "old-recipe"
        store = _Store([recipe])

        result = self.invoke(store)

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "recipe")
        self.assertEqual(self.captures, [])
        self.assertEqual(self.learns, [])
        self.assertEqual(store.recorded[0][0], "old-recipe")

    def test_miss_captures_learns_saves_and_replays_once(self):
        store = _Store()
        result = self.invoke(store, runner_fn=lambda recipe, params: {"ok": True, "steps": [], "duration_ms": 8})

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "relearned_recipe")
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(self.captures[0][0], "http://127.0.0.1:8765/")
        self.assertEqual(self.captures[0][1]["agent_prompt"], "check in Ada")
        self.assertEqual(self.learns[0][0], Path("local.har"))
        self.assertEqual(store.saved[0][1], "rec-1")
        self.assertEqual(store.recorded[0][0], "new-recipe")

    def test_verifier_plan_is_used_for_capture_and_saved_recipe_replay(self):
        store = _Store()
        plan = {
            "browser": {"url_contains": ["/checkin"], "text_contains": ["Verified success"]},
            "api": {
                "url": "http://127.0.0.1:8765/api/result",
                "assertions": [{"path": "success", "op": "equals", "value": True}],
            },
        }
        result = self.invoke(
            store,
            verifier=plan,
            runner_fn=lambda recipe, params: {"ok": True, "steps": [], "duration_ms": 8},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(self.captures[0][1]["verifier"], plan)
        self.assertEqual(store.saved[0][0].verify, plan["api"])

    def test_verifier_plan_requires_browser_and_api_assertions(self):
        with self.assertRaisesRegex(ValueError, "browser and api"):
            self.invoke(_Store(), verifier={"browser": {}})

    def test_default_fallback_uses_headless_browser_agent(self):
        store = _Store()
        captured = SimpleNamespace(
            har_path=Path("headless.har"),
            ok=True,
            duration_ms=10,
            recording_id="headless-recording",
        )
        with patch("recursive_computer_use.network.browser_agent.record_headless", return_value=captured) as capture:
            result = self.invoke(
                store,
                capture_fn=None,
                runner_fn=lambda recipe, params: {"ok": True, "steps": [], "duration_ms": 8},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(capture.call_args.args[0], "http://127.0.0.1:8765/")
        self.assertEqual(capture.call_args.kwargs["agent_prompt"], "check in Ada")
        self.assertEqual(self.learns[0][0], Path("headless.har"))

    def test_unverified_capture_does_not_train_a_recipe(self):
        store = _Store()
        result = self.invoke(
            store,
            capture_fn=lambda *_args, **_kwargs: SimpleNamespace(
                har_path=Path("failed.har"), ok=False, duration_ms=10
            ),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["path"], "fallback_failed")
        self.assertEqual(self.learns, [])
        self.assertEqual(store.saved, [])

    def test_failed_recipe_is_retired_superseded_and_replayed_once(self):
        recipe = _recipe()
        recipe.id = "old-recipe"
        store = _Store([recipe])
        outcomes = iter([
            {"ok": False, "steps": [], "duration_ms": 4, "error": "redesign"},
            {"ok": True, "steps": [], "duration_ms": 6},
        ])

        result = self.invoke(store, runner_fn=lambda _recipe, _params: next(outcomes))

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "relearned_recipe")
        self.assertEqual(store.recorded[0][0], "old-recipe")
        self.assertEqual(store.superseded[0][0], "old-recipe")
        self.assertEqual(store.recorded[1][0], "new-recipe")
        self.assertEqual(len(self.runs), 0)


if __name__ == "__main__":
    unittest.main()
