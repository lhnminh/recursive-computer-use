"""Replay validated API recipes using only the Python standard library."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .recipe import Recipe, RecipeError, render, resolve_session_vars
from .session import cookie_jar_for, cookies_for, load_storage_state

STEP_TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class StepResult:
    id: str
    status: int | None
    ms: int


@dataclass
class RunResult:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    verifier_result: dict[str, Any] | None = None


class _ScopedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, site: str) -> None:
        super().__init__()
        self.site = site.lower()

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        if urlsplit(newurl).netloc.lower() != self.site:
            raise RecipeError("redirect outside recipe scope")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def run_recipe(
    recipe: Recipe | Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    session_cookies: Mapping[str, str] | None = None,
) -> RunResult:
    """Run a recipe and report per-step timings and the verifier's verdict.

    The cookie jar starts with the site's login cookies: *session_cookies*
    (name -> value) if given, else the local saved session (see
    ``network.session``), else none. ``recipe.session`` vars are read from
    those cookies; a missing one fails the run with "log in again".

    Errors are returned without response bodies or request headers, which can
    contain task data or session material.
    """
    started = time.monotonic()
    result = RunResult(ok=False)
    try:
        if not isinstance(recipe, Recipe):
            recipe = Recipe.from_dict(recipe)
        recipe.validate()
        expected = {p.name for p in recipe.params}
        missing = expected - set(params)
        extra = set(params) - expected
        if missing or extra:
            raise RecipeError(f"params mismatch (missing={sorted(missing)}, extra={sorted(extra)})")

        values: dict[str, Any] = dict(params)
        if session_cookies is None:
            state = load_storage_state(recipe.site)
            jar = cookie_jar_for(recipe.site, state)
            session_cookies = cookies_for(recipe.site, state)
        else:
            jar = _jar_from_cookies(recipe.site, session_cookies)
        values.update(resolve_session_vars(recipe, session_cookies))
        opener = build_opener(HTTPCookieProcessor(jar), _ScopedRedirectHandler(recipe.site))
        for step in recipe.steps:
            url = render(step.url, values)
            headers = render(step.headers, values)
            body = render(step.body, values)
            data, headers = _encode_body(body, step.body_format, headers)
            request = Request(url, data=data, headers=headers, method=step.method)
            response, status, body_bytes, elapsed = _request(opener, request)
            result.steps.append(StepResult(step.id, status, elapsed))
            if status != step.expect_status:
                raise _RunFailure(f"step {step.id} returned HTTP {status}, expected {step.expect_status}")
            for extract in step.extract:
                values[extract.var] = _extract(
                    extract.source, extract.path, response, body_bytes, jar, url
                )

        result.vars = values
        if recipe.verify:
            verify_request = Request(recipe.verify["url"], method="GET")
            response, status, body_bytes, elapsed = _request(opener, verify_request)
            result.steps.append(StepResult("verify", status, elapsed))
            if status != 200:
                raise _RunFailure(f"verifier returned HTTP {status}")
            try:
                verdict = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _RunFailure("verifier returned invalid JSON") from exc
            if not isinstance(verdict, dict):
                raise _RunFailure("verifier response must be a JSON object")
            result.verifier_result = {
                key: verdict[key]
                for key in (
                    "success",
                    "wrong_field_count",
                    "wrong_click_count",
                    "action_count",
                    "policy_violations",
                    "duration_ms",
                )
                if key in verdict
            }
            result.ok = verdict.get("success") is True
            if not result.ok:
                raise _RunFailure("verifier did not confirm success")
        else:
            result.ok = True
    except (RecipeError, _RunFailure, OSError, URLError, HTTPError, ValueError, TypeError) as exc:
        result.error = str(exc) or type(exc).__name__
    finally:
        result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


class _RunFailure(RuntimeError):
    pass


def _jar_from_cookies(site: str, cookies: Mapping[str, str]) -> CookieJar:
    host = (urlsplit(f"//{site}").hostname or "").lower()
    state = {
        "cookies": [
            {"name": str(name), "value": str(value), "domain": host, "path": "/", "expires": -1}
            for name, value in cookies.items()
        ]
    }
    return cookie_jar_for(site, state)


def _encode_body(body: Any, body_format: str, headers: dict[str, str]) -> tuple[bytes | None, dict[str, str]]:
    if body is None:
        return None, headers
    if body_format == "json":
        _set_default_header(headers, "Content-Type", "application/json")
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), headers
    if body_format == "form":
        _set_default_header(headers, "Content-Type", "application/x-www-form-urlencoded")
        if not isinstance(body, Mapping):
            raise RecipeError("form body must be an object")
        return urlencode(body, doseq=True).encode("utf-8"), headers
    if body_format == "text":
        _set_default_header(headers, "Content-Type", "text/plain; charset=utf-8")
        if not isinstance(body, str):
            raise RecipeError("text body must be a string")
        return body.encode("utf-8"), headers
    raise RecipeError(f"unsupported body format {body_format!r}")


def _set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not any(key.lower() == name.lower() for key in headers):
        headers[name] = value


def _request(opener: Any, request: Request) -> tuple[Any, int, bytes, int]:
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=STEP_TIMEOUT_S)
    except HTTPError as exc:
        pass  # Non-2xx statuses are recipe results, not transport exceptions.
        response = exc
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise _RunFailure("response exceeded 2 MiB limit")
        return response, response.getcode(), body, int((time.monotonic() - started) * 1000)
    finally:
        response.close()


def _extract(source: str, path: str, response: Any, body: bytes, jar: CookieJar, url: str) -> Any:
    text = body.decode("utf-8", errors="replace")
    if source == "json":
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _RunFailure("response was not valid JSON for extraction") from exc
        for component in path.split("."):
            try:
                value = value[int(component)] if isinstance(value, list) else value[component]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise _RunFailure(f"JSON extraction path {path!r} was not found") from exc
        return value
    if source == "header":
        value = response.headers.get(path)
        if value is None:
            raise _RunFailure(f"response header {path!r} was not found")
        return value
    if source == "cookie":
        hostname = (urlsplit(url).hostname or "").lower()
        value = next(
            (
                cookie.value
                for cookie in jar
                if cookie.name == path
                and (
                    hostname == cookie.domain.lstrip(".").lower()
                    or hostname.endswith("." + cookie.domain.lstrip(".").lower())
                )
            ),
            None,
        )
        if value is None:
            raise _RunFailure(f"response cookie {path!r} was not found")
        return value
    if source == "regex":
        match = re.search(path, text)
        if match is None:
            raise _RunFailure("response did not match extraction regex")
        return match.group(1)
    raise RecipeError(f"unsupported extraction source {source!r}")
