from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursive_computer_use.network.har import Redactor, endpoints, har_sha256, load_exchanges

HAR = Path(__file__).parent / "fixtures" / "checkin.har"
SECRETS = [
    "c5rfTOKENvalue0987654321",
    "s3ss10nVALUEabcdef123456",
    "HIDDENcsrf1234567890abcdef",
    "hunter2hunter2",
    "ada@example.com",
    "g1234567890abcdefghij",
]


class HarTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor()
        self.exchanges = load_exchanges(HAR, site="127.0.0.1:8765", redactor=self.redactor)
        self.by_path = {ex.path: ex for ex in self.exchanges}

    def test_keeps_only_same_site_api_traffic_in_order(self):
        self.assertEqual(
            [(ex.method, ex.path) for ex in self.exchanges],
            [("GET", "/"), ("GET", "/api/session"), ("POST", "/api/checkin"), ("POST", "/login")],
        )
        self.assertEqual(endpoints(self.exchanges)[2], {"method": "POST", "path": "/api/checkin", "status": 200})

    def test_no_secret_survives_anywhere(self):
        dumped = json.dumps([ex.to_prompt_dict() for ex in self.exchanges])
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)
        self.assertNotIn("1234", dumped.replace("<", " "))  # the pin

    def test_same_token_gets_same_placeholder_across_exchanges(self):
        session = self.by_path["/api/session"]
        checkin = self.by_path["/api/checkin"]
        csrf = session.response_body["csrf"]
        self.assertRegex(csrf, r"^<token:\d+>$")
        self.assertEqual(checkin.request_headers["x-csrf-token"], csrf)
        sid = session.response_headers["set-cookie"]
        self.assertEqual(checkin.request_headers["cookie"], sid)
        self.assertTrue(sid.startswith("sid=<token:"))

    def test_user_values_stay_readable_except_emails(self):
        body = self.by_path["/api/checkin"].request_body
        self.assertEqual(body["full_name"], "Ada Lovelace")
        self.assertEqual(body["city"], "New York")
        self.assertRegex(body["email"], r"^<email:\d+>$")
        self.assertEqual(self.by_path["/api/checkin"].request_format, "json")
        # base64 response decoded, opaque id redacted, keys kept
        self.assertEqual(set(self.by_path["/api/checkin"].response_body), {"ok", "guest_id"})

    def test_task_text_uses_the_same_mapping(self):
        email_ph = self.by_path["/api/checkin"].request_body["email"]
        task = self.redactor.redact_text("Check in Ada Lovelace, ada@example.com, New York")
        self.assertIn(email_ph, task)
        self.assertIn("Ada Lovelace", task)

    def test_field_names_do_not_false_match(self):
        body = self.by_path["/api/session"].response_body
        self.assertEqual(body["residence"], "Paris")
        self.assertEqual(body["shipping"], "fast")

    def test_form_secrets_and_html_hints(self):
        login = self.by_path["/login"]
        self.assertEqual(login.request_format, "form")
        self.assertEqual(login.request_body["user"], "ada")
        self.assertRegex(login.request_body["password"], r"^<secret:\d+>$")
        self.assertRegex(login.request_body["pin"], r"^<secret:\d+>$")
        hints = self.by_path["/"].response_body["html_hints"]
        self.assertTrue(any('name="csrf"' in h and "<token:" in h for h in hints), hints)

    def test_sha256_is_stable(self):
        self.assertEqual(len(har_sha256(HAR)), 64)


if __name__ == "__main__":
    unittest.main()
