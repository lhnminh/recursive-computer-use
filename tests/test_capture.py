from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from demo import app
from recursive_computer_use.network import capture

TEST_SITE = ""


class FakePage:
    def __init__(self):
        self.closed = False

    def goto(self, url, **kwargs):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        with opener.open(url + "api/session") as response:
            csrf = json.loads(response.read())["csrf"]
        request = urllib.request.Request(
            url + "api/checkin",
            data=json.dumps(
                {"full_name": "Ada Lovelace", "email": "ada@example.com", "city": "New York"}
            ).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        )
        with opener.open(request):
            pass

    def bring_to_front(self):
        pass

    def is_closed(self):
        return self.closed


class FakeContext:
    def __init__(self, har_path):
        self.har_path = Path(har_path)
        self.page = FakePage()

    def new_page(self):
        return self.page

    def close(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": f"http://{TEST_SITE}/api/session",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "headers": [{"name": "set-cookie", "value": "sid=secret-cookie-value; Path=/"}],
                            "content": {"mimeType": "application/json", "text": '{"csrf":"csrf-secret-value"}'},
                        },
                    },
                    {
                        "request": {
                            "method": "POST",
                            "url": f"http://{TEST_SITE}/api/checkin",
                            "headers": [
                                {"name": "cookie", "value": "sid=secret-cookie-value"},
                                {"name": "x-csrf-token", "value": "csrf-secret-value"},
                            ],
                            "postData": {
                                "mimeType": "application/json",
                                "text": '{"full_name":"Ada Lovelace","email":"ada@example.com","city":"New York"}',
                            },
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {"mimeType": "application/json", "text": '{"success":true}'},
                        },
                    },
                ]
            }
        }
        self.har_path.write_text(json.dumps(har), encoding="utf-8")


class FakeBrowser:
    def launch(self, *, headless):
        if headless:
            raise AssertionError("capture must use headed Chromium")
        return self

    def new_context(self, *, record_har_path, **kwargs):
        self.context = FakeContext(record_har_path)
        return self.context

    def close(self):
        pass


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeBrowser()


class FakePlaywrightManager:
    def __enter__(self):
        return FakePlaywright()

    def __exit__(self, *args):
        return False


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global TEST_SITE
        cls.server = ThreadingHTTPServer((app.HOST, 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        TEST_SITE = f"{app.HOST}:{cls.server.server_port}"
        cls.url = f"http://{TEST_SITE}/"

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

    def test_records_until_verified_and_persists_only_redacted_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            har_path = Path(tmp) / "flow.har"
            stored = {}

            def persist(document):
                stored.update(document)
                return "recording-id"

            with (
                patch("playwright.sync_api.sync_playwright", return_value=FakePlaywrightManager()),
                patch.object(capture, "_persist_metadata", side_effect=persist),
            ):
                result = capture.record(
                    self.url,
                    task="Check in Ada Lovelace at ada@example.com",
                    har_path=har_path,
                    timeout_s=1,
                )
            har_exists = har_path.is_file()

        self.assertTrue(result.ok)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.source, "human")
        self.assertTrue(har_exists)
        self.assertEqual(result.recording_id, "recording-id")
        self.assertEqual(stored["exchange_count"], 2)
        self.assertEqual(stored["source"], "human")
        self.assertNotIn("ada@example.com", stored["task"])
        self.assertNotIn("secret-cookie-value", repr(stored))
        self.assertNotIn("csrf-secret-value", repr(stored))
        self.assertTrue(all(set(edge) <= {"method", "path", "status"} for edge in stored["endpoints"]))

    def test_returns_timeout_without_marking_success(self):
        class IncompletePage(FakePage):
            def goto(self, url, **kwargs):
                pass

        class IncompleteContext(FakeContext):
            def new_page(self):
                self.page = IncompletePage()
                return self.page

        class IncompleteBrowser(FakeBrowser):
            def new_context(self, *, record_har_path, **kwargs):
                self.context = IncompleteContext(record_har_path)
                return self.context

        class IncompletePlaywright:
            def __init__(self):
                self.chromium = IncompleteBrowser()

        class IncompleteManager(FakePlaywrightManager):
            def __enter__(self):
                return IncompletePlaywright()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("playwright.sync_api.sync_playwright", return_value=IncompleteManager()),
                patch.object(capture, "_persist_metadata", return_value=None),
            ):
                result = capture.record(
                    self.url,
                    task="wait for completion",
                    har_path=Path(tmp) / "timeout.har",
                    timeout_s=0.05,
                )
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)

    def test_agent_worker_uses_supported_proxy_model_and_verifier(self):
        with patch("recursive_computer_use.agent.run") as run_agent:
            capture._agent_worker("check in the guest", "http://127.0.0.1:8765/api/result")
        run_agent.assert_called_once_with(
            "check in the guest",
            model="gpt-5.6-terra",
            verifier_url="http://127.0.0.1:8765/api/result",
        )


if __name__ == "__main__":
    unittest.main()
