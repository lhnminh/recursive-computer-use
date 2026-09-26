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
- Values the user typed or chose become params. Name params in snake_case.
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
- "expect_status" is the recorded response status of that step.
- "verify": {"url": ...} only if you saw a result/status endpoint on the
  site; otherwise null.

Reply with one JSON object and nothing else:
{"name": str, "description": str (one sentence: what the task does and its
inputs), "params": [{"name", "description", "example"}], "steps": [{"id",
"method", "url", "headers", "body", "body_format", "expect_status",
"extract": [{"var", "from", "path"}]}], "verify": {"url"} | null}
"""

FILL_SYSTEM = """You extract parameter values for an API recipe from a task.

You get the recipe's description, its params (name, description, example)
and a task. Reply with one JSON object mapping every param name to the value
the task gives for it, as a string. Use exactly the given names. If the task
does not give a value for a param, map it to null."""


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
    for param in data.get("params") or []:
        if isinstance(param, dict) and _PLACEHOLDER.search(str(param.get("example") or "")):
            param["example"] = None  # never store a redacted value as an example
    recipe = Recipe.from_dict(data)
    leaked = _PLACEHOLDER.findall(json.dumps([s.to_dict() for s in recipe.steps]))
    if leaked:
        raise RecipeError(
            f"steps copy redacted values {sorted(set(leaked))}; extract them from an "
            "earlier response or make them params"
        )
    return recipe.validate()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:60] or "task"


def params_from(values: Mapping[str, Any]) -> dict[str, str]:
    """Normalize param values to strings (helper for callers and tests)."""
    return {k: str(v) for k, v in values.items()}
