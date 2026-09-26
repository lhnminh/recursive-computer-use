"""
learner.py — Turn one recorded flow into an API recipe, and fill a recipe's params.

:func:`learn_recipe` sends the redacted exchanges (``network.har``) and the
task text to the model once. The reply must be a recipe JSON object. It is
validated with :meth:`Recipe.validate`; on failure the model gets one repair
turn with the error. A recipe that copies a redaction placeholder
(``<token:1>``) instead of extracting the value is rejected: it would replay a
dead token.

:func:`fill_params` maps a new task's text to the recipe's params with one
small model call.

Only redacted data reaches the model. The model is ``gpt-5.6-terra`` through
the Codex proxy by default (``RCU_LEARNER_MODEL`` overrides it).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .har import Redactor, load_exchanges
from .recipe import Recipe, RecipeError

DEFAULT_MODEL = os.environ.get("RCU_LEARNER_MODEL", "gpt-5.6-terra")
MAX_EXCHANGES = 40
_PLACEHOLDER = re.compile(r"<(?:token|secret|email):\d+>")

LEARN_SYSTEM = """You turn a recorded browser session into an API recipe.

You get the user's task and the HTTP exchanges the browser made while a
person did that task, in order. Values are redacted: <token:N>, <secret:N>
and <email:N> stand for real values. The same placeholder means the same
value, so a placeholder in a response that reappears in a later request
shows a dependency.

Write the smallest chain of requests that completes the task on its own.

Rules:
- Keep only the requests needed for the task. Drop page loads, polling,
  telemetry and anything the task does not need.
- Values the user typed (search words, form text) become params. Values that
  only identify the session or run in the URL (e.g. a session id path
  segment) also become params. Name params in snake_case.
  Use the typed value as "example", unless it is a placeholder.
- A value that a request needs and that an earlier response supplied becomes
  an "extract" on that earlier step plus {{var}} where it is used. Extract
  from "json" (dotted path, e.g. "data.csrf" or "items.0.id"), "header"
  (header name), "cookie" (cookie name) or "regex" (one capture group, on the
  response body; use it for hidden inputs in html_hints).
- Never copy a placeholder like <token:1> into the recipe. Extract it, make
  it a param, or leave it out.
- Never add Cookie or Authorization headers. A cookie jar keeps the session.
- Every URL stays on the given site. Use the full URL with scheme.
- Keep headers the server needs (content-type, CSRF-style x- headers).
- "body_format" is "json", "form" or "text", matching the recorded request.
- "expect_status" is the recorded response status of that step. If it was a
  redirect (3xx), use the status of the page it led to (usually 200): the
  runner follows same-site redirects.
- HTML responses show "html_links" (href + text) and "html_hints" (forms,
  hidden/radio/checkbox inputs, select options). When a later request uses a
  value the user PICKED from a list on an earlier HTML page (which search
  result to open, which color or size to select), do not make it a param.
  Add to that earlier step "choose": [{"var", "regex", "mode"}]:
  - mode "one": regex with ONE capture group that matches each candidate
    value in the raw HTML, e.g. in result links "item/([A-Z0-9]+)/".
    Capture the smallest identifier (an id), never a whole URL or path,
    and reuse it where the recording used that id.
  - mode "per_name": regex with TWO capture groups (option name, option
    value), e.g. for radio inputs 'name="([^"]+)" value="([^"]+)"'. Both
    groups must match ANY name and value: other products have other option
    names (size, color, flavor), so never write a recorded name literally. The var
    holds a JSON object {name: value} of the chosen options; use {{var}}
    where the recording sent the chosen options (an empty choice is {}).
  A model later picks among the candidates using the task text.
- Put {{var}} for a whole URL path segment; values are URL-encoded.
- "verify": {"url": ...} only if you saw a result/status endpoint on the
  site; otherwise null.

Reply with one JSON object and nothing else:
{"name": str (short, for the general kind of task, e.g. "search-and-buy"),
"description": str (one sentence on the general kind of task and its inputs,
valid for ANY param values, e.g. "Search the store and buy a product that
matches a request, choosing its options"; never mention the example's
values), "params": [{"name", "description", "example"}], "steps": [{"id",
"method", "url", "headers", "body", "body_format", "expect_status",
"extract": [{"var", "from", "path"}], "choose": [{"var", "regex", "mode"}]}],
"verify": {"url"} | null}
"""

FILL_SYSTEM = """You extract parameter values for an API recipe from a task.

You get the recipe's description, its params (name, description, example)
and a task. Reply with one JSON object mapping every param name to its value
for this task, as a string. Use exactly the given names. Copy values the task
states (ids, names, emails). Compose values the user would write themselves,
such as a search query, from the task (short keywords, like the example).
Map a param to null only if the task gives no basis for it at all."""


class LearnError(RuntimeError):
    """The model could not produce a valid recipe or param values."""


def default_client() -> Any:
    from openai import OpenAI

    from ..auth import resolve

    creds = resolve()
    return OpenAI(api_key=creds.api_key, base_url=creds.base_url)


def learn_recipe(
    har_path: str | Path,
    task: str,
    *,
    site: str,
    task_key: str,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    name: str | None = None,
) -> Recipe:
    """Learn a validated ``candidate`` recipe from one recorded flow."""
    redactor = Redactor()
    exchanges = load_exchanges(har_path, site=site, redactor=redactor)
    if not exchanges:
        raise LearnError(f"no API traffic for {site} in {har_path}")
    payload = {
        "task": redactor.redact_text(task),
        "site": site,
        "exchanges": [ex.to_prompt_dict() for ex in exchanges[-MAX_EXCHANGES:]],
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": LEARN_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    client = client or default_client()
    error = ""
    for _attempt in range(2):
        reply = _complete(client, model, messages)
        try:
            return _to_recipe(reply, site=site, task_key=task_key, name=name)
        except (RecipeError, ValueError) as exc:
            error = str(exc)
            messages += [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": f"That recipe is invalid: {error}. Reply with the corrected JSON object only.",
                },
            ]
    raise LearnError(f"model did not produce a valid recipe: {error}")


def fill_params(
    recipe: Recipe,
    task: str,
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, str]:
    """Return a value for every param of *recipe*, taken from *task*."""
    if not recipe.params:
        return {}
    payload = {
        "description": recipe.description,
        "params": [p.to_dict() for p in recipe.params],
        "task": task,
    }
    reply = _complete(
        client or default_client(),
        model,
        [
            {"role": "system", "content": FILL_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    data = _parse_json(reply)
    values = {p.name: data.get(p.name) for p in recipe.params}
    missing = sorted(k for k, v in values.items() if v in (None, ""))
    if missing:
        raise LearnError(f"task gives no value for params {missing}")
    return {k: str(v) for k, v in values.items()}


# -- helpers -------------------------------------------------------------------


CHOOSE_SYSTEM = """You pick for a web task. You get the task and numbered
candidates found on a page (a value plus the page text around it). Reply
with one JSON object: {"index": <number of the best candidate>}."""

CHOOSE_OPTIONS_SYSTEM = """You pick product options for a web task. You get
the task and option groups (name + possible values). Pick the value per
group that the task asks for; skip groups the task does not mention. Reply
with one JSON object mapping option name to the exact value string."""


def llm_chooser(
    *,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    stats: dict[str, int] | None = None,
) -> Any:
    """A ``network.runner`` chooser backed by one small model call per choice."""

    def choose(task: str, ch: Any, candidates: list[dict[str, Any]]) -> Any:
        nonlocal client
        client = client or default_client()
        if stats is not None:
            stats["llm_calls"] = stats.get("llm_calls", 0) + 1
        if ch.mode == "per_name":
            payload = {"task": task, "options": candidates}
            reply = _complete(client, model, [
                {"role": "system", "content": CHOOSE_OPTIONS_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            try:
                return _parse_json(reply)
            except ValueError:
                return {}
        numbered = [{"n": i, "value": c["value"], "text": c["context"]} for i, c in enumerate(candidates)]
        reply = _complete(client, model, [
            {"role": "system", "content": CHOOSE_SYSTEM},
            {"role": "user", "content": json.dumps({"task": task, "candidates": numbered}, ensure_ascii=False)},
        ])
        try:
            return int(_parse_json(reply).get("index", 0))
        except (ValueError, TypeError):
            return 0

    return choose


def _complete(client: Any, model: str, messages: list[dict[str, Any]]) -> str:
    response = client.chat.completions.create(model=model, messages=messages)
    return response.choices[0].message.content or ""


def _parse_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in *text* (tolerates code fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("reply contains no JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return data


def _to_recipe(reply: str, *, site: str, task_key: str, name: str | None) -> Recipe:
    data = _parse_json(reply)
    # The harness owns identity and lifecycle fields, not the model.
    data.update(
        kind="api_recipe",
        status="candidate",
        version=1,
        parent_id=None,
        scope={"site": site, "task_key": task_key},
    )
    data["name"] = name or f"{site}:{_slug(data.get('name') or task_key)}"
    for step in data.get("steps") or []:
        # The runner follows same-site redirects, so a recorded 3xx ends as 2xx.
        if isinstance(step, dict) and 300 <= int(step.get("expect_status") or 200) < 400:
            step["expect_status"] = 200
    for param in data.get("params") or []:
        if isinstance(param, dict) and _PLACEHOLDER.search(str(param.get("example") or "")):
            param["example"] = None  # never store a redacted value as an example
    recipe = Recipe.from_dict(data)
    for step in recipe.steps:
        for ch in step.choose:
            if ch.mode == "per_name" and not _is_open_group(ch.regex, 1):
                raise RecipeError(
                    f"choose {ch.var!r}: the option-name group {ch.regex!r} matches one fixed name; "
                    "it must match any option name, e.g. name=\"([^\"]+)\""
                )
    leaked = _PLACEHOLDER.findall(json.dumps([s.to_dict() for s in recipe.steps]))
    if leaked:
        raise RecipeError(
            f"steps copy redacted values {sorted(set(leaked))}; extract them from an "
            "earlier response or make them params"
        )
    return recipe.validate()


def _is_open_group(regex: str, group: int) -> bool:
    """True if capture *group* of *regex* can match more than one literal string."""
    import re._parser as sre_parse  # stdlib parser; stable enough for a sanity check

    def walk(items: Any) -> bool | None:
        for op, av in items:
            if op is sre_parse.SUBPATTERN:
                if av[0] == group:
                    return any(o is not sre_parse.LITERAL for o, _ in av[-1])
                found = walk(av[-1])
                if found is not None:
                    return found
        return None

    try:
        return bool(walk(sre_parse.parse(regex)))
    except Exception:  # noqa: BLE001 - validate() reports bad regexes
        return True


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:60] or "task"


def params_from(values: Mapping[str, Any]) -> dict[str, str]:
    """Normalize param values to strings (helper for callers and tests)."""
    return {k: str(v) for k, v in values.items()}
