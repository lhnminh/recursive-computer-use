from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from recursive_computer_use.network.recipe import Recipe
from recursive_computer_use.network.runner import run_recipe


class _Handler(BaseHTTPRequestHandler):
    server_version = "RunnerTest/1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/search.json"):
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            children = [] if query == "no-results" else [{"title": "Search result"}]
            self._json(200, {"data": {"query": query, "children": children}})
        elif self.path.startswith("/search"):
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            markup = "" if query == "no-results" else f'<div data-testid="results-list"><a href="/{query}">result</a></div>'
            body = markup.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/session":
            self.send_response(200)
            self.send_header("Set-Cookie", "sid=local-session; Path=/; HttpOnly")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":{"csrf":"csrf-value"}}')
        elif self.path == "/result":
            self._json(200, {"success": getattr(self.server, "success", False)})
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.end_headers()
        else:
            self._json(404, {})

    def do_POST(self):
        if self.path == "/checkin":
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            valid = (
                self.headers.get("X-CSRF-Token") == "csrf-value"
                and self.headers.get("X-Session") == "local-session"
                and self.headers.get("Cookie", "").startswith("sid=local-session")
                and payload == {"name": "Ada", "count": 3}
            )
            self.server.success = valid
            self._json(200 if valid else 403, {"accepted": valid})
        elif self.path == "/bad":
            self._json(400, {"error": "no"})
        else:
            self._json(404, {})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.success = False
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.site = f"127.0.0.1:{cls.server.server_port}"
        cls.base = f"http://{cls.site}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def recipe(self, steps, verify=True):
        return Recipe.from_dict({
            "name": "test:checkin",
            "description": "Check in a guest.",
            "scope": {"site": self.site, "task_key": "checkin"},
            "params": [
                {"name": "full_name", "description": "Guest name"},
                {"name": "count", "description": "Numeric count"},
            ],
            "steps": steps,
            "verify": {"url": f"{self.base}/result"} if verify else None,
        })

    def test_replays_session_csrf_cookie_and_typed_body_then_verifies(self):
        self.server.success = False
        recipe = self.recipe([
            {
                "id": "session", "method": "GET", "url": f"{self.base}/session",
                "extract": [
                    {"var": "csrf", "from": "json", "path": "data.csrf"},
                    {"var": "sid", "from": "cookie", "path": "sid"},
                ],
            },
            {
                "id": "submit", "method": "POST", "url": f"{self.base}/checkin",
                "headers": {"X-CSRF-Token": "{{csrf}}", "X-Session": "{{sid}}"},
                "body": {"name": "{{full_name}}", "count": "{{count}}"},
                "extract": [],
            },
        ])

        result = run_recipe(recipe, {"full_name": "Ada", "count": 3})

        self.assertTrue(result.ok, result.error)
        self.assertEqual([step.id for step in result.steps], ["session", "submit", "verify"])
        self.assertEqual(result.vars["count"], 3)
        self.assertTrue(result.verifier_result["success"])
        self.assertTrue(all(step.ms >= 0 for step in result.steps))

    def test_verifier_supports_query_parameters_and_json_postconditions(self):
        recipe = Recipe.from_dict({
            "name": "search:results",
            "description": "Search the site and return results.",
            "scope": {"site": self.site, "task_key": "search"},
            "params": [{"name": "query"}],
            "steps": [{
                "id": "search", "method": "GET",
                "url": f"{self.base}/search.json?q={{{{query}}}}", "extract": [],
            }],
            "verify": {
                "url": f"{self.base}/search.json",
                "query": {"q": "{{query}}"},
                "assertions": [
                    {"path": "data.query", "op": "equals", "value": "{{query}}"},
                    {"path": "data.children", "op": "nonempty"},
                ],
            },
        })

        passed = run_recipe(recipe, {"query": "accessibility"})
        failed = run_recipe(recipe, {"query": "no-results"})

        self.assertTrue(passed.ok, passed.error)
        self.assertTrue(passed.verifier_result["success"])
        self.assertFalse(failed.ok)
        self.assertFalse(failed.verifier_result["success"])

    def test_verifier_supports_html_postconditions_on_same_site(self):
        recipe = Recipe.from_dict({
            "name": "search:html-results",
            "description": "Search the site and verify its result page.",
            "scope": {"site": self.site, "task_key": "search"},
            "params": [{"name": "query"}],
            "steps": [{"id": "search", "method": "GET", "url": f"{self.base}/search", "extract": []}],
            "verify": {
                "url": f"{self.base}/search",
                "query": {"q": "{{query}}"},
                "headers": {"Accept": "text/html"},
                "format": "html",
                "assertions": [{
                    "element": {"tag": "a", "attrs": {"href": {"starts_with": "/"}}},
                    "within": {"tag": "div", "attrs": {"data-testid": "results-list"}},
                    "op": "count_gte",
                    "value": 1,
                }],
            },
        })

        passed = run_recipe(recipe, {"query": "tiptour-macos"})
        failed = run_recipe(recipe, {"query": "no-results"})

        self.assertTrue(passed.ok, passed.error)
        self.assertFalse(failed.ok)

    def test_stops_after_unexpected_status_without_verifier(self):
        recipe = self.recipe([
            {"id": "bad", "method": "POST", "url": f"{self.base}/bad", "expect_status": 200, "extract": []},
            {"id": "later", "method": "GET", "url": f"{self.base}/session", "extract": []},
        ])

        result = run_recipe(recipe, {"full_name": "Ada", "count": 3})

        self.assertFalse(result.ok)
        self.assertEqual([step.id for step in result.steps], ["bad"])
        self.assertIn("expected 200", result.error)

    def test_blocks_redirect_outside_recipe_site(self):
        other = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        other_thread = threading.Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        try:
            self.server.redirect_target = f"http://127.0.0.1:{other.server_port}/session"
            recipe = self.recipe([
                {"id": "redirect", "method": "GET", "url": f"{self.base}/redirect", "extract": []},
            ], verify=False)

            result = run_recipe(recipe, {"full_name": "Ada", "count": 3})

            self.assertFalse(result.ok)
            self.assertIn("outside recipe scope", result.error)
        finally:
            other.shutdown()
            other.server_close()
            other_thread.join(timeout=2)

    def test_rejects_missing_or_extra_params(self):
        recipe = self.recipe([
            {"id": "session", "method": "GET", "url": f"{self.base}/session", "extract": []},
        ], verify=False)
        result = run_recipe(recipe, {"full_name": "Ada"})
        self.assertFalse(result.ok)
        self.assertIn("params mismatch", result.error)


if __name__ == "__main__":
    unittest.main()
