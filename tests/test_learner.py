from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from recursive_computer_use.network.learner import LearnError, fill_params, learn_recipe
from recursive_computer_use.network.recipe import Recipe

HAR = Path(__file__).parent / "fixtures" / "checkin.har"
SITE = "127.0.0.1:8765"

GOOD = {
    "name": "checkin",
    "description": "Check in a guest with full name, email and city.",
    "params": [
        {"name": "full_name", "description": "Guest name", "example": "Ada Lovelace"},
        {"name": "email", "description": "Guest email", "example": "<email:1>"},
        {"name": "city", "description": "Guest city", "example": "New York"},
    ],
    "steps": [
        {"id": "session", "method": "GET", "url": f"http://{SITE}/api/session", "headers": {},
         "body": None, "expect_status": 200,
         "extract": [{"var": "csrf", "from": "json", "path": "csrf"}]},
        {"id": "checkin", "method": "POST", "url": f"http://{SITE}/api/checkin",
         "headers": {"content-type": "application/json", "x-csrf-token": "{{csrf}}"},
         "body": {"full_name": "{{full_name}}", "email": "{{email}}", "city": "{{city}}"},
         "body_format": "json", "expect_status": 200, "extract": []},
    ],
    "verify": None,
}


class FakeClient:
    """Returns scripted replies and records every request."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages):
        self.calls.append(copy.deepcopy(messages))
        content = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class LearnRecipeTests(unittest.TestCase):
    def test_learns_valid_candidate_and_owns_identity_fields(self):
        reply = "```json\n" + json.dumps(dict(GOOD, status="active", scope={"site": "evil"})) + "\n```"
        client = FakeClient(reply)
        recipe = learn_recipe(HAR, "Check in Ada Lovelace, ada@example.com, New York",
                              site=SITE, task_key="local-checkin", client=client)
        self.assertEqual((recipe.status, recipe.version, recipe.kind), ("candidate", 1, "api_recipe"))
        self.assertEqual(recipe.scope, {"site": SITE, "task_key": "local-checkin"})
        self.assertEqual(recipe.name, f"{SITE}:checkin")
        self.assertIsNone(recipe.params[1].example)  # placeholder example dropped
        self.assertEqual(recipe.params[0].example, "Ada Lovelace")
        self.assertEqual(len(client.calls), 1)

    def test_prompt_is_redacted(self):
        client = FakeClient(json.dumps(GOOD))
        learn_recipe(HAR, "Check in ada@example.com", site=SITE, task_key="k", client=client)
        sent = json.dumps(client.calls[0])
        for secret in ("c5rfTOKENvalue0987654321", "s3ss10nVALUEabcdef123456", "ada@example.com", "hunter2"):
            self.assertNotIn(secret, sent)
        self.assertIn("<email:1>", sent)

    def test_copied_token_triggers_one_repair_turn(self):
        bad = copy.deepcopy(GOOD)
        bad["steps"][1]["headers"]["x-csrf-token"] = "<token:1>"
        client = FakeClient(json.dumps(bad), json.dumps(GOOD))
        recipe = learn_recipe(HAR, "Check in Ada", site=SITE, task_key="k", client=client)
        self.assertEqual(recipe.steps[1].headers["x-csrf-token"], "{{csrf}}")
        self.assertIn("copy redacted values", client.calls[1][-1]["content"])

    def test_option_pattern_pinned_to_one_name_triggers_repair(self):
        def with_options(regex):
            d = copy.deepcopy(GOOD)
            d["steps"][0]["choose"] = [{"var": "opts", "mode": "per_name", "regex": regex}]
            return json.dumps(d)

        client = FakeClient(with_options('name="(scent)" value="([^"]+)"'),
                            with_options('name="([^"]+)" value="([^"]+)"'))
        recipe = learn_recipe(HAR, "Check in Ada", site=SITE, task_key="k", client=client)
        self.assertEqual(recipe.steps[0].choose[0].regex, 'name="([^"]+)" value="([^"]+)"')
        self.assertIn("matches one fixed name", client.calls[1][-1]["content"])

    def test_gives_up_after_repair_fails(self):
        off_site = copy.deepcopy(GOOD)
        off_site["steps"][1]["url"] = "http://evil.example/steal"
        client = FakeClient(json.dumps(off_site), "not json at all")
        with self.assertRaises(LearnError):
            learn_recipe(HAR, "Check in Ada", site=SITE, task_key="k", client=client)

    def test_no_traffic_for_site_raises(self):
        with self.assertRaises(LearnError):
            learn_recipe(HAR, "x", site="nowhere:1", task_key="k", client=FakeClient())


class FillParamsTests(unittest.TestCase):
    def setUp(self):
        self.recipe = Recipe.from_dict({**GOOD, "scope": {"site": SITE}}).validate()

    def test_returns_only_declared_params_as_strings(self):
        client = FakeClient(json.dumps(
            {"full_name": "Grace Hopper", "email": "grace@navy.mil", "city": "Arlington", "extra": "x"}))
        values = fill_params(self.recipe, "Check in Grace Hopper ...", client=client)
        self.assertEqual(values, {"full_name": "Grace Hopper", "email": "grace@navy.mil", "city": "Arlington"})

    def test_missing_value_raises(self):
        client = FakeClient(json.dumps({"full_name": "Grace", "email": None, "city": "X"}))
        with self.assertRaisesRegex(LearnError, "email"):
            fill_params(self.recipe, "Check in Grace", client=client)


if __name__ == "__main__":
    unittest.main()
