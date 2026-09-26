"""Bounded, declarative postconditions for captured web tasks and API replays."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .recipe import render


class VerificationError(ValueError):
    """A verifier plan is malformed or its postconditions did not hold."""


_OPS = {"exists", "equals", "not_equals", "nonempty", "contains", "gte", "lte"}


def evaluate_json(
    document: Any,
    assertions: Sequence[Mapping[str, Any]],
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate safe JSON-path assertions and return a value-free verdict."""

    if not assertions or len(assertions) > 32:
        raise VerificationError("JSON verifier needs 1..32 assertions")
    values = values or {}
    checks: list[dict[str, Any]] = []
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            raise VerificationError("each JSON assertion must be an object")
        path = assertion.get("path")
        op = assertion.get("op")
        if not isinstance(path, str) or not path or len(path) > 256:
            raise VerificationError("assertion path must be a non-empty JSON path")
        if op not in _OPS:
            raise VerificationError(f"unsupported assertion operator: {op!r}")
        actual, exists = _json_path(document, path)
        expected = render(assertion.get("value"), values) if "value" in assertion else None
        passed = _test(actual, exists, op, expected)
        checks.append({"path": path, "op": op, "passed": passed})
    return {"success": all(check["passed"] for check in checks), "assertions": checks}


def evaluate_text(
    body: str,
    assertions: Sequence[Mapping[str, Any]],
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate bounded literal assertions against a text or HTML response."""

    if not assertions or len(assertions) > 32:
        raise VerificationError("text verifier needs 1..32 assertions")
    values = values or {}
    checks: list[dict[str, Any]] = []
    sample = body[:2_000_000]
    for assertion in assertions:
        if not isinstance(assertion, Mapping) or assertion.get("path") != "body":
            raise VerificationError("text assertions must use path 'body'")
        op = assertion.get("op")
        if op not in {"contains", "not_equals", "equals", "nonempty"}:
            raise VerificationError("text assertions support contains, not_equals, equals, and nonempty")
        expected = str(render(assertion.get("value", ""), values))
        if len(expected) > 2000:
            raise VerificationError("text assertion value is too long")
        passed = {
            "contains": lambda: bool(expected) and expected in sample,
            "not_equals": lambda: sample != expected,
            "equals": lambda: sample == expected,
            "nonempty": lambda: bool(sample.strip()),
        }[op]()
        checks.append({"path": "body", "op": op, "passed": passed})
    return {"success": all(check["passed"] for check in checks), "assertions": checks}


def evaluate_html(
    body: str,
    assertions: Sequence[Mapping[str, Any]],
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Count structured HTML elements, optionally scoped under a parent.

    Each assertion names an ``element`` (tag plus attribute predicates), an
    optional ancestor ``within``, and a ``count_gte``/``count_lte``/``count_eq``
    comparison. This intentionally supports a small declarative subset, not
    arbitrary selectors or executable expressions.
    """

    if not isinstance(body, str):
        raise VerificationError("HTML verifier body must be text")
    if not assertions or len(assertions) > 32:
        raise VerificationError("HTML verifier needs 1..32 assertions")
    values = values or {}
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            raise VerificationError("each HTML assertion must be an object")
        element, within = assertion.get("element"), assertion.get("within")
        if not isinstance(element, Mapping) or (within is not None and not isinstance(within, Mapping)):
            raise VerificationError("HTML assertion needs an element and optional parent matcher")
        if assertion.get("op") not in {"count_gte", "count_lte", "count_eq"}:
            raise VerificationError("HTML assertions support count_gte, count_lte, and count_eq")
        threshold = assertion.get("value")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 100_000:
            raise VerificationError("HTML count threshold must be an integer from 0 to 100000")
    parser = _HtmlCounter(assertions, values)
    parser.feed(body[:2_000_000])
    parser.close()
    checks = []
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, Mapping):
            raise VerificationError("each HTML assertion must be an object")
        op = assertion.get("op")
        threshold = assertion.get("value")
        if op not in {"count_gte", "count_lte", "count_eq"}:
            raise VerificationError("HTML assertions support count_gte, count_lte, and count_eq")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 100_000:
            raise VerificationError("HTML count threshold must be an integer from 0 to 100000")
        count = parser.counts[index]
        passed = count >= threshold if op == "count_gte" else count <= threshold if op == "count_lte" else count == threshold
        checks.append({"check": "html_count", "op": op, "count": count, "passed": passed})
    return {"success": all(check["passed"] for check in checks), "assertions": checks}


class _HtmlCounter(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, assertions: Sequence[Mapping[str, Any]], values: Mapping[str, Any]) -> None:
        super().__init__(convert_charrefs=True)
        self.assertions = assertions
        self.values = values
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.counts = [0 for _ in assertions]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {key.lower(): value or "" for key, value in attrs}
        for index, assertion in enumerate(self.assertions):
            if not isinstance(assertion, Mapping):
                raise VerificationError("each HTML assertion must be an object")
            element = assertion.get("element")
            if not isinstance(element, Mapping) or not _html_matches(tag, normalized, element, self.values):
                continue
            within = assertion.get("within")
            if within is None or any(
                _html_matches(parent_tag, parent_attrs, within, self.values)
                for parent_tag, parent_attrs in self.stack
            ):
                self.counts[index] += 1
        if tag.lower() not in self._VOID:
            self.stack.append((tag.lower(), normalized))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def _html_matches(
    tag: str,
    attrs: Mapping[str, str],
    spec: Mapping[str, Any],
    values: Mapping[str, Any],
) -> bool:
    expected_tag = spec.get("tag", "*")
    if not isinstance(expected_tag, str) or len(expected_tag) > 40:
        raise VerificationError("HTML tag must be a short string")
    if expected_tag != "*" and tag.lower() != expected_tag.lower():
        return False
    predicates = spec.get("attrs", {})
    if not isinstance(predicates, Mapping) or len(predicates) > 8:
        raise VerificationError("HTML attrs must be an object with at most 8 entries")
    for key, matcher in predicates.items():
        actual = attrs.get(str(key).lower())
        if actual is None:
            return False
        if isinstance(matcher, Mapping):
            if len(matcher) != 1:
                raise VerificationError("HTML attribute predicate needs one operator")
            operator, raw = next(iter(matcher.items()))
            expected = str(render(raw, values))
            if operator == "contains":
                matched = expected in actual
            elif operator == "starts_with":
                matched = actual.startswith(expected)
            else:
                raise VerificationError(f"unsupported HTML attribute predicate: {operator!r}")
            if not matched:
                return False
        elif actual != str(render(matcher, values)):
            return False
    return True


def verify_browser_page(page: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Check generic browser postconditions without sending page content to Atlas.

    Supported fields: ``url_contains``, ``query`` (expected URL query values),
    ``text_contains``, ``text_not_contains``, and ``selectors`` entries with
    ``css``, optional ``min_count``/``max_count`` and ``text_contains``.
    """

    checks: list[dict[str, Any]] = []
    url = str(page.url)
    for fragment in _string_list(spec.get("url_contains", []), "url_contains"):
        checks.append({"check": "url_contains", "passed": fragment in url})
    query = spec.get("query", {})
    if not isinstance(query, Mapping) or len(query) > 20:
        raise VerificationError("query must be an object with at most 20 keys")
    actual_query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    for key, expected in query.items():
        expected = str(expected)
        checks.append({
            "check": "query",
            "key": str(key),
            "passed": expected in actual_query.get(str(key), []),
        })

    text = str(page.locator("body").inner_text(timeout=1500))[:100_000]
    for fragment in _string_list(spec.get("text_contains", []), "text_contains"):
        checks.append({"check": "text_contains", "passed": fragment in text})
    for fragment in _string_list(spec.get("text_not_contains", []), "text_not_contains"):
        checks.append({"check": "text_not_contains", "passed": fragment not in text})

    selectors = spec.get("selectors", [])
    if not isinstance(selectors, list) or len(selectors) > 20:
        raise VerificationError("selectors must be a list of at most 20 checks")
    for item in selectors:
        if not isinstance(item, Mapping):
            raise VerificationError("selector check must be an object")
        css = item.get("css")
        if not isinstance(css, str) or not css or len(css) > 256:
            raise VerificationError("selector css must be a non-empty string")
        locator = page.locator(css)
        count = min(int(locator.count()), 100_000)
        minimum = int(item.get("min_count", 1))
        if not 0 <= minimum <= 100_000:
            raise VerificationError("selector min_count must be from 0 to 100000")
        passed = count >= minimum
        if "max_count" in item:
            maximum = int(item["max_count"])
            if not 0 <= maximum <= 100_000:
                raise VerificationError("selector max_count must be from 0 to 100000")
            passed = passed and count <= maximum
        contains = _string_list(item.get("text_contains", []), "selector text_contains")
        if contains:
            sample = " ".join(locator.nth(i).inner_text(timeout=1000) for i in range(min(count, 20)))
            passed = passed and all(fragment in sample for fragment in contains)
        checks.append({"check": "selector", "css": css, "count": count, "passed": passed})

    if not checks:
        raise VerificationError("browser verifier has no postconditions")
    return {"success": all(check["passed"] for check in checks), "checks": checks}


def _json_path(document: Any, path: str) -> tuple[Any, bool]:
    value = document
    for component in path.split("."):
        if isinstance(value, Mapping) and component in value:
            value = value[component]
        elif isinstance(value, list) and component.isdigit() and int(component) < len(value):
            value = value[int(component)]
        else:
            return None, False
    return value, True


def _test(actual: Any, exists: bool, op: str, expected: Any) -> bool:
    if op == "exists":
        return exists
    if op == "nonempty":
        return exists and actual is not None and actual != "" and actual != [] and actual != {}
    if not exists:
        return False
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op == "contains":
        return expected in actual if isinstance(actual, (str, list, tuple, set, dict)) else False
    if op in {"gte", "lte"}:
        try:
            return actual >= expected if op == "gte" else actual <= expected
        except TypeError:
            return False
    return False


def _string_list(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 32 or any(
        not isinstance(item, str) or len(item) > 500 for item in value
    ):
        raise VerificationError(f"{name} must be a list of at most 32 short strings")
    return value
