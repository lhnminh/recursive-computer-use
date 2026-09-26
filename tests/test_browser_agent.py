from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from recursive_computer_use.network.browser_agent import _settle, perform, run_browser_task


class _Page:
    def __init__(self):
        self.url = "http://local.test/start"
        self.calls = []

    def locator(self, selector):
        page = self

        class Locator:
            def click(self, **_kwargs):
                page.calls.append(("click", selector))
                page.url = "http://local.test/next"

            def fill(self, value, **_kwargs):
                page.calls.append(("fill", selector, value))

        return Locator()

    def wait_for_load_state(self, state, *, timeout):
        self.settle = (state, timeout)

    def evaluate(self, _script, _max_elements):
        self.observations += 1
        return {
            "url": self.url,
            "title": "test",
            "text": f"snapshot-{self.observations}",
            "elements": [],
        }


class BrowserAgentTests(unittest.TestCase):
    def test_navigation_stops_batch_before_stale_ids_are_used(self):
        page = _Page()
        actions = [
            {"op": "click", "id": "e1"},
            {"op": "fill", "id": "e2", "text": "should not run"},
        ]

        done, error = perform(page, actions, {"e1", "e2"})

        self.assertEqual(done, 1)
        self.assertIn("refreshed element IDs", error)
        self.assertEqual(len(page.calls), 1)

    def test_settle_does_not_wait_for_network_idle(self):
        page = _Page()

        _settle(page)

        self.assertEqual(page.settle, ("domcontentloaded", 500))

    def test_model_context_keeps_only_latest_snapshot(self):
        page = _Page()
        page.observations = 0
        act = SimpleNamespace(
            id="act-1",
            function=SimpleNamespace(name="act", arguments='{"actions": []}'),
        )
        finish = SimpleNamespace(
            id="finish-1",
            function=SimpleNamespace(name="finish", arguments='{"success": false, "summary": "done"}'),
        )
        responses = [act, finish]

        class _Client:
            def __init__(self):
                self.messages = []
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                self.messages.append(copy.deepcopy(kwargs["messages"]))
                call = responses.pop(0)
                message = SimpleNamespace(content=None, tool_calls=[call])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client = _Client()
        run_browser_task(page, "complete the task", client=client, verify=lambda: False)

        second_turn = client.messages[1]
        self.assertEqual(len(second_turn), 4)
        self.assertNotIn("snapshot-1", repr(second_turn))
        self.assertIn("snapshot-2", second_turn[-1]["content"])


if __name__ == "__main__":
    unittest.main()
