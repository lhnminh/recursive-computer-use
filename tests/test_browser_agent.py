from __future__ import annotations

import unittest

from recursive_computer_use.network.browser_agent import _settle, perform


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


if __name__ == "__main__":
    unittest.main()
