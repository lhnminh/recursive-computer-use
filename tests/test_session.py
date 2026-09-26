from __future__ import annotations

import json
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from recursive_computer_use.network import session
from recursive_computer_use.network.recipe import Recipe
from recursive_computer_use.network.runner import run_recipe

SID = "test-sid"
JSESSIONID = "ajax:123"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        cookies = {}
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                cookies[name] = value
        ok = (
            cookies.get("sid") == SID
            and cookies.get("JSESSIONID") == f'"{JSESSIONID}"'
            and self.headers.get("Csrf-Token") == JSESSIONID
        )
        body = json.dumps({"ok": ok}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeContext:
    def __init__(self, state):
        self.state = state

    def storage_state(self, *, path):
        Path(path).write_text(json.dumps(self.state))


def storage_state(host: str, *, sid_domain: str | None = None, sid_expires: float = -1) -> dict:
    return {
        "cookies": [
            {"name": "sid", "value": SID, "domain": sid_domain or host, "path": "/",
             "expires": sid_expires, "httpOnly": True, "secure": False, "sameSite": "Lax"},
            {"name": "JSESSIONID", "value": f'"{JSESSIONID}"', "domain": host, "path": "/",
             "expires": time.time() + 3600, "httpOnly": False, "secure": False, "sameSite": "Lax"},
            {"name": "other", "value": "x", "domain": ".example.com", "path": "/", "expires": -1},
        ],
        "origins": [],
    }


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(session, "SESSION_DIR", Path(self.tmp.name) / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_path_is_filename_safe(self):
        path = session.session_path("127.0.0.1:8765")
        self.assertEqual(path.name, "127.0.0.1_8765.json")
        self.assertNotIn("/", session.session_path("a/../b").name[:-5])

    def test_save_load_delete_with_private_modes(self):
        path = session.save_session(FakeContext(storage_state("127.0.0.1")), "127.0.0.1:9")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(len(session.load_storage_state("127.0.0.1:9")["cookies"]), 3)
        session.delete_session("127.0.0.1:9")
        self.assertIsNone(session.load_storage_state("127.0.0.1:9"))
        session.delete_session("127.0.0.1:9")  # idempotent

    def test_cookie_domain_matching_and_expiry(self):
        state = {
            "cookies": [
                {"name": "li_at", "value": "a", "domain": ".linkedin.com", "path": "/", "expires": -1},
                {"name": "host_only", "value": "b", "domain": "linkedin.com", "path": "/", "expires": -1},
                {"name": "exact", "value": "c", "domain": "www.linkedin.com", "path": "/", "expires": -1},
                {"name": "old", "value": "d", "domain": ".linkedin.com", "path": "/", "expires": 1},
                {"name": "evil", "value": "e", "domain": ".notlinkedin.com", "path": "/", "expires": -1},
            ]
        }
        self.assertEqual(
            session.cookies_for("www.linkedin.com", state), {"li_at": "a", "exact": "c"}
        )
        jar = session.cookie_jar_for("www.linkedin.com", state)
        self.assertEqual(sorted(c.name for c in jar), ["exact", "li_at"])
        self.assertEqual(session.cookies_for("www.linkedin.com"), {})  # nothing saved


class SessionReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.site = f"127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(session, "SESSION_DIR", Path(self.tmp.name) / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.recipe = Recipe.from_dict({
            "name": "test:me",
            "description": "Read the logged-in profile.",
            "scope": {"site": self.site, "task_key": "me"},
            "session": [{"var": "csrf", "from": "cookie", "path": "JSESSIONID"}],
            "steps": [{
                "id": "me",
                "method": "GET",
                "url": f"http://{self.site}/me",
                "headers": {"Csrf-Token": "{{csrf}}"},
                "expect_status": 200,
            }],
        })

    def test_fails_without_session(self):
        result = run_recipe(self.recipe, {})
        self.assertFalse(result.ok)
        self.assertIn("log in again", result.error)
        self.assertEqual(result.steps, [])

    def test_fails_when_login_cookie_missing(self):
        result = run_recipe(self.recipe, {}, session_cookies={"JSESSIONID": f'"{JSESSIONID}"'})
        self.assertFalse(result.ok)
        self.assertIn("401", result.error)

    def test_succeeds_with_explicit_cookies(self):
        result = run_recipe(
            self.recipe, {}, session_cookies={"sid": SID, "JSESSIONID": f'"{JSESSIONID}"'}
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.vars["csrf"], JSESSIONID)

    def test_succeeds_by_loading_saved_session(self):
        session.save_session(FakeContext(storage_state("127.0.0.1")), self.site)
        result = run_recipe(self.recipe, {})
        self.assertTrue(result.ok, result.error)

    def test_expired_saved_cookie_is_ignored(self):
        state = storage_state("127.0.0.1", sid_expires=time.time() - 10)
        session.save_session(FakeContext(state), self.site)
        result = run_recipe(self.recipe, {})
        self.assertFalse(result.ok)
        self.assertIn("401", result.error)


if __name__ == "__main__":
    unittest.main()
