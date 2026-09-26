from __future__ import annotations

import unittest

from recursive_computer_use.network.verify import (
    VerificationError,
    evaluate_json,
    evaluate_html,
    evaluate_text,
    verify_browser_page,
)


class _Locator:
    def __init__(self, *, text="", count=0, children=()):
        self.text = text
        self._count = count
        self.children = list(children)

    def inner_text(self, timeout=None):
        return self.text

    def count(self):
        return self._count

    def nth(self, index):
        return _Locator(text=self.children[index])


class _Page:
    url = "https://example.test/search?q=accessibility"

    def locator(self, selector):
        if selector == "body":
            return _Locator(text="Search results Accessibility guide")
        if selector == "article.result":
            return _Locator(count=1, children=["Accessibility guide"])
        return _Locator()


class VerifierContractTests(unittest.TestCase):
    def test_json_assertions_support_paths_operators_and_task_values(self):
        result = evaluate_json(
            {"data": {"children": [{"title": "Accessibility guide"}], "query": "accessibility"}},
            [
                {"path": "data.children", "op": "nonempty"},
                {"path": "data.query", "op": "equals", "value": "{{query}}"},
                {"path": "data.children.0.title", "op": "contains", "value": "guide"},
            ],
            {"query": "accessibility"},
        )
        self.assertTrue(result["success"])
        self.assertTrue(all(check["passed"] for check in result["assertions"]))

    def test_json_assertions_fail_closed_on_missing_paths_and_unknown_ops(self):
        result = evaluate_json({}, [{"path": "data.items", "op": "nonempty"}])
        self.assertFalse(result["success"])
        with self.assertRaises(VerificationError):
            evaluate_json({}, [{"path": "data", "op": "execute"}])

    def test_text_assertions_match_literals_without_exposing_response_content(self):
        result = evaluate_text(
            '<div data-testid="results-list">tiptour-macos</div>',
            [
                {"path": "body", "op": "contains", "value": 'data-testid="results-list"'},
                {"path": "body", "op": "contains", "value": "{{query}}"},
            ],
            {"query": "tiptour-macos"},
        )
        self.assertTrue(result["success"])
        self.assertNotIn("tiptour-macos", repr(result))

    def test_html_assertions_count_elements_inside_a_matching_parent(self):
        assertion = {
            "element": {"tag": "a", "attrs": {"href": {"starts_with": "/"}}},
            "within": {"tag": "div", "attrs": {"data-testid": "results-list"}},
            "op": "count_gte",
            "value": 1,
        }
        passed = evaluate_html(
            '<div data-testid="results-list"><a href="/owner/repo">repo</a></div>',
            [assertion],
        )
        failed = evaluate_html('<div data-testid="results-list"></div>', [assertion])
        self.assertTrue(passed["success"])
        self.assertFalse(failed["success"])

    def test_browser_postconditions_check_url_query_content_and_elements(self):
        result = verify_browser_page(
            _Page(),
            {
                "url_contains": ["/search"],
                "query": {"q": "accessibility"},
                "text_contains": ["Search results"],
                "selectors": [{"css": "article.result", "min_count": 1, "text_contains": ["guide"]}],
            },
        )
        self.assertTrue(result["success"])

    def test_browser_postconditions_reject_missing_result_and_empty_plan(self):
        failed = verify_browser_page(_Page(), {"selectors": [{"css": ".missing", "min_count": 1}]})
        self.assertFalse(failed["success"])
        with self.assertRaises(VerificationError):
            verify_browser_page(_Page(), {})


if __name__ == "__main__":
    unittest.main()
