from __future__ import annotations

import http.cookiejar
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo import app
from recursive_computer_use.network.recipe import Recipe
from recursive_computer_use.network.runner import run_recipe


class DemoApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer((app.HOST, 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://{app.HOST}:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        with app.LOCK:
            app.STATE = app.fresh_state()
            app.REDESIGN_ON = False
            app.SESSIONS.clear()
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, *, method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def session(self):
        status, body = self.request("/api/session")
        self.assertEqual(status, 200)
        return json.loads(body)["csrf"]

    def test_server_verifies_csrf_session_and_values(self):
        csrf = self.session()
        guest = {"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"}
        status, _ = self.request(
            "/api/checkin", method="POST", payload=guest,
            headers={"X-CSRF-Token": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(app.STATE["success"])

        wrong_guest = {**guest, "city": "London"}
        status, body = self.request(
            "/api/checkin", method="POST", payload=wrong_guest,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["success"])

        status, body = self.request(
            "/api/checkin", method="POST", payload=guest,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        result = json.loads(self.request("/api/result")[1])
        self.assertTrue(result["success"])

    def test_reset_sets_new_expected_guest(self):
        guest = {"full_name": "Grace Hopper", "email": "grace@example.com", "city": "Arlington"}
        status, body = self.request("/api/reset", method="POST", payload={"guest": guest})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["guest"], guest)
        csrf = self.session()
        page = self.request("/")[1].decode()
        self.assertIn("Grace Hopper", page)
        self.assertIn("Arlington", page)
        status, body = self.request(
            "/api/checkin", method="POST", payload=guest,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])

    def test_redesign_retires_old_route_and_accepts_v2_shape(self):
        csrf = self.session()
        status, body = self.request("/api/redesign", method="POST", payload={"on": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["on"])
        status, _ = self.request(
            "/api/checkin", method="POST",
            payload={"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 410)
        status, body = self.request(
            "/api/v2/check-in", method="POST",
            payload={"name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])

    def test_telemetry_cannot_forge_verification_success(self):
        status, _ = self.request(
            "/api/event", method="POST", payload={"type": "submit", "success": True}
        )
        self.assertEqual(status, 200)
        result = json.loads(self.request("/api/result")[1])
        self.assertFalse(result["success"])

    def test_recipe_runner_survives_reset_and_detects_then_heals_redesign(self):
        site = self.base_url.split("//", 1)[1]

        def make_recipe(version_two=False):
            submit_url = "/api/v2/check-in" if version_two else "/api/checkin"
            body = (
                {"name": "{{full_name}}", "email": "{{email}}", "city": "{{city}}"}
                if version_two
                else {"full_name": "{{full_name}}", "email": "{{email}}", "city": "{{city}}"}
            )
            return Recipe.from_dict({
                "name": "local:checkin",
                "description": "Check in a guest with name, email and city.",
                "scope": {"site": site, "task_key": "local-checkin"},
                "params": [
                    {"name": "full_name", "description": "Guest full name"},
                    {"name": "email", "description": "Guest email"},
                    {"name": "city", "description": "Guest city"},
                ],
                "steps": [
                    {
                        "id": "session", "method": "GET", "url": f"{self.base_url}/api/session",
                        "headers": {}, "body": None, "expect_status": 200,
                        "extract": [{"var": "csrf", "from": "json", "path": "csrf"}],
                    },
                    {
                        "id": "checkin", "method": "POST", "url": f"{self.base_url}{submit_url}",
                        "headers": {"X-CSRF-Token": "{{csrf}}", "Content-Type": "application/json"},
                        "body": body, "expect_status": 200, "extract": [],
                    },
                ],
                "verify": {"url": f"{self.base_url}/api/result"},
            })

        ada = {"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"}
        self.request("/api/reset", method="POST", payload={"guest": ada})
        v1 = make_recipe()
        first = run_recipe(v1, ada)
        self.assertTrue(first.ok, first.error)

        grace = {"full_name": "Grace Hopper", "email": "grace@example.net", "city": "Arlington"}
        self.request("/api/reset", method="POST", payload={"guest": grace})
        second = run_recipe(v1, grace)
        self.assertTrue(second.ok, second.error)

        self.request("/api/redesign", method="POST", payload={"on": True})
        self.request("/api/reset", method="POST", payload={"guest": grace})
        failed = run_recipe(v1, grace)
        self.assertFalse(failed.ok)
        self.assertIn("410", failed.error)

        v2 = make_recipe(version_two=True)
        healed = run_recipe(v2, grace)
        self.assertTrue(healed.ok, healed.error)


if __name__ == "__main__":
    unittest.main()
