# Verifier Plans

Recipes can use a site-neutral verifier plan instead of requiring the target
site to expose a `success: true` endpoint. A plan has two explicit parts:

- `browser` checks the first, recorded interaction against the current URL,
  query string, visible page text, and bounded CSS selector counts.
- `api` checks a JSON response after a recipe replay using an HTTP status and
  safe JSON-path assertions, bounded text assertions, or structural HTML
  element counts (optionally scoped to a matching ancestor).

The plan contains no executable code. Supported JSON operators are `exists`,
`nonempty`, `equals`, `not_equals`, `contains`, `gte`, and `lte`. Comparisons
can use recipe parameters with `{{name}}`. The verifier reports only pass/fail
and assertion identifiers; it does not persist page text or response bodies.
Verifier URLs remain within the recipe's site scope.

Example for a read-only search workflow (adapt the host and result path to the
site's documented/public search interface):

```json
{
  "browser": {
    "url_contains": ["/search"],
    "query": {"q": "tiptour-macos", "type": "repositories"},
    "selectors": [
      {"css": "[data-testid='results-list'] a[href]", "min_count": 1}
    ]
  },
  "api": {
    "url": "https://github.com/search",
    "query": {"q": "{{query}}", "type": "repositories"},
    "headers": {"Accept": "text/html"},
    "format": "html",
    "assertions": [
      {
        "element": {"tag": "a", "attrs": {"href": {"starts_with": "/"}}},
        "within": {"tag": "div", "attrs": {"data-testid": "results-list"}},
        "op": "count_gte",
        "value": 1
      }
    ]
  }
}
```

Run a recipe-first workflow with:

```sh
uv run python -m recursive_computer_use do \
  --site github.com \
  --task-key repository-search \
  --verifier examples/verifier-plans/github-repository-search.json \
  'Search GitHub repositories for tiptour-macos and report the first repository'
```

The browser assertion verifies the recorded interaction. The API assertion
verifies each replay. A missing or failing assertion prevents a recipe from
being promoted. This does not make every arbitrary web task verifiable: each
task still needs a concrete postcondition that can be checked independently.
