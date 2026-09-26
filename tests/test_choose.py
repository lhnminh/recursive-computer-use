from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from recursive_computer_use.network.recipe import Choose, Recipe, RecipeError, render_url
from recursive_computer_use.network.runner import candidates_for, run_recipe

RESULTS = """<html><body>
<a href="/item/B001/x">B001</a><h4>Red cotton t-shirt $12.00</h4>
<a href="/item/B002/x">B002</a><h4>Blue wool sweater $45.00</h4>
<a href="/item/B001/x">B001 again</a>
</body></html>"""
ITEM = """<input type="radio" name="color" value="red"><input type="radio" name="color" value="blue">
<input type="radio" name="size" value="small"><input type="radio" name="size" value="large">"""


class Shop(BaseHTTPRequestHandler):
    bought: list = []

    def do_GET(self):  # noqa: N802
        body = RESULTS if self.path.startswith("/search") else ITEM
        self._send(200, body.encode())

    def do_POST(self):  # noqa: N802
        Shop.bought.append(self.path)
        self._send(200, b"ok")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ChooseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.site = f"127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def recipe(self):
        base = f"http://{self.site}"
        return Recipe.from_dict({
            "name": "shop", "description": "Buy a product.", "scope": {"site": self.site},
            "params": [{"name": "query"}],
            "steps": [
                {"id": "search", "method": "GET", "url": base + "/search/{{query}}",
                 "choose": [{"var": "item", "regex": "/item/([A-Z0-9]+)/"}]},
                {"id": "item", "method": "GET", "url": base + "/item/{{item}}/x",
                 "choose": [{"var": "opts", "mode": "per_name",
                             "regex": 'name="([^"]+)" value="([^"]+)"'}]},
                {"id": "buy", "method": "POST", "url": base + "/done/{{item}}/{{opts}}",
                 "body": None, "body_format": "text"},
            ],
        }).validate()

    def test_candidates_are_deduplicated_with_page_context(self):
        ch = Choose(var="item", regex="/item/([A-Z0-9]+)/", context=40)
        cands = candidates_for(ch, RESULTS)
        self.assertEqual([c["value"] for c in cands], ["B001", "B002"])
        self.assertIn("Blue wool sweater", cands[1]["context"])
        opts = candidates_for(Choose(var="o", mode="per_name", regex='name="([^"]+)" value="([^"]+)"'), ITEM)
        self.assertEqual(opts, [{"name": "color", "values": ["red", "blue"]}, {"name": "size", "values": ["small", "large"]}])

    def test_chooser_picks_product_and_options_and_urls_are_encoded(self):
        seen = []

        def chooser(task, ch, cands):
            seen.append((task, ch.var))
            if ch.mode == "per_name":
                return {"color": "blue", "size": "huge", "brand": "x"}  # invalid picks dropped
            return 1

        Shop.bought.clear()
        result = run_recipe(self.recipe(), {"query": "blue sweater/wool"}, session_cookies={},
                            chooser=chooser, task="a blue sweater")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.vars["item"], "B002")
        self.assertEqual(json.loads(result.vars["opts"]), {"color": "blue"})
        self.assertEqual(seen, [("a blue sweater", "item"), ("a blue sweater", "opts")])
        self.assertEqual(Shop.bought, ["/done/B002/%7B%22color%22%3A%20%22blue%22%7D"])

    def test_without_chooser_first_candidate_and_no_options(self):
        result = run_recipe(self.recipe(), {"query": "x"}, session_cookies={})
        self.assertTrue(result.ok, result.error)
        self.assertEqual((result.vars["item"], result.vars["opts"]), ("B001", "{}"))

    def test_validation_checks_mode_groups(self):
        d = self.recipe().to_dict()
        bad = copy.deepcopy(d)
        bad["steps"][0]["choose"][0]["mode"] = "per_name"
        with self.assertRaisesRegex(RecipeError, "needs 2 capture group"):
            Recipe.from_dict(bad).validate()
        bad = copy.deepcopy(d)
        bad["steps"][1]["choose"][0]["var"] = "item"
        with self.assertRaisesRegex(RecipeError, "duplicate choose var"):
            Recipe.from_dict(bad).validate()

    def test_render_url_encodes_values(self):
        self.assertEqual(render_url("http://h/s/{{q}}", {"q": 'a b/c"{}'}), "http://h/s/a%20b/c%22%7B%7D")


if __name__ == "__main__":
    unittest.main()
