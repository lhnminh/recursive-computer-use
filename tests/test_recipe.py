from __future__ import annotations

import copy
import unittest

from recursive_computer_use.network.recipe import Recipe, RecipeError, render, template_vars

EXAMPLE = {
    "name": "127.0.0.1:8765:checkin",
    "kind": "api_recipe",
    "status": "candidate",
    "version": 1,
    "parent_id": None,
    "description": "Check in a guest with full name, email and city.",
    "scope": {"site": "127.0.0.1:8765", "task_key": "local-checkin"},
    "params": [
        {"name": "full_name", "description": "Guest full name", "example": "Ada Lovelace"},
        {"name": "email", "description": "Guest email"},
        {"name": "city", "description": "Guest city"},
    ],
    "steps": [
        {
            "id": "session",
            "method": "GET",
            "url": "http://127.0.0.1:8765/api/session",
            "headers": {},
            "body": None,
            "expect_status": 200,
            "extract": [{"var": "csrf", "from": "json", "path": "csrf"}],
        },
        {
            "id": "checkin",
            "method": "POST",
            "url": "http://127.0.0.1:8765/api/checkin",
            "headers": {"content-type": "application/json", "x-csrf-token": "{{csrf}}"},
            "body": {"full_name": "{{full_name}}", "email": "{{email}}", "city": "{{city}}"},
            "expect_status": 200,
            "extract": [],
        },
    ],
    "verify": {"url": "http://127.0.0.1:8765/api/result"},
}


def example(**changes):
    d = copy.deepcopy(EXAMPLE)
    d.update(changes)
    return d


class RecipeTests(unittest.TestCase):
    def test_contract_example_round_trips_and_validates(self):
        recipe = Recipe.from_dict(EXAMPLE).validate()
        self.assertEqual(recipe.site, "127.0.0.1:8765")
        again = recipe.to_dict()
        self.assertEqual(again["steps"][1]["headers"]["x-csrf-token"], "{{csrf}}")
        self.assertEqual(again["steps"][0]["extract"][0]["from"], "json")
        Recipe.from_dict(again).validate()

    def test_mongo_id_is_kept_out_of_the_document(self):
        recipe = Recipe.from_dict({**EXAMPLE, "_id": "abc"})
        self.assertEqual(recipe.id, "abc")
        self.assertNotIn("_id", recipe.to_dict())

    def assert_rejected(self, d, fragment):
        with self.assertRaises(RecipeError) as ctx:
            Recipe.from_dict(d).validate()
        self.assertIn(fragment, str(ctx.exception))

    def test_rejects_off_site_step(self):
        d = example()
        d["steps"][1]["url"] = "http://evil.example/api/checkin"
        self.assert_rejected(d, "outside scope.site")

    def test_rejects_templated_host(self):
        d = example()
        d["steps"][1]["url"] = "http://{{host}}/api/checkin"
        self.assert_rejected(d, "literal")

    def test_rejects_off_site_verify(self):
        self.assert_rejected(example(verify={"url": "https://evil.example/ok"}), "outside scope.site")

    def test_rejects_cookie_and_authorization_headers(self):
        for header in ("Cookie", "authorization"):
            d = example()
            d["steps"][1]["headers"][header] = "x"
            self.assert_rejected(d, "not allowed")

    def test_rejects_var_used_before_extraction(self):
        d = example()
        d["steps"][0]["headers"] = {"x-csrf-token": "{{csrf}}"}
        self.assert_rejected(d, "undefined vars ['csrf']")

    def test_rejects_unknown_extract_source_and_method(self):
        d = example()
        d["steps"][0]["extract"][0]["from"] = "screen"
        self.assert_rejected(d, "not allowed")
        d = example()
        d["steps"][0]["method"] = "TRACE"
        self.assert_rejected(d, "not allowed")

    def test_rejects_regex_without_group_and_too_many_steps(self):
        d = example()
        d["steps"][0]["extract"] = [{"var": "csrf", "from": "regex", "path": "csrf=\\w+"}]
        self.assert_rejected(d, "needs a group")
        d = example()
        d["steps"] = [dict(d["steps"][0], id=f"s{i}") for i in range(21)]
        self.assert_rejected(d, "1-20 steps")

    def test_malformed_input_raises_recipe_error(self):
        with self.assertRaises(RecipeError):
            Recipe.from_dict({"description": "no name"})


class SessionVarTests(unittest.TestCase):
    def session_recipe(self, **session):
        d = example()
        d["session"] = [dict({"var": "sess_csrf", "from": "cookie", "path": "JSESSIONID"}, **session)]
        d["steps"][1]["headers"]["csrf-token"] = "{{sess_csrf}}"
        return Recipe.from_dict(d)

    def test_session_var_defines_template_and_round_trips(self):
        recipe = self.session_recipe().validate()
        self.assertEqual(Recipe.from_dict(recipe.to_dict()).session[0].path, "JSESSIONID")

    def test_session_var_must_come_from_a_cookie(self):
        with self.assertRaisesRegex(RecipeError, "session vars"):
            self.session_recipe(**{"from": "json"}).validate()

    def test_resolve_strips_quotes_and_reports_missing_cookie(self):
        from recursive_computer_use.network.recipe import resolve_session_vars

        recipe = self.session_recipe()
        self.assertEqual(resolve_session_vars(recipe, {"JSESSIONID": '"ajax:42"'}), {"sess_csrf": "ajax:42"})
        with self.assertRaisesRegex(RecipeError, "log in again"):
            resolve_session_vars(recipe, {})


class TemplateTests(unittest.TestCase):
    def test_render_substitutes_nested_values_and_keeps_whole_value_types(self):
        body = {"n": "{{count}}", "msg": "hi {{name}}!", "list": ["{{name}}"], "k": 3}
        out = render(body, {"count": 2, "name": "Ada"})
        self.assertEqual(out, {"n": 2, "msg": "hi Ada!", "list": ["Ada"], "k": 3})

    def test_render_missing_var_raises(self):
        with self.assertRaises(RecipeError):
            render("{{nope}}", {})

    def test_template_vars_walks_everything(self):
        self.assertEqual(
            template_vars({"a": ["{{x}}", {"b": "{{ y }} and {{x}}"}], "c": 1}), {"x", "y"}
        )


if __name__ == "__main__":
    unittest.main()
